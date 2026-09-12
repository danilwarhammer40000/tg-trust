"""
Owns: nothing FSM-wise (count picker is a plain inline keyboard, no state
needed — the callback_data itself carries the chosen count).

Client-initiated self-service request for additional device links.

Split into two tiers (see follower_issuance.FREE_EXTRA_LINKS):
  - Up to FREE_EXTRA_LINKS total follower accounts: issued IMMEDIATELY,
    no admin involved — this mirrors the admin-approval path exactly
    (issue -> resync -> build card -> deliver) but skips the
    pending_request/review step entirely.
  - Anything beyond that cap: same shape as the receipt-renewal request
    in handlers/receipt.py (a pending_request on the user's own record,
    an admin review card with approve/reject), but WITHOUT any
    payment/receipt involved — purely "give me N more of my own
    sub-accounts", approved at the admin's discretion.
A single request can straddle both tiers (e.g. 1 free follower already
issued, client asks for 3 more -> 1 issued free, 2 sent for review).

Reuses the single pending_request slot on the user record — a client can't
have a renewal receipt AND an extra-links request in flight at the same
time (see the guard in extra_links_start/extra_links_pick below). That's a
deliberate simplification, not an oversight: both are rare, short-lived,
one-at-a-time asks from the same person. Note this guard only applies to
the review-tier part of a request — the free tier never touches
pending_request, so it's never blocked by it.
"""
import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only, notify_bg, notify_client, run_sync
from bot.config import ADMIN_ID, bot
from follower_issuance import FREE_EXTRA_LINKS, build_connection_card, issue_follower, leader_is_active
from core.dates import utcnow_naive
from core.db import get_followers, get_user, get_user_by_telegram_id, update_user
from core.notify import log_to_channel
from core.payment import EXTRA_LINK_SURCHARGE

router = Router()
log = logging.getLogger(__name__)

MAX_EXTRA_LINKS = 4


