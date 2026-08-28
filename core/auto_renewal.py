"""
AI-assisted auto-renewal: settings, trigger conditions, business-rule
evaluation, and the pipeline that ties Gemini extraction + core.db + the
Telegram log channel + admin review together.

Two independent triggers call process_pending_request_with_ai() (always
via core.db.claim_pending_request_for_ai() first, to prevent double
processing):
  1. Real-time, from bot/handlers/receipt.py and feedback.py, when a
     receipt arrives while auto-renewal is ON and it's currently within
     the night window (22:00-06:00 Krasnoyarsk) -- or any time of day at
     all if "fully automatic" mode is on (see should_attempt_now()).
  2. services/auto_renewal_overdue_check.py's periodic timer, for any
     receipt still unprocessed by the admin 3+ hours after submission —
     independent of time of day, but still gated on the master ON/OFF
     toggle (see is_overdue_trigger_active()).

Everything here is synchronous (plain function calls, no async/await) so
it works identically called from an aiogram handler via
loop.run_in_executor(...) and from the standalone periodic script, which
has no event loop at all.

The client is never told THAT a renewal happened automatically -- see
bot/handlers/receipt.py / feedback.py (the acknowledgement text after
submitting a receipt is identical whether auto-renewal fires or not) and
bot/handlers/auto_renewal_review.py (the disable/rollback message uses
generic wording, never the word "automatic"). What the client DOES get,
the moment auto-renewal actually approves their receipt, is the exact
same "✅ Ваша подписка продлена..." text a manual approval sends -- no
delay, no different timing (see _apply_and_request_review). The one case
where the client gets nothing at all is the anti-abuse fallback (see
AUTO_RENEWAL_LOCK_DAYS below): a blocked repeat attempt stays silent
towards the client and goes to the admin only, as a clearly marked
warning card.

Anti-abuse: auto-renewal may apply at most once per AUTO_RENEWAL_LOCK_DAYS
per user (see _is_locked / process_pending_request_with_ai). The first
receipt in that window is handled automatically; every next one during
the lock falls straight to manual review with a "possible replay/abuse"
warning card. The lock also clears early the moment a human actually
verifies the account (any manual admin approval/extension, or a rollback
via "🚫 Отключить"), and clears itself automatically once
AUTO_RENEWAL_LOCK_DAYS have passed since the last auto-renewal, even if
no admin touched it.
"""
import json
import logging
import os
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from core.dates import calc_new_expiry_months, is_expired, utcnow_naive
from core.db import get_user, update_user
from core.notify import get_file_bytes, log_to_channel, notify_admin, notify_user, send_photo_by_file_id
from core.paths import AUTO_RENEWAL_SETTINGS_PATH
from core.service import safe_sync

log = logging.getLogger(__name__)

KRASNOYARSK_TZ = ZoneInfo("Asia/Krasnoyarsk")

DEFAULT_SETTINGS = {
    "enabled": False,
    "fully_automatic": False,   # bypasses the night window entirely -- see should_attempt_now()
    "night_start": "22:00",
    "night_end": "06:00",
    "overdue_hours": 3,
    "min_amount": 100,
    "min_confidence": 0.75,
    "abuse_lock_days": 7,   # see _is_locked() -- how long one auto-renewal blocks the next
}

# Human-readable metadata for the bot's "⚙️ Настроить условия" screen — one
# place that both the settings menu and the input-validation logic below
# read from, so a new field only needs to be added here once.
#
# NOTE: there is deliberately no date-related field here. Auto-renewal only
# ever needs to know how much money came in -- it does not check the date
# printed on the receipt at all (see evaluate_receipt_extraction below).
FIELD_META = {
    "night_start": {
        "label": "🌙 Начало ночного окна",
        "prompt": "Введите время начала ночного окна в формате ЧЧ:ММ (например 22:00):",
    },
    "night_end": {
        "label": "🌅 Конец ночного окна",
        "prompt": "Введите время конца ночного окна в формате ЧЧ:ММ (например 06:00):",
    },
    "overdue_hours": {
        "label": "⏳ Порог просрочки (часов)",
        "prompt": "Через сколько часов без ответа администратора считать заявку просроченной? Число, можно дробное (например 3 или 2.5):",
    },
    "min_amount": {
        "label": "💰 Мин. сумма / кратность (₽)",
        "prompt": "Минимальная сумма и шаг кратности в рублях (например 100 = сумма должна быть кратна 100₽):",
    },
    "min_confidence": {
        "label": "🎯 Мин. уверенность Gemini",
        "prompt": "Минимальная уверенность распознавания чека, от 0 до 1 (например 0.75):",
    },
    "abuse_lock_days": {
        "label": "🔒 Блокировка повтора (дней)",
        "prompt": "Сколько дней после одного автопродления блокировать следующее для того же "
                  "пользователя (защита от накрутки)? Целое число, например 7:",
    },
}


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


