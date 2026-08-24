"""
Periodic "3+ hours unprocessed" trigger for AI auto-renewal — runs as its
own systemd timer (trustpanel-auto-renewal-check.timer), independent of
services/cleanup.py and services/post_disable_reminders.py.

Unlike the real-time night-window trigger (bot/auto_renewal_hook.py, fired
the moment a receipt arrives), this only matters for receipts that have
been sitting unanswered — checked on every run regardless of time of day,
but still respects the master ON/OFF toggle (see
core.auto_renewal.is_overdue_trigger_active): turning auto-renewal off is
meant to actually turn it off, not leave this safety net quietly running.

Fully synchronous, same as cleanup.py and post_disable_reminders.py — no
event loop needed since core.auto_renewal.process_pending_request_with_ai
is itself synchronous throughout (Gemini call, Telegram calls, DB writes).
"""
import logging
import sys

sys.path.append("/opt/trustpanel")

from core.logging_setup import setup_logging
from core.auto_renewal import is_overdue_trigger_active, is_request_overdue, process_pending_request_with_ai
from core.db import claim_pending_request_for_ai, list_users

setup_logging()
log = logging.getLogger(__name__)


def _is_overdue_candidate(user: dict) -> bool:
    pending = user.get("pending_request")
    if not pending:
        return False

    # Already resolved one way or another (approved and awaiting admin
    # review, or already fell back to manual once) -- don't re-attempt.
    # A genuinely NEW receipt submission replaces pending_request with a
    # fresh dict (see bot/handlers/receipt.py's receipt_yes), which
    # naturally resets this.
    if pending.get("ai_result"):
        return False

    return is_request_overdue(pending.get("requested_at"))


def run() -> int:
    """Returns how many requests were processed this run."""
    if not is_overdue_trigger_active():
        log.info("auto-renewal is OFF, skipping overdue check")
        return 0

    users = list_users()
    candidates = [u for u in users if _is_overdue_candidate(u)]

    if not candidates:
        log.info("no overdue pending requests found")
        return 0

    processed = 0

    for user in candidates:
        username = user.get("username")
        if not username:
            continue

        if not claim_pending_request_for_ai(username):
            log.info("skipping %s -- already claimed by another process", username)
            continue

        log.info("processing overdue request for %s", username)
        try:
            process_pending_request_with_ai(username, "overdue_3h")
            processed += 1
        except Exception:
            log.exception("unexpected error processing overdue request for %s", username)

    log.info("auto_renewal_overdue_check: processed %d request(s)", processed)
    return processed


if __name__ == "__main__":
    run()
