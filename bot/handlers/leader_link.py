"""
Owns: LeaderLink.select.

All the leader/follower group-management UI lives here:
- act_leader:   (list_users.py button) -> manage a leader's group: general
                candidate list, existing followers pre-checked, checking/
                unchecking both adds and removes -- one screen for both.
- act_ungroup:  (list_users.py button, only shown for existing leaders) ->
                same mechanics, but scoped to ONLY the current group (not
                the general list), plus a one-tap "unlink everyone" button.
- act_follow:   (list_users.py button, only shown for independent users,
                only when at least one leader exists) -> the reverse
                direction: pick which EXISTING leader this user should
                follow, from a list scoped to leaders only. No FSM needed
                here -- it's a single pick, so the follower's username is
                just threaded through each button's own callback_data.

See bot/states.py's docstring on why a different file (list_users.py)
owning the buttons that transition into LeaderLink.select is fine.
"""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only, run_sync
from bot.display import sorted_users_for_display, user_button_label
from bot.keyboards import main_menu
from bot.pagination import paginate, pagination_nav_row
from bot.states import LeaderLink
from core.dates import is_expired
from core.db import get_followers, get_leaders, get_user, link_user, list_users, unlink_user

router = Router()


# ---------------- SHARED: candidate list + keyboard building ----------------
#
# NOTE: uses sorted_users_for_display(), NOT prepare_users_for_display() --
# the hide_unlimited/hide_expired/hide_followers toggles are for general
# browsing lists and must not hide anyone from a screen whose whole job is
# to show/manage links. See bot/display.py's prepare_users_for_display()
# docstring for the same note from the other side.

def _candidate_label(u: dict, selected: set) -> str:
    username = u.get("username", "?")
    mark = "☑️" if username in selected else "⬜"
    label = user_button_label(u)
    linked_to = u.get("linked_to")
    if linked_to and username not in selected:
        # Only worth calling out when NOT selected here -- if it's checked
        # in THIS screen, the "current group" is obvious from context.
        label += f" (сейчас ведомый {linked_to})"
    return f"{mark} {label}"


def _eligible_candidates(leader_username: str) -> list:
    """
    Anyone except the leader itself and anyone who is themselves already a
    leader (has followers) — linking one leader under another would create
    a chain that core.db.update_user's one-level propagation doesn't
    support. Already-linked-to-someone-else users ARE included on purpose
    — checking one just re-parents it onto this leader. Current followers
    of THIS leader are included too (they have no followers of their own),
    which is what lets them show up pre-checked.
    """
    users = list_users() or []
    return [
        u for u in users
        if u.get("username") and u.get("username") != leader_username
        and not get_followers(u["username"])
    ]


