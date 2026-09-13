"""
Owns: ExtraLinksPayment.waiting_receipt.

Client-initiated self-service request for additional devices.

Split into two tiers (see follower_issuance.FREE_EXTRA_LINKS):
  - Up to FREE_EXTRA_LINKS total follower accounts (2 links = 4 devices,
    on top of the leader's own 2): issued IMMEDIATELY, no payment, no
    admin involved.
  - Anything beyond that cap splits again, by how much paid time the
    client has left:
      - MORE than SHORT_REMAINING_THRESHOLD_DAYS days left: the client
        pays the MARGINAL cost right now (paid_count * EXTRA_LINK_SURCHARGE
        — just what's being added, NOT a recomputed whole-month total —
        see the note on that bug below), sends a receipt, Gemini
        auto-checks it, devices are issued on success. No admin card is
        sent at request time — only once a receipt arrives, and even
        then only if auto-verify couldn't already approve it.
      - SHORT_REMAINING_THRESHOLD_DAYS days or fewer (a trial about to
        end, functionally): asking for money right now makes no sense —
        instead the client is shown the new rate and just needs to
        CONFIRM they accept it; devices are issued immediately on
        confirmation, no payment collected. The rate increase is billed
        starting their next real renewal (core.payment.calc_monthly_price
        already reflects it once the devices exist).

BUG HISTORY: an earlier version charged
core.payment.calc_monthly_price_with_additional() — the client's WHOLE
new monthly total — as the amount to pay THIS transaction. That
double-charged for links the client was already paying for (e.g.
already at 130₽/mo for 1 paid link, asked to pay 160₽ for one MORE —
should have been just the 30₽ marginal cost). That function is now only
used for the informational "итого в месяц получится X₽" line, never as
the charged amount. A second, unrelated bug from an even earlier version
prorated the charge by days remaining (core.payment.calc_rate_gap /
calc_topup_amount) — fine for a client who genuinely prepaid months in
advance, nonsensical for a brand-new request with no "already paid"
period to reconcile against (a 4-day trial got asked to pay ~4₽). Ended
up replaced twice; this version doesn't touch calc_rate_gap at all and
doesn't extend expires_at — paying the marginal surcharge changes the
ongoing RATE only, never the date.

A single request can straddle the free tier and one of the paid paths
(e.g. 1 free device pair already issued, client asks for 3 more -> 1
free, 2 requiring payment/confirmation).

Reuses the single pending_request slot on the user record — a client
can't have a renewal receipt AND an extra-devices request in flight at
the same time (see the guards in extra_links_start/extra_links_pick
below). That's a deliberate simplification, not an oversight: both are
rare, short-lived, one-at-a-time asks from the same person. Note this
guard only applies to the paid/payment-flow tier — a request entirely
within the free tier, or resolved via the trial-consent path, is never
blocked by a stale pending_request.
"""
import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only, notify_bg, notify_client, run_sync
from bot.config import ADMIN_ID, bot
from bot.keyboards import client_menu
from bot.states import ExtraLinksPayment
from follower_issuance import FREE_EXTRA_LINKS, build_connection_card, issue_follower, leader_is_active
from core.auto_renewal import try_auto_verify_extra_links_payment
from core.dates import parse_expiry, utcnow_naive
from core.db import get_followers, get_user, get_user_by_telegram_id, update_user
from core.notify import log_to_channel
from core.payment import (
    EXTRA_LINK_SURCHARGE,
    PAYMENT_LINK,
    PAYMENT_RECIPIENT,
    SHORT_REMAINING_THRESHOLD_DAYS,
    calc_monthly_price,
    calc_monthly_price_with_additional,
)

router = Router()
log = logging.getLogger(__name__)

MAX_EXTRA_LINKS = 4


