"""
Owns: AddUser.username/password/days/manual_date,
AddUserMulti.username/password/days/manual_date/continue_choice/done_actions.
"""
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.access import admin_only, run_sync
from bot.config import DOMAIN
from bot.formatting import format_full_instructions_message
from bot.keyboards import cancel_kb, main_menu
from bot.states import AddUser, AddUserMulti
from core.dates import add_calendar_months, utcnow_naive
from core.db import add_user, get_user
from core.generator import generate_link

router = Router()


# ---------------- SINGLE ----------------

@router.message(F.text == "➕ Add user")
async def menu_add(msg: Message):
    if not await admin_only(msg):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Одного", callback_data="addmode:single")],
        [InlineKeyboardButton(text="👥 Несколько", callback_data="addmode:multi")],
    ])
    await msg.answer("Сколько клиентов добавляем?", reply_markup=kb)


@router.callback_query(F.data == "addmode:single")
async def add_mode_single(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.set_state(AddUser.username)
    await call.message.answer("Enter username:", reply_markup=cancel_kb)
    await call.answer()


@router.message(AddUser.username)
async def add_username(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    await state.update_data(username=msg.text.strip())
    await state.set_state(AddUser.password)
    await msg.answer("Enter password:")


@router.message(AddUser.password)
async def add_password(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    await state.update_data(password=msg.text.strip())
    await state.set_state(AddUser.days)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="3 дня")],
            [KeyboardButton(text="1 мес")],
            [KeyboardButton(text="♾ Безлимит")],
            [KeyboardButton(text="📅 Ручной ввод даты")],
        ],
        resize_keyboard=True
    )

    await msg.answer("Выберите срок действия:", reply_markup=kb)


async def finalize_add_user(msg: Message, state: FSMContext, expires_at):
    data = await state.get_data()
    username = data["username"]
    password = data["password"]

    add_user({
        "username": username,
        "password": password,
        "created_at": utcnow_naive().strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "status": "active",
        "telegram_id": None,
        "notified_days": [],
        "pending_request": None,
    })

    await run_sync()

    link = generate_link(username, DOMAIN)

    await msg.answer(
        format_full_instructions_message(username, password, expires_at, link),
        reply_markup=ReplyKeyboardRemove()
    )

    await msg.answer("Menu:", reply_markup=main_menu)
    await state.clear()


def compute_new_user_expiry(label: str):
    now = utcnow_naive()

    if label == "3 дня":
        return (now + timedelta(days=3)).strftime("%Y-%m-%d")
    if label == "1 мес":
        return add_calendar_months(now, 1).strftime("%Y-%m-%d")
    if label == "♾ Безлимит":
        return None
    return None


@router.message(AddUser.days)
async def add_days(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    text = msg.text.strip()

    if text == "📅 Ручной ввод даты":
        await state.set_state(AddUser.manual_date)
        await msg.answer("Введите дату истечения в формате YYYY-MM-DD:", reply_markup=cancel_kb)
        return

    if text not in ("3 дня", "1 мес", "♾ Безлимит"):
        await msg.answer("Выберите один из вариантов на клавиатуре.")
        return

    expires_at = compute_new_user_expiry(text)

    await finalize_add_user(msg, state, expires_at)


@router.message(AddUser.manual_date)
async def add_manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    try:
        datetime.strptime(msg.text.strip(), "%Y-%m-%d")
    except ValueError:
        await msg.answer("Неверный формат. Введите дату как YYYY-MM-DD.")
        return

    await finalize_add_user(msg, state, msg.text.strip())


# ---------------- MULTIPLE ----------------

multi_step_cancel_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="❌ Отменить этого клиента")],
        [KeyboardButton(text="❌ Отменить всё добавление")],
    ],
    resize_keyboard=True
)


@router.callback_query(F.data == "addmode:multi")
async def add_mode_multi(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.set_state(AddUserMulti.username)
    await state.update_data(batch=[])
    await call.message.answer("👤 Клиент №1 — введите username:", reply_markup=ReplyKeyboardRemove())
    await call.answer()


async def handle_multi_cancel(msg: Message, state: FSMContext) -> bool:
    """
    Handles the two special cancel buttons shared by every step of the
    multi-add loop. Returns True if the caller should stop processing
    (a cancel was handled), False if this was normal input to validate.
    """
    text = (msg.text or "").strip()

    if text == "❌ Отменить всё добавление":
        await state.clear()
        await msg.answer("Добавление отменено, никто не сохранён.", reply_markup=main_menu)
        return True

    if text == "❌ Отменить этого клиента":
        data = await state.get_data()
        batch = data.get("batch", [])

        await state.update_data(current_username=None, current_password=None)

        if not batch:
            await state.clear()
            await msg.answer("Отменил — пока никто не был добавлен в очередь.", reply_markup=main_menu)
            return True

        await msg.answer("Ок, этот клиент отменён.", reply_markup=ReplyKeyboardRemove())
        await show_multi_continue_choice(msg, state, batch)
        return True

    return False


async def show_multi_continue_choice(msg: Message, state: FSMContext, batch: list):
    await state.set_state(AddUserMulti.continue_choice)

    names = "\n".join(f"• {u['username']} ({u['expires_at'] or '∞'})" for u in batch)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="multiadd:more")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="multiadd:done")],
    ])

    await msg.answer(f"В очереди ({len(batch)}):\n{names}\n\nЧто дальше?", reply_markup=kb)


