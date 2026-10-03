"""
Owns: LeaderLink.select.

All the leader/follower group-management UI lives here:
- act_leader:   (list_users.py button) -> a small 2-button choice first:
                "➕ Выпустить нового ведомого" (issue a brand-new "-2"/"-3"
                sub-account, no state needed, done in one tap) or
                "📋 Выбрать из списка существующих" (-> leaderpick:, which
                is what actually opens the checkbox multi-select screen:
                general candidate list, this leader's own current
                followers pre-checked, checking/unchecking both adds and
                removes -- one screen for both).
- act_ungroup:  (list_users.py button, only shown for existing leaders) ->
                same checkbox mechanics, but scoped to ONLY the current
                group (not the general list), plus a one-tap "unlink
                everyone" button.
- act_follow:   (list_users.py button, only shown for independent users)
                -> the reverse direction: pick who this user should
                follow. Starts on a list scoped to existing leaders, with
                a "📋 Показать всех свободных" button to switch to a
                second list of genuinely unattached users (for starting a
                brand new group instead of joining an existing one). No
                FSM needed -- it's a single pick, so the follower's
                username (and current list mode) is threaded directly
                through each button's own callback_data.

See bot/states.py's docstring on why a different file (list_users.py)
owning the buttons that transition into LeaderLink.select is fine.

ORDERING RULE for issuing a brand-new sub-account (issue_new_follower
below): create the DB record, THEN run_sync() (if the leader is active),
THEN build the connection card. Building the card before the resync would
hand back a broken link — see follower_issuance.py's module docstring.
"""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only, run_sync
from bot.display import sorted_users_for_display, user_button_label
from bot.keyboards import main_menu
from bot.pagination import paginate, pagination_nav_row
from bot.states import LeaderLink
from follower_issuance import build_connection_card, issue_follower, leader_is_active
from core.dates import is_expired
from core.db import get_followers, get_leaders, get_unlinked_users, get_user, link_user, list_users, unlink_user

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
    return f"{mark} {user_button_label(u)}"


def _eligible_candidates(leader_username: str) -> list:
    """
    Only genuinely free users (not the leader itself, not themselves a
    leader elsewhere, not already someone ELSE's follower) — PLUS this
    leader's own current followers, so they still show up pre-checked and
    can be unchecked to remove.

    Deliberately excludes followers of OTHER leaders now (previously they
    were included, letting the general list double as a "re-parent"
    picker — but that cluttered the general "who can I add" list with
    people already spoken for elsewhere. Re-parenting is still possible,
    just not from this general list — unlink them from their current
    leader first via that leader's "💔 Разгруппировать", then they show up
    here as free.
    """
    current_followers = {f["username"] for f in get_followers(leader_username)}
    users = list_users() or []
    return [
        u for u in users
        if u.get("username") and u.get("username") != leader_username
        and not get_followers(u["username"])  # not themselves a leader
        and (u["username"] in current_followers or not u.get("linked_to"))  # free, or already in THIS group
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

    if total == 0:
        return f"👑 Ведомые {leader_username}: подходящих существующих пользователей для привязки нет."

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


# ---------------- ENTRY POINT: choose issue-new vs pick-existing ----------------

@router.callback_query(F.data.startswith("act_leader:"))
async def action_leader_start(call: CallbackQuery):
    if not await admin_only(call):
        return

    leader_username = call.data.split(":", 1)[1]
    leader = get_user(leader_username)
    if not leader:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выпустить нового ведомого", callback_data=f"issuefollow:{leader_username}")],
        [InlineKeyboardButton(text="📋 Выбрать из списка существующих", callback_data=f"leaderpick:{leader_username}")],
    ])

    await call.message.answer(f"👑 {leader_username} — что сделать?", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("leaderpick:"))
async def action_leader_pick_start(call: CallbackQuery, state: FSMContext):
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


# ---------------- ISSUE A BRAND-NEW SUB-ACCOUNT ("-2", "-3", ...) ----------------
#
# No FSM state involved -- reachable straight from the act_leader: entry
# menu above, in one tap.