async def _send_admin_review_card(caption: str, kb_rows: list, tg_id, file_id=None, is_photo=True) -> None:
    """
    Same "💬 Открыть чат в Telegram" tg://user?id=... deep-link pattern as
    bot/handlers/list_users.py's _send_user_card() — see that function's
    docstring for why this specific button sometimes gets rejected by
    Telegram (BUTTON_USER_INVALID / BUTTON_USER_PRIVACY_RESTRICTED) and
    why the fix is "retry once without that one button" rather than
    failing the whole card. Attaches the receipt (file_id) when one is
    given — the manual-review path only ever runs once a receipt exists.
    """
    rows = list(kb_rows)
    if tg_id:
        rows = rows + [[InlineKeyboardButton(text="💬 Открыть чат в Telegram", url=f"tg://user?id={tg_id}")]]

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    kb_no_chat = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        if file_id and is_photo:
            await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
        elif file_id:
            await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)
        else:
            await bot.send_message(ADMIN_ID, caption, reply_markup=kb)
    except TelegramBadRequest as e:
        if tg_id and "BUTTON_USER_" in str(e):
            log.info("tg://user deep link rejected for telegram_id=%s (%s), resending without it", tg_id, e)
            if file_id and is_photo:
                await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb_no_chat)
            elif file_id:
                await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb_no_chat)
            else:
                await bot.send_message(ADMIN_ID, caption, reply_markup=kb_no_chat)
        else:
            raise


def _free_remaining(username: str, followers: list = None) -> int:
    """How many more devices `username` can get before hitting
    FREE_EXTRA_LINKS — counts ALL current followers, however they were
    issued (admin-issued ones count too), since this is a cap on total
    extra devices, not on self-service requests specifically."""
    if followers is None:
        followers = get_followers(username)
    return max(0, FREE_EXTRA_LINKS - len(followers))


def _remaining_days(user: dict):
    """None if there's no valid expiry to compute from (treated as
    "not short" — only a genuinely short, valid remaining period routes
    to the trial-consent path; missing/unparseable data falls back to
    the normal payment flow rather than silently giving away free
    devices)."""
    expires_at = user.get("expires_at")
    if not expires_at:
        return None
    dt = parse_expiry(expires_at)
    if not dt:
        return None
    return (dt.date() - utcnow_naive().date()).days


async def _issue_now(username: str, count: int, followers_snapshot: list) -> list:
    """Issues `count` new follower accounts immediately (no approval),
    following the exact same issue -> resync -> build-card ordering the
    admin-approval path uses (see follower_issuance.py's docstring for
    why that order matters). Returns the list of (username, card) pairs
    actually created — may be shorter than `count` if issue_follower()
    ever returns None (shouldn't happen for an existing leader)."""
    was_active = leader_is_active(get_user(username))

    created_usernames = []
    for _ in range(count):
        new_username = issue_follower(username, existing_followers=followers_snapshot)
        if not new_username:
            break
        created_usernames.append(new_username)
        followers_snapshot = followers_snapshot + [{"username": new_username}]

    if was_active and created_usernames:
        await run_sync()

    return [(u, build_connection_card(u)) for u in created_usernames]


def _device_button_label(n: int, free_left: int) -> str:
    """
    Short button label in DEVICES (each link = 2 devices), kept as
    compact as possible so 4 buttons fit in one horizontal row without
    Telegram wrapping them — just the device range, plus a short "+N₽"
    when part of this choice needs payment. No "/мес" suffix on the
    button itself (that's spelled out in the message text instead).
    """
    device_range = f"{n * 2 - 1}-{n * 2}"
    paid_units = max(0, n - free_left)
    if paid_units == 0:
        return device_range
    return f"{device_range} +{paid_units * EXTRA_LINK_SURCHARGE}₽"


