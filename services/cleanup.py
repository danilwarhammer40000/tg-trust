import logging
import sys
from datetime import timezone

sys.path.append("/opt/trustpanel")

from core.logging_setup import setup_logging
from core.dates import parse_expiry, utcnow_naive
from core.db import list_users, update_user
from core.service import full_resync_and_reload, mark_user_inactive
from core.notify import notify_user, notify_admin
from core.payment import PAYMENT_INFO, ACCESS_EXPIRED_MESSAGE

setup_logging()
log = logging.getLogger(__name__)

WARNING_DAYS = (7, 3, 0)


# -----------------------------
# T-7 / T-3 warnings
# -----------------------------

def check_upcoming_expirations(users, now):
    for u in users:
        if u.get("status") != "active":
            continue

        username = u.get("username")
        exp_dt = parse_expiry(u.get("expires_at"))
        if not username or not exp_dt:
            continue

        days_left = (exp_dt.date() - now.date()).days
        if days_left < 0:
            continue

        notified = u.get("notified_days") or []

        for w in WARNING_DAYS:
            if days_left == w and w not in notified:
                has_tg = bool(u.get("telegram_id"))
                log.info(
                    "T-%s warning for %s (expires %s, telegram_id=%s)",
                    w, username, exp_dt.date(), "set" if has_tg else "MISSING",
                )

                if w == 0:
                    text = (
                        f"⏳ Сегодня последний день вашего доступа ({exp_dt.date()}).\n"
                        f"Чтобы не потерять доступ — пришлите чек об оплате прямо в этот чат сегодня.\n\n"
                        f"{PAYMENT_INFO}"
                    )
                else:
                    text = (
                        f"⏳ Ваш доступ истекает через {w} дн. ({exp_dt.date()}).\n"
                        f"Чтобы продлить — пришлите чек об оплате прямо в этот чат.\n\n"
                        f"{PAYMENT_INFO}"
                    )

                notify_user(u, text)
                notified.append(w)
                update_user(username, notified_days=notified)


# -----------------------------
# Core logic
# -----------------------------

def run() -> bool:
    """
    Runs the full cleanup cycle: T-7/T-3 warnings + disabling expired users.

    Called both by the daily systemd timer AND on-demand from the bot's
    "🔄 Sync users" button, so the two stay in sync instead of drifting apart.

    Returns True if a full credentials resync + trusttunnel restart was
    already triggered (i.e. at least one user was disabled) — callers can use
    this to avoid forcing a second, redundant resync/restart right after.
    """
    users = list_users()
    now = utcnow_naive().replace(tzinfo=timezone.utc)

    # 1) warn about upcoming expirations (T-7 / T-3)
    check_upcoming_expirations(users, now)

    # 2) find/disable actually expired users (T-0)
    expired_users = []

    for u in users:
        if u.get("status") != "active":
            continue

        username = u.get("username")
        if not username:
            continue

        exp_dt = parse_expiry(u.get("expires_at"))
        if not exp_dt:
            continue

        if exp_dt.date() < now.date():
            expired_users.append(u)

    if not expired_users:
        log.info("No expired users found")
        return False

    log.info("Expired users: %d", len(expired_users))

    changed = False

    for u in expired_users:
        username = u.get("username")
        try:
            log.info("DISABLING: %s", username)

            mark_user_inactive(username)
            # A real expiry is a clean cycle boundary -- release the
            # auto-renewal anti-abuse lock (see core/auto_renewal.py) so
            # the next genuine payment can be auto-renewed again instead
            # of being forced to manual review forever after the first
            # auto-renewal this account ever got.
            update_user(username, status="inactive", notified_days=[], auto_renewal_applied=False)

            notify_user(u, ACCESS_EXPIRED_MESSAGE)
            notify_admin(f"⚠️ Пользователь {username} отключён (истёк срок).")

            changed = True

        except Exception:
            log.exception("failed to disable %s", username)

    if not changed:
        log.info("No changes applied")
        return False

    log.info("FULL RESYNC TRIGGERED")
    try:
        full_resync_and_reload()
        return True
    except Exception:
        log.exception("resync failed")
        return False


if __name__ == "__main__":
    run()
