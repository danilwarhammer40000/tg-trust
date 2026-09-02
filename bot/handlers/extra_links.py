"""
Owns: nothing FSM-wise (count picker is a plain inline keyboard, no state
needed — the callback_data itself carries the chosen count).

Client-initiated self-service request for additional device links — same
shape as the receipt-renewal request in handlers/receipt.py (a
pending_request on the user's own record, an admin review card with
approve/reject), but WITHOUT any payment/receipt involved: this is purely
"give me N more of my own sub-accounts", approved at the admin's
discretion.

Reuses the single pending_request slot on the user record — a client can't
have a renewal receipt AND an extra-links request in flight at the same
time (see the guard in extra_links_start/extra_links_pick below). That's a
deliberate simplification, not an oversight: both are rare, short-lived,
one-at-a-time asks from the same person.
"""
import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only, notify_bg, notify_client, run_sync
from bot.config import ADMIN_ID, bot
from bot.follower_issuance import issue_follower, leader_is_active
from core.dates import utcnow_naive
from core.db import get_followers, get_user, get_user_by_telegram_id, update_user
from core.notify import log_to_channel

router = Router()
log = logging.getLogger(__name__)

MAX_EXTRA_LINKS = 4


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

    await call.message.answer(
        "ℹ️ Одна ссылка подключает до 2 устройств одновременно.\n\n"
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

    if user.get("pending_request"):
        await call.answer("У вас уже есть необработанный запрос.", show_alert=True)
        return

    count = int(call.data.split(":", 2)[2])
    username = user["username"]

    update_user(username, pending_request={
        "type": "extra_links",
        "count": count,
        "requested_at": utcnow_naive().isoformat(),
    })

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Выдать {count}", callback_data=f"exlreview:{username}:approve"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"exlreview:{username}:reject"),
    ]])

    caption = f"🔌 Запрос доп. ссылок от {username}: {count} шт. (без оплаты)"
    await bot.send_message(ADMIN_ID, caption, reply_markup=kb)
    await notify_bg(log_to_channel, caption)

    await call.message.answer(f"✅ Запрос на {count} доп. ссылок отправлен администратору.")
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

    # action == "approve"
    was_active = leader_is_active(user)

    created = []
    followers_snapshot = get_followers(username)
    for _ in range(count):
        new_username, card = issue_follower(username, existing_followers=followers_snapshot)
        if not new_username:
            break
        created.append((new_username, card))
        followers_snapshot = followers_snapshot + [{"username": new_username}]

    if was_active and created:
        await run_sync()

    try:
        await call.message.edit_text((call.message.text or "") + f"\n\n✅ Выдано {len(created)} из {count}")
    except Exception:
        pass

    if user.get("telegram_id") and created:
        delivered = await notify_client(
            bot, user["telegram_id"],
            f"✅ Администратор выдал вам {len(created)} доп. {'ссылку' if len(created) == 1 else 'ссылки/ссылок'}:",
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
        f"✅ Выдано {len(created)} доп. ссылок для {username} (запрошено {count})."
    )

    await call.answer("Готово")
