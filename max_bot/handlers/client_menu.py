"""
Owns: status, "my link", payment info, and the instructions submenu
(app install steps + RU-sites routing/bypass lists) — the MAX-side
equivalent of bot/handlers/client_menu.py.

MAX has no persistent bottom reply-keyboard like Telegram's
ReplyKeyboardMarkup (everything is inline buttons attached per-message, or
typed commands) — so instead of one client_menu keyboard shown once,
every response here re-attaches a small inline keyboard with the other
options, and /start's own reply is the main entry point.
"""
import logging
import os

from maxapi import Router, F
from maxapi.types import CallbackButton, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from bot.formatting import extract_qr_link, format_connection_message
from core.dates import is_expired
from core.db import get_user_by_max_chat_id
from core.generator import generate_link
from core.instructions import (
    ANDROID_BYPASS_DOMAINS,
    IOS_BYPASS_DOMAINS,
    ROUTING_INTRO,
    render_android_instructions,
    render_ios_instructions,
)
from core.payment import ACCESS_EXPIRED_MESSAGE, PAYMENT_INFO
from max_bot.handlers.start import _extract_chat_id

router = Router()
log = logging.getLogger(__name__)

DOMAIN = os.getenv("TRUSTTUNNEL_DOMAIN")


@router.message_created(F.message.body.text == "/status")
async def client_status(event: MessageCreated):
    chat_id = _extract_chat_id(event)
    user = get_user_by_max_chat_id(chat_id)
    if not user:
        await event.message.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    username = user.get("username")

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await event.message.answer(f"👤 {username}\n\n{ACCESS_EXPIRED_MESSAGE}")
        return

    expires_at = user.get("expires_at")
    status_line = "∞ бессрочно" if not expires_at else expires_at
    await event.message.answer(f"👤 {username}\n⏳ Доступ до: {status_line}")


@router.message_created(F.message.body.text == "/pay")
async def client_payment_info(event: MessageCreated):
    await event.message.answer(PAYMENT_INFO)


@router.message_created(F.message.body.text == "/link")
async def client_my_link(event: MessageCreated):
    chat_id = _extract_chat_id(event)
    user = get_user_by_max_chat_id(chat_id)
    if not user:
        await event.message.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await event.message.answer(ACCESS_EXPIRED_MESSAGE)
        return

    username = user.get("username")
    link = generate_link(username, DOMAIN)

    await event.message.answer(
        format_connection_message(username, user.get("password"), user.get("expires_at"), link)
    )


# ---------------- INSTRUCTIONS ----------------

@router.message_created(F.message.body.text == "/help")
async def client_instructions_menu(event: MessageCreated):
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📲 Подключение", payload="instr_connect"))
    builder.row(CallbackButton(text="🇷🇺 РФ-сайты и ВПН", payload="instr_routing"))
    await event.message.answer(text="Что показать?", attachments=[builder.as_markup()])


@router.message_callback(F.callback.payload == "instr_connect")
async def instructions_connect(event: MessageCallback):
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📱 iOS", payload="howto_ios"))
    builder.row(CallbackButton(text="🤖 Android", payload="howto_android"))
    await event.message.answer(text="Выберите вашу платформу:", attachments=[builder.as_markup()])


@router.message_callback(F.callback.payload == "instr_routing")
async def instructions_routing(event: MessageCallback):
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📋 Список для Android", payload="route_android"))
    builder.row(CallbackButton(text="📋 Список для iOS", payload="route_ios"))
    await event.message.answer(text=ROUTING_INTRO, attachments=[builder.as_markup()])


@router.message_callback(F.callback.payload == "route_android")
async def routing_list_android(event: MessageCallback):
    # NOTE: unlike Telegram's HTML <pre> tap-to-copy block, MAX's exact
    # monospace/code formatting tag isn't confirmed here — sending as plain
    # text for now. Revisit once MAX's HTML/Markdown formatting option is
    # verified against a real send (see core/notify.py's MAX section and
    # README.md for the same caveat).
    await event.message.answer(ANDROID_BYPASS_DOMAINS)


@router.message_callback(F.callback.payload == "route_ios")
async def routing_list_ios(event: MessageCallback):
    await event.message.answer(IOS_BYPASS_DOMAINS)


@router.message_callback(F.callback.payload == "howto_ios")
async def howto_ios(event: MessageCallback):
    chat_id = event.message.recipient.chat_id
    user = get_user_by_max_chat_id(chat_id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await event.message.answer(render_ios_instructions(link))


@router.message_callback(F.callback.payload == "howto_android")
async def howto_android(event: MessageCallback):
    chat_id = event.message.recipient.chat_id
    user = get_user_by_max_chat_id(chat_id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await event.message.answer(render_android_instructions(link))
