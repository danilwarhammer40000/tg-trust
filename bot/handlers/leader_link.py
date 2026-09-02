"""
Owns: LeaderLink.select.

Two entry points from handlers/list_users.py's user_actions_menu share this
one state/router:
  - action_leader_start ("👑 Назначить ведущим" / "👑 Управление ведомыми")
    → mode="manage": multi-select which existing users follow this leader.
  - action_ungroup_start ("💔 Разгруппировать") → mode="ungroup": pick which
    of this leader's current followers to detach.
action_unlink (unlinking a single follower from ITS card, not the leader's)
and action_follow_start ("🔗 Сделать ведомым" from a non-leader's own card,
letting them pick which existing leader to join) are one-shot / their own
small stateless flows, not FSM.

"➕ Выпустить нового ведомого" (mode="manage" only) is deliberately NOT
gated on there being any existing candidates to link — it creates a brand
new account from scratch (see bot/follower_issuance.py), so "nobody else
to link" is not a reason to hide it.
"""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only, run_sync
from bot.display import sorted_users_for_display, user_button_label
from bot.follower_issuance import issue_follower, leader_is_active
from bot.keyboards import main_menu
from bot.states import LeaderLink
from core.db import get_followers, get_unlinked_users, get_user, link_user, list_users, unlink_user

router = Router()


def _eligible_candidates(leader_username: str) -> list:
    """
    Anyone who could become a follower of this leader: not the leader
    itself, not already following someone else, and not itself a leader
    of other followers (no nested groups — keeps the sync logic in
    core.db.update_user, which only follows one hop, correct).
    """
    all_users = list_users()
    leader_usernames = {u.get("linked_to") for u in all_users if u.get("linked_to")}

    return [
        u for u in all_users
        if u.get("username") != leader_username
        and not u.get("linked_to")
        and u.get("username") not in leader_usernames
    ]


def _build_select_kb(leader_username: str, candidates: list, selected: set, mode: str) -> InlineKeyboardMarkup:
    rows = []

    if mode == "manage":
        rows.append([InlineKeyboardButton(text="➕ Выпустить нового ведомого", callback_data="leadersel:issue")])

    for u in candidates:
        username = u.get("username")
        mark = "✅ " if username in selected else "⬜ "
        rows.append([InlineKeyboardButton(
            text=f"{mark}{user_button_label(u)}",
            callback_data=f"leadersel:toggle:{username}"
        )])

    rows.append([
        InlineKeyboardButton(text="✅ Готово", callback_data="leadersel:done"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="leadersel:cancel"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    leader_username = data.get("leader")
    mode = data.get("mode", "manage")
    selected = set(data.get("selected", []))

    if mode == "manage":
        candidates = sorted_users_for_display(_eligible_candidates(leader_username))
        if candidates:
            header = f"👑 Управление ведомыми для {leader_username}\n\nОтметьте, кто присоединяется, либо выпустите нового:"
        else:
            header = (
                f"👑 Управление ведомыми для {leader_username}\n\n"
                f"Нет свободных существующих пользователей для привязки — "
                f"но можно выпустить нового:"
            )
    else:
        candidates = sorted_users_for_display(get_followers(leader_username))
        header = f"💔 Разгруппировать — ведомые {leader_username}\n\nОтметьте, кого отвязать:"

    kb = _build_select_kb(leader_username, candidates, selected, mode)

    try:
        await call.message.edit_text(header, reply_markup=kb)
    except Exception:
        await call.message.answer(header, reply_markup=kb)


async def _start(call: CallbackQuery, state: FSMContext, mode: str):
    if not await admin_only(call):
        return

    leader_username = call.data.split(":", 1)[1]

    if mode == "ungroup" and not get_followers(leader_username):
        await call.answer("У этого пользователя нет ведомых.", show_alert=True)
        return

    preselected = {u["username"] for u in get_followers(leader_username)} if mode == "manage" else set()

    await state.set_state(LeaderLink.select)
    await state.update_data(leader=leader_username, mode=mode, selected=list(preselected))

    await _render(call, state)
    await call.answer()


@router.callback_query(F.data.startswith("act_leader:"))
async def action_leader_start(call: CallbackQuery, state: FSMContext):
    await _start(call, state, mode="manage")


@router.callback_query(F.data.startswith("act_ungroup:"))
async def action_ungroup_start(call: CallbackQuery, state: FSMContext):
    await _start(call, state, mode="ungroup")


@router.callback_query(F.data == "leadersel:issue", LeaderLink.select)
async def leadersel_issue(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    leader_username = data.get("leader")

    leader = get_user(leader_username)
    if not leader:
        await call.answer("Ведущий не найден.", show_alert=True)
        return

    new_username, card = issue_follower(leader_username)

    if leader_is_active(leader):
        await run_sync()

    await state.clear()

    await call.message.answer(f"✅ Выпущен новый ведомый: {new_username}")
    await call.message.answer(card, reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data.startswith("leadersel:toggle:"), LeaderLink.select)
async def leadersel_toggle(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 2)[2]

    data = await state.get_data()
    selected = set(data.get("selected", []))

    if username in selected:
        selected.discard(username)
    else:
        selected.add(username)

    await state.update_data(selected=list(selected))
    await _render(call, state)
    await call.answer()


@router.callback_query(F.data == "leadersel:cancel", LeaderLink.select)
async def leadersel_cancel(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data == "leadersel:done", LeaderLink.select)
async def leadersel_done(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    leader_username = data.get("leader")
    mode = data.get("mode", "manage")
    selected = set(data.get("selected", []))
    await state.clear()

    if mode == "manage":
        current = {u["username"] for u in get_followers(leader_username)}
        to_link = selected - current
        to_unlink = current - selected

        for username in to_link:
            link_user(username, leader_username)
        for username in to_unlink:
            unlink_user(username)

        await call.message.answer(
            f"✅ Готово. Ведомых у {leader_username}: {len(selected)}.",
            reply_markup=main_menu
        )
    else:
        for username in selected:
            unlink_user(username)

        await call.message.answer(
            f"💔 Отвязано: {len(selected)}.",
            reply_markup=main_menu
        )

    await call.answer()


# ---------------- FOLLOW: from a non-leader's own card ----------------

@router.callback_query(F.data.startswith("act_follow:"))
async def action_follow_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    from core.db import get_leaders
    leaders = sorted_users_for_display(get_leaders())
    leaders = [u for u in leaders if u.get("username") != username]

    pool = leaders if leaders else sorted_users_for_display(
        [u for u in get_unlinked_users() if u.get("username") != username]
    )
    note = "Выберите ведущего:" if leaders else "Пока нет ни одной группы — выберите, к кому присоединить (станет ведущим):"

    if not pool:
        await call.answer("Нет доступных пользователей, чтобы сделать ведомым.", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(text=user_button_label(u), callback_data=f"followsel:{username}:{u['username']}")]
        for u in pool
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="followsel:cancel")])

    await call.message.answer(note, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("followsel:") & ~F.data.contains("cancel"))
async def action_follow_pick(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, follower_username, leader_username = call.data.split(":")

    ok = link_user(follower_username, leader_username)
    if not ok:
        await call.answer("Не удалось привязать.", show_alert=True)
        return

    await call.message.answer(
        f"🔗 {follower_username} теперь ведомый у {leader_username}.",
        reply_markup=main_menu
    )
    await call.answer()


@router.callback_query(F.data == "followsel:cancel")
async def action_follow_cancel(call: CallbackQuery):
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()
