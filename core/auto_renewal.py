"""
AI-assisted auto-renewal: settings, trigger conditions, business-rule
evaluation, and the pipeline that ties Gemini extraction + core.db + the
Telegram log channel + admin review together.

Two independent triggers call process_pending_request_with_ai() (always
via core.db.claim_pending_request_for_ai() first, to prevent double
processing):
  1. Real-time, from bot/handlers/receipt.py and feedback.py, when a
     receipt arrives while auto-renewal is ON and it's currently within
     the night window (22:00-06:00 Krasnoyarsk).
  2. services/auto_renewal_overdue_check.py's periodic timer, for any
     receipt still unprocessed by the admin 3+ hours after submission —
     independent of time of day, but still gated on the master ON/OFF
     toggle (see is_overdue_trigger_active()).

Everything here is synchronous (plain function calls, no async/await) so
it works identically called from an aiogram handler via
loop.run_in_executor(...) and from the standalone periodic script, which
has no event loop at all.
"""
import json
import logging
import os
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from core.dates import calc_new_expiry_months, is_expired, parse_expiry, utcnow_naive
from core.db import get_user, update_user
from core.notify import get_file_bytes, log_to_channel, notify_admin, notify_user, send_photo_by_file_id
from core.paths import AUTO_RENEWAL_SETTINGS_PATH
from core.service import safe_sync

log = logging.getLogger(__name__)

KRASNOYARSK_TZ = ZoneInfo("Asia/Krasnoyarsk")
NIGHT_START = dt_time(22, 0)
NIGHT_END = dt_time(6, 0)

OVERDUE_THRESHOLD = timedelta(hours=3)

MIN_AMOUNT_RUB = 100
MIN_CONFIDENCE = 0.75
PAYMENT_DATE_TOLERANCE_DAYS = 1  # accept "today" or "yesterday" (Krasnoyarsk-local)

DEFAULT_SETTINGS = {"enabled": False}


# ---------------- SETTINGS ----------------

