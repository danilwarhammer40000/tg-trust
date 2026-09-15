"""
Owns: the MAX-side "📱 Мои устройства" / extra-devices self-service flow
— the MAX equivalent of bot/handlers/extra_links.py. Same underlying
rules (follower_issuance.FREE_EXTRA_LINKS, core.payment.EXTRA_LINK_SURCHARGE,
SHORT_REMAINING_THRESHOLD_DAYS, calc_monthly_price_with_additional) —
this file only builds the MAX-side UI/wiring around them; the actual
pricing/eligibility logic is shared and platform-agnostic.

DESIGN — no FSM (see max_bot/handlers/receipt.py's module docstring for
the general reasoning): "waiting for a receipt for this specific
extra-devices request" is tracked the same way it already is for
long-lived state on this platform — the pending_request field itself.
Once extra_devices_pick sets pending_request={"type": "extra_links", ...}
with no receipt attached yet, the NEXT attachment this client sends is
routed here (not treated as a generic "is this a receipt?" prompt) by
max_bot/handlers/receipt.py's any_media_received.

Approval is unified with the Telegram side exactly like renewal receipts
already are: the admin fallback card uses the same "exlreview:{username}:..."
callback prefix bot/handlers/extra_links.py's extra_links_review already
handles — admin only ever operates from Telegram, regardless of which
bot the client used.
"""
import logging

import requests

from maxapi import Router, F
from maxapi.types import CallbackButton, MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from core.auto_renewal import try_auto_verify_extra_links_payment
from core.dates import parse_expiry, utcnow_naive
from core.db import get_followers, get_user, get_user_by_max_chat_id, update_user
from core.notify import notify_admin, send_photo_bytes
from core.payment import (
    EXTRA_LINK_SURCHARGE,
    PAYMENT_LINK,
    PAYMENT_RECIPIENT,
    SHORT_REMAINING_THRESHOLD_DAYS,
    calc_monthly_price,
    calc_monthly_price_with_additional,
)
from follower_issuance import FREE_EXTRA_LINKS, build_connection_card, issue_follower, leader_is_active
from max_bot.handlers.start import _extract_chat_id

router = Router()
log = logging.getLogger(__name__)

MAX_EXTRA_LINKS = 4


def _free_remaining(username: str, followers: list = None) -> int:
    if followers is None:
        followers = get_followers(username)
    return max(0, FREE_EXTRA_LINKS - len(followers))


def _remaining_days(user: dict):
    """None if there's no valid expiry to compute from — routes to the
    normal payment flow rather than the trial-consent one (see
    bot/handlers/extra_links.py's _remaining_days for the same choice)."""
    expires_at = user.get("expires_at")
    if not expires_at:
        return None
    dt = parse_expiry(expires_at)
    if not dt:
        return None
    return (dt.date() - utcnow_naive().date()).days


def _issue_now_sync(username: str, count: int, followers_snapshot: list) -> list:
    """
    Synchronous issuance — mirrors bot/handlers/extra_links.py's
    _issue_now() and core/auto_renewal.py's _issue_followers_sync(), but
    called from a plain (non-FSM) MAX callback handler rather than an
    aiogram one: uses core.service.safe_sync() directly since there's no
    bot.access.run_sync() equivalent to await here.
    """
    from core.service import safe_sync

    was_active = leader_is_active(get_user(username))

    created_usernames = []
    for _ in range(count):
        new_username = issue_follower(username, existing_followers=followers_snapshot)
        if not new_username:
            break
        created_usernames.append(new_username)
        followers_snapshot = followers_snapshot + [{"username": new_username}]

    if was_active and created_usernames:
        safe_sync()

    return [(u, build_connection_card(u)) for u in created_usernames]


def _device_button_label(n: int, free_left: int) -> str:
    """Same relative numbering as bot/handlers/extra_links.py's
    _device_button_label — "how many NEW devices this choice adds",
    never an absolute count (see that function's docstring for why an
    absolute-numbering version was tried and reverted)."""
    device_range = f"{n * 2 - 1}-{n * 2}"
    paid_units = max(0, n - free_left)
    if paid_units == 0:
        return device_range
    return f"{device_range} +{paid_units * EXTRA_LINK_SURCHARGE}₽"


