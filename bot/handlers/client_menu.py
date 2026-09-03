"""
The client-facing informational buttons: status, "my connections", payment
info, and the instructions submenu (app install steps + RU-sites
routing/bypass lists). None of these use FSM state.
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


# ---------------- MY CONNECTIONS ----------------
#
# Listens for both the current label ("🔗 Мои подключения") and the old one
# ("🔗 Моя ссылка") — a client's Telegram app may have the old label cached
# on its reply keyboard until it re-renders, and both must keep working.
#
# Deliberately never sends a connection card straight away, even when the
# client only has one account (their own): the link is only generated
# (generate_link() shells out to the trusttunnel binary — not free) once
# they actually tap a specific connection. This message is always just a
# picker.
#
# A client normally has exactly one connection (their own account). If an
# admin issued extra device-links for them (handlers/leader_link.py's
# "➕ Выпустить нового ведомого") or they requested some themselves
# (handlers/extra_links.py) and got approved, they become a "leader" of
# their own "-2"/"-3"/... sub-accounts (see follower_issuance.py) and see
# more than one button here.

@router.message(F.text.in_({"🔗 Мои подключения", "🔗 Моя ссылка"}))
async def client_my_link(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await msg.answer(ACCESS_EXPIRED_MESSAGE)
        return

    username = user.get("username")
    followers = get_followers(username)
    accounts = [user] + followers

    rows = [
        [InlineKeyboardButton(text=f"🔌 {a.get('username')}", callback_data=f"myconn:{a.get('username')}")]
        for a in accounts if a.get("username")
    ]
    rows.append([InlineKeyboardButton(text="📱 Подключить ещё устройства", callback_data="extralinks:start")])

    note = "\n\nℹ️ Одна ссылка подключает до 2 устройств одновременно." if len(accounts) == 1 else ""
    await msg.answer(
        f"Ваши подключения ({len(accounts)}):{note}\n\nНажмите на нужное, чтобы получить карточку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("myconn:"))
async def client_my_connection_card(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    own_username = user.get("username")
    target_username = call.data.split(":", 1)[1]

    # SECURITY: only the caller's own account or their own followers are
    # reachable here — target_username comes from callback_data, which a
    # client could in principle tamper with, so this is re-checked
    # server-side rather than trusting that the button they were shown was
    # the only one they could tap.
    allowed = {own_username} | {f.get("username") for f in get_followers(own_username)}
    if target_username not in allowed:
        await call.answer("Доступ запрещён.", show_alert=True)
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
