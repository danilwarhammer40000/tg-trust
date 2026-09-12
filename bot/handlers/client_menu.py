"""
The client-facing informational buttons: status, "my connections", payment
info, and the instructions submenu (app install steps + RU-sites
routing/bypass lists). Also owns the rate-reconciliation flow for
clients who prepaid at a lower rate before getting extra links
("🔄 Пересчитать срок" / "💰 Доплатить сейчас", RateTopup FSM state) — see
core/payment.py's calc_rate_gap for why this exists at all.
"""
import html

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only, is_admin, notify_bg
from bot.config import ADMIN_ID, bot, DOMAIN
from bot.formatting import extract_qr_link, format_connection_message
from bot.keyboards import bot_usage_button_kb, client_menu, instructions_menu_kb, platform_choice_kb, routing_platform_kb
from bot.states import RateTopup
from core.auto_renewal import try_auto_verify_topup
from core.dates import is_expired, utcnow_naive
from core.db import get_followers, get_user, get_user_by_telegram_id, update_user
from core.generator import generate_link
from core.notify import log_to_channel
from follower_issuance import FREE_EXTRA_LINKS
from core.instructions import (
    ANDROID_BYPASS_DOMAINS,
    IOS_BYPASS_DOMAINS,
    ROUTING_INTRO,
    render_android_instructions,
    render_bot_usage_instructions,
    render_ios_instructions,
)
from core.payment import (
    ACCESS_EXPIRED_MESSAGE,
    PAYMENT_INFO,
    EXTRA_LINK_SURCHARGE,
    calc_monthly_price,
    calc_rate_gap,
    calc_recalculated_expiry,
    calc_topup_amount,
)

router = Router()


@router.message(F.text == "ℹ️ Мой статус")
async def client_status(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    username = user.get("username")

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await msg.answer(f"👤 {username}\n\n{ACCESS_EXPIRED_MESSAGE}")
        return

    expires_at = user.get("expires_at")
    status_line = "∞ бессрочно" if not expires_at else expires_at
    await msg.answer(f"👤 {username}\n⏳ Доступ до: {status_line}")


@router.message(F.text == "💳 Реквизиты для оплаты")
async def client_payment_info(msg: Message):
    if is_admin(msg.from_user.id):
        return

    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer(PAYMENT_INFO)
        return

    username = user["username"]
    price, surcharged = calc_monthly_price(username)

    if surcharged:
        word = "ссылка" if surcharged == 1 else "ссылки" if surcharged < 5 else "ссылок"
        note = (
            f"\n\nℹ️ У вас {surcharged} доп. {word} сверх бесплатного лимита "
            f"(+{surcharged * EXTRA_LINK_SURCHARGE}₽/мес). Со следующего продления "
            f"сумма к оплате — {price}₽/мес."
        )
        await msg.answer(PAYMENT_INFO + note)
    else:
        await msg.answer(PAYMENT_INFO)

    gap = calc_rate_gap(username)
    if gap:
        remaining_days, old_rate, new_rate = gap
        topup = calc_topup_amount(username)
        expires_at = user.get("expires_at")

        # Which button(s) to offer depends on how much paid time is left:
        # under a month, "Пересчитать" would shrink an already-short
        # period into something barely meaningful — only "Доплатить"
        # makes sense there. A month or more left, recalculating actually
        # produces a sensible new date, so that's the one offered instead.
        if remaining_days < 30:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Доплатить", callback_data="rate:topup:info")],
            ])
            await msg.answer(
                f"⚠️ У вас уже оплачено до {expires_at} по ставке {old_rate}₽/мес, "
                f"но сейчас ваша ставка — {new_rate}₽/мес (доп. ссылки сверх лимита). "
                f"Оставшегося оплаченного срока меньше месяца — доплатите разницу "
                f"(~{topup}₽), дата истечения при этом не изменится.\n\n"
                "⚠️ Новые доп. ссылки не будут выдаваться, пока эта доплата не закрыта.",
                reply_markup=kb
            )
        else:
            new_expiry = calc_recalculated_expiry(username)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Пересчитать моё подключение", callback_data="rate:recalc:info")],
            ])
            await msg.answer(
                f"⚠️ У вас уже оплачено до {expires_at} по ставке {old_rate}₽/мес, "
                f"но сейчас ваша ставка — {new_rate}₽/мес (доп. ссылки сверх лимита).\n\n"
                f"🔄 «Пересчитать моё подключение» — новая дата истечения станет "
                f"{new_expiry} (уже уплаченные деньги пересчитываются под новую "
                "ставку, без доплаты).\n\n"
                "Можно ничего не делать — тогда доплата спишется только на "
                "следующем продлении, а срок останется как есть.",
                reply_markup=kb
            )