@router.message_created(F.message.body.text == "/devices")
async def my_devices(event: MessageCreated):
    chat_id = _extract_chat_id(event)
    user = get_user_by_max_chat_id(chat_id)
    if not user:
        await event.message.answer("Вы ещё не привязаны. Пришлите вашу карточку подключения (Username/Password).")
        return

    username = user["username"]
    followers = get_followers(username)
    total_links = len(followers) + 1
    total_devices = total_links * 2
    link_word = "ссылку" if total_links == 1 else "ссылки" if total_links < 5 else "ссылок"

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="➕ Подключить ещё устройства", payload="extradev_start"))

    await event.message.answer(
        text=(
            f"📱 Ваши подключения: {total_links} {link_word} ({total_devices} устройств).\n\n"
            "Используйте /link, чтобы получить карточку основного подключения."
        ),
        attachments=[builder.as_markup()],
    )


@router.message_callback(F.callback.payload == "extradev_start")
async def extra_devices_start(event: MessageCallback):
    chat_id = event.message.recipient.chat_id
    user = get_user_by_max_chat_id(chat_id)
    if not user:
        await event.message.answer("Не удалось определить ваш аккаунт.")
        return

    username = user["username"]
    if user.get("pending_request"):
        await event.message.answer(
            "У вас уже есть незавершённый запрос — дождитесь его завершения, прежде чем начинать новый."
        )
        return

    followers = get_followers(username)
    free_left = _free_remaining(username, followers)
    total_links = len(followers) + 1
    total_devices = total_links * 2
    link_word = "ссылку" if total_links == 1 else "ссылки" if total_links < 5 else "ссылок"

    builder = InlineKeyboardBuilder()
    for n in range(1, MAX_EXTRA_LINKS + 1):
        builder.row(CallbackButton(text=_device_button_label(n, free_left), payload=f"extradev_req:{n}"))

    parts = [
        f"Вы уже получили {total_links} {link_word} на {total_devices} устройств "
        "(включая основное подключение).",
        "",
        "Выберите, сколько устройств хотите подключить дополнительно.",
        "На каждую ссылку подключаются два устройства.",
    ]
    if free_left:
        parts.append(f"\n🆓 Бесплатно доступно ещё {free_left * 2} устройства (сразу, автоматически).")
    parts.append(f"\n— {EXTRA_LINK_SURCHARGE}₽/мес за два устройства (1 доп. ссылка) сверх бесплатного лимита.")

    await event.message.answer(text="\n".join(parts), attachments=[builder.as_markup()])


@router.message_callback(F.callback.payload.startswith("extradev_req:"))
async def extra_devices_pick(event: MessageCallback):
    chat_id = event.message.recipient.chat_id
    user = get_user_by_max_chat_id(chat_id)
    if not user:
        await event.message.answer("Не удалось определить ваш аккаунт.")
        return

    count = int(event.callback.payload.split(":", 1)[1])
    username = user["username"]

    if user.get("pending_request"):
        await event.message.answer("У вас уже есть незавершённый запрос.")
        return

    followers = get_followers(username)
    free_count = min(count, _free_remaining(username, followers))
    paid_count = count - free_count

    reply_lines = []

    # --- FREE TIER: issued immediately, no payment ---
    if free_count:
        created = _issue_now_sync(username, free_count, followers)
        for _, card in created:
            await event.message.answer(card)

        if created:
            reply_lines.append(f"🆓 Бесплатно подключено: {len(created) * 2} устройства.")
            notify_admin(f"🆓 [MAX] Автовыдача бесплатных доп. устройств для {username}: {len(created)} ссылок.")
        followers = followers + [{"username": u} for u, _ in created]

    if not paid_count:
        if reply_lines:
            await event.message.answer("\n".join(reply_lines))
        return

    amount_due = paid_count * EXTRA_LINK_SURCHARGE
    new_total = calc_monthly_price_with_additional(username, paid_count)
    total_devices = (len(followers) + 1 + paid_count) * 2

    remaining_days = _remaining_days(user)

    # --- SHORT REMAINING TIME (trial, basically): no payment collected
    # upfront — same reasoning as bot/handlers/extra_links.py. ---
    if remaining_days is not None and remaining_days <= SHORT_REMAINING_THRESHOLD_DAYS:
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="✅ Согласен", payload=f"extradev_trialok:{paid_count}"))
        builder.row(CallbackButton(text="❌ Отмена", payload="extradev_trialcancel"))
        await event.message.answer(
            text=(
                f"⏳ У вас осталось {remaining_days} дн. — для {paid_count * 2} доп. устройств "
                f"ставка увеличится на {amount_due}₽/мес (итого будет {new_total}₽/мес). "
                "Устройства подключаются сразу, без оплаты сейчас — новая ставка "
                "начнёт действовать со следующего продления. Согласны?"
            ),
            attachments=[builder.as_markup()],
        )
        if reply_lines:
            await event.message.answer("\n".join(reply_lines))
        return

    # --- NORMAL PAYMENT FLOW: pay the marginal cost now, receipt
    # auto-checked (see max_bot/handlers/receipt.py), then issued. Rate
    # only — no date change (same as the Telegram side). ---
    update_user(username, pending_request={
        "type": "extra_links",
        "count": paid_count,
        "amount_due": amount_due,
        "requested_at": utcnow_naive().isoformat(),
        "source": "max",
    })

    await event.message.answer(
        f"💰 Ещё {paid_count * 2} устройства — это {amount_due}₽ к вашей основной "
        f"подписке (итого в месяц получится {new_total}₽ за {total_devices} устройств).\n\n"
        f"Переведите по ссылке ниже ещё {amount_due}₽\n\n"
        "Чек или скрин для проверки отправьте прямо сюда в чат ✅.\n\n"
        f"{PAYMENT_LINK}\n{PAYMENT_RECIPIENT}"
    )
    if reply_lines:
        await event.message.answer("\n".join(reply_lines))


