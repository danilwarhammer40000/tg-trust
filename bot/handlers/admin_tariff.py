"""
Owns: SetTariff.waiting (defined here, it is only used by this module).

Admin-only manual tariff correction for one client: pin a fixed monthly price,
or go back to the automatic one. Reached from the user card ("💰 Тариф").
The logic lives in core/tariff.py; this file is only the Telegram UI.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only
from bot.keyboards import main_menu
from core import tariff
from core.db import get_user

router = Router()


class SetTariff(StatesGroup):
    waiting = State()


def _card(username: str):
    info = tariff.describe(username)

    if info["custom"]:
        text = (
            f"💰 Тариф {username}\n\n"
            f"Сейчас: {info['current']} ₽/мес (задан вручную)\n"
            f"Автоматически было бы: {info['auto']} ₽/мес"
        )
    else:
        text = (
            f"💰 Тариф {username}\n\n"
            f"Сейчас: {info['current']} ₽/мес (автоматически)"
        )

    rows = [[InlineKeyboardButton(text="✍️ Задать цену", callback_data=f"tariff_set:{username}")]]
    if info["custom"]:
        rows.append([InlineKeyboardButton(text="♻️ Вернуть автоматический", callback_data=f"tariff_reset:{username}")])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("act_tariff:"))
async def tariff_open(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    if user.get("linked_to"):
        await call.answer(f"Ведомый: тариф определяет ведущий ({user['linked_to']})", show_alert=True)
        return

    text, kb = _card(username)
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("tariff_set:"))
async def tariff_set_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    await state.set_state(SetTariff.waiting)
    await state.update_data(tariff_username=username)

    await call.message.answer(
        f"Введите цену для {username} в ₽/мес, целым числом "
        f"({tariff.MIN_RATE}–{tariff.MAX_RATE}).\n"
        "Новая цена начнёт действовать со следующего продления. Для отмены напишите «отмена»."
    )
    await call.answer()


@router.message(SetTariff.waiting)
async def tariff_set_apply(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    text = (msg.text or "").strip()
    if text.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await msg.answer("Отменено.", reply_markup=main_menu)
        return

    rate = tariff.parse_rate(text)
    if rate is None:
        await msg.answer(f"Нужно целое число от {tariff.MIN_RATE} до {tariff.MAX_RATE}. Попробуйте ещё раз или напишите «отмена».")
        return

    data = await state.get_data()
    username = data.get("tariff_username")
    await state.clear()

    try:
        tariff.set_custom_rate(username, rate)
    except ValueError as e:
        await msg.answer(f"Не получилось: {e}", reply_markup=main_menu)
        return

    await msg.answer(
        f"✅ {username}: тариф {rate} ₽/мес (вручную). Срок доступа не менялся.",
        reply_markup=main_menu,
    )


@router.callback_query(F.data.startswith("tariff_reset:"))
async def tariff_reset(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    try:
        auto = tariff.clear_custom_rate(username)
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
        return

    await call.message.answer(f"♻️ {username}: снова автоматический тариф, {auto} ₽/мес.", reply_markup=main_menu)
    await call.answer()
