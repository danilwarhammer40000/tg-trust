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


@router.callback_query(F.data == "extralinks:start")
async def extra_links_start(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    if user.get("pending_request"):
        await call.answer(
            "У вас уже есть необработанный запрос — дождитесь ответа администратора.",
            show_alert=True
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"+{n}", callback_data=f"extralinks:req:{n}")
        for n in range(1, MAX_EXTRA_LINKS + 1)
    ]])

    free_left = _free_remaining(user["username"])
    if free_left:
        free_note = (
            f"🆓 Бесплатно и сразу — доступно ещё {free_left} "
            f"{'ссылка' if free_left == 1 else 'ссылки'} "
            f"(до {free_left * 2} устройств).\n"
            "Сверх этого — по согласованию с администратором."
        )
    else:
        free_note = "ℹ️ Бесплатный лимит уже использован — новые ссылки потребуют согласования с администратором."

    await call.message.answer(
        "ℹ️ Одна ссылка подключает до 2 устройств одновременно.\n\n"
        f"{free_note}\n\n"
        "Сколько дополнительных ссылок нужно?",
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

    # A pending_request is only ever needed for the review-tier part, so
    # the "one request in flight" guard only applies when this pick
    # actually needs review — a request that's entirely within the free
    # tier is never blocked by a stale pending_request.
    if review_count and user.get("pending_request"):
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

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Выдать {review_count}", callback_data=f"exlreview:{username}:approve")],
            [InlineKeyboardButton(text="💚 Без наценки", callback_data=f"exlreview:{username}:approve_free")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"exlreview:{username}:reject")],
        ])

        await bot.send_message(ADMIN_ID, caption, reply_markup=kb)
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
            + (" без повышения тарифа:" if waived else f" (+{EXTRA_LINK_SURCHARGE}₽/мес за каждую — см. «💳 Реквизиты для оплаты»):")
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