def get_setting(key: str):
    return _load_settings().get(key, DEFAULT_SETTINGS.get(key))


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


def is_fully_automatic_enabled() -> bool:
    return _load_settings().get("fully_automatic", False)


def toggle_fully_automatic() -> bool:
    """Fully-automatic mode makes should_attempt_now() ignore the night
    window entirely — every receipt gets an immediate AI attempt, any time
    of day, as long as the master toggle is also ON."""
    settings = _load_settings()
    settings["fully_automatic"] = not settings.get("fully_automatic", False)
    _save_settings(settings)
    return settings["fully_automatic"]


def log_channel_configured() -> bool:
    from core.notify import LOG_CHANNEL_ID
    return bool(LOG_CHANNEL_ID)


def _parse_hhmm(raw: str) -> dt_time:
    h, m = raw.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("out of range")
    return dt_time(h, m)


def set_setting_validated(key: str, raw_value: str):
    """
    Validates and saves one editable trigger setting from admin-typed text.
    Returns (ok: bool, error_message_or_none: str) — used by
    bot/handlers/auto_renewal_review.py so the bot can show a specific
    "что не так" message instead of silently accepting garbage. This is
    the one place all editable fields get bounds-checked, since a bad
    value here (e.g. confidence > 1, negative hours) could otherwise let
    the wrong receipts through automatically.
    """
    raw_value = (raw_value or "").strip()

    if key in ("night_start", "night_end"):
        try:
            _parse_hhmm(raw_value)
        except (ValueError, IndexError):
            return False, "Формат должен быть ЧЧ:ММ, например 22:00"
        value = raw_value

    elif key == "overdue_hours":
        try:
            value = float(raw_value.replace(",", "."))
            if value <= 0:
                raise ValueError
        except ValueError:
            return False, "Введите положительное число часов, например 3 или 2.5"

    elif key == "min_amount":
        try:
            value = int(raw_value)
            if value <= 0:
                raise ValueError
        except ValueError:
            return False, "Введите положительное целое число рублей, например 100"

    elif key == "min_confidence":
        try:
            value = float(raw_value.replace(",", "."))
            if not (0 <= value <= 1):
                raise ValueError
        except ValueError:
            return False, "Введите число от 0 до 1, например 0.75"

    elif key == "abuse_lock_days":
        try:
            value = int(raw_value)
            if value <= 0:
                raise ValueError
        except ValueError:
            return False, "Введите положительное целое число дней, например 7"

    else:
        return False, f"Неизвестный параметр: {key}"

    settings = _load_settings()
    settings[key] = value
    _save_settings(settings)
    return True, None


def reset_settings_to_defaults() -> None:
    settings = _load_settings()
    enabled = settings.get("enabled", False)  # keep ON/OFF as-is, only reset the trigger tuning
    _save_settings({**DEFAULT_SETTINGS, "enabled": enabled})


# ---------------- TIME WINDOW ----------------

def krasnoyarsk_now() -> datetime:
    return datetime.now(KRASNOYARSK_TZ)


def is_in_night_window(now: datetime = None) -> bool:
    """Night window bounds are configurable (default 22:00-06:00
    Krasnoyarsk) — handles both a wrapping window (start > end, e.g.
    22:00-06:00) and a same-day window (start < end, e.g. 09:00-18:00, in
    case an admin ever wants to invert the idea and only auto-renew during
    the day) the same way."""
    now = now or krasnoyarsk_now()
    t = now.time()

    start = _parse_hhmm(get_setting("night_start"))
    end = _parse_hhmm(get_setting("night_end"))

    if start <= end:
        return start <= t < end
    return t >= start or t < end


def should_attempt_now() -> bool:
    """The real-time trigger's condition: master toggle ON, and either
    fully-automatic mode is on (any time of day) or it's currently in the
    night window."""
    if not is_auto_renewal_enabled():
        return False
    if is_fully_automatic_enabled():
        return True
    return is_in_night_window()


