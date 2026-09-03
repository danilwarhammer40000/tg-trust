import json
import logging
import os
import stat
import tempfile
from datetime import datetime
from typing import List, Dict, Optional

from filelock import FileLock

from core.dates import parse_expiry, utcnow_naive

log = logging.getLogger(__name__)

DB_PATH = os.getenv("TRUSTPANEL_DB_PATH", "/opt/trustpanel/data/users.json")
LOCK_PATH = DB_PATH + ".lock"

# every read-modify-write cycle (get -> mutate -> save) happens under this
# lock. FileLock is process-safe and already a project dependency (used in
# core/credentials.py).
_lock = FileLock(LOCK_PATH, timeout=10)

# How long an AI claim on a pending_request can sit with no recorded result
# before it's considered abandoned/crashed and safe to reclaim (either by a
# retry, or by falling through to manual approval). See
# claim_pending_request_for_ai() below and bot/handlers/receipt.py's
# _ai_in_progress_or_done(), which reads this same constant.
AI_CLAIM_STALE_SECONDS = 300

# expires_at/status are the only two fields that get redirected+propagated
# across a leader/follower group — see update_user()'s docstring.
_SYNC_KEYS = ("expires_at", "status")


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

        # A deleted leader can't leave its followers pointing at a ghost
        # username -- unlink them so they become independent records with
        # whatever expires_at/status they last had synced.
        for u in data:
            if u.get("linked_to") == username:
                u["linked_to"] = None

        save(data)


def get_user(username: str) -> Optional[Dict]:
    for u in load():
        if u.get("username") == username:
            return u
    return None


def update_user(username: str, **kwargs) -> bool:
    """
    Returns True if a matching user was found and updated.

    expires_at/status are special: if `username` is itself a follower
    (has linked_to set), a change to either of those two fields is
    redirected onto the LEADER's record instead, then that new value is
    pushed out to every follower of that leader (this user included) --
    so a follower's access can never independently drift from its
    leader's. Every other field (telegram_id, pending_request,
    notified_days, ...) is applied to `username`'s own record only, follower
    or not.

    If `username` is a leader (or independent) itself, an expires_at/status
    change still propagates outward to its own followers, if any.
    """
    with _lock:
        data = load()
        by_name = {u.get("username"): u for u in data if u.get("username")}
        user = by_name.get(username)

        if not user:
            log.warning("update_user: no such user %r (kwargs=%r)", username, kwargs)
            return False

        sync_kwargs = {k: v for k, v in kwargs.items() if k in _SYNC_KEYS}
        other_kwargs = {k: v for k, v in kwargs.items() if k not in _SYNC_KEYS}

        if other_kwargs:
            user.update(other_kwargs)

        if sync_kwargs:
            leader_username = user.get("linked_to") or username
            leader = by_name.get(leader_username, user)
            leader.update(sync_kwargs)

            for u in data:
                if u.get("linked_to") == leader_username:
                    u.update(sync_kwargs)

        save(data)
        return True


# ================= TELEGRAM / MAX LOOKUP =================

def get_user_by_telegram_id(tg_id: int) -> Optional[Dict]:
    tg_id = str(tg_id)

    for u in load():
        if str(u.get("telegram_id")) == tg_id:
            return u
    return None


def get_user_by_max_chat_id(chat_id) -> Optional[Dict]:
    chat_id = str(chat_id)

    for u in load():
        if str(u.get("max_chat_id")) == chat_id:
            return u
    return None


def username_exists(username: str) -> bool:
    return get_user(username) is not None


# ================= LEADER / FOLLOWER LINKS =================

def get_followers(leader_username: str) -> List[Dict]:
    return [u for u in load() if u.get("linked_to") == leader_username]


def get_leaders() -> List[Dict]:
    """Every user who has at least one follower."""
    data = load()
    leader_names = {u.get("linked_to") for u in data if u.get("linked_to")}
    return [u for u in data if u.get("username") in leader_names]


def get_unlinked_users() -> List[Dict]:
    """Users who are neither a leader nor a follower of anyone."""
    data = load()
    leader_names = {u.get("linked_to") for u in data if u.get("linked_to")}
    return [
        u for u in data
        if u.get("username") and not u.get("linked_to") and u.get("username") not in leader_names
    ]


def link_user(follower_username: str, leader_username: str) -> bool:
    """
    Attaches follower_username to leader_username: sets linked_to and
    immediately syncs expires_at/status from the leader's current values
    (same fields update_user() keeps in sync afterwards). Returns False if
    either username doesn't exist, or they're the same user.
    """
    if not follower_username or not leader_username or follower_username == leader_username:
        return False

    with _lock:
        data = load()
        by_name = {u.get("username"): u for u in data if u.get("username")}
        follower = by_name.get(follower_username)
        leader = by_name.get(leader_username)

        if not follower or not leader:
            return False

        follower["linked_to"] = leader_username
        follower["expires_at"] = leader.get("expires_at")
        follower["status"] = leader.get("status", "active")

        save(data)
        return True


def unlink_user(username: str) -> bool:
    """Clears linked_to on username's own record. The record keeps
    whatever expires_at/status it last had synced -- it just stops
    tracking its former leader going forward. Returns False if the user
    wasn't linked to begin with (or doesn't exist)."""
    with _lock:
        data = load()
        for u in data:
            if u.get("username") == username:
                if not u.get("linked_to"):
                    return False
                u["linked_to"] = None
                save(data)
                return True
        return False


# ================= AI AUTO-RENEWAL CLAIM =================

def claim_pending_request_for_ai(username: str) -> bool:
    """
    Atomically "claims" username's current pending_request for AI
    processing, under the same lock as every other read-modify-write here.

    Returns True if the claim succeeded (caller should proceed to run the
    Gemini pipeline). Returns False if: there's no pending_request at all,
    or it's already been claimed and that claim is still fresh (younger
    than AI_CLAIM_STALE_SECONDS) with no result recorded yet -- i.e.
    something else is actively working on it right now.

    A claim older than AI_CLAIM_STALE_SECONDS with no ai_result is treated
    as abandoned (the process handling it presumably crashed) and can be
    reclaimed -- otherwise one crash would permanently wedge that request
    out of both automatic AND manual approval.
    """
    with _lock:
        data = load()
        for u in data:
            if u.get("username") != username:
                continue

            pending = u.get("pending_request")
            if not pending:
                return False

            claimed_at = pending.get("ai_claimed_at")
            if claimed_at and not pending.get("ai_result"):
                try:
                    claimed_dt = datetime.fromisoformat(claimed_at)
                    still_fresh = (utcnow_naive() - claimed_dt).total_seconds() < AI_CLAIM_STALE_SECONDS
                except ValueError:
                    still_fresh = False  # unparseable timestamp -- treat as stale, allow reclaim

                if still_fresh:
                    return False

            pending["ai_claimed_at"] = utcnow_naive().isoformat()
            pending.pop("ai_result", None)
            u["pending_request"] = pending

            save(data)
            return True

        return False