@router.callback_query(F.data == "extralinks:start")
async def extra_links_start(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    if user.get("pending_request"):
        pending_type = (user.get("pending_request") or {}).get("type")
        if pending_type == "rate_topup":
            msg = (
                "У вас есть неоплаченная доплата за уже выданные устройства — "
                "оплатите её («💳 Реквизиты для оплаты» → «💰 Доплатить»), прежде "
                "чем запрашивать новые."
            )
        elif pending_type == "extra_links":
            msg = "У вас уже есть заявка на доп. устройства, ожидающая оплаты или проверки."
        else:
            msg = "У вас уже есть необработанный запрос — дождитесь ответа администратора."
        await call.answer(msg, show_alert=True)
        return

    free_left = _free_remaining(user["username"])

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=_device_button_label(n, free_left),
            callback_data=f"extralinks:req:{n}"
        )
        for n in range(1, MAX_EXTRA_LINKS + 1)
    ]])

    parts = [
        "📱 Подключить ещё устройства",
        "",
        "Выберите, сколько устройств хотите подключить дополнительно — вам "
        "будут выданы ссылки для подключения.",
        "",
        "На каждую ссылку подключаются два устройства.",
    ]
    if free_left:
        parts.append(f"\n🆓 Бесплатно доступно ещё {free_left * 2} устройства (сразу, автоматически).")
    parts += [
        f"\n— {EXTRA_LINK_SURCHARGE}₽/мес за два устройства (1 доп. ссылка) сверх бесплатного лимита.",
        "\nСколько ещё устройств хотите подключить?",
    ]

    await call.message.answer("\n".join(parts), reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("extralinks:req:"))
async def extra_links_pick(call: CallbackQuery, state: FSMContext):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    count = int(call.data.split(":", 2)[2])
    username = user["username"]

    followers = get_followers(username)
    free_count = min(count, _free_remaining(username, followers))
    paid_count = count - free_count

    pending = user.get("pending_request") or {}

    # Rate-topup gate: while the client owes money on devices they
    # already have (see bot/handlers/client_menu.py's "💰 Доплатить"), no
    # NEW devices get issued at all — not even free-tier ones — until
    # that clears. Narrower and more aggressive than the guard right
    # below: it blocks both tiers, specifically for this one type.
    if pending.get("type") == "rate_topup":
        await call.answer(
            "У вас есть неоплаченная доплата за уже выданные устройства — "
            "оплатите её («💳 Реквизиты для оплаты» → «💰 Доплатить»), прежде "
            "чем запрашивать новые.",
            show_alert=True
        )
        return

    # A pending_request is only ever needed for the paid tier, so this
    # guard only applies when this pick actually needs payment — a
    # request entirely within the free tier is never blocked by one.
    if paid_count and pending:
        await call.answer("У вас уже есть необработанный запрос.", show_alert=True)
        return

    reply_lines = []

    # --- FREE TIER: issued immediately, no payment, no admin ---
    if free_count:
        created = await _issue_now(username, free_count, followers)
        for _, card in created:
            await call.message.answer(card)

        if created:
            reply_lines.append(f"🆓 Бесплатно подключено: {len(created) * 2} устройства.")
            await notify_bg(
                log_to_channel,
                f"🆓 Автовыдача бесплатных доп. устройств для {username}: {len(created)} ссылок."
            )
        followers = followers + [{"username": u} for u, _ in created]

    if not paid_count:
        if reply_lines:
            await call.message.answer("\n".join(reply_lines))
        await call.answer()
        return

    amount_due = paid_count * EXTRA_LINK_SURCHARGE
    new_total = calc_monthly_price_with_additional(username, paid_count)
    total_devices = (len(followers) + 1 + paid_count) * 2  # +1 for the leader's own account

    remaining_days = _remaining_days(user)

    # --- SHORT REMAINING TIME (trial, basically): no payment collected
    # upfront — asking a 4-day trial to pay right now produced a
    # nonsensical result in an earlier version (see module docstring).
    # Just confirm the new rate and issue immediately. ---
    if remaining_days is not None and remaining_days <= SHORT_REMAINING_THRESHOLD_DAYS:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Согласен", callback_data=f"extralinks:trialok:{paid_count}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="extralinks:trialcancel")],
        ])
        await call.message.answer(
            f"⏳ У вас осталось {remaining_days} дн. — для {paid_count * 2} доп. устройств "
            f"ставка увеличится на {amount_due}₽/мес (итого будет {new_total}₽/мес). "
            "Устройства подключаются сразу, без оплаты сейчас — новая ставка "
            "начнёт действовать со следующего продления. Согласны?",
            reply_markup=kb
        )
        if reply_lines:
            await call.message.answer("\n".join(reply_lines))
        await call.answer()
        return

    # --- NORMAL PAYMENT FLOW: pay the marginal cost now, receipt
    # auto-checked, then issued. Rate only — no date change. ---
    update_user(username, pending_request={
        "type": "extra_links",
        "count": paid_count,
        "amount_due": amount_due,
        "requested_at": utcnow_naive().isoformat(),
    })
    await state.set_state(ExtraLinksPayment.waiting_receipt)
    await state.update_data(epay_username=username)

    await call.message.answer(
        f"💰 Ещё {paid_count * 2} устройства — это {amount_due}₽ к вашей основной "
        f"подписке (итого в месяц получится {new_total}₽ за {total_devices} устройств).\n\n"
        f"Переведите по ссылке ниже ещё {amount_due}₽\n\n"
        "Чек или скрин для проверки отправьте прямо сюда в чат ✅.\n\n"
        f"{PAYMENT_LINK}\n{PAYMENT_RECIPIENT}"
    )
    if reply_lines:
        await call.message.answer("\n".join(reply_lines))
    await call.answer()


