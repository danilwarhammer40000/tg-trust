"""
Owns: nothing of its own — this is purely an entry-point/router screen.

"⚙️ Настройки" replaces four separate top-level main_menu buttons
(Сортировка БД, Автопродление, Sync users, и "🎟 Управление триалами" that
used to live inside "🗄 База данных") plus "🔍 Проверить привязки" that
used to live inside "📢 Рассылка" — all five now live under one submenu
here instead, so main_menu doesn't keep growing with buttons the admin
rarely needs day-to-day.

Every underlying feature keeps living in its own file exactly as before
(bot/handlers/sorting.py, auto_renewal_review.py, sync_deploy.py,
database.py's db:trials, broadcast.py's bcast_mode:check) — this file
only adds the buttons that route to them. Two of the five
(db:trials, bcast_mode:check) are pre-existing callback_data values
reused as-is; the other three (settings:sorting, settings:autoren,
settings:sync) are new callback handlers added directly in their
respective files, replacing the old direct-from-main-menu Message
handlers.

Also owns the "💬 Переписки" chat-history browser (list clients with
stored messages -> pick one -> see recent history -> optionally delete
the whole thing) — implemented directly in this file since it's a new
feature with no pre-existing home, though the entry-point BUTTON now
lives in bot/handlers/broadcast.py's "📢 Обращения" menu instead of here
(callback_data "settings:chats:0" is unchanged, so nothing else about
this feature moved — only which menu links to it). Storage/retrieval
itself lives in core/messages.py; see that module's docstring for the
"keep forever, delete only on explicit admin action" contract.
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only
from bot.keyboards import main_menu
from core.messages import delete_conversation, get_messages, list_conversations

router = Router()

# How many of the most recent messages to show on one screen — storage
# itself is never trimmed (core/messages.py), this only bounds the
# single message the admin gets back so a very long history doesn't
# blow past Telegram's ~4096-char message limit.
HISTORY_DISPLAY_LIMIT = 40
CONVERSATIONS_PER_PAGE = 15


def settings_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Сортировка БД", callback_data="settings:sorting")],
        [InlineKeyboardButton(text="🎟 Управление триалами", callback_data="db:trials")],
        [InlineKeyboardButton(text="🤖 Автопродление", callback_data="settings:autoren")],
        [InlineKeyboardButton(text="🔄 Sync users", callback_data="settings:sync")],
        [InlineKeyboardButton(text="🔍 Проверка привязок", callback_data="bcast_mode:check")],
    ])


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(msg: Message):
    if not await admin_only(msg):
        return
    await msg.answer("⚙️ Настройки", reply_markup=settings_menu_kb())


def _chats_list_kb(page: int, conversations: list) -> InlineKeyboardMarkup:
    start = page * CONVERSATIONS_PER_PAGE
    page_items = conversations[start:start + CONVERSATIONS_PER_PAGE]

    rows = [
        [InlineKeyboardButton(
            text=f"{c['username']} ({c['count']})",
            callback_data=f"settings:chat:{c['username']}"
        )]
        for c in page_items
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"settings:chats:{page - 1}"))
    if start + CONVERSATIONS_PER_PAGE < len(conversations):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"settings:chats:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("settings:chats:"))
async def chats_list(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 2)[2])
    conversations = list_conversations()

    if not conversations:
        await call.message.answer("💬 Пока нет ни одной сохранённой переписки.")
        await call.answer()
        return

    await call.message.answer(
        f"💬 Переписки ({len(conversations)}) — выберите клиента:",
        reply_markup=_chats_list_kb(page, conversations)
    )
    await call.answer()


def _format_message(m: dict) -> str:
    arrow = "⬅️ клиент" if m.get("direction") == "in" else "➡️ админ"
    ts = (m.get("timestamp") or "")[:16].replace("T", " ")
    body = m.get("text") or ("📎 [файл]" if m.get("file_id") else "")
    return f"{ts} {arrow}: {body}"


@router.callback_query(F.data.startswith("settings:chat:"))
async def chat_view(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 2)[2]
    messages = get_messages(username, limit=HISTORY_DISPLAY_LIMIT)

    if not messages:
        await call.answer("Переписка пуста или уже удалена.", show_alert=True)
        return

    total = len(get_messages(username))
    header = f"💬 {username} — последние {len(messages)} из {total}:\n\n"
    body = "\n".join(_format_message(m) for m in messages)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить всю переписку", callback_data=f"settings:chatdel:{username}")],
    ])

    # Telegram's message limit is ~4096 chars — HISTORY_DISPLAY_LIMIT=40
    # short lines normally fits comfortably, but a run of long messages
    # could still overflow, so truncate defensively rather than let
    # send_message fail outright.
    text = header + body
    if len(text) > 3800:
        text = text[:3800] + "\n\n… (обрезано, слишком длинная переписка для одного экрана)"

    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("settings:chatdel:"))
async def chat_delete_confirm(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 2)[2]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 Да, удалить переписку с {username}", callback_data=f"settings:chatdelyes:{username}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings:chats:0")],
    ])
    await call.message.answer(f"Удалить ВСЮ переписку с {username}? Это необратимо.", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("settings:chatdelyes:"))
async def chat_delete_execute(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 2)[2]
    removed = delete_conversation(username)

    await call.message.answer(f"🗑 Удалено сообщений: {removed} ({username}).", reply_markup=main_menu)
    await call.answer()