def is_overdue_trigger_active() -> bool:
    """The periodic checker only fires the overdue rule while the master
    toggle is ON — flipping auto-renewal off should actually turn it off,
    not leave the overdue safety net silently running."""
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

    threshold = timedelta(hours=get_setting("overdue_hours"))
    return (reference - requested_at) >= threshold


# ---------------- ANTI-ABUSE LOCK ----------------

def _is_locked(user: dict):
    """
    Returns (locked: bool, unlock_date: date_or_None). A user is locked
    out of auto-renewal if their last auto-renewal (auto_renewal_applied)
    happened less than `abuse_lock_days` ago. Missing/unparseable
    timestamp on an otherwise-set flag is treated as locked (fail safe --
    better to fall back to manual once than to accidentally let a replay
    through).

    Cleared early (see callers of update_user(..., auto_renewal_applied=False)
    in bot/handlers/receipt.py, extend.py, auto_renewal_review.py's
    aircheck:disable, and services/cleanup.py) the moment a human actually
    verifies the account, or automatically once the window above has
    simply passed.
    """
    if not user.get("auto_renewal_applied"):
        return False, None

    applied_at = user.get("auto_renewal_applied_at")
    lock_days = get_setting("abuse_lock_days")

    if not applied_at:
        return True, None

    try:
        applied_dt = datetime.fromisoformat(applied_at)
    except ValueError:
        return True, None

    unlock_dt = applied_dt + timedelta(days=lock_days)
    if utcnow_naive() >= unlock_dt:
        return False, None

    return True, unlock_dt.date()


# ---------------- BUSINESS RULES ----------------

def evaluate_receipt_extraction(extraction: dict):
    """
    Pure decision function — never calls Gemini or touches the DB. Given
    what Gemini extracted, returns (approved: bool, months: int, reason: str).

    This is the actual gate on who gets auto-renewed — Gemini only reads
    the image, it never decides. A forged or unreadable receipt fails one
    of these checks and falls back to the normal manual queue instead of
    granting access. Thresholds are all admin-editable (see
    set_setting_validated / bot/handlers/auto_renewal_review.py).

    By design this only cares about how much money came in (and how
    confidently that amount was read) -- it deliberately does NOT check
    the date printed on the receipt. A screenshot of an old payment still
    represents real money that was transferred, and requiring "today or
    yesterday" caused legitimate receipts to bounce to manual review for
    no reason other than a delay in sending it. The one-shot anti-abuse
    lock below (see process_pending_request_with_ai / auto_renewal_applied)
    is what actually prevents the same receipt being replayed for repeat
    auto-renewals, not the date.
    """
    min_amount = get_setting("min_amount")
    min_confidence = get_setting("min_confidence")

    if not isinstance(extraction, dict) or not extraction.get("readable"):
        notes = (extraction or {}).get("notes") if isinstance(extraction, dict) else None
        return False, 0, f"Чек не распознан как читаемый платёжный документ: {notes or 'нет деталей'}"

    confidence = extraction.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < min_confidence:
        return False, 0, f"Низкая уверенность распознавания ({confidence!r} < {min_confidence})"

    amount = extraction.get("amount")
    if not isinstance(amount, (int, float)) or amount < min_amount or amount % min_amount != 0:
        return False, 0, f"Сумма не кратна {min_amount}₽ или не распознана: {amount!r}"

    months = int(amount) // min_amount
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
        "night_window": "ночной режим",
        "overdue_3h": "заявка висела > порога",
        "manual_retry": "повтор вручную",
    }.get(trigger, trigger)


def _log_or_warn(caption: str, file_id: str = None, is_photo: bool = True) -> None:
    """
    Wraps log_to_channel() with a visible failure path. Previously a
    misconfigured/inaccessible LOG_CHANNEL_ID (wrong ID, or the bot added
    as a channel member without the "Post Messages" admin permission —
    the single most common cause) failed completely silently: the admin
    would keep getting the normal Telegram notifications and never notice
    the audit trail simply wasn't being written anywhere.

    See also core.notify.diagnose_log_channel() / the "🔍 Диагностика"
    button in the "🤖 Автопродление" menu — that runs live Bot API checks
    (getMe / getChat / getChatMember / a real test send) and reports back
    Telegram's own error text instead of this function's best guess.
    """
    ok = log_to_channel(caption, file_id=file_id, is_photo=is_photo)
    if not ok and log_channel_configured():
        notify_admin(
            "⚠️ Не удалось записать событие в лог-канал автопродления "
            "(LOG_CHANNEL_ID настроен, но отправка не удалась).\n\n"
            "Самая частая причина: бот добавлен в канал, но у него не включено "
            "право «Публикация сообщений» — зайдите в настройки канала → "
            "Администраторы → права бота, и включите его.\n"
            "Также проверьте, что LOG_CHANNEL_ID в .env указан верно "
            "(для приватных каналов обычно начинается с -100).\n\n"
            "Точную причину можно посмотреть в «🤖 Автопродление» → «🔍 Диагностика»."
        )


