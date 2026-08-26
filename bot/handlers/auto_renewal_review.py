"""
Owns: AutoRenewalSettings.waiting_value.

Three things live in this file:
1. "🤖 Автопродление" admin menu — ON/OFF toggle, "🚀 Полностью
   автоматический режим" toggle, and "⚙️ Настроить условия" (opens the
   trigger-tuning submenu). Turning the master toggle ON is refused if
   LOG_CHANNEL_ID isn't configured (see core.auto_renewal.
   log_channel_configured) — rule 4 was "everything gets logged", so the
   feature simply can't run without somewhere to log to.
2. The settings submenu — one "✏️ field: value" row per editable trigger
   parameter (night window bounds, overdue threshold, min amount, min
   confidence, date tolerance). Tapping one asks for a new value as plain
   text; core.auto_renewal.set_setting_validated() does all the bounds
   checking, this file just relays its ok/error result.
3. aircheck:{username}:confirm / aircheck:{username}:disable — the
   post-hoc review buttons attached to every auto-renewal decision card
   (core/auto_renewal.py's _apply_and_request_review). "Подтвердить" just
   clears the pending review; "Отключить" disables the account AND rolls
   the expiry/status back to what they were before the auto-renewal —
   full undo, not just a disable. airretry:{username} (attached to
   *fallback* cards instead) re-runs the AI pipeline on demand — useful
   when the fallback reason was a transient problem (e.g. an outdated
   Gemini model name) rather than a genuinely bad receipt.
"""
import asyncio
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import core.auto_renewal as auto_renewal
from bot.access import admin_only, run_sync
from bot.keyboards import main_menu
from bot.states import AutoRenewalSettings
from core.db import claim_pending_request_for_ai, get_user, update_user
from core.notify import log_to_channel, notify_user

router = Router()
log = logging.getLogger(__name__)


# ---------------- MAIN MENU ----------------

def auto_renewal_menu_kb() -> InlineKeyboardMarkup:
    enabled = auto_renewal.is_auto_renewal_enabled()
    fully_auto = auto_renewal.is_fully_automatic_enabled()

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅ Включено' if enabled else '⬜ Выключено'} — переключить",
            callback_data="autoren:toggle"
        )],
        [InlineKeyboardButton(
            text=f"{'🚀 Полностью автоматически' if fully_auto else '🌙 Только в ночном окне'} — переключить",
            callback_data="autoren:toggle_full"
        )],
        [InlineKeyboardButton(text="⚙️ Настроить условия", callback_data="autoren:settings")],
    ])


