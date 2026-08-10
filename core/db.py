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

        # If the deleted user was a leader, its followers would otherwise be
        # left with a dangling linked_to pointing at nobody. Unlink them so
        # they become independent (keeping whatever expires_at/status they
        # last had synced) instead of silently orphaned.
        for u in data:
            if u.get("linked_to") == username:
                u["linked_to"] = None

        save(data)


def get_user(username: str) -> Optional[Dict]:
    for u in load():
        if u.get("username") == username:
            return u
    return None


# ================= LEADER / FOLLOWER LINKING =================
#
# A "follower" record has linked_to set to its leader's username. Its
# expires_at/status are meant to always mirror the leader's — see
# update_user() below, which is the single place that enforces this:
# any change to expires_at/status is redirected to the leader (if the
# target is a follower) and then fanned out to every follower of whoever
# actually got updated. notified_days and telegram_id are deliberately
# NOT synced — each linked account can still have its own Telegram and
# its own notification history, only the actual access (expiry + active/
# inactive) is shared.

_SYNCED_FIELDS = ("expires_at", "status")


def get_followers(username: str) -> List[Dict]:
    return [u for u in load() if u.get("linked_to") == username]


def link_user(follower_username: str, leader_username: str) -> bool:
    """
    Makes follower_username a follower of leader_username, immediately
    copying the leader's current expires_at/status onto it. Returns False
    if either username doesn't exist or they're the same user.
    """
    if follower_username == leader_username:
        return False

    with _lock:
        data = load()
        leader = next((u for u in data if u.get("username") == leader_username), None)
        follower = next((u for u in data if u.get("username") == follower_username), None)

        if leader is None or follower is None:
            return False

        follower["linked_to"] = leader_username
        for field in _SYNCED_FIELDS:
            follower[field] = leader.get(field)

        save(data)
        return True


def unlink_user(username: str) -> bool:
    """Detaches a follower — it keeps its last-synced expires_at/status but
    stops mirroring the (former) leader going forward."""
    with _lock:
        data = load()
        target = next((u for u in data if u.get("username") == username), None)

        if target is None or not target.get("linked_to"):
            return False

        target["linked_to"] = None
        save(data)
        return True


def update_user(username: str, **kwargs) -> bool:
    """
    Returns True if a matching user was found and updated.

    If kwargs touches expires_at and/or status, and `username` is itself a
    follower (has linked_to set), the change is redirected onto the leader
    instead — a follower's real access is never independently editable.
    Whichever record actually ends up updated (leader or a plain
    independent user), the same fields then get copied onto every one of
    ITS followers, so the whole group stays in sync in one atomic write.
    """
    touches_sync_fields = any(f in kwargs for f in _SYNCED_FIELDS)

    with _lock:
        data = load()
        target = next((u for u in data if u.get("username") == username), None)

        if target is None:
            log.warning("update_user: no such user %r (kwargs=%r)", username, kwargs)
            return False

        effective = target
        if touches_sync_fields and target.get("linked_to"):
            leader = next((u for u in data if u.get("username") == target["linked_to"]), None)
            if leader is not None:
                effective = leader
            # else: dangling link (leader was deleted but this record wasn't
            # cleaned up somehow) -- fall back to updating the record itself.

        effective.update(kwargs)

        if touches_sync_fields:
            for u in data:
                if u is not effective and u.get("linked_to") == effective.get("username"):
                    for field in _SYNCED_FIELDS:
                        if field in kwargs:
                            u[field] = effective.get(field)

        save(data)
        return True


# ================= TELEGRAM LOOKUP =================

def get_user_by_telegram_id(tg_id: int) -> Optional[Dict]:
    tg_id = str(tg_id)

    for u in load():
        if str(u.get("telegram_id")) == tg_id:
            return u
    return None


def username_exists(username: str) -> bool:
    return get_user(username) is not None