async def _send_admin_review_card(caption: str, kb_rows: list, tg_id) -> None:
    """
    Same "💬 Открыть чат в Telegram" tg://user?id=... deep-link pattern as
    bot/handlers/list_users.py's _send_user_card() — see that function's
    docstring for why this specific button sometimes gets rejected by
    Telegram (BUTTON_USER_INVALID / BUTTON_USER_PRIVACY_RESTRICTED,
    depending on the target's resolvability/privacy settings) and why the
    fix is "retry once without that one button" rather than failing the
    whole card. Duplicated here rather than imported because the two
    call sites send to different chats via different means (call.message
    vs bot.send_message(ADMIN_ID, ...)) — not worth threading a chat_id
    parameter through list_users.py's version for one extra call site.
    """
    rows = list(kb_rows)
    if tg_id:
        rows = rows + [[InlineKeyboardButton(text="💬 Открыть чат в Telegram", url=f"tg://user?id={tg_id}")]]

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await bot.send_message(ADMIN_ID, caption, reply_markup=kb)
    except TelegramBadRequest as e:
        if tg_id and "BUTTON_USER_" in str(e):
            log.info("tg://user deep link rejected for telegram_id=%s (%s), resending without it", tg_id, e)
            await bot.send_message(ADMIN_ID, caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        else:
            raise


def _free_remaining(username: str, followers: list = None) -> int:
    """How many more follower accounts `username` can get before hitting
    FREE_EXTRA_LINKS — counts ALL current followers, however they were
    issued (admin-issued ones count too), since this is a cap on total
    extra accounts, not on self-service requests specifically."""
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


def _link_count_button_label(n: int, free_left: int) -> str:
    """
    Label for one "how many extra links" button — phrased in DEVICES, not
    links, since that's what the client actually cares about ("1-2",
    "3-4"... rather than a bare link count they'd have to multiply by 2
    themselves). callback_data still carries the link count `n` — device
    range is purely a display transform, nothing downstream changes.

    Always shows the actual price for choosing exactly `n` links: whichever
    of the `n` links fall inside `free_left` are free, anything beyond
    that adds EXTRA_LINK_SURCHARGE per link. Adapts automatically to how
    much of the free tier is already used — if free_left is 0 (free tier
    already used up), even the "1-2" button shows a price; if free_left
    covers the whole request, it shows "бесплатно" with no price at all.
    """
    device_range = f"{n * 2 - 1}-{n * 2}"
    paid_units = max(0, n - free_left)
    if paid_units == 0:
        return f"{device_range} (бесплатно)"
    return f"{device_range} (+{paid_units * EXTRA_LINK_SURCHARGE}₽/мес)"


@router.callback_query(F.data == "extralinks:start")
async def extra_links_start(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    if user.get("pending_request"):
        pending_type = (user.get("pending_request") or {}).get("type")
        if pending_type == "rate_topup":
            await call.answer(
                "У вас есть неоплаченная доплата за уже выданные доп. ссылки — "
                "оплатите её («💳 Реквизиты для оплаты» → «💰 Доплатить сейчас»), "
                "прежде чем запрашивать новые.",
                show_alert=True
            )
        else:
            await call.answer(
                "У вас уже есть необработанный запрос — дождитесь ответа администратора.",
                show_alert=True
            )
        return

    free_left = _free_remaining(user["username"])

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=_link_count_button_label(n, free_left),
            callback_data=f"extralinks:req:{n}"
        )
        for n in range(1, MAX_EXTRA_LINKS + 1)
    ]])

    if free_left:
        free_note = (
            f"🆓 Бесплатно и сразу — доступно ещё {free_left} "
            f"{'ссылка' if free_left == 1 else 'ссылки'} "
            f"(до {free_left * 2} устройств).\n"
            f"Сверх этого — {EXTRA_LINK_SURCHARGE}₽/мес за каждую доп. ссылку "
            "(по согласованию с администратором)."
        )
    else:
        free_note = (
            "ℹ️ Бесплатный лимит уже использован — каждая новая ссылка "
            f"стоит {EXTRA_LINK_SURCHARGE}₽/мес и потребует согласования с администратором."
        )

    await call.message.answer(
        "📱 Подключение доп. устройств\n\n"
        "Как это работает:\n"
        "1️⃣ Каждая ссылка — это отдельный QR/конфиг, который можно "
        "подключить на ДО 2 устройств одновременно (например, телефон + "
        "ноутбук на одну ссылку).\n"
        "2️⃣ Выберите ниже, сколько ДОП. ссылок нужно — сверх той, что у "
        "вас уже есть.\n"
        "3️⃣ То, что укладывается в бесплатный лимит, выдаётся сразу же "
        "автоматически. Остальное — заявка администратору на согласование.\n\n"
        f"{free_note}\n\n"
        "Сколько ещё устройств хотите подключить? (цена на кнопке — итоговая "
        "надбавка к месячному платежу за этот выбор)",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("extralinks:req:"))
