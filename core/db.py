import json
import logging
import os
import stat
import tempfile
from typing import List, Dict, Optional

from filelock import FileLock

from core.dates import parse_expiry

log = logging.getLogger(__name__)

DB_PATH = os.getenv("TRUSTPANEL_DB_PATH", "/opt/trustpanel/data/users.json")
LOCK_PATH = DB_PATH + ".lock"

# CHANGED: every read-modify-write cycle (get -> mutate -> save) now happens
# under this lock. Previously two concurrent bot handlers (e.g. an admin
# extending a user while cleanup.py disables another) could race: both load()
# the same snapshot, both save() their own version, and whichever wrote last
# silently discards the other's change. FileLock is process-safe and already
# a project dependency (used in core/credentials.py), so no new dependency.
_lock = FileLock(LOCK_PATH, timeout=10)


def _ensure():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if not os.path.exists(DB_PATH):
        with open(DB_PATH, "w") as f:
            json.dump([], f)
        _chmod_private(DB_PATH)


def _chmod_private(path: str) -> None:
    """Users' passwords live in this file — keep it unreadable by other users."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as e:
        log.warning("could not chmod %s: %s", path, e)


def load() -> List[Dict]:
    _ensure()
    try:
        with open(DB_PATH, "r") as f:
            data = json.load(f)
        return [u for u in data if isinstance(u, dict)]
    except (OSError, json.JSONDecodeError) as e:
        log.error("failed to load %s: %s", DB_PATH, e)
        return []


def save(data: List[Dict]):
    _ensure()
    tmp_dir = os.path.dirname(DB_PATH)

    with tempfile.NamedTemporaryFile("w", delete=False, dir=tmp_dir) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name

    _chmod_private(tmp_path)
    os.replace(tmp_path, DB_PATH)


# ================= SORTING =================

def _sort_key(u: Dict):
    username = (u.get("username") or "").lower()
    expires_at = u.get("expires_at")

    if not expires_at:
        # unlimited access -> always on top, alphabetical
        return (0, "", username)

    dt = parse_expiry(expires_at)
    if dt is None:
        # unparseable date -> pushed to the very end
        return (2, 0, username)

    # further-out expiry sorts higher; sort by -ordinal (descending date)
    return (1, -dt.toordinal(), username)


# ================= USERS =================

def list_users() -> List[Dict]:
    data = load()
    return sorted(data, key=_sort_key)


def add_user(user: Dict) -> Dict:
    with _lock:
        data = load()
        data.append(user)
        save(data)
    return user


def delete_user(username: str) -> None:
    with _lock:
        data = load()
        data = [u for u in data if u.get("username") != username]
        save(data)


def get_user(username: str) -> Optional[Dict]:
    for u in load():
        if u.get("username") == username:
            return u
    return None


def update_user(username: str, **kwargs) -> bool:
    """Returns True if a matching user was found and updated."""
    with _lock:
        data = load()
        found = False

        for u in data:
            if u.get("username") == username:
                u.update(kwargs)
                found = True
                break

        if found:
            save(data)
        else:
            log.warning("update_user: no such user %r (kwargs=%r)", username, kwargs)

        return found


# ================= TELEGRAM LOOKUP =================

def get_user_by_telegram_id(tg_id: int) -> Optional[Dict]:
    tg_id = str(tg_id)

    for u in load():
        if str(u.get("telegram_id")) == tg_id:
            return u
    return None


def username_exists(username: str) -> bool:
    return get_user(username) is not None
