"""
Owns: Feedback.waiting, Feedback.media_confirm, AdminMessage.personal,
AdminMessage.personal_confirm.

Two entry points feed into AdminMessage.personal from OUTSIDE this file
(handlers/list_users.py's "✉️ Написать" button also sets that state) — that's
fine, see bot/states.py's docstring on the state-ownership rule: a
different file may *transition into* a state without owning its handlers.
"""
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import auto_renewal_hook
from bot.access import admin_only, is_admin
from bot.config import ADMIN_ID, bot
from bot.keyboards import cancel_kb, client_menu, main_menu, renewal_admin_kb
from bot.states import AdminMessage, Feedback
from core.dates import is_expired, utcnow_naive
from core.db import get_user, get_user_by_telegram_id, update_user
from core.notify import log_to_channel

router = Router()
log = logging.getLogger(__name__)


@router.message(F.text == "✉️ Написать администратору")
async def client_feedback_start(msg: Message, state: FSMContext):
    if is_admin(msg.from_user.id):
        return

    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    await state.set_state(Feedback.waiting)
    await msg.answer("Напишите сообщение администратору одним сообщением:", reply_markup=cancel_kb)


@router.message(Feedback.waiting, F.photo | F.document)
async def client_feedback_media(msg: Message, state: FSMContext):
    """
    If a photo/document is attached here, ask whether it's a payment
    receipt (same question the general receipt catch-all asks in
    handlers/receipt.py) before deciding where it goes — see
    feedback_media_as_receipt / feedback_media_as_message below. This
    handler MUST be registered before client_feedback_send (no content
    filter) further down, since aiogram checks handlers in registration
    order and the first matching one wins.
    """
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await state.clear()
        await msg.answer("Не удалось определить ваш аккаунт.", reply_markup=client_menu)
        return

    is_photo = bool(msg.photo)
    file_id = msg.photo[-1].file_id if is_photo else msg.document.file_id

    await state.update_data(
        media_file_id=file_id,
        media_is_photo=is_photo,
        media_caption=msg.caption or msg.text,
        media_username=user["username"],
    )
    await state.set_state(Feedback.media_confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Это чек на оплату", callback_data="fbmedia:receipt")],
        [InlineKeyboardButton(text="✉️ Обычное сообщение", callback_data="fbmedia:message")],
    ])
    await msg.answer("📎 Это чек об оплате для проверки, или просто файл к сообщению?", reply_markup=kb)


@router.callback_query(F.data == "fbmedia:receipt", Feedback.media_confirm)
async def feedback_media_as_receipt(call: CallbackQuery, state: FSMContext):
    """Same handling as the general receipt flow (receipt:yes in
    handlers/receipt.py) — routes into the renewal approval queue with the
    ➕1мес/➕2мес/✍️/❌ admin buttons, unless AI auto-renewal claims it
    first (see bot/auto_renewal_hook.py). If auto-renewal applies, the
    client was already sent the standard "✅ Ваша подписка продлена..."
    text synchronously inside try_auto_renewal (see
    core/auto_renewal.py's _apply_and_request_review) -- this handler
    must NOT send a second acknowledgement on top of that. The
    "Отправлено администратору" line below only fires for the manual/
    fallback path, where it's still the client's only signal that
    anything happened."""
    data = await state.get_data()
    file_id = data.get("media_file_id")
    username = data.get("media_username")
    is_photo = data.get("media_is_photo", True)
    await state.clear()

    update_user(username, pending_request={
        "type": "renewal",
        "receipt_file_id": file_id,
        "receipt_is_photo": is_photo,
        "requested_at": utcnow_naive().isoformat()
    })

    user = get_user(username) or {}
    current_expiry = user.get("expires_at")
    expiry_line = current_expiry or "∞ (безлимит)"
    if current_expiry and is_expired(current_expiry):
        expiry_line += " (уже истёк)"

    caption = f"📥 Заявка на продление от {username}\n⏳ Текущая дата истечения: {expiry_line}"

    log_to_channel(caption, file_id=file_id, is_photo=is_photo)

    result = await auto_renewal_hook.try_auto_renewal(username, file_id, is_photo)

    if result == "approved":
        await call.answer()
        return

    if result == "fallback":
        await call.message.answer("✅ Отправлено администратору на проверку чека.", reply_markup=client_menu)
        await call.answer()
        return

    kb = renewal_admin_kb(username)

    if is_photo:
        await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
    else:
        await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)

    await call.message.answer("✅ Отправлено администратору на проверку чека.", reply_markup=client_menu)
    await call.answer()