@router.callback_query(F.data == "rate:recalc:info")
async def rate_recalc_info(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    new_expiry = calc_recalculated_expiry(user["username"])
    if not new_expiry:
        await call.answer("Пересчитывать уже нечего — ставка не менялась.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Да, укоротить до {new_expiry}", callback_data="rate:recalc:yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="rate:recalc:cancel")],
    ])
    await call.message.answer(
        f"Новая дата истечения после пересчёта: {new_expiry} (было {user.get('expires_at')}).\n"
        "Это НЕОБРАТИМО — после этого ставка станет текущей, и повторно "
        "пересчитать уже будет нечего. Подтверждаете?",
        reply_markup=kb
    )
    await call.answer()


@router.callback_query(F.data == "rate:recalc:cancel")
async def rate_recalc_cancel(call: CallbackQuery):
    await call.message.answer("Отменено, дата не изменена.", reply_markup=client_menu)
    await call.answer()


@router.callback_query(F.data == "rate:recalc:yes")
async def rate_recalc_confirm(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    username = user["username"]
    new_expiry = calc_recalculated_expiry(username)
    if not new_expiry:
        await call.answer("Пересчитывать уже нечего.", show_alert=True)
        return

    new_rate, _ = calc_monthly_price(username)
    update_user(username, expires_at=new_expiry)
    update_user(username, last_renewal_rate=new_rate)

    await call.message.answer(
        f"✅ Готово. Новая дата истечения: {new_expiry}.",
        reply_markup=client_menu
    )
    await notify_bg(
        log_to_channel,
        f"🔄 {username} самостоятельно пересчитал срок под новую ставку ({new_rate}₽/мес) — новая дата: {new_expiry}."
    )
    await call.answer()


@router.callback_query(F.data == "rate:topup:info")
async def rate_topup_info(call: CallbackQuery, state: FSMContext):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    topup = calc_topup_amount(user["username"])
    if not topup:
        await call.answer("Доплачивать уже нечего — ставка не менялась.", show_alert=True)
        return

    await state.set_state(RateTopup.waiting_receipt)
    await state.update_data(topup_username=user["username"], topup_amount=topup)

    await call.message.answer(
        f"💰 Чтобы сохранить текущую дату истечения без изменений, доплатите "
        f"{topup}₽ (по тем же реквизитам, что и обычно) и пришлите чек следующим "
        "сообщением сюда."
    )
    await call.answer()


@router.message(RateTopup.waiting_receipt, F.photo | F.document)
async def rate_topup_receipt(msg: Message, state: FSMContext):
    data = await state.get_data()
    username = data.get("topup_username")
    topup_amount = data.get("topup_amount")
    await state.clear()

    is_photo = bool(msg.photo)
    file_id = msg.photo[-1].file_id if is_photo else msg.document.file_id

    update_user(username, pending_request={
        "type": "rate_topup",
        "amount": topup_amount,
        "receipt_file_id": file_id,
        "receipt_is_photo": is_photo,
        "requested_at": utcnow_naive().isoformat(),
    })

    caption = (
        f"💰 Доплата ставки от {username}: ожидается ~{topup_amount}₽\n"
        "Дата истечения НЕ изменится — только фиксируется новая ставка как "
        "«уже оплаченная». Проверьте чек и подтвердите:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять доплату", callback_data=f"rtopup:{username}:approve")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rtopup:{username}:reject")],
    ])

    if is_photo:
        admin_msg = await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
    else:
        admin_msg = await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)

    await notify_bg(log_to_channel, caption)

    # Manual card above is sent unconditionally either way — this only
    # SOMETIMES beats the admin to it. If it applied the top-up, the
    # pending_request is already cleared, so a later tap on "✅ Принять
    # доплату" for the same request harmlessly no-ops via the "already
    # processed" check in rate_topup_review below — but edit the card's
    # buttons away too, so the admin isn't looking at stale actionable
    # buttons for a request that's already resolved.
    auto_applied = try_auto_verify_topup(username)

    if auto_applied:
        try:
            await admin_msg.edit_caption(caption=caption + "\n\n✅ Подтверждено автоматически (Gemini).")
        except Exception:
            pass
        await msg.answer(
            "✅ Доплата принята автоматически — дата истечения не изменилась, "
            "ставка обновлена.",
            reply_markup=client_menu
        )
    else:
        await msg.answer("✅ Отправлено администратору на проверку.", reply_markup=client_menu)