@router.callback_query(F.data.startswith("extralinks:trialok:"))
async def extra_links_trial_confirm(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    paid_count = int(call.data.split(":", 2)[2])
    username = user["username"]

    created = await _issue_now(username, paid_count, get_followers(username))
    for _, card in created:
        await call.message.answer(card)

    if created:
        new_rate, _ = calc_monthly_price(username)
        update_user(username, last_renewal_rate=new_rate)
        await call.message.answer(
            f"✅ Подключено. Ваша ставка теперь {new_rate}₽/мес — начнёт списываться "
            "со следующего продления."
        )
        await notify_bg(
            log_to_channel,
            f"✅ {username} (короткий остаток срока) согласился на новую ставку "
            f"{new_rate}₽/мес и получил {len(created)} доп. ссылок автоматически, без оплаты сейчас."
        )
    else:
        await call.message.answer("Не получилось подключить — попробуйте ещё раз или напишите администратору.")

    await call.answer()


@router.callback_query(F.data == "extralinks:trialcancel")
async def extra_links_trial_cancel(call: CallbackQuery):
    await call.message.answer("Хорошо, ничего не подключаю.")
    await call.answer()


@router.message(ExtraLinksPayment.waiting_receipt, F.photo | F.document)
async def extra_links_payment_receipt(msg: Message, state: FSMContext):
    data = await state.get_data()
    username = data.get("epay_username")
    await state.clear()

    user = get_user(username)
    if not user:
        await msg.answer("Не удалось определить ваш аккаунт.", reply_markup=client_menu)
        return

    pending = user.get("pending_request") or {}
    if pending.get("type") != "extra_links":
        await msg.answer("Заявка уже не актуальна.", reply_markup=client_menu)
        return

    is_photo = bool(msg.photo)
    file_id = msg.photo[-1].file_id if is_photo else msg.document.file_id

    pending = dict(pending)
    pending["receipt_file_id"] = file_id
    pending["receipt_is_photo"] = is_photo
    update_user(username, pending_request=pending)

    auto_applied = try_auto_verify_extra_links_payment(username)

    if auto_applied:
        await msg.answer("✅ Оплата подтверждена, устройства подключены — карточки выше.", reply_markup=client_menu)
        return

    count = pending.get("count", 1)
    amount_due = pending.get("amount_due")
    caption = (
        f"💰 Оплата доп. устройств от {username}\n"
        f"{count * 2} устройства, ожидается ~{amount_due}₽ (доплата к текущей ставке)\n\n"
        f"«✅ Выдать» — подтвердить оплату и выдать.\n"
        f"«💚 Без наценки» — выдать бесплатно, без учёта наценки.\n"
        f"«❌ Отклонить» — не выдавать."
    )
    kb_rows = [
        [InlineKeyboardButton(text=f"✅ Выдать {count}", callback_data=f"exlreview:{username}:approve")],
        [InlineKeyboardButton(text="💚 Без наценки", callback_data=f"exlreview:{username}:approve_free")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"exlreview:{username}:reject")],
    ]
    await _send_admin_review_card(caption, kb_rows, user.get("telegram_id"), file_id=file_id, is_photo=is_photo)
    await notify_bg(log_to_channel, caption)

    await msg.answer("✅ Чек отправлен администратору на проверку.", reply_markup=client_menu)


