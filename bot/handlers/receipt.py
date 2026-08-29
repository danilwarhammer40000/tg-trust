"""
Owns: ReceiptConfirm.waiting, RenewalApproval.manual_date.

any_media_received is the entry point for a client sending a photo/document
OUTSIDE of any other flow (StateFilter(None)) — e.g. spontaneously sending
a payment screenshot without going through "✉️ Написать администратору"
first. The equivalent flow reached FROM inside the feedback conversation
lives in handlers/feedback.py (client_feedback_media) — the two are
separate entry points into conceptually the same "is this a receipt?"
question, deliberately duplicated rather than shared, since they attach to
different states and different confirm-button callback_data. Both hook
into the AI auto-renewal pipeline (core/auto_renewal.py) the same way —
see receipt_yes below.

Client-facing behaviour: if auto-renewal actually applies, the client
gets the standard renewal text immediately (sent from inside
core/auto_renewal.py, not from here) and nothing else. Otherwise — auto-
renewal wasn't applicable, or was attempted and fell back to manual — the
client gets the normal "Отправлено администратору. Ждите подтверждения."
acknowledgement, same as if auto-renewal didn't exist. See
bot/auto_renewal_hook.py's try_auto_renewal() docstring for the exact
three-way outcome this dispatches on.
"""
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import auto_renewal_hook
from bot.access import admin_only, notify_bg, run_sync
from bot.config import ADMIN_ID, bot
from bot.keyboards import cancel_kb, main_menu, renewal_admin_kb
from bot.states import ReceiptConfirm, RenewalApproval
from core.dates import calc_new_expiry_months, is_expired, utcnow_naive
from core.db import get_user, get_user_by_telegram_id, update_user
from core.notify import log_to_channel

router = Router()
log = logging.getLogger(__name__)


@router.message(StateFilter(None), F.from_user.id != ADMIN_ID, F.photo | F.document)
async def any_media_received(msg: Message, state: FSMContext):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer(
            "Я вас пока не узнал 🤔\n"
            "Сначала пришлите вашу карточку подключения (текст с Username/Password), "
            "чтобы я мог связать вас с аккаунтом."
        )
        return

    is_photo = bool(msg.photo)
    file_id = msg.photo[-1].file_id if is_photo else msg.document.file_id

    await state.update_data(
        receipt_file_id=file_id,
        receipt_username=user["username"],
        receipt_is_photo=is_photo,
    )
    await state.set_state(ReceiptConfirm.waiting)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отправить", callback_data="receipt:yes")],
        [InlineKeyboardButton(text="❌ Нет, это не то", callback_data="receipt:no")]
    ])

    await msg.answer("📎 Это чек на продление? Отправляем администратору на проверку?", reply_markup=kb)


@router.callback_query(F.data == "receipt:yes", ReceiptConfirm.waiting)
async def receipt_yes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    file_id = data.get("receipt_file_id")
    username = data.get("receipt_username")
    is_photo = data.get("receipt_is_photo", True)

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

    caption = (
        f"📥 Заявка на продление от {username}\n"
        f"⏳ Текущая дата истечения: {expiry_line}"
    )

    # Every receipt submission gets logged with the file, regardless of
    # whether auto-renewal ends up handling it — rule "логируем всё".
    await notify_bg(log_to_channel, caption, file_id=file_id, is_photo=is_photo)

    await state.clear()

    # Three possible outcomes -- see bot/auto_renewal_hook.py's docstring:
    #   "approved" -> client already notified, admin already has a card. Do nothing more.
    #   "fallback" -> admin already has a card (from the pipeline itself, possibly
    #                 the anti-abuse warning). Do NOT send a second one -- just
    #                 acknowledge the client, who hasn't heard anything yet.
    #   "skipped"  -> auto-renewal didn't run at all. Full normal manual flow.
    result = await auto_renewal_hook.try_auto_renewal(username, file_id, is_photo)

    if result == "approved":
        await call.answer()
        return

    if result == "fallback":
        await call.message.answer("✅ Отправлено администратору. Ждите подтверждения.")
        await call.answer()
        return

    kb = renewal_admin_kb(username)

    if is_photo:
        await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
    else:
        await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)

    await call.message.answer("✅ Отправлено администратору. Ждите подтверждения.")
    await call.answer()


@router.callback_query(F.data == "receipt:no", ReceiptConfirm.waiting)
async def receipt_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Хорошо, отменил. Если это всё же чек — пришлите его ещё раз.")
    await call.answer()


@router.message(ReceiptConfirm.waiting, F.photo | F.document)
async def media_while_waiting_confirm(msg: Message):
    await msg.answer(
        "У вас уже есть чек, ожидающий подтверждения ⬆️\n"
        "Сначала нажмите «✅ Да, отправить» или «❌ Нет, это не то» на предыдущем сообщении."
    )


# ---------------- ADMIN: APPROVE / REJECT RENEWAL ----------------

