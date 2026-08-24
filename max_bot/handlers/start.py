"""
Owns: onboarding (bind existing account vs. trial signup) and the initial
/start entry point.

Design note: no FSM here (or anywhere in max_bot/) — see
max_bot/handlers/receipt.py's module docstring for why. Every flow here is
either a single command/button tap, or a plain "does this text look like a
connection card" check on any incoming text message — the same logic
bot/handlers/start.py's bind_by_card uses on the Telegram side, reused
as-is via bot.formatting (which has no aiogram dependency).
"""
import logging
from datetime import timedelta

from maxapi import Router, F
from maxapi.filters.command import CommandStart
from maxapi.types import CallbackButton, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from bot.formatting import CARD_RE, looks_like_card
from core.dates import utcnow_naive
from core.db import add_user, get_user, get_user_by_max_chat_id, update_user
from core.notify import notify_admin
from core.trial import generate_trial_password, generate_username_from_name, has_used_trial, mark_trial_used

router = Router()
log = logging.getLogger(__name__)


def _extract_chat_id(event: MessageCreated) -> int:
    """
    NOTE (unverified against a real install): best-informed guess based on
    the documented Message object (message.recipient is a Recipient —
    user/bot/chat/channel — and MAX's own GET /messages endpoint takes a
    chat_id query param, so a Recipient should expose one the same way).
    Verify this against a real event payload and fix in ONE place if wrong
    — every other file in max_bot/ calls this function rather than reading
    the field directly.
    """
    return event.message.recipient.chat_id


@router.message_created(CommandStart())
async def start(event: MessageCreated):
    chat_id = _extract_chat_id(event)
    user = get_user_by_max_chat_id(chat_id)

    if user:
        await event.message.answer(f"👋 С возвращением, {user.get('username')}!")
        return

    if has_used_trial(chat_id):
        await event.message.answer(
            "👋 Привет! Чтобы я мог присылать вам уведомления об истечении доступа,\n"
            "пришлите сюда вашу карточку подключения целиком "
            "(то сообщение с Username / Password, которое вам отправил администратор)."
        )
        return

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✅ Да, пользуюсь — привязать карточку", payload="onboard_existing"))
    builder.row(CallbackButton(text="🆕 Нет — попробовать 4 дня бесплатно", payload="onboard_trial"))

    await event.message.answer(
        text="👋 Привет! Вы уже пользуетесь клубным TrustTunnel VPN?",
        attachments=[builder.as_markup()],
    )


@router.message_callback(F.callback.payload == "onboard_existing")
async def onboard_existing(event: MessageCallback):
    await event.message.answer(
        "Пришлите сюда вашу карточку подключения целиком "
        "(то сообщение с Username / Password, которое вам отправил администратор)."
    )


@router.message_callback(F.callback.payload == "onboard_trial")
async def onboard_trial_start(event: MessageCallback):
    chat_id = event.message.recipient.chat_id  # same field as _extract_chat_id, see note there

    if has_used_trial(chat_id):
        await event.message.answer("Пробный период уже был использован этим аккаунтом.")
        return

    display_name = event.callback.user.name if event.callback.user else None
    username = generate_username_from_name(display_name, None, chat_id)
    password = generate_trial_password()
    expires_at = (utcnow_naive() + timedelta(days=4)).strftime("%Y-%m-%d")

    add_user({
        "username": username,
        "password": password,
        "created_at": utcnow_naive().strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "status": "active",
        "max_chat_id": chat_id,
        "notified_days": [],
        "pending_request": None,
    })

    mark_trial_used(chat_id)

    # Same rationale as the Telegram bot's run_sync(): rebuild
    # credentials.toml + restart trusttunnel so the new account actually
    # works. Kept synchronous here (not offloaded to an executor) since
    # max_bot is a much lower-traffic bot than the Telegram one for now —
    # revisit if that changes.
    from core.service import safe_sync
    safe_sync()

    await event.message.answer(
        f"🎁 Пробный доступ на 4 дня активирован! Доступ до {expires_at}.\n\n"
        f"👤 Username: {username}\n"
        f"🔑 Password: {password}\n"
        f"⏳ Expires: {expires_at}\n\n"
        "Об истечении напомню заранее, а продлить сможете прямо в этом чате."
    )

    notify_admin(f"🆕 [MAX] Новая регистрация по триалу: {username} (chat id {chat_id}), доступ до {expires_at}.")


@router.message_created(F.message.body.text.func(looks_like_card))
async def bind_by_card(event: MessageCreated):
    text = event.message.body.text
    match = CARD_RE.search(text)
    if not match:
        await event.message.answer(
            "Не смог распознать карточку. Пришлите её целиком, без изменений, "
            "так как её отправил администратор."
        )
        return

    username, password = match.group(1), match.group(2)
    user = get_user(username)

    if not user or user.get("password") != password:
        await event.message.answer("Не нашёл такой аккаунт. Проверьте, что скопировали карточку без изменений.")
        return

    chat_id = _extract_chat_id(event)
    update_user(username, max_chat_id=chat_id)

    await event.message.answer(
        f"✅ Готово, {username}! Теперь я буду присылать уведомления об истечении доступа."
    )

    notify_admin(f"🔔 [MAX] Клиент {username} привязал MAX (chat id {chat_id}) — подписан на уведомления.")
