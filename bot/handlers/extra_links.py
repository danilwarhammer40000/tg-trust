"""
Owns: ExtraLinksPayment.waiting_receipt.

Client-initiated self-service request for additional devices.

Split into two tiers (see follower_issuance.FREE_EXTRA_LINKS):
  - Up to FREE_EXTRA_LINKS total follower accounts (2 links = 4 devices,
    on top of the leader's own 2): issued IMMEDIATELY, no payment, no
    admin involved.
  - Anything beyond that cap: the client is shown the amount owed
    (count * EXTRA_LINK_SURCHARGE, per month) and payment details, and
    asked to send a receipt. NO admin card is sent at request time —
    only once a receipt actually arrives, and even then only if
    core.auto_renewal.try_auto_verify_extra_links_payment() couldn't
    already auto-approve it via Gemini. This deliberately replaces an
    earlier version that sent extra devices on credit (surcharge applied
    silently, billed at the client's next renewal) — that produced a
    confusing "pay 4 rubles" prompt for a trial account with almost no
    paid time left, since the follow-up reconciliation math in
    core/payment.py's calc_rate_gap is meant for clients who prepaid
    MONTHS in advance, not hours. Paying upfront for a fixed monthly
    amount, every time, sidesteps that entirely.
A single request can straddle both tiers (e.g. 1 free device pair
already issued, client asks for 3 more -> 1 free, 2 requiring payment).

Reuses the single pending_request slot on the user record — a client
can't have a renewal receipt AND an extra-devices request in flight at
the same time (see the guards in extra_links_start/extra_links_pick
below). That's a deliberate simplification, not an oversight: both are
rare, short-lived, one-at-a-time asks from the same person. Note this
guard only applies to the paid tier — a request entirely within the free
tier is never blocked by a stale pending_request.
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
from core.dates import calc_new_expiry_months, utcnow_naive
from core.db import get_followers, get_user, get_user_by_telegram_id, update_user
from core.notify import log_to_channel
from core.payment import EXTRA_LINK_SURCHARGE, PAYMENT_INFO, calc_monthly_price, calc_monthly_price_with_additional

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

    if free_left:
        free_note = f"🆓 Бесплатно доступно ещё {free_left * 2} устройства (сразу, автоматически)."
    else:
        free_note = "Бесплатный лимит уже использован."

    await call.message.answer(
        "📱 Подключить ещё устройства\n\n"
        "Одна ссылка = до 2 устройств одновременно. Сверх бесплатного "
        f"лимита — {EXTRA_LINK_SURCHARGE}₽/мес за каждую доп. ссылку (оплата "
        "по чеку, дальше выдаётся автоматически).\n\n"
        f"{free_note}\n\n"
        "Сколько всего устройств хотите иметь?",
        reply_markup=kb
    )
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

    # --- PAID TIER: pay a month upfront at the new rate, receipt
    # auto-checked, then issued (and the month applied to expires_at) ---
    if paid_count:
        amount_due = calc_monthly_price_with_additional(username, paid_count)

        update_user(username, pending_request={
            "type": "extra_links",
            "count": paid_count,
            "amount_due": amount_due,
            "requested_at": utcnow_naive().isoformat(),
        })
        await state.set_state(ExtraLinksPayment.waiting_receipt)
        await state.update_data(epay_username=username)

        await call.message.answer(
            f"💰 Ещё {paid_count * 2} устройства — это {amount_due}₽ за месяц по "
            "вашей новой ставке (с учётом этих устройств).\n\n"
            "Оплатите по реквизитам ниже и пришлите чек следующим сообщением — "
            "устройства будут подключены автоматически после проверки, доступ "
            "продлится на месяц по этой ставке:\n\n"
            f"{PAYMENT_INFO}"
        )
        reply_lines.append("📨 Ожидаю чек на оплату доп. устройств.")

    if reply_lines:
        await call.message.answer("\n".join(reply_lines))
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
        f"{count * 2} устройства, ожидается ~{amount_due}₽ (месяц по новой ставке)\n\n"
        f"«✅ Выдать» — подтвердить оплату, выдать и продлить на месяц по этой ставке.\n"
        f"«💚 Без наценки» — выдать бесплатно, без оплаты и без продления.\n"
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
    # shared _issue_now() helper. The difference: "approve" confirms a
    # real payment for one month at the new rate — issues the devices
    # AND extends expires_at by a month AND fixes last_renewal_rate to
    # that new rate (this payment IS a renewal, not just an unlock).
    # "approve_free" bumps surcharge_exempt_links instead — a plain
    # grant, no payment, no date change, these links never add to price.
    created = await _issue_now(username, count, get_followers(username))
    waived = action == "approve_free"

    if waived and created:
        update_user(username, surcharge_exempt_links=(user.get("surcharge_exempt_links") or 0) + len(created))
    elif created:
        new_rate, _ = calc_monthly_price(username)
        new_expires_at = calc_new_expiry_months(user.get("expires_at"), 1)
        update_user(username, expires_at=new_expires_at, status="active")
        update_user(username, last_renewal_rate=new_rate)

    status_note = "✅ Выдано {} из {}{}".format(
        len(created), count, " (без наценки)" if waived else " + продлено на 1 мес"
    )
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + f"\n\n{status_note}")
    except Exception:
        pass

    if user.get("telegram_id") and created:
        client_note = (
            f"✅ Администратор подключил вам ещё {len(created) * 2} устройства"
            + (" без повышения тарифа:" if waived else (
                f" — оплата получена, доступ продлён на 1 месяц по ставке "
                f"{calc_monthly_price(username)[0]}₽/мес:"
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