def process_pending_request_with_ai(username: str, trigger: str) -> bool:
    """
    MUST be called only after core.db.claim_pending_request_for_ai(username)
    returned True. Returns True if auto-approved (client's access already
    extended, admin sent a review card) — False if it fell back to the
    normal manual queue (the fallback card itself already has the usual
    ➕1мес/➕2мес/✍️/❌ buttons attached, plus a 🔄 retry button — see
    _fallback_to_manual).
    """
    try:
        return _process_pending_request_with_ai_inner(username, trigger)
    except Exception:
        log.exception("auto-renewal pipeline crashed for %s (trigger=%s)", username, trigger)
        _log_or_warn(f"🔥 Автопродление упало с ошибкой для {username} ({_trigger_label(trigger)}) — см. логи сервера.")
        notify_admin(f"🔥 Автопродление упало с ошибкой для {username}, заявка осталась в ручной очереди.")
        return False


def _process_pending_request_with_ai_inner(username: str, trigger: str) -> bool:
    from core.gemini_client import GeminiError, extract_receipt_data

    user = get_user(username)
    if not user:
        return False

    # ---- anti-abuse: auto-renewal can apply at most once per lock window ----
    # Without this, a client could resubmit the same (or a slightly
    # doctored) receipt repeatedly and have auto-renewal extend their
    # access again and again, unattended. The first receipt in a window
    # is handled automatically; every next one falls straight to manual
    # review with a clearly marked warning card -- no Gemini call spent
    # on it. See _is_locked() for exactly when this clears.
    locked, unlock_date = _is_locked(user)
    if locked:
        unlock_line = f" (снимется {unlock_date})" if unlock_date else ""
        _fallback_to_manual(
            username,
            "Автопродление для этого пользователя уже было применено недавно и "
            f"повторно сработать не может{unlock_line} — похоже на попытку "
            "повторно использовать чек ради ещё одного автопродления. "
            "Нужна ручная проверка.",
            trigger,
            anti_abuse=True,
        )
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


