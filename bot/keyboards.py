"""
Keyboards used across more than one handlers/ file. A keyboard used by
only one feature (e.g. the "3 дня / 1 мес / ♾" reply keyboard in
add_user.py) stays defined in that file instead of here — this module is
only for the ones with more than one caller.
"""
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Add user")],
        [KeyboardButton(text="📋 List users")],
        [KeyboardButton(text="🗑 Удаление пользователей")],
        [KeyboardButton(text="🔗 Get link")],
        [KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="🗄 База данных")],
        [KeyboardButton(text="⚙️ Сортировка БД")],
        [KeyboardButton(text="🤖 Автопродление")],
        [KeyboardButton(text="🔄 Sync users")],
        [KeyboardButton(text="🚀 Деплой")]
    ],
    resize_keyboard=True
)

# NOTE: label changed from "🔗 Моя ссылка" to "🔗 Мои подключения" — a
# client can now have more than one active connection (their own account
# plus any "-2"/"-3" extra-device sub-accounts issued via
# handlers/extra_links.py or the admin's "➕ Выпустить нового" button), and
# handlers/client_menu.py's client_my_link() branches on that. Old clients
# whose Telegram client cached the previous button label still match —
# client_my_link() listens for BOTH texts, see its F.text.in_({...}) filter.
client_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Мой статус")],
        [KeyboardButton(text="🔗 Мои подключения")],
        [KeyboardButton(text="📖 Инструкция")],
        [KeyboardButton(text="💳 Реквизиты для оплаты")],
        [KeyboardButton(text="✉️ Написать администратору")],
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Cancel")]],
    resize_keyboard=True
)


def platform_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 iOS", callback_data="howto:ios")],
        [InlineKeyboardButton(text="🤖 Android", callback_data="howto:android")],
    ])


def instructions_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Подключение", callback_data="instr:connect")],
        [InlineKeyboardButton(text="🇷🇺 РФ-сайты и ВПН", callback_data="instr:routing")],
    ])


def routing_platform_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список для Android", callback_data="route:android")],
        [InlineKeyboardButton(text="📋 Список для iOS", callback_data="route:ios")],
    ])


def renewal_admin_kb(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕1 мес", callback_data=f"apr:{username}:30"),
            InlineKeyboardButton(text="➕2 мес", callback_data=f"apr:{username}:60"),
        ],
        [InlineKeyboardButton(text="✍️ Ручная дата", callback_data=f"apr:{username}:manual")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"apr:{username}:reject")]
    ])
