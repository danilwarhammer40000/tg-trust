"""
Entry points into the bot: /start (admin vs. returning client vs. new
client onboarding), the trial signup itself, the global "❌ Cancel" button
(works from ANY FSM state — no state filter on purpose), and binding an
existing account by pasting its connection card.

None of these own a persistent FSM state of their own (onboarding uses
inline buttons, not FSM), so there's no state-ownership overlap with any
other handlers/ file.
"""
import time

from aiogram import Router, F
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from bot.access import is_admin
from bot.config import ADMIN_ID, bot
from bot.formatting import CARD_RE, looks_like_card
from bot.keyboards import client_menu, main_menu
from core.db import (
    get_user,
    get_user_by_invite_token,
    get_user_by_telegram_id,
    update_user,
)
from bot.handlers.trial_requests import send_trial_request_card
from core.trial import add_trial_request, has_used_trial
from core.instructions import render_bot_usage_instructions

router = Router()


@router.message(CommandStart())
async def start(msg: Message, command: CommandObject):
    if is_admin(msg.from_user.id):
        await msg.answer("TrustPanel online", reply_markup=main_menu)
        return

    payload = command.args or ""
    if payload.startswith("invite_"):
        await _handle_invite(msg, payload[len("invite_"):])
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
        [InlineKeyboardButton(text="🆕 Нет — запросить 4 дня бесплатно", callback_data="onboard:trial")],
    ])

    await msg.answer(
        "👋 Привет! Вы уже пользуетесь клубным TrustTunnel VPN?",
        reply_markup=kb
    )


async def _handle_invite(msg: Message, token: str):
    """
    Consumes an invite-link deep-link payload. See core/invite.py's module
    docstring for the "infinite but one-time" contract this implements:
    a token still resolves to a user record via
    core.db.get_user_by_invite_token() for as long as it's the CURRENT
    token on that record (regenerating replaces it, invalidating the old
    one immediately) -- but ONE-TIME means it only actually binds once:
    the moment telegram_id gets set below, any further tap of the same
    link (by anyone) finds the token still resolving to a user, but that
    user already has a telegram_id, so it's rejected as "already used"
    rather than silently re-binding (which would let a second person
    hijack someone else's account just by reusing a forwarded link).
    """
    invited = get_user_by_invite_token(token)

    if not invited:
        await msg.answer(
            "⚠️ Эта ссылка недействительна — возможно, она устарела или была "
            "перегенерирована. Обратитесь к администратору."
        )
        return

    if invited.get("telegram_id"):
        await msg.answer(
            "⚠️ Эта ссылка уже использована. Если это ошибка — напишите администратору."
        )
        return

    username = invited["username"]
    update_user(username, telegram_id=msg.from_user.id)

    await msg.answer(
        f"✅ Готово, {username}! Аккаунт привязан, буду присылать уведомления об истечении доступа.",
        reply_markup=client_menu
    )
    await msg.answer(render_bot_usage_instructions())

    tg_username = f"@{msg.from_user.username}" if msg.from_user.username else "(без username)"
    await bot.send_message(
        ADMIN_ID,
        f"🔗 Клиент {username} привязан по инвайт-ссылке, Telegram {tg_username} (id {msg.from_user.id})."
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
    """The trial is NOT issued here. The client only files a request; the admin
    approves or rejects it (bot/handlers/trial_requests.py) and the account is
    created at that moment."""
    uid = call.from_user.id

    if has_used_trial(uid) or get_user_by_telegram_id(uid):
        await call.answer("Пробный период уже был использован этим аккаунтом.", show_alert=True)
        return

    is_new = add_trial_request(uid, call.from_user.first_name, call.from_user.username)

    # Re-send the card to the admin on a repeat tap too (e.g. the first one got
    # buried in the chat) — but never create a second request.
    await send_trial_request_card(uid, call.from_user.first_name, call.from_user.username)

    if is_new:
        text = "📨 Заявка на пробный доступ отправлена администратору. Как только она будет одобрена — пришлю данные для подключения."
    else:
        text = "⏳ Ваша заявка уже у администратора — напомнил ему. Ожидайте, пришлю данные сразу после одобрения."
    await call.message.answer(text)
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
#
# Brute-force guard: a wrong username/password pair here is cheap to try
# repeatedly (the only cost is sending a message), so lock a Telegram
# account out after too many wrong guesses in a row. In-memory only —
# resets on bot restart — which is an accepted tradeoff for keeping this
# simple; the goal is to stop quick automated guessing, not to survive a
# determined attacker across restarts.
_BIND_FAILED_ATTEMPTS: dict[int, list] = {}
BIND_MAX_ATTEMPTS = 5
BIND_LOCKOUT_SECONDS = 15 * 60


def _bind_locked_out(user_id: int) -> bool:
    now = time.monotonic()
    attempts = [t for t in _BIND_FAILED_ATTEMPTS.get(user_id, []) if now - t < BIND_LOCKOUT_SECONDS]
    _BIND_FAILED_ATTEMPTS[user_id] = attempts
    return len(attempts) >= BIND_MAX_ATTEMPTS


def _bind_record_failure(user_id: int) -> None:
    _BIND_FAILED_ATTEMPTS.setdefault(user_id, []).append(time.monotonic())


def _bind_clear_failures(user_id: int) -> None:
    _BIND_FAILED_ATTEMPTS.pop(user_id, None)


@router.message(
    StateFilter(None),
    F.from_user.id != ADMIN_ID,
    F.text.func(looks_like_card),
)
async def bind_by_card(msg: Message, state: FSMContext):
    if _bind_locked_out(msg.from_user.id):
        await msg.answer(
            "⏳ Слишком много неудачных попыток привязки подряд. "
            "Подождите немного и попробуйте снова, или обратитесь к администратору."
        )
        return

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
        _bind_record_failure(msg.from_user.id)
        await msg.answer("Не нашёл такой аккаунт. Проверьте, что скопировали карточку без изменений.")
        return

    _bind_clear_failures(msg.from_user.id)
    update_user(username, telegram_id=msg.from_user.id)

    await msg.answer(
        f"✅ Готово, {username}! Теперь я буду присылать уведомления об истечении доступа.",
        reply_markup=client_menu
    )

    await msg.answer(render_bot_usage_instructions())

    tg_username = f"@{msg.from_user.username}" if msg.from_user.username else "(без username)"
    await bot.send_message(
        ADMIN_ID,
        f"🔔 Клиент {username} привязал Telegram {tg_username} (id {msg.from_user.id}) — подписан на уведомления."
    )
