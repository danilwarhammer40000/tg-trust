"""
Owns: LeaderLink.select.

Entry point (act_leader: callback) is a button built in
handlers/list_users.py's user_actions_menu — see bot/states.py's docstring
on why a different file owning the button that transitions into this
state is fine.
"""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only, run_sync
from bot.display import prepare_users_for_display, user_button_label
from bot.keyboards import main_menu
from bot.pagination import paginate, pagination_nav_row
from bot.states import LeaderLink
from core.dates import is_expired
from core.db import get_followers, get_user, link_user, list_users

router = Router()


def _candidate_label(u: dict, selected: set) -> str:
    username = u.get("username", "?")
    mark = "☑️" if username in selected else "⬜"
    label = user_button_label(u)
    if u.get("linked_to"):
        label += f" (сейчас ведомый {u['linked_to']})"
    return f"{mark} {label}"


def _eligible_candidates(leader_username: str) -> list:
    """
    Anyone except the leader itself and anyone who is themselves already a
    leader (has followers) — linking one leader under another would create
    a chain that core.db.update_user's one-level propagation doesn't
    support. Already-linked-to-someone-else users ARE included on purpose
    — picking one just re-parents it onto the new leader.
    """
    users = list_users() or []
    return [
        u for u in users
        if u.get("username") and u.get("username") != leader_username
        and not get_followers(u["username"])
    ]


def build_leader_link_kb(users: list, selected: set, page: int) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=_candidate_label(u, selected),
            callback_data=f"leadertoggle:{u['username']}"
        )]
        for u in page_users
    ]
    rows += pagination_nav_row(page, total_pages, "leaderpage")
    rows.append([
        InlineKeyboardButton(text=f"👑 Готово ({len(selected)})", callback_data="leadersel:done"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="leadersel:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


def leader_link_label(leader_username: str, total: int, page: int, total_pages: int) -> str:
    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    return (
        f"👑 Назначаем {leader_username} ведущим.\n"
        f"Выберите, кто становится ведомым ({total} доступно{suffix}) — "
        f"им сразу проставится текущая дата/статус {leader_username}:"
    )


@router.callback_query(F.data.startswith("act_leader:"))
async def action_leader_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    leader_username = call.data.split(":", 1)[1]
    leader = get_user(leader_username)
    if not leader:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    candidates = prepare_users_for_display(_eligible_candidates(leader_username))
    if not candidates:
        await call.message.answer(
            "Нет подходящих кандидатов (все остальные уже сами ведущие, "
            "или в базе больше никого нет)."
        )
        await call.answer()
        return

    await state.set_state(LeaderLink.select)
    await state.update_data(leader=leader_username, selected=[], page=0)

    kb, total_pages, page = build_leader_link_kb(candidates, set(), 0)
    await call.message.answer(leader_link_label(leader_username, len(candidates), page, total_pages), reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("leaderpage:"), LeaderLink.select)
async def leader_link_page(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    await state.update_data(page=page)

    data = await state.get_data()
    leader_username = data.get("leader")
    selected = set(data.get("selected", []))
    candidates = prepare_users_for_display(_eligible_candidates(leader_username))

    kb, total_pages, page = build_leader_link_kb(candidates, selected, page)
    try:
        await call.message.edit_text(
            leader_link_label(leader_username, len(candidates), page, total_pages), reply_markup=kb
        )
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("leadertoggle:"), LeaderLink.select)
async def toggle_leader_candidate(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    data = await state.get_data()
    leader_username = data.get("leader")
    selected = set(data.get("selected", []))
    page = data.get("page", 0)

    if username in selected:
        selected.discard(username)
    else:
        selected.add(username)

    await state.update_data(selected=list(selected))

    candidates = prepare_users_for_display(_eligible_candidates(leader_username))

    try:
        kb, total_pages, page = build_leader_link_kb(candidates, selected, page)
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass

    await call.answer()


@router.callback_query(F.data == "leadersel:cancel", LeaderLink.select)
async def cancel_leader_link(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data == "leadersel:done", LeaderLink.select)
async def confirm_leader_link(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    leader_username = data.get("leader")
    selected = data.get("selected", [])
    await state.clear()

    if not selected:
        await call.message.answer("Никого не выбрано, ничего не изменено.", reply_markup=main_menu)
        await call.answer()
        return

    leader = get_user(leader_username)
    if not leader:
        await call.message.answer("Ведущий не найден (был удалён?), отменено.", reply_markup=main_menu)
        await call.answer()
        return

    # If the leader was expired/inactive, linking a follower flips it from
    # "null-expiry, always in credentials.toml" to "expired/inactive, gets
    # excluded" -- that's the one case that actually changes trusttunnel
    # membership and needs a resync. If the leader is active and valid,
    # followers just swap a null expiry for a real (still valid) one --
    # already included either way, nothing to resync.
    was_expired_or_inactive = leader.get("status") != "active" or is_expired(leader.get("expires_at"))

    linked = [username for username in selected if link_user(username, leader_username)]

    if was_expired_or_inactive:
        await run_sync()

    names = ", ".join(linked)
    expiry_label = leader.get("expires_at") or "∞"
    await call.message.answer(
        f"👑 {leader_username} теперь ведущий для: {names}\n"
        f"Им проставлена текущая дата/статус {leader_username}: {expiry_label}.",
        reply_markup=main_menu
    )
    await call.answer()