@router.message(ExtraLinksPayment.waiting_receipt)
async def extra_links_payment_wrong_content(msg: Message):
    await msg.answer("Пришлите, пожалуйста, именно фото или файл чека.")


@router.callback_query(F.data.startswith("exlreview:"))
async def extra_links_review(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, username, action = call.data.split(":")
    user = get_user(username)

    pending = (user or {}).get("pending_request") or {}
    if not user or pending.get("type") != "extra_links":
        await call.answer("Заявка уже обработана.", show_alert=True)
        try:
            await call.message.edit_caption(caption=(call.message.caption or call.message.text or "") + "\n\n⚠️ Уже обработано.")
        except Exception:
            pass
        return

    count = pending.get("count", 1)
    update_user(username, pending_request=None)

    if action == "reject":
        try:
            await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ Отклонено")
        except Exception:
            pass

        if user.get("telegram_id"):
            await notify_client(
                bot, user["telegram_id"],
                "❌ Оплата/запрос на доп. устройства отклонён администратором.",
                clear_username=username
            )

        await call.answer("Отклонено")
        return

    # action == "approve" or "approve_free" — same issue -> resync ->
    # build-card ordering as the free tier in extra_links_pick, via the
    # shared _issue_now() helper. The only difference between the two is
    # whether these links count toward the monthly-price surcharge (see
    # core/payment.py's calc_monthly_price) — "approve_free" bumps
    # surcharge_exempt_links so these specific links never add to the
    # client's price, "approve" leaves it as-is so they do (and confirms
    # last_renewal_rate at the new total). Neither touches expires_at —
    # this is a rate change, not a renewal.
    created = await _issue_now(username, count, get_followers(username))
    waived = action == "approve_free"

    if waived and created:
        update_user(username, surcharge_exempt_links=(user.get("surcharge_exempt_links") or 0) + len(created))
    elif created:
        new_rate, _ = calc_monthly_price(username)
        update_user(username, last_renewal_rate=new_rate)

    status_note = "✅ Выдано {} из {}{}".format(
        len(created), count, " (без наценки)" if waived else ""
    )
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + f"\n\n{status_note}")
    except Exception:
        pass

    if user.get("telegram_id") and created:
        client_note = (
            f"✅ Администратор подключил вам ещё {len(created) * 2} устройства"
            + (" без повышения тарифа:" if waived else (
                f" — оплата подтверждена, ставка теперь {calc_monthly_price(username)[0]}₽/мес:"
            ))
        )
        delivered = await notify_client(
            bot, user["telegram_id"],
            client_note,
            clear_username=username
        )
        if delivered:
            for _, card in created:
                try:
                    await bot.send_message(user["telegram_id"], card)
                except (TelegramBadRequest, TelegramForbiddenError):
                    log.warning("could not deliver a new connection card to %s", username)

    await notify_bg(
        log_to_channel,
        f"✅ Выдано {len(created)} доп. ссылок ({len(created) * 2} устройств) для {username} "
        f"(запрошено {count})" + (", без наценки." if waived else ".")
    )

    await call.answer("Готово")
