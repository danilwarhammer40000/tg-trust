import logging
import subprocess
import time

from core.credentials import rebuild_credentials_from_db
from core.db import list_users

log = logging.getLogger(__name__)

TRUSTTUNNEL_SERVICE = "trusttunnel.service"


def restart_trusttunnel(wait_ready: bool = True, max_wait_seconds: float = 5.0) -> None:
    result = subprocess.run(
        ["systemctl", "restart", TRUSTTUNNEL_SERVICE],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        log.error("trusttunnel restart failed: %s", result.stderr.strip())
        return

    log.info("trusttunnel restarted")

    if not wait_ready:
        return

    # BUG FIX: `systemctl restart` returning success only means systemd has
    # HANDED OFF the restart — for a forking/simple-type unit that's well
    # before the new process has actually finished starting up and
    # re-reading its config. core.generator.generate_link() was running
    # right after this call returned (via bot.access.run_sync()) and
    # occasionally hitting the service before it was really ready, silently
    # falling back to a non-functional placeholder link. Poll
    # `systemctl is-active` for a short window so callers get a real
    # "it's actually up" signal instead of just "systemd accepted the
    # restart command".
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        check = subprocess.run(
            ["systemctl", "is-active", TRUSTTUNNEL_SERVICE],
            capture_output=True,
            text=True,
        )
        if check.stdout.strip() == "active":
            return
        time.sleep(0.3)

    log.warning(
        "trusttunnel restarted but not reported 'active' within %.1fs — "
        "proceeding anyway, generate_link() retries will absorb a bit more lag",
        max_wait_seconds,
    )


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