@router.message(RateTopup.waiting_receipt)
async def rate_topup_wrong_content(msg: Message):
    await msg.answer("Пришлите, пожалуйста, именно фото или файл чека.")


@router.callback_query(F.data.startswith("rtopup:"))
async def rate_topup_review(call: CallbackQuery):
    if not await admin_only(call):
        return

    _, username, action = call.data.split(":")
    user = get_user(username)

    pending = (user or {}).get("pending_request") or {}
    if not user or pending.get("type") != "rate_topup":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return

    update_user(username, pending_request=None)

    if action == "reject":
        try:
            await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ Отклонено")
        except Exception:
            pass
        if user.get("telegram_id"):
            await bot.send_message(
                user["telegram_id"],
                "❌ Доплата не подтверждена администратором. Свяжитесь для уточнения."
            )
        await call.answer("Отклонено")
        return

    new_rate, _ = calc_monthly_price(username)
    update_user(username, last_renewal_rate=new_rate)

    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + f"\n\n✅ Принято, ставка обновлена до {new_rate}₽/мес")
    except Exception:
        pass

    if user.get("telegram_id"):
        await bot.send_message(
            user["telegram_id"],
            f"✅ Доплата принята. Дата истечения не изменилась ({user.get('expires_at')}), "
            f"ставка зафиксирована как оплаченная: {new_rate}₽/мес."
        )

    await notify_bg(log_to_channel, f"✅ Доплата ставки для {username} принята, ставка обновлена до {new_rate}₽/мес (дата не менялась).")
    await call.answer("Готово")


# ---------------- MY CONNECTIONS ----------------
#
# Listens for both the current label ("🔗 Мои подключения") and the old one
# ("🔗 Моя ссылка") — a client's Telegram app may have the old label cached
# on its reply keyboard until it re-renders, and both must keep working.
#
# Deliberately never sends a connection card straight away, even when the
# client only has one account (their own): the link is only generated
# (generate_link() shells out to the trusttunnel binary — not free) once
# they actually tap a specific connection. This message is always just a
# picker.
#
# A client normally has exactly one connection (their own account). If an
# admin issued extra device-links for them (handlers/leader_link.py's
# "➕ Выпустить нового ведомого") or they requested some themselves
# (handlers/extra_links.py) and got approved, they become a "leader" of
# their own "-2"/"-3"/... sub-accounts (see follower_issuance.py) and see
# more than one button here — the header spells out how many of those are
# extra (this mirrors the admin-side "👑 Доп. ссылок выпущено: N" counter
# in handlers/list_users.py's user_actions_menu()).

