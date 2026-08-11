"""
Follow-up reminders AFTER a user has already been disabled (as opposed to
services/cleanup.py's T-7/T-3/T-0 warnings, which run BEFORE/AT expiry).

Runs as its own systemd timer (trustpanel-post-disable.timer) — deliberately
separate from cleanup.py so the two schedules/failure modes don't get
tangled together, even though they share the same underlying data.

There's no separate "disabled_at" timestamp stored anywhere — expires_at
IS the disable date, since cleanup.py disables a user on the same day its
expires_at passes. So "days since disabled" is simply today - expires_at.
"""
import logging
import sys

sys.path.append("/opt/trustpanel")

from core.logging_setup import setup_logging
from core.dates import parse_expiry, utcnow_naive
from core.db import list_users, update_user
from core.notify import notify_user
from core.payment import PAYMENT_INFO

setup_logging()
log = logging.getLogger(__name__)

# Days after the disable date to send a follow-up nudge.
REMINDER_DAYS = (1, 3)


def _reminder_text(days_since: int) -> str:
    if days_since == 1:
        return (
            "❌ Напоминаем: доступ отключён уже 1 день.\n"
            "Продлите сейчас, чтобы не потерять настройки — пришлите чек об оплате прямо в этот чат.\n\n"
            f"{PAYMENT_INFO}"
        )
    return (
        f"❌ Доступ отключён уже {days_since} дня.\n"
        "Если не продлить в ближайшее время, аккаунт может быть удалён при следующей чистке базы.\n\n"
        f"{PAYMENT_INFO}"
    )


def run() -> int:
    """Returns how many reminders were sent this run."""
    users = list_users()
    today = utcnow_naive().date()

    sent = 0

    for u in users:
        if u.get("status") == "active":
            continue

        username = u.get("username")
        exp_dt = parse_expiry(u.get("expires_at"))
        if not username or not exp_dt:
            continue  # inactive with no real expiry -- not a normal expiry-disable, skip

        days_since = (today - exp_dt.date()).days
        if days_since < 0:
            continue  # inactive but not actually past due yet -- shouldn't normally happen

        already_sent = set(u.get("post_disable_notified") or [])

        for d in REMINDER_DAYS:
            if days_since != d or d in already_sent:
                continue

            has_tg = bool(u.get("telegram_id"))
            log.info(
                "post-disable +%sd reminder for %s (disabled %s, telegram_id=%s)",
                d, username, exp_dt.date(), "set" if has_tg else "MISSING",
            )

            notify_user(u, _reminder_text(d))

            already_sent.add(d)
            # NOT a synced field (see core.db._SYNCED_FIELDS) -- each linked
            # account tracks its own reminder history independently, same
            # as notified_days.
            update_user(username, post_disable_notified=sorted(already_sent))
            sent += 1

    log.info("post_disable_reminders: sent %d reminder(s)", sent)
    return sent


if __name__ == "__main__":
    run()
