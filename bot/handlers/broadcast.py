"""
Owns: AdminMessage.broadcast, AdminMessage.broadcast_confirm,
AdminMessage.select_recipients, AdminMessage.selective_text,
AdminMessage.selective_confirm.
"""
import asyncio
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.access import admin_only
from bot.config import bot
from bot.display import prepare_users_for_display
from bot.keyboards import cancel_kb, main_menu
from bot.pagination import paginate, pagination_nav_row
from bot.states import AdminMessage
from core.db import get_user, list_users

router = Router()
log = logging.getLogger(__name__)


def recipient_button_label(u: dict, selected: set) -> str:
    username = u.get("username", "?")
    expires_at = u.get("expires_at")
    mark = "☑️" if username in selected else "⬜"
    return f"{mark} {username} ({expires_at or '∞'})"


def build_recipient_kb(users: list, selected: set, page: int) -> tuple:
    page_users, total_pages, page = paginate(users, page)

    rows = [
        [InlineKeyboardButton(
            text=recipient_button_label(u, selected),
            callback_data=f"selrecipient:{u['username']}"
        )]
        for u in page_users if u.get("username")
    ]
    rows += pagination_nav_row(page, total_pages, "selrecipientpage")
    rows.append([
        InlineKeyboardButton(text=f"✅ Готово ({len(selected)})", callback_data="selrecipients:done"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="selrecipients:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows), total_pages, page


def recipient_picker_label(total: int, page: int, total_pages: int) -> str:
    suffix = f", стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    return f"Выберите получателей ({total} всего{suffix}), тап переключает ☑️/⬜, затем «Готово»:"


def format_send_report(sent: int, failures: list) -> str:
    """failures: list of (username, reason) tuples."""
    report = f"✅ Отправлено: {sent}\n❌ Ошибок: {len(failures)}"

    if failures:
        MAX_SHOWN = 20
        lines = [f"• {name}: {reason}" for name, reason in failures[:MAX_SHOWN]]
        report += "\n\n" + "\n".join(lines)

        remaining = len(failures) - MAX_SHOWN
        if remaining > 0:
            report += f"\n… и ещё {remaining}"

    return report


def failure_reason(user) -> str:
    if not user:
        return "пользователь не найден"
    if not user.get("telegram_id"):
        return "нет привязанного Telegram"
    return "ошибка отправки"


async def run_binding_check() -> str:
    users = [u for u in (list_users() or []) if u.get("telegram_id")]
    if not users:
        return "Нет пользователей с привязанным Telegram."

    broken = []

    for u in users:
        try:
            # send_chat_action ("typing…") is gated by the same "bot can't
            # initiate conversation" restriction as sendMessage, but leaves
            # no message in the chat history and sends no push notification —
            # unlike get_chat(), which succeeds even without a real chat.
            await bot.send_chat_action(u["telegram_id"], "typing")
        except Exception as e:
            broken.append((u.get("username", "?"), str(e)))
        await asyncio.sleep(0.05)

    if not broken:
        return f"✅ Все {len(users)} привязок рабочие."

    MAX_SHOWN = 20
    lines = [f"• {name}: {reason}" for name, reason in broken[:MAX_SHOWN]]
    report = f"⚠️ Проблемных привязок: {len(broken)} из {len(users)}\n\n" + "\n".join(lines)

    remaining = len(broken) - MAX_SHOWN
    if remaining > 0:
        report += f"\n… и ещё {remaining}"

    report += "\n\nЭтих клиентов нужно попросить активировать бота (открыть чат и написать хоть что-нибудь)."

    return report


@router.message(F.text == "📢 Рассылка")
async def broadcast_menu(msg: Message):
    if not await admin_only(msg):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Всем", callback_data="bcast_mode:all")],
        [InlineKeyboardButton(text="🎯 Выбрать получателей", callback_data="bcast_mode:select")],
        [InlineKeyboardButton(text="🔍 Проверить привязки", callback_data="bcast_mode:check")],
    ])
    await msg.answer("Кому отправить рассылку?", reply_markup=kb)


@router.callback_query(F.data == "bcast_mode:check")
async def broadcast_mode_check(call: CallbackQuery):
    if not await admin_only(call):
        return

    await call.message.answer("🔄 Проверяю привязки (без отправки сообщений)...")
    await call.answer()

    report = await run_binding_check()
    await call.message.answer(report, reply_markup=main_menu)


@router.callback_query(F.data == "bcast_mode:all")
async def broadcast_mode_all(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.set_state(AdminMessage.broadcast)
    await call.message.answer(
        "Введите текст рассылки — уйдёт всем клиентам с привязанным Telegram:",
        reply_markup=cancel_kb
    )
    await call.answer()


@router.callback_query(F.data == "bcast_mode:select")
async def broadcast_mode_select(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("telegram_id") and u.get("username")])
    if not users:
        await call.message.answer("Нет клиентов с привязанным Telegram.")
        await call.answer()
        return

    await state.set_state(AdminMessage.select_recipients)
    await state.update_data(selected=[], page=0)

    kb, total_pages, page = build_recipient_kb(users, set(), 0)
    await call.message.answer(recipient_picker_label(len(users), page, total_pages), reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("selrecipientpage:"), AdminMessage.select_recipients)
async def recipient_picker_page(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    page = int(call.data.split(":", 1)[1])
    await state.update_data(page=page)

    data = await state.get_data()
    selected = set(data.get("selected", []))
    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("telegram_id") and u.get("username")])

    kb, total_pages, page = build_recipient_kb(users, selected, page)
    try:
        await call.message.edit_text(recipient_picker_label(len(users), page, total_pages), reply_markup=kb)
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("selrecipient:"), AdminMessage.select_recipients)
async def toggle_recipient(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    username = call.data.split(":", 1)[1]

    data = await state.get_data()
    selected = set(data.get("selected", []))
    page = data.get("page", 0)

    if username in selected:
        selected.discard(username)
    else:
        selected.add(username)

    await state.update_data(selected=list(selected))

    users = prepare_users_for_display([u for u in (list_users() or []) if u.get("telegram_id") and u.get("username")])

    try:
        kb, total_pages, page = build_recipient_kb(users, selected, page)
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass  # "message is not modified" if tapped same state twice quickly — harmless

    await call.answer()


@router.callback_query(F.data == "selrecipients:cancel", AdminMessage.select_recipients)
async def cancel_recipient_selection(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    await state.clear()
    await call.message.answer("Отменено.", reply_markup=main_menu)
    await call.answer()


@router.callback_query(F.data == "selrecipients:done", AdminMessage.select_recipients)
async def confirm_recipient_selection(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    data = await state.get_data()
    selected = data.get("selected", [])

    if not selected:
        await call.answer("Выберите хотя бы одного получателя", show_alert=True)
        return

    await state.set_state(AdminMessage.selective_text)

    names = ", ".join(selected)
    await call.message.answer(
        f"Получатели ({len(selected)}): {names}\n\nВведите сообщение для них:",
        reply_markup=cancel_kb
    )
    await call.answer()


@router.message(AdminMessage.selective_text)
async def selective_message_preview(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    if not msg.text:
        await msg.answer("Пришлите текстовое сообщение.")
        return

    data = await state.get_data()
    selected = data.get("selected", [])

    await state.update_data(text=msg.text)
    await state.set_state(AdminMessage.selective_confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить ({len(selected)} чел.)", callback_data="selective:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="selective:cancel")],
    ])

    names = ", ".join(selected)
    await msg.answer(
        f"Получатели ({len(selected)}): {names}\n\nТекст сообщения:\n\n{msg.text}\n\nОтправляем?",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("selective:"), AdminMessage.selective_confirm)
async def selective_message_confirm(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = data.get("selected", [])
    text = data.get("text")
    await state.clear()

    if action == "cancel":
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    sent = 0
    failures = []  # (username, reason)

    for username in selected:
        user = get_user(username)
        if user and user.get("telegram_id"):
            try:
                await bot.send_message(user["telegram_id"], text)
                sent += 1
            except Exception as e:
                log.warning("selective broadcast failed for %s: %s", username, e)
                failures.append((username, str(e)))
        else:
            failures.append((username, failure_reason(user)))
        await asyncio.sleep(0.05)

    await call.message.answer(format_send_report(sent, failures), reply_markup=main_menu)
    await call.answer()


@router.message(AdminMessage.broadcast)
async def broadcast_preview(msg: Message, state: FSMContext):
    if not await admin_only(msg):
        return

    await state.update_data(broadcast_text=msg.text)
    await state.set_state(AdminMessage.broadcast_confirm)

    users = list_users() or []
    recipients = [u for u in users if u.get("telegram_id")]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Отправить ({len(recipients)} чел.)", callback_data="bcast:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bcast:cancel")]
    ])

    await msg.answer(
        f"Получателей: {len(recipients)}\n\nТекст:\n{msg.text}\n\nОтправляем?",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("bcast:"), AdminMessage.broadcast_confirm)
async def broadcast_confirm(call: CallbackQuery, state: FSMContext):
    if not await admin_only(call):
        return

    action = call.data.split(":")[1]

    if action == "cancel":
        await state.clear()
        await call.message.answer("Отменено.", reply_markup=main_menu)
        await call.answer()
        return

    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()

    users = list_users() or []
    recipients = [u for u in users if u.get("telegram_id")]

    sent = 0
    failures = []  # (username, reason)

    await call.message.answer(f"🔄 Отправка {len(recipients)} сообщениям...")

    for u in recipients:
        try:
            await bot.send_message(u["telegram_id"], text)
            sent += 1
        except Exception as e:
            log.warning("broadcast failed for %s: %s", u.get("username"), e)
            failures.append((u.get("username", "?"), str(e)))
        await asyncio.sleep(0.05)  # мягкий троттлинг, чтобы не упереться в лимиты Telegram

    await call.message.answer(format_send_report(sent, failures), reply_markup=main_menu)
    await call.answer()