def _fallback_admin_kb(username: str) -> dict:
    """
    Plain-dict mirror of bot/keyboards.py's renewal_admin_kb() (this
    module sends via raw HTTP, not aiogram objects — if you change one,
    check the other), plus a 🔄 retry button. Attaching the normal approve
    buttons here means a fallback (e.g. a transient Gemini error, like an
    outdated model name) doesn't cost the admin an extra trip to 📋 List
    users to approve manually — everything needed is on this one card.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "➕1 мес", "callback_data": f"apr:{username}:30"},
                {"text": "➕2 мес", "callback_data": f"apr:{username}:60"},
            ],
            [{"text": "✍️ Ручная дата", "callback_data": f"apr:{username}:manual"}],
            [{"text": "🔄 Повторить автопроверку", "callback_data": f"airretry:{username}"}],
            [{"text": "❌ Отклонить", "callback_data": f"apr:{username}:reject"}],
        ]
    }


def _fallback_to_manual(username, reason, trigger, file_id=None, is_photo=True, extraction=None, anti_abuse=False):
    user = get_user(username)
    pending = (user or {}).get("pending_request") or {}
    pending["ai_result"] = "fallback"
    pending["ai_fallback_reason"] = reason
    pending["ai_trigger"] = trigger
    pending["ai_anti_abuse"] = anti_abuse
    update_user(username, pending_request=pending)

    log.info(
        "auto-renewal fallback for %s (%s)%s: %s",
        username, trigger, " [ANTI-ABUSE]" if anti_abuse else "", reason,
    )

    if anti_abuse:
        # Deliberately a different, louder header than the generic
        # fallback below -- this is not "Gemini couldn't read it", it's
        # "someone may be trying to replay a receipt for a second
        # auto-renewal". Client gets nothing at all for this case (see
        # module docstring) -- only the admin sees this card.
        caption = (
            f"🚨 ЗАЩИТА ОТ НАКРУТКИ ({_trigger_label(trigger)})\n"
            f"👤 {username}\n"
            f"Автопродление уже применялось недавно этому пользователю — "
            f"повторное сработать не могло.\n"
            f"{reason}\n\n"
            f"Клиенту ничего не отправлено. Проверьте чек внимательно "
            f"(возможен повтор/подделка) — одобрить вручную, повторить "
            f"автопроверку позже или отклонить:"
        )
    else:
        caption = (
            f"⚠️ Автопродление не сработало ({_trigger_label(trigger)})\n"
            f"👤 {username}\n"
            f"Причина: {reason}\n\n"
            f"Можно одобрить вручную, повторить автопроверку (например, если "
            f"причина — временная ошибка Gemini) или отклонить:"
        )

    kb = _fallback_admin_kb(username)

    if file_id:
        if is_photo:
            send_photo_by_file_id(file_id, caption=caption, reply_markup=kb)
        else:
            from core.notify import send_document_by_file_id
            send_document_by_file_id(file_id, caption=caption, reply_markup=kb)
    else:
        # No file to attach (e.g. couldn't even download it) -- admin still
        # needs SOME way to act, so fall back to a plain text notice; the
        # normal apr:/airretry: buttons work the same either way since they
        # only reference the username, not this specific message.
        notify_admin(caption)

    _log_or_warn(caption, file_id=file_id, is_photo=is_photo)


def _apply_and_request_review(username, months, extraction, trigger, file_id=None, is_photo=True):
    """
    Applies the renewal immediately AND notifies the client immediately —
    the exact same "✅ Ваша подписка продлена..." text a manual approval
    sends, no delay. The admin still gets a post-hoc review card
    ("✅ Подтвердить" / "🚫 Отключить") so a wrong auto-approval can be
    caught and rolled back after the fact, but that review no longer
    gates when the client hears about it — see the module docstring for
    why (the anti-abuse fallback is the case that stays silent, not this
    one).
    """
    user = get_user(username)
    previous_expires_at = user.get("expires_at")
    previous_status = user.get("status")
    was_expired_or_inactive = previous_status != "active" or is_expired(previous_expires_at)

    new_expires_at = calc_new_expiry_months(previous_expires_at, months)
    applied_at = utcnow_naive().isoformat()

    pending = user.get("pending_request") or {}
    pending["ai_result"] = "approved"
    pending["ai_decision"] = {
        "months": months,
        "previous_expires_at": previous_expires_at,
        "previous_status": previous_status,
        "new_expires_at": new_expires_at,
        "extraction": extraction,
        "trigger": trigger,
        "decided_at": applied_at,
    }

    # Two separate update_user() calls, deliberately: core.db.update_user()
    # redirects a call onto the leader (and fans out to the whole group)
    # whenever expires_at/status are among the kwargs -- if pending_request/
    # auto_renewal_applied were bundled into that same call, they'd land on
    # the leader's record instead of this specific account's, for any user
    # who happens to be a follower. Splitting keeps expires_at/status going
    # through the leader-sync path while pending_request/auto_renewal_*
    # always land on `username` itself, exactly as intended.
    update_user(username, expires_at=new_expires_at, status="active")
    update_user(
        username,
        notified_days=[],
        post_disable_notified=[],
        pending_request=pending,
        auto_renewal_applied=True,        # anti-abuse lock, see _is_locked()
        auto_renewal_applied_at=applied_at,
    )

    if was_expired_or_inactive:
        safe_sync()

    # The client's only signal, ever, that anything happened -- same
    # wording bot/handlers/receipt.py's manual approval uses.
    notify_user(user, f"✅ Ваша подписка продлена до {new_expires_at}. Спасибо!")

    amount = extraction.get("amount")
    confidence = extraction.get("confidence")
    caption = (
        f"🤖 Автопродление применено ({_trigger_label(trigger)})\n"
        f"👤 {username}\n"
        f"💰 Сумма по чеку: {amount}₽ → {months} мес.\n"
        f"📅 {previous_expires_at or '∞'} → {new_expires_at}\n"
        f"🎯 Уверенность распознавания: {confidence}\n\n"
        f"Клиент уже уведомлён о продлении. Проверьте чек — если что-то не так, "
        f"«🚫 Отключить» откатит и статус, и дату, и отправит клиенту сообщение "
        f"об отмене. «✅ Подтвердить» просто закрывает карточку без доп. действий:"
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

    _log_or_warn(caption, file_id=file_id, is_photo=is_photo)