@router.callback_query(F.data == "fbmedia:message", Feedback.media_confirm)
async def feedback_media_as_message(call: CallbackQuery, state: FSMContext):
    """Forwards the actual photo/document to the admin (with the ↩️ Ответить
    button), instead of the old silent placeholder-text-only behaviour."""
    data = await state.get_data()
    file_id = data.get("media_file_id")
    username = data.get("media_username")
    is_photo = data.get("media_is_photo", True)
    caption_text = data.get("media_caption")
    await state.clear()

    tg_id = call.from_user.id
    caption = f"✉️ Обращение от {username} (tg id: {tg_id}):"
    if caption_text:
        caption += f"\n\n{caption_text}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"reply:{username}")]
    ])

    if is_photo:
        await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
    else:
        await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)

    await call.message.answer("✅ Отправлено администратору.", reply_markup=client_menu)
    await call.answer()


@router.message(Feedback.media_confirm)
async def feedback_media_confirm_fallback(msg: Message):
    await msg.answer(
        "У вас есть файл, ожидающий подтверждения ⬆️\n"
        "Сначала нажмите «💳 Это чек на оплату» или «✉️ Обычное сообщение» на предыдущем сообщении."
    )


@router.message(Feedback.waiting)
async def client_feedback_send(msg: Message, state: FSMContext):
    user = get_user_by_telegram_id(msg.from_user.id)
    await state.clear()

    if not user:
        await msg.answer("Не удалось определить ваш аккаунт.", reply_markup=client_menu)
        return

    text = msg.text or "[сообщение без текста]"
    username = user.get("username")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Ответить", callback_data=f"reply:{username}")]
    ])

    await bot.send_message(
        ADMIN_ID,
        f"✉️ Обращение от {username} (tg id: {msg.from_user.id}):\n\n{text}",
        reply_markup=kb
    )

    await msg.answer("✅ Отправлено администратору.", reply_markup=client_menu)


# ---------------- ADMIN: PERSONAL MESSAGE (reply to feedback OR act_call) ----------------

@router.callback_query(F.data.startswith("reply:"))
async def reply_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    await state.update_data(target_username=username)
    await state.set_state(AdminMessage.personal)

    await call.message.answer(f"Введите сообщение для {username}:", reply_markup=cancel_kb)
    await call.answer()


@router.message(AdminMessage.personal)
async def personal_message_preview(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("target_username")

    if not msg.text:
        await msg.answer("Пришлите текстовое сообщение.")
        return

    user = get_user(username) if username else None

    if not user or not user.get("telegram_id"):
        await state.clear()
        await msg.answer(f"⚠️ У {username} нет привязанного Telegram — сообщение не отправлено.", reply_markup=main_menu)
        return

    await state.update_data(text=msg.text)
    await state.set_state(AdminMessage.personal_confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="personal:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="personal:cancel")],
    ])

    await msg.answer(
        f"Получатель: {username}\n\nТекст сообщения:\n\n{msg.text}\n\nОтправляем?",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("personal:"), AdminMessage.personal_confirm)
async def personal_message_confirm(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    username = data.get("target_username")
    text = data.get("text")
    await state.clear()

    if action == "cancel":
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    user = get_user(username) if username else None

    if not user or not user.get("telegram_id"):
        await call.message.answer(f"⚠️ У {username} нет привязанного Telegram — сообщение не отправлено.", reply_markup=main_menu)
        await call.answer()
        return

    try:
        await bot.send_message(user["telegram_id"], f"✉️ Сообщение от администратора:\n\n{text}")
        await call.message.answer(f"✅ Отправлено {username}.", reply_markup=main_menu)
    except Exception as e:
        log.warning("failed to send personal message to %s: %s", username, e)
        await call.message.answer(f"❌ Не удалось отправить {username}: {e}", reply_markup=main_menu)

    await call.answer()