def _ai_in_progress_or_done(pending: dict):
    """
    Returns (blocked: bool, message: str) — used to keep a manual approval
    from colliding with the AI pipeline processing (or having already
    processed) the same request. See core.db.claim_pending_request_for_ai
    for the matching "claim" side of this — same staleness rule (a claim
    older than AI_CLAIM_STALE_SECONDS with no recorded result is treated
    as abandoned/crashed, not blocking).
    """
    if not pending:
        return False, ""

    if pending.get("ai_result") == "approved":
        return True, "Эта заявка уже обработана автоматически — проверьте карточку в чате с ботом."

    claimed_at = pending.get("ai_claimed_at")
    if claimed_at and not pending.get("ai_result"):
        try:
            from core.db import AI_CLAIM_STALE_SECONDS
            claimed_dt = datetime.fromisoformat(claimed_at)
            still_fresh = (utcnow_naive() - claimed_dt).total_seconds() < AI_CLAIM_STALE_SECONDS
        except ValueError:
            still_fresh = False
        if still_fresh:
            return True, "Заявка сейчас обрабатывается автоматически, попробуйте через минуту."

    return False, ""


@router.callback_query(F.data.startswith("apr:"))
async def approve_renewal(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    _, username, action = call.data.split(":")
    user = get_user(username)

    if not user:
        await call.answer("User not found", show_alert=True)
        return

    blocked, message = _ai_in_progress_or_done(user.get("pending_request"))
    if blocked:
        await call.answer(message, show_alert=True)
        return

    if action == "reject":
        update_user(username, pending_request=None)
        await call.message.edit_caption(caption=f"❌ Заявка {username} отклонена")
        await notify_bg(log_to_channel, f"❌ Заявка {username} отклонена администратором (вручную).")

        if user.get("telegram_id"):
            await bot.send_message(
                user["telegram_id"],
                "❌ Чек не подтверждён администратором. Свяжитесь для уточнения."
            )

        await call.answer()
        return

    if action == "manual":
        await state.set_state(RenewalApproval.manual_date)
        await state.update_data(target_username=username)
        await call.message.answer(
            f"Введите новую дату истечения для {username} (YYYY-MM-DD):",
            reply_markup=cancel_kb
        )
        await call.answer()
        return

    # action here is "30" (1 мес) or "60" (2 мес) — both mean calendar months,
    # not a literal day count (see bot/keyboards.py's renewal_admin_kb).
    months = int(action) // 30
    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))
    new_expires = calc_new_expiry_months(user.get("expires_at"), months)

    # Split into two calls -- see core/auto_renewal.py's
    # _apply_and_request_review for why (core.db.update_user() redirects
    # expires_at/status to the leader for a follower account; bundling
    # non-sync fields into the same call would misroute them).
    update_user(username, expires_at=new_expires, status="active")
    update_user(
        username,
        pending_request=None,
        notified_days=[],
        post_disable_notified=[],
        # A real manual approval is exactly the "human checked it" event
        # that resets the auto-renewal anti-abuse lock for next cycle —
        # see core/auto_renewal.py's process pipeline.
        auto_renewal_applied=False,
        auto_renewal_applied_at=None,
    )

    # Only resync + restart trusttunnel if this user was actually missing from
    # credentials.toml (expired/inactive). If they were already active, nothing
    # in credentials.toml changes, and restarting would needlessly drop every
    # other connected client.
    if was_expired_or_inactive:
        await run_sync()

    await call.message.edit_caption(caption=f"✅ {username} продлён до {new_expires}")
    await notify_bg(log_to_channel, f"✅ {username} продлён вручную администратором до {new_expires} ({months} мес.).")

    if user.get("telegram_id"):
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена до {new_expires}. Спасибо!"
        )

    await call.answer("Готово")


@router.message(RenewalApproval.manual_date)
async def approve_renewal_manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("target_username")
    await state.clear()

    try:
        new_expires = datetime.strptime(msg.text.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        await msg.answer("Неверный формат даты. Используйте YYYY-MM-DD.", reply_markup=main_menu)
        return

    user = get_user(username)
    if not user:
        await msg.answer("Пользователь не найден.", reply_markup=main_menu)
        return

    blocked, message = _ai_in_progress_or_done(user.get("pending_request"))
    if blocked:
        await msg.answer(message, reply_markup=main_menu)
        return

    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))

    update_user(username, expires_at=new_expires, status="active")
    update_user(
        username,
        pending_request=None,
        notified_days=[],
        post_disable_notified=[],
        auto_renewal_applied=False,
        auto_renewal_applied_at=None,
    )

    if was_expired_or_inactive:
        await run_sync()

    await notify_bg(log_to_channel, f"✅ {username} продлён вручную администратором до {new_expires} (ручная дата).")

    if user.get("telegram_id"):
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена до {new_expires}. Спасибо!"
        )

    await msg.answer(f"✅ {username} продлён до {new_expires}", reply_markup=main_menu)
