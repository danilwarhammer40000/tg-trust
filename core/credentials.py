import logging
import os
import stat
import tempfile

import toml
from filelock import FileLock

from core.dates import parse_expiry, utcnow_naive

log = logging.getLogger(__name__)

CREDENTIALS_PATH = os.getenv("TRUSTTUNNEL_CREDENTIALS_PATH", "/opt/trusttunnel/credentials.toml")
LOCK_PATH = CREDENTIALS_PATH + ".lock"

lock = FileLock(LOCK_PATH, timeout=10)


def load_credentials() -> dict:
    """
    CHANGED: previously hand-parsed with line.startswith("username")/
    ("password") checks, which silently drops any field it doesn't
    recognize and breaks quietly if the file's format ever changes.
    `toml` is already a project dependency (see requirements.txt) — this
    just actually uses it.
    """
    if not os.path.exists(CREDENTIALS_PATH):
        return {"client": []}

    try:
        with open(CREDENTIALS_PATH, "r") as f:
            data = toml.load(f)
        data.setdefault("client", [])
        return data
    except (OSError, toml.TomlDecodeError) as e:
        log.error("failed to parse %s: %s", CREDENTIALS_PATH, e)
        return {"client": []}


def _chmod_private(path: str) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as e:
        log.warning("could not chmod %s: %s", path, e)


def atomic_write(data: dict) -> None:
    os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)

    with lock:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, dir=os.path.dirname(CREDENTIALS_PATH)
        ) as tmp:
            toml.dump(data, tmp)
            tmp_path = tmp.name

        _chmod_private(tmp_path)
        os.replace(tmp_path, CREDENTIALS_PATH)


# -------------------------
# FULL REBUILD
# -------------------------
def rebuild_credentials_from_db(users: list) -> None:
    clients = []

    for u in users:
        if u.get("status") != "active":
            continue

        # CHANGED: was datetime.fromisoformat(exp) here (bot.py writes dates
        # as "YYYY-MM-DD", which fromisoformat also happens to accept, but
        # that's a coincidence, not a contract). Now uses the shared parser
        # from core/dates.py.
        #
        # NOTE on purpose: unlike core.dates.is_expired() (used for display,
        # where we don't want a data glitch to visually flag someone as
        # expired), this function decides who actually gets network
        # credentials. On a genuinely unparseable date we exclude the user
        # rather than grant access -- same as the original behaviour --
        # because "fail closed" is the right default for an access list.
        exp = u.get("expires_at")
        if exp:
            exp_dt = parse_expiry(exp)
            if exp_dt is None or exp_dt.date() < utcnow_naive().date():
                continue

        username = u.get("username")
        password = u.get("password")

        if not username or not password:
            log.warning("skipping user with missing username/password: %r", u)
            continue

        clients.append({"username": username, "password": password})

    atomic_write({"client": clients})


def remove_user_from_credentials(username: str) -> None:
    data = load_credentials()

    data["client"] = [
        c for c in data.get("client", [])
        if c.get("username") != username
    ]

    atomic_write(data)
