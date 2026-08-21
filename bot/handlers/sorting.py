from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only
from bot.display import (
    is_grouping_enabled,
    is_hide_expired_enabled,
    is_hide_followers_enabled,
    is_hide_unlimited_enabled,
    is_sort_soonest_first_enabled,
    toggle_grouping,
    toggle_hide_expired,
    toggle_hide_followers,
    toggle_hide_unlimited,
    toggle_sort_soonest_first,
)

router = Router()


def sorting_menu_kb() -> InlineKeyboardMarkup:
    grouping_on = is_grouping_enabled()
    hide_unlimited_on = is_hide_unlimited_enabled()
    hide_expired_on = is_hide_expired_enabled()
    hide_followers_on = is_hide_followers_enabled()
    soonest_first_on = is_sort_soonest_first_enabled()

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if grouping_on else '⬜'} Группировка по подписке (сверху подписанные)",
            callback_data="sort:toggle_group"
        )],
        [InlineKeyboardButton(
            text=f"{'⏳ Сначала истекающие' if soonest_first_on else '♾ Сначала безлимитные/дальние'}",
            callback_data="sort:toggle_direction"
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if hide_unlimited_on else '⬜'} Скрывать безлимитных",
            callback_data="sort:toggle_hide"
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if hide_expired_on else '⬜'} Скрывать просроченных",
            callback_data="sort:toggle_hide_expired"
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if hide_followers_on else '⬜'} Скрывать ведомых",
            callback_data="sort:toggle_hide_followers"
        )],
    ])


@router.message(F.text == "⚙️ Сортировка БД")
async def sorting_menu(msg: Message):
    if not await admin_only(msg):
        return

    await msg.answer("⚙️ Настройки отображения списков (тап переключает):", reply_markup=sorting_menu_kb())


@router.callback_query(F.data == "sort:toggle_group")
async def sorting_toggle_group(call: CallbackQuery):
    if not await admin_only(call):
        return

    toggle_grouping()
    try:
        await call.message.edit_reply_markup(reply_markup=sorting_menu_kb())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "sort:toggle_direction")
async def sorting_toggle_direction(call: CallbackQuery):
    if not await admin_only(call):
        return

    now_soonest_first = toggle_sort_soonest_first()
    try:
        await call.message.edit_reply_markup(reply_markup=sorting_menu_kb())
    except Exception:
        pass
    await call.answer(
        "Теперь сначала истекающие" if now_soonest_first else "Теперь сначала безлимитные/дальние"
    )


@router.callback_query(F.data == "sort:toggle_hide")
async def sorting_toggle_hide(call: CallbackQuery):
    if not await admin_only(call):
        return

    toggle_hide_unlimited()
    try:
        await call.message.edit_reply_markup(reply_markup=sorting_menu_kb())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "sort:toggle_hide_expired")
async def sorting_toggle_hide_expired(call: CallbackQuery):
    if not await admin_only(call):
        return

    toggle_hide_expired()
    try:
        await call.message.edit_reply_markup(reply_markup=sorting_menu_kb())
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "sort:toggle_hide_followers")
async def sorting_toggle_hide_followers(call: CallbackQuery):
    if not await admin_only(call):
        return

    toggle_hide_followers()
    try:
        await call.message.edit_reply_markup(reply_markup=sorting_menu_kb())
    except Exception:
        pass
    await call.answer()
