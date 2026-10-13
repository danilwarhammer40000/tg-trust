"""
Admin approval of free-trial requests.

Flow: a new client taps «🆕 запросить 4 дня бесплатно» (bot/handlers/start.py) →
only a request is stored (core/trial.py: trial_requests.json) and the admin gets
a card with ✅/❌ → on ✅ this module creates the account, marks the trial as
used, resyncs the tunnel and sends the client their credentials; on ❌ the
client is told and the trial is marked as used so the request cannot be spammed
(the admin can re-allow someone from «🗄 База данных → 🎟 Триалы»).

Nothing reaches the VPN (credentials.toml / tunnel restart) before the admin
approves, which also stops a flood of fake sign-ups from restarting the
tunnel over and over.
"""
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.access import admin_only, run_sync
from bot.config import ADMIN_ID, bot
from bot.keyboards import client_menu, platform_choice_kb
from core.dates import utcnow_naive
from core.db import DuplicateUsernameError, add_user, get_user_by_telegram_id
from core.instructions import render_bot_usage_instructions
from core.trial import (
    generate_trial_password,
    generate_username_from_name,
    has_used_trial,
    mark_trial_used,
    pop_trial_request,
)

router = Router()
log = logging.getLogger(__name__)

TRIAL_DAYS = 4


def _who(display_name, handle, account_id) -> str:
    tg = f"@{handle}" if handle else "(без username)"
    return f"{display_name or '—'} {tg} (id {account_id})"


async def send_trial_request_card(account_id: int, display_name, handle) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"trialreq:ok:{account_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"trialreq:no:{account_id}"),
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"🆕 Заявка на пробный доступ ({TRIAL_DAYS} дня)\n{_who(display_name, handle, account_id)}",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("trialreq:"))
async def trial_request_decision(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, action, raw_id = call.data.split(":", 2)
    account_id = int(raw_id)

    # Atomic: the first tap gets the request, any later tap sees None.
    req = pop_trial_request(account_id)
    if req is None:
        await call.answer("Заявка уже обработана.", show_alert=True)
        return

    who = _who(req.get("display_name"), req.get("handle"), account_id)

    if action == "no":
        mark_trial_used(account_id)
        try:
            await bot.send_message(account_id, "❌ К сожалению, заявка на пробный доступ отклонена администратором.")
        except Exception:
            log.warning("could not tell %s that the trial was rejected", account_id)
        await call.message.edit_text(f"❌ Заявка отклонена\n{who}")
        await call.answer("Отклонено")
        return

    # ---- approve ----
    if has_used_trial(account_id) or get_user_by_telegram_id(account_id):
        await call.message.edit_text(f"⚠️ Не создано: у этого аккаунта уже есть доступ или использован триал.\n{who}")
        await call.answer()
        return

    username = generate_username_from_name(req.get("display_name"), req.get("handle"), account_id)
    password = generate_trial_password()
    expires_at = (utcnow_naive() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")

    try:
        add_user({
            "username": username,
            "password": password,
            "created_at": utcnow_naive().strftime("%Y-%m-%d"),
            "expires_at": expires_at,
            "status": "active",
            "telegram_id": account_id,
            "notified_days": [],
            "pending_request": None,
        })
    except DuplicateUsernameError:
        await call.message.edit_text(f"⚠️ Не создано: имя {username} уже занято.\n{who}")
        await call.answer()
        return

    mark_trial_used(account_id)
    await run_sync()

    await call.message.edit_text(f"✅ Заявка одобрена: {username}, доступ до {expires_at}\n{who}")

    try:
        await bot.send_message(
            account_id,
            f"🎁 Заявка одобрена! Пробный доступ на {TRIAL_DAYS} дня активирован. Доступ до {expires_at}.\n\n"
            f"👤 Username: {username}\n"
            f"🔑 Password: {password}\n"
            f"⏳ Expires: {expires_at}\n\n"
            "Об истечении напомню заранее, а продлить сможете прямо в этом чате.",
            reply_markup=client_menu,
        )
        await bot.send_message(
            account_id,
            "📖 Осталось настроить подключение — выберите вашу платформу:",
            reply_markup=platform_choice_kb(),
        )
        await bot.send_message(account_id, render_bot_usage_instructions())
    except Exception:
        log.exception("trial approved for %s but the client could not be messaged", account_id)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Аккаунт {username} создан, но написать клиенту не удалось (возможно, он заблокировал бота).",
        )

    await call.answer("Одобрено")
