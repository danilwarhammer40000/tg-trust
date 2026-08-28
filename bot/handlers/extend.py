"""
Owns: ExtendUser.manual. (ExtendUser.mode is set by handlers/list_users.py's
action_extend but has no dedicated message/callback handler filtering on it
by name — the "ext:*" callback below matches globally, since at that point
in the flow the only thing the admin can do is tap one of the extend
buttons.)
"""
from datetime import datetime

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.access import admin_only, run_sync
from bot.config import bot
from bot.states import ExtendUser
from core.dates import calc_new_expiry, calc_new_expiry_months, is_expired
from core.db import get_user, update_user

router = Router()


@router.callback_query(F.data.startswith("ext:"))
async def extend_handler(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    username = data.get("username")

    mode = call.data.split(":")[1]

    user = get_user(username)
    if not user:
        await call.message.answer("User not found")
        return

    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))
    new_expires_at = user.get("expires_at")

    # Every branch below is a real, admin-verified renewal -- this is
    # exactly the kind of human check that resets the auto-renewal
    # anti-abuse lock (auto_renewal_applied) for the next cycle, see
    # core/auto_renewal.py. Each branch is TWO update_user() calls, not
    # one: core.db.update_user() redirects expires_at/status onto the
    # leader (and fans out to the group) whenever they're in the kwargs —
    # bundling notified_days/auto_renewal_applied into that same call
    # would misroute them onto the leader for a follower account instead
    # of staying on `username` itself.
    if mode == "0":
        update_user(username, expires_at=None, status="active")
        update_user(username, notified_days=[], post_disable_notified=[], auto_renewal_applied=False, auto_renewal_applied_at=None)
        new_expires_at = None

    elif mode == "3":
        new_expires_at = calc_new_expiry(user.get("expires_at"), 3)
        update_user(username, expires_at=new_expires_at, status="active")
        update_user(username, notified_days=[], post_disable_notified=[], auto_renewal_applied=False, auto_renewal_applied_at=None)

    elif mode == "30":
        # "1 месяц" must be a calendar month (same day next month), not a
        # flat +30 days — otherwise short months quietly shift the renewal
        # date earlier every cycle.
        new_expires_at = calc_new_expiry_months(user.get("expires_at"), 1)
        update_user(username, expires_at=new_expires_at, status="active")
        update_user(username, notified_days=[], post_disable_notified=[], auto_renewal_applied=False, auto_renewal_applied_at=None)

    elif mode == "manual":
        await state.set_state(ExtendUser.manual)
        await call.message.answer("Send date YYYY-MM-DD")
        await call.answer()
        return

    if was_expired_or_inactive:
        await run_sync()

    if user.get("telegram_id"):
        expiry_line = "бессрочно" if not new_expires_at else new_expires_at
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена. Доступ действует до: {expiry_line}"
        )

    await state.clear()

    expiry_label = "бессрочно" if not new_expires_at else new_expires_at
    await call.message.answer(f"✅ {username}: обновлено, доступ до {expiry_label}")
    await call.answer()


@router.message(ExtendUser.manual)
async def manual_date(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    data = await state.get_data()
    username = data.get("username")

    try:
        datetime.strptime(msg.text.strip(), "%Y-%m-%d")
    except ValueError:
        # BUGFIX (kept from the original): was a bare `except:`, which also
        # swallows KeyboardInterrupt/SystemExit/genuine bugs, not just a bad
        # date string.
        await msg.answer("Wrong format YYYY-MM-DD")
        return

    user = get_user(username) or {}
    was_expired_or_inactive = user.get("status") != "active" or is_expired(user.get("expires_at"))
    new_expires_at = msg.text.strip()

    update_user(username, expires_at=new_expires_at, status="active")
    update_user(
        username,
        notified_days=[],
        post_disable_notified=[],
        auto_renewal_applied=False,
        auto_renewal_applied_at=None,
    )

    if was_expired_or_inactive:
        await run_sync()

    if user.get("telegram_id"):
        await bot.send_message(
            user["telegram_id"],
            f"✅ Ваша подписка продлена. Доступ действует до: {new_expires_at}"
        )

    await state.clear()
    await msg.answer(f"✅ {username}: обновлено, доступ до {new_expires_at}")
