"""
Entry points into the bot: /start (admin vs. returning client vs. new
client onboarding), the trial signup itself, the global "❌ Cancel" button
(works from ANY FSM state — no state filter on purpose), and binding an
existing account by pasting its connection card.

None of these own a persistent FSM state of their own (onboarding uses
inline buttons, not FSM), so there's no state-ownership overlap with any
other handlers/ file.
"""
from datetime import timedelta

from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from bot.access import is_admin, run_sync
from bot.config import ADMIN_ID, bot
from bot.formatting import CARD_RE, looks_like_card
from bot.keyboards import client_menu, main_menu, platform_choice_kb
from bot.trial import generate_trial_password, generate_username_from_name, has_used_trial, mark_trial_used
from core.dates import utcnow_naive
from core.db import add_user, get_user, get_user_by_telegram_id, update_user

router = Router()


@router.message(F.text == "/start")
async def start(msg: Message):
    if is_admin(msg.from_user.id):
        await msg.answer("TrustPanel online", reply_markup=main_menu)
        return

    user = get_user_by_telegram_id(msg.from_user.id)
    if user:
        await msg.answer(
            f"👋 С возвращением, {user.get('username')}!",
            reply_markup=client_menu
        )
        return

    if has_used_trial(msg.from_user.id):
        await msg.answer(
            "👋 Привет! Чтобы я мог присылать вам уведомления об истечении доступа,\n"
            "пришлите сюда вашу карточку подключения целиком "
            "(то сообщение с Username / Password, которое вам отправил администратор)."
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, пользуюсь — привязать карточку", callback_data="onboard:existing")],
        [InlineKeyboardButton(text="🆕 Нет — попробовать 4 дня бесплатно", callback_data="onboard:trial")],
    ])

    await msg.answer(
        "👋 Привет! Вы уже пользуетесь клубным TrustTunnel VPN?",
        reply_markup=kb
    )


@router.callback_query(F.data == "onboard:existing")
async def onboard_existing(call: CallbackQuery):
    await call.message.answer(
        "Пришлите сюда вашу карточку подключения целиком "
        "(то сообщение с Username / Password, которое вам отправил администратор)."
    )
    await call.answer()


@router.callback_query(F.data == "onboard:trial")
async def onboard_trial_start(call: CallbackQuery):
    if has_used_trial(call.from_user.id):
        await call.answer("Пробный период уже был использован этим аккаунтом.", show_alert=True)
        return

    username = generate_username_from_name(
        call.from_user.first_name, call.from_user.username, call.from_user.id
    )
    password = generate_trial_password()
    expires_at = (utcnow_naive() + timedelta(days=4)).strftime("%Y-%m-%d")

    add_user({
        "username": username,
        "password": password,
        "created_at": utcnow_naive().strftime("%Y-%m-%d"),
        "expires_at": expires_at,
        "status": "active",
        "telegram_id": call.from_user.id,
        "notified_days": [],
        "pending_request": None,
    })

    mark_trial_used(call.from_user.id)

    await run_sync()

    await call.message.answer(
        f"🎁 Пробный доступ на 4 дня активирован! Доступ до {expires_at}.\n\n"
        f"👤 Username: {username}\n"
        f"🔑 Password: {password}\n"
        f"⏳ Expires: {expires_at}\n\n"
        "Об истечении напомню заранее, а продлить сможете прямо в этом чате.",
        reply_markup=client_menu
    )

    await call.message.answer(
        "📖 Осталось настроить подключение — выберите вашу платформу:",
        reply_markup=platform_choice_kb()
    )

    await bot.send_message(
        ADMIN_ID,
        f"🆕 Новая регистрация по триалу: {username} (tg id {call.from_user.id}), доступ до {expires_at}."
    )

    await call.answer()


# ---------------- CANCEL ----------------
# No state filter on purpose — must work regardless of which FSM flow the
# user is currently in.

@router.message(F.text.lower() == "❌ cancel")
async def cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Cancelled", reply_markup=ReplyKeyboardRemove())
    if is_admin(msg.from_user.id):
        await msg.answer("Menu:", reply_markup=main_menu)
    else:
        await msg.answer("Menu:", reply_markup=client_menu)


# ---------------- BIND BY CARD ----------------

@router.message(
    StateFilter(None),
    F.from_user.id != ADMIN_ID,
    F.text.func(looks_like_card),
)
async def bind_by_card(msg: Message, state: FSMContext):
    match = CARD_RE.search(msg.text)
    if not match:
        await msg.answer(
            "Не смог распознать карточку. Пришлите её целиком, без изменений, "
            "так как её отправил администратор."
        )
        return

    username, password = match.group(1), match.group(2)
    user = get_user(username)

    if not user or user.get("password") != password:
        await msg.answer("Не нашёл такой аккаунт. Проверьте, что скопировали карточку без изменений.")
        return

    update_user(username, telegram_id=msg.from_user.id)

    await msg.answer(
        f"✅ Готово, {username}! Теперь я буду присылать уведомления об истечении доступа.",
        reply_markup=client_menu
    )

    tg_username = f"@{msg.from_user.username}" if msg.from_user.username else "(без username)"
    await bot.send_message(
        ADMIN_ID,
        f"🔔 Клиент {username} привязал Telegram {tg_username} (id {msg.from_user.id}) — подписан на уведомления."
    )