async def extra_links_pick(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    count = int(call.data.split(":", 2)[2])
    username = user["username"]

    followers = get_followers(username)
    free_count = min(count, _free_remaining(username, followers))
    review_count = count - free_count

    pending = user.get("pending_request") or {}

    # Rate-topup gate: while the client owes a top-up for a rate increase
    # that already happened (see bot/handlers/client_menu.py's
    # "💰 Доплатить сейчас"), NO new extra links get issued at all — not
    # even the free-tier ones — until they pay it and it clears (manually
    # or via core.auto_renewal.try_auto_verify_topup). This is a
    # narrower, more aggressive gate than the general "one request in
    # flight" rule right below: it blocks BOTH tiers, specifically for
    # this one pending_request type.
    if pending.get("type") == "rate_topup":
        await call.answer(
            "У вас есть неоплаченная доплата за уже выданные доп. ссылки — "
            "оплатите её («💳 Реквизиты для оплаты» → «💰 Доплатить сейчас»), "
            "прежде чем запрашивать новые.",
            show_alert=True
        )
        return

    # A pending_request is only ever needed for the review-tier part, so
    # the "one request in flight" guard only applies when this pick
    # actually needs review — a request that's entirely within the free
    # tier is never blocked by a stale pending_request.
    if review_count and pending:
        await call.answer("У вас уже есть необработанный запрос.", show_alert=True)
        return

    reply_lines = []

    # --- FREE TIER: issued immediately, no admin involved ---
    if free_count:
        created = await _issue_now(username, free_count, followers)
        for _, card in created:
            await call.message.answer(card)

        if created:
            reply_lines.append(f"🆓 Выдано автоматически (бесплатно): {len(created)}.")
            await notify_bg(
                log_to_channel,
                f"🆓 Автовыдача бесплатных доп. ссылок для {username}: {len(created)} шт."
            )

    # --- REVIEW TIER: sent to the admin, same as before ---
    if review_count:
        update_user(username, pending_request={
            "type": "extra_links",
            "count": review_count,
            "requested_at": utcnow_naive().isoformat(),
        })

        current_total = len(followers) + free_count  # includes free-tier ones just issued above
        caption = (
            f"🔌 Запрос доп. ссылок от {username}\n"
            f"Сейчас выпущено доп. ссылок: {current_total} (сверх основной)\n"
            f"Запрашивает ещё: {review_count} шт. сверх бесплатного лимита\n\n"
            f"«✅ Выдать» — с наценкой +{EXTRA_LINK_SURCHARGE}₽/мес за каждую сверх лимита.\n"
            f"«💚 Без наценки» — выдать столько же, но без повышения тарифа."
        )

        kb_rows = [
            [InlineKeyboardButton(text=f"✅ Выдать {review_count}", callback_data=f"exlreview:{username}:approve")],
            [InlineKeyboardButton(text="💚 Без наценки", callback_data=f"exlreview:{username}:approve_free")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"exlreview:{username}:reject")],
        ]

        await _send_admin_review_card(caption, kb_rows, user.get("telegram_id"))
        await notify_bg(log_to_channel, caption)

        reply_lines.append(f"📨 Запрос на {review_count} доп. ссылок сверх бесплатного лимита отправлен администратору.")

    await call.message.answer("\n".join(reply_lines) if reply_lines else "Запрос обработан.")
    await call.answer()


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
            await call.message.edit_text((call.message.text or call.message.caption or "") + "\n\n⚠️ Уже обработано.")
        except Exception:
            pass
        return

    count = pending.get("count", 1)
    update_user(username, pending_request=None)

    if action == "reject":
        try:
            await call.message.edit_text((call.message.text or "") + "\n\n❌ Отклонено")
        except Exception:
            pass

        if user.get("telegram_id"):
            await notify_client(
                bot, user["telegram_id"],
                "❌ Запрос на дополнительные ссылки отклонён администратором.",
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
    # client's price, "approve" leaves it as-is so they do.
    created = await _issue_now(username, count, get_followers(username))
    waived = action == "approve_free"

    if waived and created:
        update_user(username, surcharge_exempt_links=(user.get("surcharge_exempt_links") or 0) + len(created))

    status_note = "✅ Выдано {} из {}{}".format(
        len(created), count, " (без наценки)" if waived else ""
    )
    try:
        await call.message.edit_text((call.message.text or "") + f"\n\n{status_note}")
    except Exception:
        pass

    if user.get("telegram_id") and created:
        client_note = (
            f"✅ Администратор выдал вам {len(created)} доп. {'ссылку' if len(created) == 1 else 'ссылки/ссылок'}"
            + (" без повышения тарифа:" if waived else (
                f" (+{EXTRA_LINK_SURCHARGE}₽/мес за каждую — сумма к оплате увеличится "
                f"НАЧИНАЯ СО СЛЕДУЮЩЕГО продления, см. «💳 Реквизиты для оплаты»):"
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
        f"✅ Выдано {len(created)} доп. ссылок для {username} (запрошено {count})"
        + (", без наценки." if waived else ".")
    )

    await call.answer("Готово")
