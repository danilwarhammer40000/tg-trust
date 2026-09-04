"""
Owns: SetTelegramId.waiting.

action_extend transitions into ExtendUser.mode (owned by handlers/extend.py),
action_call transitions into AdminMessage.personal (owned by
handlers/feedback.py), and action_leader_start/action_ungroup_start
transition into LeaderLink.select (owned by handlers/leader_link.py) — see
bot/states.py's docstring on why that's fine. action_unlink and
action_follow_start (also in leader_link.py) are one-shot/stateless.
"""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only, run_sync
from bot.config import DOMAIN
from bot.display import build_follower_counts, prepare_users_for_display, user_button_label
from bot.formatting import extract_telegram_id_from_message, format_full_instructions_message
from bot.keyboards import cancel_kb, main_menu
from bot.pagination import paginate, pagination_nav_row
from bot.states import AdminMessage, ExtendUser, SetTelegramId
from core.dates import is_expired
from core.db import delete_user, get_followers, get_user, list_users, unlink_user, update_user
from core.generator import generate_link

router = Router()


# ---------------- LIST USERS ----------------

def build_list_users_kb(users: list, page: int, follower_counts: dict) -> tuple:
    """Returns (keyboard, total_pages, page). follower_counts comes from
    bot.display.build_follower_counts() over the FULL (unfiltered) user
    list — see that function's docstring for why it can't just be derived
    from `users` (this page's already-filtered/paginated slice)."""
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=user_button_label(u, follower_counts.get(u.get("username"), 0)),
            callback_data=f"user:{u.get('username')}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "listpage")

    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


@router.message(F.text == "📋 List users")
async def menu_list(msg: Message):
    if not await admin_only(msg):
        return

    all_users = list_users() or []
    follower_counts = build_follower_counts(all_users)
    users = prepare_users_for_display(all_users)

    if not users:
        await msg.answer("No users")
        return

    kb, total_pages, page = build_list_users_kb(users, 0, follower_counts)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    await msg.answer(label, reply_markup=kb)


@router.callback_query(F.data.startswith("listpage:"))
async def menu_list_page(call: CallbackQuery):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    all_users = list_users() or []
    follower_counts = build_follower_counts(all_users)
    users = prepare_users_for_display(all_users)

    kb, total_pages, page = build_list_users_kb(users, page, follower_counts)
    label = f"Select user ({len(users)} всего" + (f", стр. {page + 1}/{total_pages}" if total_pages > 1 else "") + "):"

    try:
        await call.message.edit_text(label, reply_markup=kb)
    except Exception:
        pass  # "message is not modified" if the same page is tapped twice
    await call.answer()


# ---------------- USER ACTIONS MENU ----------------

@router.callback_query(F.data.startswith("user:"))
async def user_actions_menu(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username) or {}

    tg_id = user.get("telegram_id")
    sub_status = f"🔔 Подписан (id {tg_id})" if tg_id else "🔕 Не подписан на уведомления"

    link_lines = []
    linked_to = user.get("linked_to")
    followers = [] if linked_to else get_followers(username)
    if linked_to:
        link_lines.append(f"🔗 Ведомый у: {linked_to}")
    elif followers:
        names = ", ".join(f.get("username", "?") for f in followers)
        # Explicit count, not just implied by the name list — this is the
        # "сколько дополнительных ссылок выпущено для человека" figure.
        link_lines.append(f"👑 Доп. ссылок выпущено: {len(followers)} ({names})")
    link_status = ("\n" + "\n".join(link_lines)) if link_lines else ""

    rows = [
        [InlineKeyboardButton(text="🔗 Get link", callback_data=f"act_link:{username}")],
        [InlineKeyboardButton(text="⏳ Extend", callback_data=f"act_extend:{username}")],
        [InlineKeyboardButton(text="❌ Delete", callback_data=f"act_del:{username}")],
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"act_call:{username}")],
        [InlineKeyboardButton(
            text="🆔 Telegram ID",
            callback_data=f"act_setid:{username}"
        )],
    ]

    if tg_id:
        # tg://user?id=... opens a direct 1:1 chat with that Telegram
        # account in the client app — a real chat window, not routed
        # through the bot like "✉️ Написать" above. Works in Telegram
        # Desktop/mobile as long as the app can resolve the numeric id
        # (it generally can once the account has messaged the bot, which
        # is exactly when telegram_id gets set in the first place).
        rows.append([InlineKeyboardButton(text="💬 Открыть чат в Telegram", url=f"tg://user?id={tg_id}")])

    # A follower's own expiry/status is redirected to its leader anyway (see
    # core.db.update_user), so it can't become a leader itself — offer only
    # "unlink" for it.
    if linked_to:
        rows.append([InlineKeyboardButton(text="🔓 Отвязать", callback_data=f"act_unlink:{username}")])
    else:
        is_leader = bool(followers)

        leader_label = "👑 Управление ведомыми" if is_leader else "👑 Назначить ведущим"
        rows.append([InlineKeyboardButton(text=leader_label, callback_data=f"act_leader:{username}")])

        if is_leader:
            rows.append([InlineKeyboardButton(text="💔 Разгруппировать", callback_data=f"act_ungroup:{username}")])
        else:
            # The flow itself (leader_link.py's action_follow_start) falls
            # back to a "free users" list when there are no leaders yet, so
            # no need to gate this button on get_leaders() anymore.
            rows.append([InlineKeyboardButton(text="🔗 Сделать ведомым", callback_data=f"act_follow:{username}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await call.message.answer(f"👤 {username}\n{sub_status}{link_status}\n\nChoose action:", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("act_unlink:"))
async def action_unlink(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username)

    if not user or not user.get("linked_to"):
        await call.answer("Уже не привязан", show_alert=True)
        return

    former_leader = user["linked_to"]
    unlink_user(username)

    await call.message.answer(
        f"🔓 {username} отвязан от {former_leader}. Дата/статус сохранены как собственные "
        f"и больше не будут меняться автоматически вместе с {former_leader}.",
        reply_markup=main_menu
    )
    await call.answer()


@router.callback_query(F.data.startswith("act_link:"))
async def action_get_link(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username) or {}
    link = generate_link(username, DOMAIN)

    await call.message.answer(
        format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)
    )
    await call.answer()


@router.callback_query(F.data.startswith("act_extend:"))
async def action_extend(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    await state.update_data(username=username)
    await state.set_state(ExtendUser.mode)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ 3 дня", callback_data="ext:3")],
        [InlineKeyboardButton(text="➕ 1 мес", callback_data="ext:30")],
        [InlineKeyboardButton(text="♾ Безлимит", callback_data="ext:0")],
        [InlineKeyboardButton(text="✍️ Ручной ввод", callback_data="ext:manual")]
    ])

    await call.message.answer(f"Extend user: {username}", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("act_del:"))
async def action_delete_confirm(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 Да, удалить {username}", callback_data=f"act_del_yes:{username}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="act_del_no")],
    ])

    await call.message.answer(f"⚠️ Удалить пользователя {username}? Это необратимо.", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "act_del_no")
