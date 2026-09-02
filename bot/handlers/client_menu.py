"""
The client-facing informational buttons: status, "my connections", payment
info, and the instructions submenu (app install steps + RU-sites
routing/bypass lists). None of these use FSM state.

client_my_connections ("🔗 Мои подключения", renamed from "🔗 Моя ссылка")
now branches on whether this client has any issued sub-accounts (followers,
see bot/follower_issuance.py): with none, it behaves exactly like the old
"🔗 Моя ссылка" did — straight to the connection card, plus a note that one
link covers 2 devices and a button to request more. With followers, it
shows a picker instead — one button per account, own included — and reuses
the same "request more" button underneath. The actual request flow
(picking a count, notifying the admin, admin approve/reject) lives in
handlers/extra_links.py — this file only renders the entry-point button and
the "which of MY accounts" picker.
"""
import html

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import is_admin
from bot.config import DOMAIN
from bot.formatting import extract_qr_link, format_connection_message
from bot.keyboards import instructions_menu_kb, platform_choice_kb, routing_platform_kb
from core.dates import is_expired
from core.db import get_followers, get_user, get_user_by_telegram_id
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

MORE_DEVICES_NOTE = "ℹ️ Одна ссылка подключает до 2 устройств одновременно."


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


def _more_devices_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Подключить ещё устройства", callback_data="extralinks:start")]
    ])


@router.message(F.text == "🔗 Мои подключения")
async def client_my_connections(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await msg.answer(ACCESS_EXPIRED_MESSAGE)
        return

    username = user.get("username")
    followers = get_followers(username)

    if not followers:
        link = generate_link(username, DOMAIN)
        await msg.answer(
            format_connection_message(username, user.get("password"), user.get("expires_at"), link)
            + f"\n\n{MORE_DEVICES_NOTE} Нужно больше — жмите ниже.",
            reply_markup=_more_devices_kb()
        )
        return

    rows = [[InlineKeyboardButton(text=f"👤 {username} (основное)", callback_data=f"myconn:{username}")]]
    for f in followers:
        rows.append([InlineKeyboardButton(text=f"📱 {f.get('username')}", callback_data=f"myconn:{f.get('username')}")])
    rows.append([InlineKeyboardButton(text="📱 Подключить ещё устройства", callback_data="extralinks:start")])

    await msg.answer(
        f"{MORE_DEVICES_NOTE}\n\nУ вас {1 + len(followers)} подключения. Выберите, какое показать:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("myconn:"))
async def client_my_connection_card(call: CallbackQuery):
    requester = get_user_by_telegram_id(call.from_user.id)
    if not requester:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    target_username = call.data.split(":", 1)[1]

    # SECURITY: only the client's own account or one of THEIR OWN
    # followers is reachable here — never trust the callback_data alone,
    # since a crafted callback could otherwise ask for anyone's
    # credentials.
    own_username = requester.get("username")
    allowed = {own_username} | {f.get("username") for f in get_followers(own_username)}

    if target_username not in allowed:
        await call.answer("Недоступно.", show_alert=True)
        return

    target = get_user(target_username)
    if not target:
        await call.answer("Не найдено.", show_alert=True)
        return

    link = generate_link(target_username, DOMAIN)
    await call.message.answer(
        format_connection_message(target_username, target.get("password"), target.get("expires_at"), link)
    )
    await call.answer()


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
