"""
Owns: AutoRenewalSettings.waiting_value.

Four things live in this file:
1. "🤖 Автопродление" admin menu — ON/OFF toggle, "🚀 Полностью
   автоматический режим" toggle, "⚙️ Настроить условия" (opens the
   trigger-tuning submenu), and "🔍 Диагностика" (live Bot API checks for
   why the log channel might not be receiving posts). Turning the master
   toggle ON is refused if LOG_CHANNEL_ID isn't configured (see
   core.auto_renewal.log_channel_configured) — rule 4 was "everything
   gets logged", so the feature simply can't run without somewhere to
   log to.
2. The settings submenu — one "✏️ field: value" row per editable trigger
   parameter (night window bounds, overdue threshold, min amount, min
   confidence). Tapping one asks for a new value as plain text;
   core.auto_renewal.set_setting_validated() does all the bounds
   checking, this file just relays its ok/error result.
3. aircheck:{username}:confirm / aircheck:{username}:disable — the
   post-hoc review buttons attached to every auto-renewal decision card
   (core/auto_renewal.py's _apply_and_request_review). "Подтвердить"
   sends the client the SAME renewal message a manual approval would
   (bot/handlers/receipt.py) — this is the only point the client learns
   their subscription was renewed at all; nothing is sent earlier.
   "Отключить" is a full undo (status AND expiry both roll back to what
   they were before the auto-renewal, and the one-shot anti-abuse lock is
   released so a genuine future payment can auto-renew again) — the
   message the client gets never mentions "automatic", by the same rule.
   airretry:{username} (attached to *fallback* cards instead) re-runs the
   AI pipeline on demand — useful when the fallback reason was a
   transient problem (e.g. an outdated Gemini model name) rather than a
   genuinely bad receipt.

Client-invisibility rule: nowhere in this file (or in
bot/handlers/receipt.py / feedback.py) does any client-facing message
mention "автоматически" / auto-renewal. As far as the client is concerned
every renewal is "the admin checked it, eventually" — see each message's
wording below.
"""
import asyncio
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import core.auto_renewal as auto_renewal
import core.gemini_client as gemini_client
from bot.access import admin_only, notify_bg, run_sync
from bot.keyboards import main_menu
from bot.states import AutoRenewalSettings
from core.db import claim_pending_request_for_ai, get_user, update_user
from core.notify import diagnose_log_channel, log_to_channel, notify_user

router = Router()
log = logging.getLogger(__name__)


# ---------------- MAIN MENU ----------------