async def action_delete_cancel(call: CallbackQuery):
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data.startswith("act_del_yes:"))
async def action_delete_execute(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username)
    was_active = bool(
        user and user.get("status") == "active" and not is_expired(user.get("expires_at"))
    )

    delete_user(username)

    if was_active:
        await run_sync()

    await call.message.answer(f"❌ Deleted: {username}", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data.startswith("act_call:"))
async def action_call(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username)

    if not user or not user.get("telegram_id"):
        await call.answer("У пользователя нет привязанного Telegram", show_alert=True)
        return

    await state.update_data(target_username=username)
    await state.set_state(AdminMessage.personal)

    await call.message.answer(f"Введите сообщение для {username}:", reply_markup=cancel_kb)
    await call.answer()


# ---------------- TELEGRAM ID: write / clear ----------------

@router.callback_query(F.data.startswith("act_setid:"))
async def action_set_id_menu(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username) or {}
    current = user.get("telegram_id")
    current_line = f"Текущее значение: {current}" if current else "Сейчас не задан."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Записать/перезаписать", callback_data=f"setid:write:{username}")],
        [InlineKeyboardButton(text="🗑 Очистить значение", callback_data=f"setid:clear:{username}")],
    ])

    await call.message.answer(f"🆔 Telegram ID для {username}\n{current_line}", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("setid:write:"))
async def action_set_id_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 2)[2]

    await state.set_state(SetTelegramId.waiting)
    await state.update_data(target_username=username)

    await call.message.answer(
        f"Отправьте Telegram ID клиента {username} числом,\n"
        f"или перешлите сюда любое его сообщение — возьму ID оттуда.",
        reply_markup=cancel_kb
    )
    await call.answer()


@router.callback_query(F.data.startswith("setid:clear:"))
async def action_set_id_clear(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 2)[2]
    update_user(username, telegram_id=None)

    await call.message.answer(
        f"🗑 Telegram ID для {username} очищен — уведомления ему приходить больше не будут, "
        f"пока не привяжете заново.",
        reply_markup=main_menu
    )
    await call.answer()


@router.message(SetTelegramId.waiting)
async def action_set_id_apply(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("target_username")
    await state.clear()

    tg_id = extract_telegram_id_from_message(msg)

    if tg_id is None:
        await msg.answer(
            "Не смог распознать ID. Пришлите число, либо перешлите сообщение от клиента "
            "(не сработает, если у него в приватности скрыта пересылка).",
            reply_markup=main_menu
        )
        return

    update_user(username, telegram_id=tg_id)

    await msg.answer(
        f"✅ {username} теперь привязан к Telegram ID {tg_id}. Уведомления будут приходить туда.",
        reply_markup=main_menu
    )
