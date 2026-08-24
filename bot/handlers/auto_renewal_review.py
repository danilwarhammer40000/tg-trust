"""
Owns no FSM state — everything here is either a menu toggle or a one-shot
review action driven by callback_data alone.

Two things live in this file:
1. "🤖 Автопродление" admin menu — shows current ON/OFF state and the
   toggle button. Turning it ON is refused if LOG_CHANNEL_ID isn't
   configured (see core.auto_renewal.log_channel_configured) — rule 4 was
   "everything gets logged", so the feature simply can't run without
   somewhere to log to.
2. aircheck:{username}:confirm / aircheck:{username}:disable — the
   post-hoc review buttons attached to every auto-renewal decision card
   (core/auto_renewal.py's _apply_and_request_review). "Подтвердить" just
   clears the pending review; "Отключить" disables the account AND rolls
   the expiry/status back to what they were before the auto-renewal —
   full undo, not just a disable.
"""
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import core.auto_renewal as auto_renewal
from bot.access import admin_only, run_sync
from core.db import get_user, update_user
from core.notify import log_to_channel, notify_user

router = Router()
log = logging.getLogger(__name__)


def auto_renewal_menu_kb() -> InlineKeyboardMarkup:
    enabled = auto_renewal.is_auto_renewal_enabled()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅ Включено' if enabled else '⬜ Выключено'} — переключить",
            callback_data="autoren:toggle"
        )],
    ])


def _status_text() -> str:
    enabled = auto_renewal.is_auto_renewal_enabled()
    log_ok = auto_renewal.log_channel_configured()

    lines = [
        "🤖 Автопродление по чеку через Gemini",
        "",
        f"Статус: {'✅ включено' if enabled else '⬜ выключено'}",
        f"Канал лога: {'✅ настроен' if log_ok else '❌ НЕ настроен (LOG_CHANNEL_ID в .env)'}",
        "",
        "Условия срабатывания:",
        "• 22:00–06:00 по Красноярску — сразу при поступлении чека",
        "• Заявка висит без ответа администратора > 3 часов — в любое время суток",
        "",
        "Решение принимает не ИИ напрямую — Gemini только распознаёт сумму/дату "
        "с чека, дальше код проверяет по правилам (сумма кратна 100₽, дата "
        "сегодня/вчера, уверенность распознавания). Каждое автопродление "
        "приходит сюда на проверку — можно подтвердить или отключить с откатом.",
    ]
    return "\n".join(lines)


@router.message(F.text == "🤖 Автопродление")
async def auto_renewal_menu(msg: Message):
    if not await admin_only(msg):
        return
    await msg.answer(_status_text(), reply_markup=auto_renewal_menu_kb())


@router.callback_query(F.data == "autoren:toggle")
async def auto_renewal_toggle(call: CallbackQuery):
    if not await admin_only(call):
        return

    # Trying to turn ON without a log channel configured would silently
    # violate "everything gets logged" -- refuse instead of turning on a
    # feature that can't do what it promises.
    if not auto_renewal.is_auto_renewal_enabled() and not auto_renewal.log_channel_configured():
        await call.answer(
            "Сначала настройте LOG_CHANNEL_ID в .env — без канала лога "
            "включить автопродление нельзя (правило «логируем всё»).",
            show_alert=True,
        )
        return

    auto_renewal.toggle_auto_renewal()

    try:
        await call.message.edit_text(_status_text(), reply_markup=auto_renewal_menu_kb())
    except Exception:
        pass
    await call.answer()


# ---------------- REVIEW: confirm / disable ----------------

@router.callback_query(F.data.startswith("aircheck:"))
async def auto_renewal_review(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, username, action = call.data.split(":", 2)
    user = get_user(username)

    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    pending = user.get("pending_request") or {}
    decision = pending.get("ai_decision")

    if not decision:
        await call.answer("Эта заявка уже обработана.", show_alert=True)
        try:
            await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n⚠️ Уже обработано.")
        except Exception:
            pass
        return

    if action == "confirm":
        update_user(username, pending_request=None)
        log_to_channel(f"✅ Автопродление {username} подтверждено администратором.")
        try:
            await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n✅ Подтверждено администратором.")
        except Exception:
            pass
        await call.answer("Подтверждено")
        return

    if action == "disable":
        previous_expires_at = decision.get("previous_expires_at")
        previous_status = decision.get("previous_status", "active")

        # Full rollback, not just a disable -- undo the auto-renewal
        # entirely: status AND expiry both go back to what they were
        # right before Gemini's decision was applied.
        update_user(
            username,
            status="inactive",
            expires_at=previous_expires_at,
            pending_request=None,
        )
        await run_sync()

        notify_user(
            user,
            "❌ Автоматическое продление отменено администратором после проверки. "
            "Если это ошибка — напишите администратору."
        )

        log_to_channel(
            f"🚫 Автопродление {username} отклонено администратором — "
            f"откат: статус inactive, дата вернулась на {previous_expires_at or '∞'}."
        )
        try:
            await call.message.edit_caption(
                caption=(call.message.caption or "") + "\n\n🚫 Отклонено, доступ отключён, дата откачена."
            )
        except Exception:
            pass
        await call.answer("Отключено и откачено")
        return

    await call.answer("Неизвестное действие", show_alert=True)