def build_leader_link_kb(leader_username: str, users: list, selected: set, page: int, mode: str) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = []
    if mode == "ungroup" and users:
        rows.append([InlineKeyboardButton(text="🔨 Отвязать всех сразу", callback_data="leadersel:unlink_all")])

    rows += [
        [InlineKeyboardButton(
            text=_candidate_label(u, selected),
            callback_data=f"leadertoggle:{u['username']}"
        )]
        for u in page_users
    ]
    rows += pagination_nav_row(page, total_pages, "leaderpage")
    rows.append([
        InlineKeyboardButton(text=f"✅ Готово ({len(selected)})", callback_data="leadersel:done"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="leadersel:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


def leader_link_label(leader_username: str, total: int, page: int, total_pages: int, mode: str) -> str:
    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""

    if mode == "ungroup":
        return (
            f"💔 Группа {leader_username} ({total} чел{suffix}).\n"
            f"Снимите ☑️ у тех, кого нужно отвязать, затем «Готово» — "
            f"или «Отвязать всех сразу» одним тапом."
        )

    return (
        f"👑 Ведомые {leader_username} ({total} доступно{suffix}).\n"
        f"☑️ отмечены уже ведомые. Отметьте новых, чтобы добавить, снимите "
        f"галочку, чтобы отвязать — им сразу проставится текущая дата/статус {leader_username}."
    )


async def _render(call: CallbackQuery, state: FSMContext, edit: bool):
    data = await state.get_data()
    leader_username = data.get("leader")
    mode = data.get("mode", "manage")
    selected = set(data.get("selected", []))
    page = data.get("page", 0)

    if mode == "ungroup":
        candidates = sorted_users_for_display(get_followers(leader_username))
    else:
        candidates = sorted_users_for_display(_eligible_candidates(leader_username))

    kb, total_pages, page = build_leader_link_kb(leader_username, candidates, selected, page, mode)
    label = leader_link_label(leader_username, len(candidates), page, total_pages, mode)

    if edit:
        try:
            await call.message.edit_text(label, reply_markup=kb)
        except Exception:
            pass
    else:
        await call.message.answer(label, reply_markup=kb)


async def _start(call: CallbackQuery, state: FSMContext, leader_username: str, mode: str):
    leader = get_user(leader_username)
    if not leader:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    if mode == "ungroup":
        candidates = sorted_users_for_display(get_followers(leader_username))
        if not candidates:
            await call.answer("У этого пользователя пока нет ведомых.", show_alert=True)
            return
        preselected = {u["username"] for u in candidates}
    else:
        candidates = sorted_users_for_display(_eligible_candidates(leader_username))
        if not candidates:
            await call.message.answer(
                "Нет подходящих кандидатов (все остальные уже сами ведущие, "
                "или в базе больше никого нет)."
            )
            await call.answer()
            return
        preselected = {u["username"] for u in get_followers(leader_username)}

    await state.set_state(LeaderLink.select)
    await state.update_data(
        leader=leader_username,
        mode=mode,
        selected=list(preselected),
        original_followers=list(preselected),
        page=0,
    )

    await _render(call, state, edit=False)
    await call.answer()


@router.callback_query(F.data.startswith("act_leader:"))
async def action_leader_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return
    await _start(call, state, call.data.split(":", 1)[1], mode="manage")


@router.callback_query(F.data.startswith("act_ungroup:"))
async def action_ungroup_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return
    await _start(call, state, call.data.split(":", 1)[1], mode="ungroup")


@router.callback_query(F.data.startswith("leaderpage:"), LeaderLink.select)
async def leader_link_page(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    await state.update_data(page=page)
    await _render(call, state, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("leadertoggle:"), LeaderLink.select)
async def toggle_leader_candidate(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    data = await state.get_data()
    selected = set(data.get("selected", []))

    if username in selected:
        selected.discard(username)
    else:
        selected.add(username)

    await state.update_data(selected=list(selected))
    await _render(call, state, edit=True)
    await call.answer()


@router.callback_query(F.data == "leadersel:cancel", LeaderLink.select)
async def cancel_leader_link(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data == "leadersel:unlink_all", LeaderLink.select)
async def unlink_all_now(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    leader_username = data.get("leader")
    await state.clear()

    followers = get_followers(leader_username)
    for u in followers:
        unlink_user(u["username"])

    # Unlinking never changes credentials.toml membership (each follower
    # keeps exactly the status/expiry it already had, just as an
    # independent record now) -- no run_sync() needed here.

    names = ", ".join(u["username"] for u in followers) or "(никого)"
    await call.message.answer(f"💔 Группа {leader_username} расформирована: {names}", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data == "leadersel:done", LeaderLink.select)
async def confirm_leader_link(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    leader_username = data.get("leader")
    selected = set(data.get("selected", []))
    original_followers = set(data.get("original_followers", []))
    await state.clear()

    leader = get_user(leader_username)
    if not leader:
        await call.message.answer("Ведущий не найден (был удалён?), отменено.", reply_markup=main_menu)
        await call.answer()
        return

    to_link = selected - original_followers
    to_unlink = original_followers - selected

    if not to_link and not to_unlink:
        await call.message.answer("Без изменений.", reply_markup=main_menu)
        await call.answer()
        return

    # If the leader was expired/inactive, linking a NEW follower flips it
    # from "null-expiry, always in credentials.toml" to "expired/inactive,
    # gets excluded" -- that's the one case that actually changes
    # trusttunnel membership and needs a resync. Unlinking never needs one
    # (see unlink_all_now's comment above) — only check the link half.
    was_expired_or_inactive = leader.get("status") != "active" or is_expired(leader.get("expires_at"))

    linked = [username for username in to_link if link_user(username, leader_username)]
    unlinked = [username for username in to_unlink if unlink_user(username)]

    if linked and was_expired_or_inactive:
        await run_sync()

    lines = [f"👑 Группа {leader_username} обновлена."]
    if linked:
        lines.append(f"➕ Добавлены: {', '.join(linked)}")
    if unlinked:
        lines.append(f"➖ Отвязаны: {', '.join(unlinked)}")

    await call.message.answer("\n".join(lines), reply_markup=main_menu)
    await call.answer()


# ---------------- REVERSE DIRECTION: "🔗 Сделать ведомым" ----------------
#
# No FSM needed -- it's a single pick from a list scoped to existing
# leaders, so the follower's own username is threaded directly through
# each button's callback_data instead of being stored in state.

def build_follow_leader_kb(follower_username: str, leaders: list, page: int) -> tuple:
    page_leaders, total_pages, page = paginate(leaders, page)

    rows = [
        [InlineKeyboardButton(
            text=user_button_label(l),
            callback_data=f"followto:{follower_username}:{l['username']}"
        )]
        for l in page_leaders
    ]
    rows += pagination_nav_row(page, total_pages, f"followpage:{follower_username}")
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


@router.callback_query(F.data.startswith("act_follow:"))
async def action_follow_start(call: CallbackQuery):
    if not await admin_only(call):
        return

    follower_username = call.data.split(":", 1)[1]
    leaders = sorted_users_for_display(get_leaders())

    if not leaders:
        await call.answer("Пока нет ни одного ведущего с ведомыми.", show_alert=True)
        return

    kb, total_pages, page = build_follow_leader_kb(follower_username, leaders, 0)
    label = f"К кому привязать {follower_username} как ведомого ({len(leaders)} ведущих)?"
    await call.message.answer(label, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("followpage:"))
async def follow_leader_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, follower_username, page_str = call.data.split(":", 2)
    page = int(page_str)
    leaders = sorted_users_for_display(get_leaders())

    kb, total_pages, page = build_follow_leader_kb(follower_username, leaders, page)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("followto:"))
async def follow_leader_confirm(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, follower_username, leader_username = call.data.split(":", 2)

    leader = get_user(leader_username)
    if not leader:
        await call.answer("Ведущий не найден", show_alert=True)
        return

    was_expired_or_inactive = leader.get("status") != "active" or is_expired(leader.get("expires_at"))

    if not link_user(follower_username, leader_username):
        await call.answer("Не удалось привязать.", show_alert=True)
        return

    if was_expired_or_inactive:
        await run_sync()

    expiry_label = leader.get("expires_at") or "∞"
    await call.message.answer(
        f"🔗 {follower_username} теперь ведомый {leader_username} "
        f"(текущая дата/статус: {expiry_label}).",
        reply_markup=main_menu
    )
    await call.answer()
