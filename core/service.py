import logging
import subprocess

from core.credentials import rebuild_credentials_from_db
from core.db import list_users

log = logging.getLogger(__name__)

TRUSTTUNNEL_SERVICE = "trusttunnel.service"


def restart_trusttunnel() -> None:
    result = subprocess.run(
        ["systemctl", "restart", TRUSTTUNNEL_SERVICE],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log.error("trusttunnel restart failed: %s", result.stderr.strip())
    else:
        log.info("trusttunnel restarted")


# ---------------- FULL SYNC ----------------

def full_resync_and_reload() -> None:
    users = list_users()
    rebuild_credentials_from_db(users)
    restart_trusttunnel()


def mark_user_inactive(username: str) -> None:
    # Purely a logical marker now — credentials.toml is rebuilt from
    # users.json on every sync, so there's nothing else to do here.
    # Kept as a named no-op (rather than removed) so call sites stay
    # self-documenting about *why* a user is being disabled.
    log.debug("mark_user_inactive(%s): no-op, credentials rebuilt on next sync", username)


# ---------------- SAFE SYNC ----------------

def safe_sync() -> str:
    try:
        full_resync_and_reload()
        return "OK"
    except Exception as e:
        log.exception("sync failed")
        return f"ERROR: {e}"
