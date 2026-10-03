"""
Owns: MassDelete.select, MassDelete.confirm.
"""
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only, run_sync
from bot.display import prepare_users_for_display, user_button_label
from bot.keyboards import main_menu
from bot.pagination import paginate, pagination_nav_row
from bot.states import MassDelete
from core.dates import is_expired
from core.db import delete_user, get_user, list_users

router = Router()
log = logging.getLogger(__name__)


def build_delete_kb(users: list, selected: set, page: int) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=("☑️ " if u.get("username") in selected else "⬜ ") + user_button_label(u),
            callback_data=f"deltoggle:{u['username']}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "delpage")
    rows.append([
        InlineKeyboardButton(text=f"🗑 Готово ({len(selected)})", callback_data="delsel:done"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="delsel:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


def mass_delete_label(total: int, page: int, total_pages: int) -> str:
    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    return f"Выберите пользователей для удаления ({total} всего{suffix}), тап переключает ☑️/⬜:"


@router.message(F.text == "🗑 Удаление пользователей")
async def mass_delete_start(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("username")])
    if not users:
        await msg.answer("Список пользователей пуст.")
        return

    await state.set_state(MassDelete.select)
    await state.update_data(selected=[], page=0)

    kb, total_pages, page = build_delete_kb(users, set(), 0)
    await msg.answer(mass_delete_label(len(users), page, total_pages), reply_markup=kb)


@router.callback_query(F.data.startswith("delpage:"), MassDelete.select)
async def mass_delete_page(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    await state.update_data(page=page)

    data = await state.get_data()
    selected = set(data.get("selected", []))
    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("username")])

    kb, total_pages, page = build_delete_kb(users, selected, page)
    try:
        await call.message.edit_text(mass_delete_label(len(users), page, total_pages), reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("deltoggle:"), MassDelete.select)
async def toggle_delete_selection(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    try:
        username = call.data.split(":", 1)[1]

        data = await state.get_data()
        selected = set(data.get("selected", []))
        page = data.get("page", 0)

        if username in selected:
            selected.discard(username)
        else:
            selected.add(username)

        await state.update_data(selected=list(selected))

        users = prepare_users_for_display([u for u in (list_users() or []) if u.get("username")])

        try:
            kb, total_pages, page = build_delete_kb(users, selected, page)
            await call.message.edit_reply_markup(reply_markup=kb)
        except Exception as e:
            log.debug("edit_reply_markup failed (likely unchanged markup): %s", e)

        await call.answer()
    except Exception as e:
        log.exception("mass delete toggle failed")
        await call.answer(f"Ошибка: {e}", show_alert=True)


@router.callback_query(F.data == "delsel:cancel", MassDelete.select)
async def cancel_mass_delete(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data == "delsel:done", MassDelete.select)
async def mass_delete_ask_confirmation(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    try:
        data = await state.get_data()
        selected = data.get("selected", [])

        if not selected:
            await call.answer("Выберите хотя бы одного пользователя", show_alert=True)
            return

        await state.set_state(MassDelete.confirm)

        names = "\n".join(f"• {u}" for u in selected)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 Да, удалить ({len(selected)})", callback_data="delconfirm:yes")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="delconfirm:no")],
        ])

        await call.message.answer(
            f"⚠️ Удалить {len(selected)} пользователей? Это необратимо:\n\n{names}",
            reply_markup=kb
        )
        await call.answer()
    except Exception as e:
        log.exception("mass delete confirmation step failed")
        await call.answer(f"Ошибка: {e}", show_alert=True)


@router.callback_query(F.data.startswith("delconfirm:"), MassDelete.confirm)
async def mass_delete_execute(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":")[1]

    data = await state.get_data()
    selected = data.get("selected", [])
    await state.clear()

    if action == "no":
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    deleted = 0
    needs_resync = False

    for username in selected:
        user = get_user(username)

        # Was this user actually present in credentials.toml before deletion?
        # rebuild_credentials_from_db() only includes active, non-expired users —
        # deleting someone who was already inactive/expired changes nothing there.
        if user and user.get("status") == "active" and not is_expired(user.get("expires_at")):
            needs_resync = True

        try:
            delete_user(username)
            deleted += 1
        except Exception:
            log.exception("mass delete failed for %s", username)

    # One resync + trusttunnel restart for the whole batch, not per-user — and
    # only if it's actually needed, to avoid disrupting every connected client
    # for a no-op (e.g. cleaning up a batch of already-expired accounts).
    if needs_resync:
        await run_sync()

    await call.message.answer(f"✅ Удалено пользователей: {deleted}", reply_markup=main_menu)
    await call.answer()