@router.message_callback(F.callback.payload.startswith("extradev_trialok:"))
async def extra_devices_trial_confirm(event: MessageCallback):
    chat_id = event.message.recipient.chat_id
    user = get_user_by_max_chat_id(chat_id)
    if not user:
        await event.message.answer("Не удалось определить ваш аккаунт.")
        return

    paid_count = int(event.callback.payload.split(":", 1)[1])
    username = user["username"]

    created = _issue_now_sync(username, paid_count, get_followers(username))
    for _, card in created:
        await event.message.answer(card)

    if created:
        new_rate, _ = calc_monthly_price(username)
        update_user(username, last_renewal_rate=new_rate)
        await event.message.answer(
            f"✅ Подключено. Ваша ставка теперь {new_rate}₽/мес — начнёт списываться "
            "со следующего продления."
        )
        notify_admin(
            f"✅ [MAX] {username} (короткий остаток срока) согласился на новую ставку "
            f"{new_rate}₽/мес и получил {len(created)} доп. ссылок автоматически, без оплаты сейчас."
        )
    else:
        await event.message.answer("Не получилось подключить — попробуйте ещё раз или напишите администратору.")


@router.message_callback(F.callback.payload == "extradev_trialcancel")
async def extra_devices_trial_cancel(event: MessageCallback):
    await event.message.answer("Хорошо, ничего не подключаю.")


async def handle_extra_links_receipt(event: MessageCreated, user: dict, pending: dict, url: str) -> None:
    """
    Called from max_bot/handlers/receipt.py's any_media_received when the
    client already has a pending_request of type "extra_links" awaiting
    a receipt — this attachment IS that receipt, no "is this a receipt?"
    confirmation needed (unlike a generic renewal receipt, we already
    know exactly what this is for).
    """
    username = user["username"]

    update_user(username, pending_request={**pending, "receipt_url": url, "source": "max"})

    auto_applied = try_auto_verify_extra_links_payment(username)

    if auto_applied:
        await event.message.answer("✅ Оплата подтверждена, устройства подключены — карточки выше.")
        return

    count = pending.get("count", 1)
    amount_due = pending.get("amount_due")

    try:
        photo_bytes = requests.get(url, timeout=15).content
    except requests.RequestException as e:
        log.error("failed to download MAX extra-devices receipt for %s: %s", username, e)
        await event.message.answer("⚠️ Не удалось переслать чек администратору, попробуйте ещё раз.")
        return

    caption = (
        f"💰 [MAX] Оплата доп. устройств от {username}\n"
        f"{count * 2} устройства, ожидается ~{amount_due}₽ (доплата к текущей ставке)\n\n"
        f"«✅ Выдать» — подтвердить оплату и выдать.\n"
        f"«💚 Без наценки» — выдать бесплатно, без учёта наценки.\n"
        f"«❌ Отклонить» — не выдавать."
    )
    kb = {
        "inline_keyboard": [
            [{"text": f"✅ Выдать {count}", "callback_data": f"exlreview:{username}:approve"}],
            [{"text": "💚 Без наценки", "callback_data": f"exlreview:{username}:approve_free"}],
            [{"text": "❌ Отклонить", "callback_data": f"exlreview:{username}:reject"}],
        ]
    }
    sent = send_photo_bytes(photo_bytes, filename="max_extra_devices_receipt.jpg", caption=caption, reply_markup=kb)

    if sent:
        await event.message.answer("✅ Чек отправлен администратору на проверку.")
    else:
        await event.message.answer("⚠️ Не удалось переслать чек администратору, попробуйте ещё раз.")
        notify_admin(f"⚠️ Не удалось переслать MAX-чек (доп. устройства) от {username} — проверьте вручную.")