@router.callback_query(F.data.startswith("issuefollow:"))
async def issue_new_follower(call: CallbackQuery):
    if not await admin_only(call):
        return

    leader_username = call.data.split(":", 1)[1]
    leader = get_user(leader_username)
    if not leader:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    was_active = leader_is_active(leader)

    # STEP 1: DB only, no card/link generation yet.
    new_username = issue_follower(leader_username)
    if not new_username:
        await call.answer("Не удалось выпустить нового ведомого.", show_alert=True)
        return

    # STEP 2: resync BEFORE building the card — see follower_issuance.py's
    # module docstring for why this order matters.
    if was_active:
        await run_sync()

    # STEP 3: now it's safe to generate the actual connection link.
    card = build_connection_card(new_username)

    await call.message.answer(f"✅ Выпущен новый ведомый: {new_username}")
    await call.message.answer(card)
    await call.message.answer("Готово.", reply_markup=main_menu)
    await call.answer()


# ---------------- REVERSE DIRECTION: "🔗 Сделать ведомым" ----------------
#
# No FSM needed -- it's a single pick, so the follower's own username (and
# now the current list mode) is threaded directly through each button's
# callback_data instead of being stored in state.
#
# Two modes:
# - "leaders": the common case -- attach to an EXISTING group.
# - "free": fallback for when the leader you want isn't a leader yet --
#   scoped to users who are neither a leader nor a follower of anyone.
#   Picking one there makes them a first-time leader (link_user() doesn't
#   care whether the target already had followers or not).

def build_follow_picker_kb(follower_username: str, users: list, page: int, mode: str) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = []
    if mode == "leaders":
        rows.append([InlineKeyboardButton(text="📋 Показать всех свободных", callback_data=f"followall:{follower_username}")])
    else:
        rows.append([InlineKeyboardButton(text="👑 Показать только ведущих", callback_data=f"act_follow:{follower_username}")])

    rows += [
        [InlineKeyboardButton(
            text=user_button_label(u),
            callback_data=f"followto:{follower_username}:{u['username']}"
        )]
        for u in page_users
    ]
    rows += pagination_nav_row(page, total_pages, f"followpage:{follower_username}:{mode}")
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


def follow_picker_label(follower_username: str, total: int, page: int, total_pages: int, mode: str) -> str:
    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""

    if mode == "free":
        return (
            f"К кому привязать {follower_username}? Свободные пользователи "
            f"({total}{suffix}) — выбор станет новым ведущим:"
        )
    return f"К кому привязать {follower_username} как ведомого — существующие ведущие ({total}{suffix})?"


def _follow_candidates(follower_username: str, mode: str) -> list:
    if mode == "free":
        users = [u for u in get_unlinked_users() if u.get("username") != follower_username]
    else:
        users = get_leaders()
    return sorted_users_for_display(users)


async def _render_follow_picker(call: CallbackQuery, follower_username: str, mode: str, page: int, edit: bool):
    candidates = _follow_candidates(follower_username, mode)

    if not candidates:
        text = (
            "У этого пользователя пока нет ведомых."
            if mode == "leaders"
            else "Нет свободных пользователей для привязки."
        )
        await call.answer(text, show_alert=True)
        return

    kb, total_pages, page = build_follow_picker_kb(follower_username, candidates, page, mode)
    label = follow_picker_label(follower_username, len(candidates), page, total_pages, mode)

    if edit:
        try:
            await call.message.edit_text(label, reply_markup=kb)
        except Exception:
            pass
    else:
        await call.message.answer(label, reply_markup=kb)


@router.callback_query(F.data.startswith("act_follow:"))
async def action_follow_start(call: CallbackQuery):
    if not await admin_only(call):
        return

    follower_username = call.data.split(":", 1)[1]

    # If there are no leaders at all yet, skip straight to "free" mode --
    # showing an empty leaders list with nothing to do but tap through to
    # "free" anyway would just be an extra step for no reason.
    mode = "leaders" if get_leaders() else "free"

    await _render_follow_picker(call, follower_username, mode=mode, page=0, edit=False)
    await call.answer()


@router.callback_query(F.data.startswith("followall:"))
async def action_follow_show_all(call: CallbackQuery):
    if not await admin_only(call):
        return

    follower_username = call.data.split(":", 1)[1]
    await _render_follow_picker(call, follower_username, mode="free", page=0, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("followpage:"))
async def follow_picker_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, follower_username, mode, page_str = call.data.split(":", 3)
    await _render_follow_picker(call, follower_username, mode=mode, page=int(page_str), edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("followto:"))
async def follow_leader_confirm(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, follower_username, leader_username = call.data.split(":", 2)

    leader = get_user(leader_username)
    if not leader:
        await call.answer("Пользователь не найден", show_alert=True)
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
