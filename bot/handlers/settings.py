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
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only
from bot.config import bot, dp
from bot.keyboards import main_menu
from core.db import get_user, list_users, update_user
from core.messages import delete_conversation, get_messages, list_conversations

router = Router()

# How many of the most recent messages to show on one screen — storage
# itself is never trimmed (core/messages.py), this only bounds the
# single message the admin gets back so a very long history doesn't
# blow past Telegram's ~4096-char message limit.
HISTORY_DISPLAY_LIMIT = 40
CONVERSATIONS_PER_PAGE = 15
STUCK_PER_PAGE = 15


def settings_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Сортировка БД", callback_data="settings:sorting")],
        [InlineKeyboardButton(text="🎟 Управление триалами", callback_data="db:trials")],
        [InlineKeyboardButton(text="🤖 Автопродление", callback_data="settings:autoren")],
        [InlineKeyboardButton(text="🔄 Sync users", callback_data="settings:sync")],
        [InlineKeyboardButton(text="🔍 Проверка привязок", callback_data="bcast_mode:check")],
        [InlineKeyboardButton(text="🔓 Закрыть висящие заявки", callback_data="settings:stuck:0")],
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


# Human-readable label for each pending_request type — see the various
# handlers that set this field (bot/handlers/receipt.py, extra_links.py,
# client_menu.py) for the full list of possible values.
_PENDING_TYPE_LABELS = {
    "renewal": "продление по чеку",
    "extra_links": "оплата доп. устройств",
    "rate_topup": "доплата по ставке",
}


def _pending_type_label(pending: dict) -> str:
    t = (pending or {}).get("type", "?")
    return _PENDING_TYPE_LABELS.get(t, t)


def _stuck_list_kb(page: int, users: list) -> InlineKeyboardMarkup:
    start = page * STUCK_PER_PAGE
    page_items = users[start:start + STUCK_PER_PAGE]

    rows = [
        [InlineKeyboardButton(
            text=f"{u['username']} — {_pending_type_label(u.get('pending_request'))}",
            callback_data=f"settings:stuckuser:{u['username']}"
        )]
        for u in page_items
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"settings:stuck:{page - 1}"))
    if start + STUCK_PER_PAGE < len(users):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"settings:stuck:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("settings:stuck:"))
async def stuck_requests_list(call: CallbackQuery):
    """
    Lists every user with a non-empty pending_request — the admin doesn't
    need to already know WHICH client is stuck (see
    bot/handlers/extra_links.py's ExtraLinksPayment.waiting_receipt and
    similar states: a client who navigates away mid-flow without sending
    a receipt or explicitly cancelling leaves pending_request set AND
    their FSM state stuck on "waiting for a receipt", which then
    intercepts every message they send afterwards — even taps of
    unrelated menu buttons — until it's cleared).
    """
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 2)[2])
    users = [u for u in (list_users() or []) if u.get("pending_request")]

    if not users:
        await call.message.answer("🔓 Нет ни одной висящей заявки.")
        await call.answer()
        return

    await call.message.answer(
        f"🔓 Висящие заявки ({len(users)}) — выберите клиента, чтобы закрыть:",
        reply_markup=_stuck_list_kb(page, users)
    )
    await call.answer()


async def _clear_fsm_state(telegram_id) -> None:
    """
    Resets a client's FSM state/data by hand, given only their
    telegram_id — needed because a stuck state-scoped catch-all handler
    intercepts EVERY message from that user until cleared, and normally
    only clears itself when its own flow finishes or is cancelled. This
    is the manual escape hatch for when it doesn't (client closed the
    app mid-flow, lost connection, etc.) — no aiogram update needed, this
    talks to the FSM storage directly via a StorageKey.
    """
    if not telegram_id:
        return
    key = StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id)
    await dp.storage.set_state(key, state=None)
    await dp.storage.set_data(key, data={})


@router.callback_query(F.data.startswith("settings:stuckuser:"))
async def stuck_request_clear(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 2)[2]
    user = get_user(username)

    if not user:
        await call.answer("Пользователь не найден.", show_alert=True)
        return

    pending_label = _pending_type_label(user.get("pending_request"))
    update_user(username, pending_request=None)
    await _clear_fsm_state(user.get("telegram_id"))

    await call.message.answer(f"🔓 {username}: заявка ({pending_label}) закрыта, диалог сброшен.")
    await call.answer("Готово")
