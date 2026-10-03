from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only
from bot.config import DOMAIN
from bot.display import prepare_users_for_display, user_button_label
from bot.formatting import format_full_instructions_message
from bot.pagination import paginate, pagination_nav_row
from core.db import get_user, list_users
from core.generator import generate_link

router = Router()


def build_get_link_kb(users: list, page: int) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=user_button_label(u),
            callback_data=f"link:{u.get('username')}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "linkpage")

    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


@router.message(F.text == "🔗 Get link")
async def menu_link(msg: Message):
    if not await admin_only(msg):
        return

    users = prepare_users_for_display(list_users() or [])

    if not users:
        await msg.answer("No users")
        return

    kb, total_pages, page = build_get_link_kb(users, 0)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    await msg.answer(label, reply_markup=kb)


@router.callback_query(F.data.startswith("linkpage:"))
async def menu_link_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    users = prepare_users_for_display(list_users() or [])

    kb, total_pages, page = build_get_link_kb(users, page)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    try:
        await call.message.edit_text(label, reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("link:"))
async def link_callback(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":")[1]

    user = get_user(username) or {}
    link = generate_link(username, DOMAIN)

    await call.message.answer(
        format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)
    )

    await call.answer()