def _load_settings() -> dict:
    try:
        with open(AUTO_RENEWAL_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULT_SETTINGS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def _save_settings(settings: dict) -> None:
    os.makedirs(os.path.dirname(AUTO_RENEWAL_SETTINGS_PATH), exist_ok=True)
    with open(AUTO_RENEWAL_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f)


def is_auto_renewal_enabled() -> bool:
    return _load_settings().get("enabled", False)


def set_auto_renewal_enabled(value: bool) -> None:
    settings = _load_settings()
    settings["enabled"] = value
    _save_settings(settings)


def toggle_auto_renewal() -> bool:
    """Flips the setting and returns the new value. Callers (the bot
    handler) are responsible for checking log_channel_configured() first —
    this function doesn't refuse to turn on without a log channel, so it
    stays usable from a script/console too."""
    new_value = not is_auto_renewal_enabled()
    set_auto_renewal_enabled(new_value)
    return new_value


def log_channel_configured() -> bool:
    from core.notify import LOG_CHANNEL_ID
    return bool(LOG_CHANNEL_ID)


# ---------------- TIME WINDOW ----------------

def krasnoyarsk_now() -> datetime:
    return datetime.now(KRASNOYARSK_TZ)


def is_in_night_window(now: datetime = None) -> bool:
    """22:00-06:00 Krasnoyarsk time, wrapping midnight."""
    now = now or krasnoyarsk_now()
    t = now.time()
    return t >= NIGHT_START or t < NIGHT_END


def should_attempt_now() -> bool:
    """The real-time trigger's condition: master toggle ON and currently
    in the night window."""
    return is_auto_renewal_enabled() and is_in_night_window()


def is_overdue_trigger_active() -> bool:
    """The periodic checker only fires the 3h-overdue rule while the
    master toggle is ON — flipping auto-renewal off should actually turn
    it off, not leave the overdue safety net silently running."""
    return is_auto_renewal_enabled()


def is_request_overdue(requested_at_iso: str, now: datetime = None) -> bool:
    if not requested_at_iso:
        return False
    try:
        requested_at = datetime.fromisoformat(requested_at_iso)
    except ValueError:
        return False

    reference = now or utcnow_naive()
    if requested_at.tzinfo is not None:
        requested_at = requested_at.astimezone().replace(tzinfo=None)

    return (reference - requested_at) >= OVERDUE_THRESHOLD


# ---------------- BUSINESS RULES ----------------

def evaluate_receipt_extraction(extraction: dict):
    """
    Pure decision function — never calls Gemini or touches the DB. Given
    what Gemini extracted, returns (approved: bool, months: int, reason: str).

    This is the actual gate on who gets auto-renewed — Gemini only reads
    the image, it never decides. A forged or unreadable receipt fails one
    of these checks and falls back to the normal manual queue instead of
    granting access.
    """
    if not isinstance(extraction, dict) or not extraction.get("readable"):
        notes = (extraction or {}).get("notes") if isinstance(extraction, dict) else None
        return False, 0, f"Чек не распознан как читаемый платёжный документ: {notes or 'нет деталей'}"

    confidence = extraction.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < MIN_CONFIDENCE:
        return False, 0, f"Низкая уверенность распознавания ({confidence!r} < {MIN_CONFIDENCE})"

    amount = extraction.get("amount")
    if not isinstance(amount, (int, float)) or amount < MIN_AMOUNT_RUB or amount % MIN_AMOUNT_RUB != 0:
        return False, 0, f"Сумма не кратна {MIN_AMOUNT_RUB}₽ или не распознана: {amount!r}"

    payment_date = parse_expiry(extraction.get("payment_date") or "")
    if payment_date is None:
        return False, 0, f"Не удалось определить дату платежа: {extraction.get('payment_date')!r}"

    today_kra = krasnoyarsk_now().date()
    delta_days = (today_kra - payment_date.date()).days
    if delta_days < 0 or delta_days > PAYMENT_DATE_TOLERANCE_DAYS:
        return False, 0, f"Дата платежа не сегодня/вчера (Красноярск): {payment_date.date()}"

    months = int(amount) // MIN_AMOUNT_RUB
    return True, months, "OK"


# ---------------- FILE RETRIEVAL ----------------

def _fetch_receipt_file(pending: dict):
    """Returns (bytes, mime_type, file_id, is_photo) or (None, None, None, None)."""
    if pending.get("source") == "max":
        # MAX-origin receipts don't keep a re-fetchable file reference
        # today (see max_bot/handlers/receipt.py) — and MAX development is
        # paused for now anyway. Falls back to manual, same as any other
        # unreadable case.
        return None, None, None, None

    file_id = pending.get("receipt_file_id")
    if not file_id:
        return None, None, None, None

    is_photo = pending.get("receipt_is_photo", True)
    file_bytes, file_path = get_file_bytes(file_id)
    if file_bytes is None:
        return None, None, None, None

    if is_photo:
        mime_type = "image/jpeg"
    elif file_path and file_path.lower().endswith(".pdf"):
        mime_type = "application/pdf"
    elif file_path and file_path.lower().endswith(".png"):
        mime_type = "image/png"
    else:
        mime_type = "application/octet-stream"

    return file_bytes, mime_type, file_id, is_photo


# ---------------- MAIN PIPELINE ----------------

def _trigger_label(trigger: str) -> str:
    return {
        "night_window": "ночной режим 22:00-06:00",
        "overdue_3h": "заявка висела > 3ч",
    }.get(trigger, trigger)


def process_pending_request_with_ai(username: str, trigger: str) -> bool:
    """
    MUST be called only after core.db.claim_pending_request_for_ai(username)
    returned True. Returns True if auto-approved (client already notified,
    access already extended, admin sent a review card) — False if it fell
    back to the normal manual queue (caller should make sure the usual
    manual-approval notification with the ➕1мес/➕2мес/✍️/❌ buttons has
    been or gets sent).
    """
    try:
        return _process_pending_request_with_ai_inner(username, trigger)
    except Exception:
        log.exception("auto-renewal pipeline crashed for %s (trigger=%s)", username, trigger)
        log_to_channel(f"🔥 Автопродление упало с ошибкой для {username} ({_trigger_label(trigger)}) — см. логи сервера.")
        notify_admin(f"🔥 Автопродление упало с ошибкой для {username}, заявка осталась в ручной очереди.")
        return False


def _process_pending_request_with_ai_inner(username: str, trigger: str) -> bool:
    from core.gemini_client import GeminiError, extract_receipt_data

    user = get_user(username)
    if not user:
        return False

    pending = user.get("pending_request") or {}

    file_bytes, mime_type, file_id, is_photo = _fetch_receipt_file(pending)
    if file_bytes is None:
        _fallback_to_manual(username, "Не удалось получить файл чека для проверки", trigger)
        return False

    try:
        extraction = extract_receipt_data(file_bytes, mime_type)
    except GeminiError as e:
        _fallback_to_manual(username, f"Ошибка при обращении к Gemini: {e}", trigger, file_id, is_photo)
        return False

    approved, months, reason = evaluate_receipt_extraction(extraction)

    if not approved:
        _fallback_to_manual(username, reason, trigger, file_id, is_photo, extraction)
        return False

    _apply_and_request_review(username, months, extraction, trigger, file_id, is_photo)
    return True


def _fallback_to_manual(username, reason, trigger, file_id=None, is_photo=True, extraction=None):
    user = get_user(username)
    pending = (user or {}).get("pending_request") or {}
    pending["ai_result"] = "fallback"
    pending["ai_fallback_reason"] = reason
    pending["ai_trigger"] = trigger
    update_user(username, pending_request=pending)

    log.info("auto-renewal fallback for %s (%s): %s", username, trigger, reason)

    caption = (
        f"⚠️ Автопродление не сработало ({_trigger_label(trigger)})\n"
        f"👤 {username}\n"
        f"Причина: {reason}\n\n"
        f"Заявка доступна для обычного ручного одобрения."
    )
    log_to_channel(caption, file_id=file_id, is_photo=is_photo)
    notify_admin(caption)


def _apply_and_request_review(username, months, extraction, trigger, file_id=None, is_photo=True):
    user = get_user(username)
    previous_expires_at = user.get("expires_at")
    previous_status = user.get("status")
    was_expired_or_inactive = previous_status != "active" or is_expired(previous_expires_at)

    new_expires_at = calc_new_expiry_months(previous_expires_at, months)

    pending = user.get("pending_request") or {}
    pending["ai_result"] = "approved"
    pending["ai_decision"] = {
        "months": months,
        "previous_expires_at": previous_expires_at,
        "previous_status": previous_status,
        "new_expires_at": new_expires_at,
        "extraction": extraction,
        "trigger": trigger,
        "decided_at": utcnow_naive().isoformat(),
    }

    update_user(
        username,
        expires_at=new_expires_at,
        status="active",
        notified_days=[],
        post_disable_notified=[],
        pending_request=pending,
    )

    if was_expired_or_inactive:
        safe_sync()

    notify_user(user, f"✅ Ваша подписка продлена до {new_expires_at}. Спасибо!")

    amount = extraction.get("amount")
    confidence = extraction.get("confidence")
    caption = (
        f"🤖 Автопродление применено ({_trigger_label(trigger)})\n"
        f"👤 {username}\n"
        f"💰 Сумма по чеку: {amount}₽ → {months} мес.\n"
        f"📅 {previous_expires_at or '∞'} → {new_expires_at}\n"
        f"🎯 Уверенность распознавания: {confidence}\n\n"
        f"Проверьте чек и подтвердите, либо отключите с откатом даты:"
    )

    review_kb = {
        "inline_keyboard": [[
            {"text": "✅ Подтвердить", "callback_data": f"aircheck:{username}:confirm"},
            {"text": "🚫 Отключить", "callback_data": f"aircheck:{username}:disable"},
        ]]
    }

    if file_id:
        if is_photo:
            send_photo_by_file_id(file_id, caption=caption, reply_markup=review_kb)
        else:
            from core.notify import send_document_by_file_id
            send_document_by_file_id(file_id, caption=caption, reply_markup=review_kb)
    else:
        notify_admin(caption)

    log_to_channel(caption, file_id=file_id, is_photo=is_photo)