@router.message(F.text.in_({"🔗 Мои подключения", "🔗 Моя ссылка"}))
async def client_my_link(msg: Message):
    user = get_user_by_telegram_id(msg.from_user.id)
    if not user:
        await msg.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    if user.get("status") != "active" or is_expired(user.get("expires_at")):
        await msg.answer(ACCESS_EXPIRED_MESSAGE)
        return

    username = user.get("username")
    followers = get_followers(username)
    accounts = [user] + followers

    rows = [
        [InlineKeyboardButton(text=f"🔌 {a.get('username')}", callback_data=f"myconn:{a.get('username')}")]
        for a in accounts if a.get("username")
    ]
    rows.append([InlineKeyboardButton(text="📱 Подключить ещё устройства", callback_data="extralinks:start")])

    free_left = max(0, FREE_EXTRA_LINKS - len(followers))
    free_note = (
        f"\n\n🆓 Дополнительно вы можете бесплатно подключить ещё "
        f"{free_left * 2} устройства ({free_left} {'ссылка' if free_left == 1 else 'ссылки'}) — "
        f"автоматически, без подтверждения администратора."
    ) if free_left else ""

    if followers:
        header = f"Ваши подключения: {len(accounts)} всего (основное + {len(followers)} доп.).{free_note}"
    else:
        header = f"Ваши подключения: 1.\n\nℹ️ Одна ссылка подключает до 2 устройств одновременно.{free_note}"

    await msg.answer(
        f"{header}\n\nНажмите на нужное, чтобы получить карточку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("myconn:"))
async def client_my_connection_card(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    if not user:
        await call.answer("Не удалось определить ваш аккаунт.", show_alert=True)
        return

    own_username = user.get("username")
    target_username = call.data.split(":", 1)[1]

    # SECURITY: only the caller's own account or their own followers are
    # reachable here — target_username comes from callback_data, which a
    # client could in principle tamper with, so this is re-checked
    # server-side rather than trusting that the button they were shown was
    # the only one they could tap.
    allowed = {own_username} | {f.get("username") for f in get_followers(own_username)}
    if target_username not in allowed:
        await call.answer("Доступ запрещён.", show_alert=True)
        return

    target = get_user(target_username)
    if not target:
        await call.answer("Не найдено.", show_alert=True)
        return

    link = generate_link(target_username, DOMAIN)
    await call.message.answer(
        format_connection_message(target_username, target.get("password"), target.get("expires_at"), link)
    )
    await call.answer()


# ---------------- INSTRUCTIONS MENU ----------------

@router.message(F.text == "📖 Инструкции")
async def client_instructions_menu(msg: Message):
    if is_admin(msg.from_user.id):
        return
    await msg.answer("Что показать?", reply_markup=instructions_menu_kb())


@router.callback_query(F.data == "instr:connect")
async def instructions_connect(call: CallbackQuery):
    await call.message.answer("Выберите вашу платформу:", reply_markup=platform_choice_kb())
    await call.answer()


@router.callback_query(F.data == "instr:routing")
async def instructions_routing(call: CallbackQuery):
    await call.message.answer(ROUTING_INTRO, reply_markup=routing_platform_kb())
    await call.answer()


@router.callback_query(F.data == "instr:bot_usage")
async def instructions_bot_usage(call: CallbackQuery):
    await call.message.answer(render_bot_usage_instructions())
    await call.answer()


@router.callback_query(F.data == "route:android")
async def routing_list_android(call: CallbackQuery):
    # Wrapped in <pre> so Telegram renders it as a monospace block with a
    # tap-to-copy affordance — much easier to grab the whole list on mobile
    # than plain text.
    await call.message.answer(f"<pre>{html.escape(ANDROID_BYPASS_DOMAINS)}</pre>", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "route:ios")
async def routing_list_ios(call: CallbackQuery):
    await call.message.answer(f"<pre>{html.escape(IOS_BYPASS_DOMAINS)}</pre>", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "howto:ios")
async def howto_ios(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await call.message.answer(render_ios_instructions(link), reply_markup=bot_usage_button_kb())
    await call.answer()


@router.callback_query(F.data == "howto:android")
async def howto_android(call: CallbackQuery):
    user = get_user_by_telegram_id(call.from_user.id)
    link = extract_qr_link(generate_link(user["username"], DOMAIN)) if user and user.get("username") else None

    await call.message.answer(render_android_instructions(link), reply_markup=bot_usage_button_kb())
    await call.answer()
