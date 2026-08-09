"""
The client-facing informational buttons: status, "my link", payment info,
and the instructions submenu (app install steps + RU-sites routing/bypass
lists). None of these use FSM state.
"""
import html

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

from bot.access import is_admin
from bot.config import DOMAIN
from bot.formatting import extract_qr_link, format_connection_message
from bot.keyboards import instructions_menu_kb, platform_choice_kb, routing_platform_kb
from core.dates import is_expired
from core.db import get_user_by_telegram_id
from core.generator import generate_link
from core.instructions import (
    ANDROID_BYPASS_DOMAINS,
    IOS_BYPASS_DOMAINS,
    ROUTING_INTRO,
    render_android_instructions,
    render_ios_instructions,
)
from core.payment import ACCESS_EXPIRED_MESSAGE, PAYMENT_INFO

router = Router()


@router.message(F.text == "ℹ️ Мой статус")
async def client_status(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    username = user.get("username")

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await msg.answer(f"👤 {username}\n\n{ACCESS_EXPIRED_MESSAGE}")
        return

    expires_at = user.get("expires_at")
    status_line = "∞ бессрочно" if not expires_at else expires_at
    await msg.answer(f"👤 {username}\n⏳ Доступ до: {status_line}")


@router.message(F.text == "💳 Реквизиты для оплаты")
async def client_payment_info(msg: Message):
    if is_admin(msg.from_user.id):
        return
    await msg.answer(PAYMENT_INFO)


@router.message(F.text == "🔗 Моя ссылка")
async def client_my_link(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await msg.answer(ACCESS_EXPIRED_MESSAGE)
        return

    username = user.get("username")
    link = generate_link(username, DOMAIN)

    await msg.answer(
        format_connection_message(username, user.get("password"), user.get("expires_at"), link)
    )


# ---------------- INSTRUCTIONS MENU ----------------

@router.message(F.text == "📖 Инструкция")
async def client_instructions_menu(msg: Message):
    if is_admin(msg.from_user.id):
        return
    await msg.answer("Что показать?", reply_markup=instructions_menu_kb())


@router.callback_query(F.data == "instr:connect")
async def instructions_connect(call: CallbackQuery):
    await call.message.answer("Выберите вашу платформу:", reply_markup=platform_choice_kb())
    await call.answer()


@router.callback_query(F.data == "instr:routing")
async def instructions_routing(call: CallbackQuery):
    await call.message.answer(ROUTING_INTRO, reply_markup=routing_platform_kb())
    await call.answer()


@router.callback_query(F.data == "route:android")
async def routing_list_android(call: CallbackQuery):
    # Wrapped in <pre> so Telegram renders it as a monospace block with a
    # tap-to-copy affordance — much easier to grab the whole list on mobile
    # than plain text.
    await call.message.answer(f"<pre>{html.escape(ANDROID_BYPASS_DOMAINS)}</pre>", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "route:ios")
async def routing_list_ios(call: CallbackQuery):
    await call.message.answer(f"<pre>{html.escape(IOS_BYPASS_DOMAINS)}</pre>", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "howto:ios")
async def howto_ios(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await call.message.answer(render_ios_instructions(link))
    await call.answer()


@router.callback_query(F.data == "howto:android")
async def howto_android(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await call.message.answer(render_android_instructions(link))
    await call.answer()
