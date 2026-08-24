"""
Owns: the "is this a payment receipt?" confirm step for photos/documents a
client sends.

DESIGN DECISION — no FSM: the Telegram bot's equivalent flow
(bot/handlers/receipt.py) uses aiogram FSM state (ReceiptConfirm.waiting)
to remember "this specific photo is awaiting a yes/no answer" between the
photo arriving and the confirm button being tapped. This file deliberately
avoids maxapi's FSM (MemoryContext/StatesGroup) instead, because I could
not verify its exact API against a real install (see README.md's MAX bot
section for what's confirmed vs. not). Instead, the pending photo is kept
in a small in-process dict, keyed by a short random token that's embedded
directly in the confirm button's callback payload — no persistent state
needed for a single yes/no round-trip. Trade-off: if the bot process
restarts between the client sending the photo and tapping the button, the
pending confirmation is lost and they just need to resend the photo. Given
how rare that window is, this is an acceptable simplification for the MVP.
"""
import logging
import secrets

import requests

from maxapi import Router, F
from maxapi.types import MessageCallback, MessageCreated, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from core.dates import is_expired, utcnow_naive
from core.db import get_user, get_user_by_max_chat_id, update_user
from core.notify import notify_admin, send_photo_bytes
from max_bot.handlers.start import _extract_chat_id

router = Router()
log = logging.getLogger(__name__)

# token -> {"username": str, "url": str}. Small and short-lived by nature
# (cleared on confirm/reject) — no cap needed for realistic traffic, but if
# this ever needs to survive a restart or scale across processes, move it
# to core/db.py's pending_request field on the user instead (that field
# already exists and is disk-backed — see how the Telegram-side receipt
# flow uses it).
_pending: dict = {}


def _new_token() -> str:
    token = secrets.token_hex(4)
    while token in _pending:
        token = secrets.token_hex(4)
    return token


@router.message_created(F.message.body.attachments)
async def any_media_received(event: MessageCreated):
    chat_id = _extract_chat_id(event)
    user = get_user_by_max_chat_id(chat_id)
    if not user:
        await event.message.answer(
            "Я вас пока не узнал 🤔\n"
            "Сначала пришлите вашу карточку подключения (текст с Username/Password), "
            "чтобы я мог связать вас с аккаунтом."
        )
        return

    attachments = event.message.body.attachments
    # NOTE (unverified): assumes the first image/file attachment exposes a
    # direct download URL at .payload.url — this is the common shape across
    # similar bot APIs, but hasn't been confirmed against a real MAX
    # attachment payload. If wrong, this is the one place to fix it.
    attachment = attachments[0]
    url = attachment.payload.url

    token = _new_token()
    _pending[token] = {"username": user["username"], "url": url}

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✅ Да, отправить", payload=f"rcpt_yes:{token}"))
    builder.row(CallbackButton(text="❌ Нет, это не то", payload=f"rcpt_no:{token}"))

    await event.message.answer(
        text="📎 Это чек на продление? Отправляем администратору на проверку?",
        attachments=[builder.as_markup()],
    )


def _renewal_admin_reply_markup(username: str) -> dict:
    """
    Mirrors bot/keyboards.py's renewal_admin_kb() in raw Telegram JSON
    shape (core/notify.py sends via raw HTTP, not aiogram objects — if you
    change one, change the other). Lets the Telegram admin approve/reject a
    MAX-submitted receipt with the exact same buttons as a Telegram-
    submitted one: bot/handlers/receipt.py's approve_renewal already
    handles "apr:{username}:..." regardless of which bot the client used.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "➕1 мес", "callback_data": f"apr:{username}:30"},
                {"text": "➕2 мес", "callback_data": f"apr:{username}:60"},
            ],
            [{"text": "✍️ Ручная дата", "callback_data": f"apr:{username}:manual"}],
            [{"text": "❌ Отклонить", "callback_data": f"apr:{username}:reject"}],
        ]
    }


@router.message_callback(F.callback.payload.startswith("rcpt_yes:"))
async def receipt_yes(event: MessageCallback):
    token = event.callback.payload.split(":", 1)[1]
    pending = _pending.pop(token, None)

    if not pending:
        await event.message.answer("Эта заявка уже устарела. Пришлите чек ещё раз.")
        return

    username = pending["username"]

    update_user(username, pending_request={
        "type": "renewal",
        "source": "max",
        "requested_at": utcnow_naive().isoformat(),
    })

    user = get_user(username) or {}
    current_expiry = user.get("expires_at")
    expiry_line = current_expiry or "∞ (безлимит)"
    if current_expiry and is_expired(current_expiry):
        expiry_line += " (уже истёк)"

    try:
        photo_bytes = requests.get(pending["url"], timeout=15).content
    except requests.RequestException as e:
        log.error("failed to download MAX receipt attachment for %s: %s", username, e)
        await event.message.answer("⚠️ Не удалось переслать чек администратору, попробуйте ещё раз.")
        return

    caption = (
        f"📥 [MAX] Заявка на продление от {username}\n"
        f"⏳ Текущая дата истечения: {expiry_line}"
    )

    sent = send_photo_bytes(
        photo_bytes,
        filename="max_receipt.jpg",
        caption=caption,
        reply_markup=_renewal_admin_reply_markup(username),
    )

    if sent:
        await event.message.answer("✅ Отправлено администратору. Ждите подтверждения.")
    else:
        await event.message.answer("⚠️ Не удалось переслать чек администратору, попробуйте ещё раз.")
        notify_admin(f"⚠️ Не удалось переслать MAX-чек от {username} — проверьте вручную.")


@router.message_callback(F.callback.payload.startswith("rcpt_no:"))
async def receipt_no(event: MessageCallback):
    token = event.callback.payload.split(":", 1)[1]
    _pending.pop(token, None)
    await event.message.answer("Хорошо, отменил. Если это всё же чек — пришлите его ещё раз.")
