"""
Admin-only Xray controls, reached from the user card ("🛰 Xray").

Nothing here is reachable by clients, and Xray is opt-in: a user has no Xray
access until the admin switches it on here. The data lives in its own file
(data/xray.json), see core/xray.py.
"""
import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only
from core import xray
from core.db import get_user

router = Router()
log = logging.getLogger(__name__)


def _card(username: str):
    user = get_user(username) or {"username": username}
    state = xray.client_state(user)

    if not state["configured"]:
        return f"🛰 Xray для {username}\n\n{state['reason']}.", None

    if state["enabled"]:
        status = "✅ включён"
        if state["reason"]:
            status += f"\n⚠️ Сейчас не работает: {state['reason']}."
        rows = [
            [InlineKeyboardButton(text="🔗 Показать ссылку", callback_data=f"xr_link:{username}")],
            [InlineKeyboardButton(text="🚫 Выключить", callback_data=f"xr_off:{username}")],
            [InlineKeyboardButton(text="🔄 Выдать новую ссылку", callback_data=f"xr_new:{username}")],
        ]
    else:
        status = "⛔ выключен"
        rows = [[InlineKeyboardButton(text="✅ Включить", callback_data=f"xr_on:{username}")]]

    text = f"🛰 Xray для {username}\n\nСтатус: {status}\nСрок доступа берётся из подписки пользователя."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _sync_now() -> str:
    """Applies the change to the live Xray. Returns '' on success, else the error text."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, xray.sync_xray_from_users)
        return ""
    except Exception as e:
        log.exception("xray sync failed")
        return str(e)[:300]


async def _show_card(call: CallbackQuery, username: str, prefix: str = ""):
    text, kb = _card(username)
    await call.message.answer(prefix + text, reply_markup=kb)


@router.callback_query(F.data.startswith("act_xray:"))
async def xray_open(call: CallbackQuery):
    if not await admin_only(call):
        return
    username = call.data.split(":", 1)[1]
    await _show_card(call, username)
    await call.answer()


@router.callback_query(F.data.startswith("xr_on:"))
async def xray_on(call: CallbackQuery):
    if not await admin_only(call):
        return
    username = call.data.split(":", 1)[1]
    user = get_user(username)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    try:
        xray.enable_client(username)
    except Exception as e:
        await call.answer(f"Не получилось: {e}"[:190], show_alert=True)
        return

    error = await _sync_now()
    await _show_card(call, username, "⚠️ Включено, но применить не удалось: " + error + "\n\n" if error else "")

    block = xray.xray_block(get_user(username))
    if block:
        await call.message.answer(block, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("xr_off:"))
async def xray_off(call: CallbackQuery):
    if not await admin_only(call):
        return
    username = call.data.split(":", 1)[1]

    try:
        xray.disable_client(username)
    except Exception as e:
        await call.answer(f"Не получилось: {e}"[:190], show_alert=True)
        return

    error = await _sync_now()
    await _show_card(call, username, "⚠️ Выключено, но применить не удалось: " + error + "\n\n" if error else "")
    await call.answer()


@router.callback_query(F.data.startswith("xr_link:"))
async def xray_link(call: CallbackQuery):
    if not await admin_only(call):
        return
    username = call.data.split(":", 1)[1]

    block = xray.xray_block(get_user(username))
    if block:
        await call.message.answer(block, parse_mode="HTML")
    else:
        await call.message.answer("Xray для этого пользователя выключен.")
    await call.answer()


@router.callback_query(F.data.startswith("xr_new:"))
async def xray_new_confirm(call: CallbackQuery):
    if not await admin_only(call):
        return
    username = call.data.split(":", 1)[1]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Да, выдать новую", callback_data=f"xr_new_yes:{username}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"act_xray:{username}")],
    ])
    await call.message.answer(
        f"Выдать {username} новую ссылку Xray? Старая сразу перестанет работать.", reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("xr_new_yes:"))
async def xray_new_apply(call: CallbackQuery):
    if not await admin_only(call):
        return
    username = call.data.split(":", 1)[1]

    try:
        xray.rotate_client(username)
    except Exception as e:
        await call.answer(f"Не получилось: {e}"[:190], show_alert=True)
        return

    error = await _sync_now()
    if error:
        await call.message.answer("⚠️ Новая ссылка создана, но применить не удалось: " + error)

    block = xray.xray_block(get_user(username))
    if block:
        await call.message.answer(block, parse_mode="HTML")
    await call.answer()