def _status_text() -> str:
    enabled = auto_renewal.is_auto_renewal_enabled()
    fully_auto = auto_renewal.is_fully_automatic_enabled()
    log_ok = auto_renewal.log_channel_configured()

    if fully_auto:
        window_line = "• Круглосуточно, при поступлении любого чека (полностью автоматический режим)"
    else:
        window_line = (
            f"• {auto_renewal.get_setting('night_start')}–{auto_renewal.get_setting('night_end')} "
            f"по Красноярску — сразу при поступлении чека"
        )

    lines = [
        "🤖 Автопродление по чеку через Gemini",
        "",
        f"Статус: {'✅ включено' if enabled else '⬜ выключено'}",
        f"Канал лога: {'✅ настроен' if log_ok else '❌ НЕ настроен (LOG_CHANNEL_ID в .env)'}",
        "",
        "Условия срабатывания:",
        window_line,
        f"• Заявка висит без ответа администратора > {auto_renewal.get_setting('overdue_hours')} ч. "
        f"— в любое время суток",
        "",
        "Решение принимает не ИИ напрямую — Gemini только распознаёт сумму/дату "
        "с чека, дальше код проверяет по правилам (см. «⚙️ Настроить условия»). "
        "Каждое автопродление приходит на проверку — можно подтвердить или "
        "отключить с откатом.",
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


@router.callback_query(F.data == "autoren:toggle_full")
async def auto_renewal_toggle_full(call: CallbackQuery):
    if not await admin_only(call):
        return

    now_full = auto_renewal.toggle_fully_automatic()

    try:
        await call.message.edit_text(_status_text(), reply_markup=auto_renewal_menu_kb())
    except Exception:
        pass
    await call.answer(
        "Теперь работает круглосуточно" if now_full else "Теперь только в ночном окне"
    )


# ---------------- SETTINGS SUBMENU ----------------

def _settings_text() -> str:
    lines = ["⚙️ Условия срабатывания автопродления:", ""]
    for key, meta in auto_renewal.FIELD_META.items():
        lines.append(f"{meta['label']}: {auto_renewal.get_setting(key)}")
    return "\n".join(lines)


def _settings_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"✏️ {meta['label']}", callback_data=f"autoren:edit:{key}")]
        for key, meta in auto_renewal.FIELD_META.items()
    ]
    rows.append([InlineKeyboardButton(text="↩️ Сбросить по умолчанию", callback_data="autoren:reset")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="autoren:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "autoren:settings")
async def auto_renewal_settings_menu(call: CallbackQuery):
    if not await admin_only(call):
        return
    try:
        await call.message.edit_text(_settings_text(), reply_markup=_settings_kb())
    except Exception:
        await call.message.answer(_settings_text(), reply_markup=_settings_kb())
    await call.answer()


@router.callback_query(F.data == "autoren:back")
async def auto_renewal_back(call: CallbackQuery):
    if not await admin_only(call):
        return
    try:
        await call.message.edit_text(_status_text(), reply_markup=auto_renewal_menu_kb())
    except Exception:
        await call.message.answer(_status_text(), reply_markup=auto_renewal_menu_kb())
    await call.answer()


@router.callback_query(F.data == "autoren:reset")
async def auto_renewal_reset(call: CallbackQuery):
    if not await admin_only(call):
        return
    auto_renewal.reset_settings_to_defaults()
    try:
        await call.message.edit_text(_settings_text(), reply_markup=_settings_kb())
    except Exception:
        pass
    await call.answer("Условия сброшены к значениям по умолчанию")


@router.callback_query(F.data.startswith("autoren:edit:"))
async def auto_renewal_edit_start(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    key = call.data.split(":", 2)[2]
    meta = auto_renewal.FIELD_META.get(key)
    if not meta:
        await call.answer("Неизвестный параметр", show_alert=True)
        return

    await state.set_state(AutoRenewalSettings.waiting_value)
    await state.update_data(field=key)

    current = auto_renewal.get_setting(key)
    await call.message.answer(f"{meta['label']}\nТекущее значение: {current}\n\n{meta['prompt']}")
    await call.answer()


@router.message(AutoRenewalSettings.waiting_value)
async def auto_renewal_edit_apply(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    key = data.get("field")
    await state.clear()

    ok, error = auto_renewal.set_setting_validated(key, msg.text or "")

    if not ok:
        await msg.answer(f"❌ {error}\n\nЗначение не сохранено, попробуйте ещё раз через «⚙️ Настроить условия».", reply_markup=main_menu)
        return

    meta = auto_renewal.FIELD_META.get(key, {})
    new_value = auto_renewal.get_setting(key)
    await msg.answer(
        f"✅ {meta.get('label', key)} сохранено: {new_value}",
        reply_markup=main_menu
    )


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


# ---------------- RETRY (attached to fallback cards) ----------------

@router.callback_query(F.data.startswith("airretry:"))
async def auto_renewal_retry(call: CallbackQuery):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]
    user = get_user(username)

    if not user or not user.get("pending_request"):
        await call.answer("Заявка не найдена или уже обработана.", show_alert=True)
        return

    if not claim_pending_request_for_ai(username):
        await call.answer("Уже обрабатывается — подождите немного.", show_alert=True)
        return

    await call.answer("Повторяю проверку через Gemini...")

    loop = asyncio.get_event_loop()
    approved = await loop.run_in_executor(
        None, auto_renewal.process_pending_request_with_ai, username, "manual_retry"
    )

    note = "\n\n🔄 Повторная проверка: одобрено (см. новую карточку выше)." if approved \
        else "\n\n🔄 Повторная проверка снова не прошла — см. новое сообщение."
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + note)
    except Exception:
        pass