def auto_renewal_menu_kb() -> InlineKeyboardMarkup:
    enabled = auto_renewal.is_auto_renewal_enabled()
    fully_auto = auto_renewal.is_fully_automatic_enabled()

    rows = [
        [InlineKeyboardButton(
            text=f"{'✅ Включено' if enabled else '⬜ Выключено'} — переключить",
            callback_data="autoren:toggle"
        )],
        [InlineKeyboardButton(
            text=f"{'🚀 Полностью автоматически' if fully_auto else '🌙 Только в ночном окне'} — переключить",
            callback_data="autoren:toggle_full"
        )],
        [InlineKeyboardButton(text="⚙️ Настроить условия", callback_data="autoren:settings")],
        [InlineKeyboardButton(text="🔍 Диагностика лога", callback_data="autoren:diag")],
    ]

    # Only shown when GEMINI_PROXY_URL is actually set in .env -- nothing
    # to toggle otherwise. Lets the admin switch between "route the
    # Gemini call through the proxy" and "connect directly" live, without
    # touching .env or restarting the bot -- e.g. to check whether a
    # currently-down proxy server is the actual cause of a failure.
    if gemini_client.proxy_configured():
        proxy_on = gemini_client.is_proxy_enabled()
        rows.append([InlineKeyboardButton(
            text=f"{'🌐 Прокси для Gemini: Включен' if proxy_on else '🔌 Прокси для Gemini: Выключен'} — переключить",
            callback_data="autoren:toggle_proxy"
        )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    ]

    if gemini_client.proxy_configured():
        proxy_line = "✅ включен" if gemini_client.is_proxy_enabled() else "🔌 выключен (прямое подключение)"
        lines.append(f"Прокси для Gemini: {proxy_line}")

    lines += [
        "",
        "Условия срабатывания:",
        window_line,
        f"• Заявка висит без ответа администратора > {auto_renewal.get_setting('overdue_hours')} ч. "
        f"— в любое время суток",
        "",
        "Решение принимает не ИИ напрямую — Gemini только распознаёт сумму "
        "с чека (дата платежа не проверяется, важна только сумма), дальше "
        "код проверяет по правилам (см. «⚙️ Настроить условия»).",
        "",
        f"🔒 Защита от накрутки: после одного автопродления следующее для "
        f"того же пользователя блокируется на {auto_renewal.get_setting('abuse_lock_days')} дн. "
        f"(настраивается). Первая заявка — автоматически, все последующие "
        f"в этот период — только вручную, с отдельной пометкой "
        f"«🚨 ЗАЩИТА ОТ НАКРУТКИ» в карточке админу. Снимается раньше срока "
        f"вручную (любое ручное продление или «🚫 Отключить»), либо само "
        f"по истечении срока.",
        "",
        "Клиент уведомляется о продлении сразу же, тем же текстом, что и "
        "при ручном одобрении — про автопродление он не узнаёт ничего. "
        "Единственный случай полной тишины для клиента — срабатывание "
        "защиты от накрутки: заявка уходит только администратору.",
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


@router.callback_query(F.data == "autoren:toggle_proxy")
async def auto_renewal_toggle_proxy(call: CallbackQuery):
    if not await admin_only(call):
        return

    if not gemini_client.proxy_configured():
        # Shouldn't normally be reachable (the button only renders when a
        # proxy IS configured), but .env could have changed without a
        # bot restart reflecting it yet -- fail safely rather than throw.
        await call.answer("GEMINI_PROXY_URL не задан в .env — нечего переключать.", show_alert=True)
        return

    now_on = gemini_client.toggle_proxy_enabled()

    try:
        await call.message.edit_text(_status_text(), reply_markup=auto_renewal_menu_kb())
    except Exception:
        pass
    await call.answer(
        "Прокси включен — Gemini идёт через него" if now_on
        else "Прокси выключен — прямое подключение к Gemini"
    )


# ---------------- SETTINGS SUBMENU ----------------

def _settings_text() -> str:
    lines = ["⚙️ Условия срабатывания автопродления:", ""]
    for key, meta in auto_renewal.FIELD_META.items():
        lines.append(f"{meta['label']}: {auto_renewal.get_setting(key)}")
    lines += ["", "Дата платежа на чеке не проверяется — важна только сумма."]
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


# ---------------- DIAGNOSTICS ----------------

def _format_diag(d: dict) -> str:
    lines = ["🔍 Диагностика лог-канала автопродления", ""]

    lines.append(f"BOT_TOKEN в .env: {'✅ задан' if d['bot_token_set'] else '❌ НЕ задан'}")
    lines.append(f"ADMIN_ID в .env: {'✅ задан' if d['admin_id_set'] else '❌ НЕ задан'}")
    lines.append(f"LOG_CHANNEL_ID в .env: {'✅ задан (' + str(d['log_channel_id']) + ')' if d['log_channel_id_set'] else '❌ НЕ задан'}")
    lines.append("")

    if d["get_me_ok"]:
        lines.append(f"getMe: ✅ токен рабочий, бот @{d['bot_username']} (id {d['bot_id']})")
    else:
        lines.append(f"getMe: ❌ {d['get_me_error']}")

    if not d["log_channel_id_set"]:
        lines.append("")
        lines.append("Дальше проверять нечего — сначала задайте LOG_CHANNEL_ID в .env и перезапустите бота.")
        return "\n".join(lines)

    if d["get_chat_ok"]:
        lines.append(f"getChat: ✅ канал виден, «{d['chat_title']}»")
    else:
        lines.append(f"getChat: ❌ {d['get_chat_error']}")
        lines.append("   → скорее всего неверный LOG_CHANNEL_ID, либо бота там вообще нет.")

    if d["member_status"] is not None:
        status_ru = {
            "creator": "создатель",
            "administrator": "администратор",
            "member": "участник",
            "restricted": "ограничен",
            "left": "НЕ состоит в канале",
            "kicked": "исключён/забанен",
        }.get(d["member_status"], d["member_status"])
        lines.append(f"Статус бота в канале: {status_ru}")

        if d["member_status"] in ("left", "kicked"):
            lines.append("   → бот не состоит в канале (или был удалён) — добавьте его заново как администратора.")
        elif d["can_post_messages"] is False:
            lines.append(
                "can_post_messages: ❌ ВЫКЛЮЧЕНО — вот и причина. Статуса «администратор» "
                "недостаточно: зайдите в настройки канала → Администраторы → права бота → "
                "включите «Публикация сообщений»."
            )
        elif d["can_post_messages"] is True:
            lines.append("can_post_messages: ✅ включено")
    elif d["get_member_error"]:
        lines.append(f"getChatMember: ❌ {d['get_member_error']}")

    lines.append("")
    if d["test_send_ok"]:
        lines.append("Тестовая отправка в канал: ✅ УСПЕШНО — лог физически работает прямо сейчас.")
        lines.append("Если сообщения всё равно не появляются в реальных сценариях — проверьте, что "
                      "включён сам тумблер автопродления и что канал в .env совпадает с этим же ID.")
    else:
        lines.append(f"Тестовая отправка в канал: ❌ {d['test_send_error']}")
        lines.append("   → это точная причина, по которой log_to_channel() сейчас не работает.")

    return "\n".join(lines)


@router.callback_query(F.data == "autoren:diag")
async def auto_renewal_diag(call: CallbackQuery):
    if not await admin_only(call):
        return

    await call.answer("Проверяю...")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, diagnose_log_channel)

    await call.message.answer(_format_diag(result))


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

        # Client was already notified the moment auto-renewal applied
        # (see core/auto_renewal.py's _apply_and_request_review) --
        # "Подтвердить" just closes this review card, nothing more to send.
        await notify_bg(log_to_channel, f"✅ Автопродление {username} подтверждено администратором (доп. действий не требуется).")
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
        # right before Gemini's decision was applied. Also releases the
        # anti-abuse lock (auto_renewal_applied/_at) early -- this
        # auto-renewal is being treated as if it never happened, so it
        # shouldn't cost the user their next legitimate chance either.
        # Split into two calls -- core.db.update_user() redirects
        # expires_at/status onto the leader (and fans out to the group)
        # whenever they're in the kwargs; bundling pending_request/
        # auto_renewal_applied into that same call would misroute them
        # onto the leader for a follower account instead of staying on
        # `username` itself.
        update_user(username, status="inactive", expires_at=previous_expires_at)
        update_user(username, pending_request=None, auto_renewal_applied=False, auto_renewal_applied_at=None)
        await run_sync()

        # The client was already told "продлено" -- now they need to be
        # told it's off again. Generic wording, same as any other manual
        # rejection: never mentions "automatic" so the client learns
        # nothing about how auto-renewal works.
        await notify_bg(
            notify_user,
            user,
            "❌ Продление отменено администратором после проверки. "
            "Если это ошибка — напишите администратору."
        )
        await notify_bg(
            log_to_channel,
            f"🚫 Автопродление {username} отклонено администратором — "
            f"откат: статус inactive, дата вернулась на {previous_expires_at or '∞'}, "
            f"защита от повторной накрутки снята."
        )
        try:
            await call.message.edit_caption(
                caption=(call.message.caption or "") + "\n\n🚫 Отклонено, доступ отключён, дата откачена, клиент уведомлён."
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
