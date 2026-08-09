"""
Generic pagination for inline keyboards.

Telegram inline keyboards silently break past a certain size (the client
either refuses to render the message's reply_markup, or truncates it) —
with enough clients, one row per user meant the list just cut off partway
through with no error, no scroll, nothing. Every "one row per user" list
in this bot (List users, Get link, mass delete, broadcast recipient
picker, trial management) goes through this module instead of dumping
every row into one InlineKeyboardMarkup.

The router here only holds the "noop" page-indicator button — the actual
per-list ":{page}" callbacks (listpage:, linkpage:, delpage:, ...) each
live in their own feature's handlers/ file, next to the keyboard-builder
that uses pagination_nav_row().
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton

router = Router()

USERS_PAGE_SIZE = 15


def paginate(items: list, page: int):
    """Returns (page_items, total_pages, page) — page is clamped into range."""
    total_pages = max(1, (len(items) + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * USERS_PAGE_SIZE
    page_items = items[start:start + USERS_PAGE_SIZE]

    return page_items, total_pages, page


def pagination_nav_row(page: int, total_pages: int, callback_prefix: str) -> list:
    """
    Builds the ⬅️/➡️ row for a given page. callback_prefix's handler must
    accept a trailing ":{page}" and re-render the same list at that page —
    see e.g. F.data.startswith("listpage:") in handlers/list_users.py.
    """
    if total_pages <= 1:
        return []

    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"{callback_prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"{callback_prefix}:{page + 1}"))

    return [row]


@router.callback_query(F.data == "noop")
async def noop_callback(call: CallbackQuery):
    """The "3/7" page-indicator button in the middle of the nav row — not clickable."""
    await call.answer()