@router.message(AddUserMulti.username)
async def multi_add_username(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    username = (msg.text or "").strip()
    data = await state.get_data()
    batch = data.get("batch", [])

    if get_user(username) or any(u["username"] == username for u in batch):
        await msg.answer("Это имя уже занято (в базе или уже в текущей очереди). Введите другое:")
        return

    await state.update_data(current_username=username)
    await state.set_state(AddUserMulti.password)
    await msg.answer("Введите password:")


@router.message(AddUserMulti.password)
async def multi_add_password(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    password = (msg.text or "").strip()
    await state.update_data(current_password=password)
    await state.set_state(AddUserMulti.days)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="3 дня")],
            [KeyboardButton(text="1 мес")],
            [KeyboardButton(text="♾ Безлимит")],
            [KeyboardButton(text="📅 Ручной ввод даты")],
            [KeyboardButton(text="❌ Отменить этого клиента")],
            [KeyboardButton(text="❌ Отменить всё добавление")],
        ],
        resize_keyboard=True
    )
    await msg.answer("Выберите срок действия:", reply_markup=kb)


@router.message(AddUserMulti.days)
async def multi_add_days(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    text = (msg.text or "").strip()

    if text == "📅 Ручной ввод даты":
        await state.set_state(AddUserMulti.manual_date)
        await msg.answer("Введите дату истечения в формате YYYY-MM-DD:", reply_markup=multi_step_cancel_kb)
        return

    if text not in ("3 дня", "1 мес", "♾ Безлимит"):
        await msg.answer("Выберите один из вариантов на клавиатуре.")
        return

    await finish_multi_entry(msg, state, compute_new_user_expiry(text))


@router.message(AddUserMulti.manual_date)
async def multi_add_manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return
    if await handle_multi_cancel(msg, state):
        return

    try:
        datetime.strptime(msg.text.strip(), "%Y-%m-%d")
    except ValueError:
        await msg.answer("Неверный формат. Введите дату как YYYY-MM-DD.")
        return

    await finish_multi_entry(msg, state, msg.text.strip())


async def finish_multi_entry(msg: Message, state: FSMContext, expires_at):
    data = await state.get_data()
    batch = data.get("batch", [])

    batch.append({
        "username": data["current_username"],
        "password": data["current_password"],
        "expires_at": expires_at,
    })

    await state.update_data(batch=batch, current_username=None, current_password=None)
    await msg.answer(f"✅ {data['current_username']} добавлен в очередь.", reply_markup=ReplyKeyboardRemove())
    await show_multi_continue_choice(msg, state, batch)


@router.callback_query(F.data == "multiadd:more", AddUserMulti.continue_choice)
async def multi_add_more(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    n = len(data.get("batch", [])) + 1

    await state.set_state(AddUserMulti.username)
    await call.message.answer(f"👤 Клиент №{n} — введите username:")
    await call.answer()


@router.callback_query(F.data == "multiadd:done", AddUserMulti.continue_choice)
async def multi_add_done(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    batch = data.get("batch", [])

    if not batch:
        await state.clear()
        await call.message.answer("Список пуст, ничего не добавлено.", reply_markup=main_menu)
        await call.answer()
        return

    created_usernames = []
    for entry in batch:
        add_user({
            "username": entry["username"],
            "password": entry["password"],
            "created_at": utcnow_naive().strftime("%Y-%m-%d"),
            "expires_at": entry["expires_at"],
            "status": "active",
            "telegram_id": None,
            "notified_days": [],
            "pending_request": None,
        })
        created_usernames.append(entry["username"])

    # One resync + trusttunnel restart for the whole batch, not per-user.
    await run_sync()

    await state.set_state(AddUserMulti.done_actions)
    await state.update_data(created_usernames=created_usernames)

    names = ", ".join(created_usernames)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Завершить", callback_data="multiadd:finish")],
        [InlineKeyboardButton(text="📇 Получить карточки", callback_data="multiadd:cards")],
    ])

    await call.message.answer(
        f"✅ Добавлено {len(created_usernames)}, туннель пересобран:\n{names}",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data == "multiadd:finish", AddUserMulti.done_actions)
async def multi_add_finish(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Готово.", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data == "multiadd:cards", AddUserMulti.done_actions)
async def multi_add_get_cards(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    usernames = data.get("created_usernames", [])
    await state.clear()

    for username in usernames:
        user = get_user(username)
        if not user:
            continue
        link = generate_link(username, DOMAIN)
        await call.message.answer(
            format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)
        )

    await call.message.answer("Готово — карточки отправлены выше.", reply_markup=main_menu)
    await call.answer()
