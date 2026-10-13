import json
import logging
import os
import shutil
import stat
import tempfile
import time
from datetime import datetime
from typing import List, Dict, Optional

from filelock import FileLock

from core.dates import parse_expiry, utcnow_naive

log = logging.getLogger(__name__)

DB_PATH = os.getenv("TRUSTPANEL_DB_PATH", "/opt/trustpanel/data/users.json")
LOCK_PATH = DB_PATH + ".lock"
# Copy of the last known-good users.json, refreshed on every save(). If the
# main file is ever found corrupted, load() rolls back to this one.
BACKUP_PATH = DB_PATH + ".bak"

# every read-modify-write cycle (get -> mutate -> save) happens under this
# lock. FileLock is process-safe and already a project dependency (used in
# core/credentials.py).
_lock = FileLock(LOCK_PATH, timeout=10)

# How long an AI claim on a pending_request can sit with no recorded result
# before it's considered abandoned/crashed and safe to reclaim (either by a
# retry, or by falling through to manual approval). See
# claim_pending_request_for_ai() below and bot/handlers/receipt.py's
# _ai_in_progress_or_done(), which reads this same constant.
# Must be longer than the slowest legitimate run of the Gemini pipeline (model
# cascade with retries can take 20+ minutes in the worst case), otherwise a
# second process reclaims a claim that is still being worked on. As a second
# line of defence apply-time uses pop_pending_request() below.
AI_CLAIM_STALE_SECONDS = 3600

# expires_at/status are the only two fields that get redirected+propagated
# across a leader/follower group — see update_user()'s docstring.
_SYNC_KEYS = ("expires_at", "status")


class DBCorruptedError(Exception):
    """users.json is unreadable AND there is no valid backup to roll back to.
    Raised instead of returning [] on purpose: an empty list would be written
    back by the next add/update (wiping every client) and would make the next
    credentials rebuild disconnect everyone."""
    pass


class DuplicateUsernameError(Exception):
    """Raised by add_user() when the username is already taken. Username
    is the de-facto primary key everywhere in this file -- get_user()
    returns the first match, update_user() only touches the first match,
    delete_user() removes EVERY matching record at once -- so a duplicate
    doesn't just sit there harmlessly, it silently breaks all three the
    moment it exists. Enforced here, in the one function that actually
    creates a user record, rather than only at whichever call site
    happens to check first, so no future code path (a new handler, a
    migration script, a second VPN integration) can create one by
    accident."""
    pass


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


def _read_valid(path: str):
    """Parses `path` as a users list. Returns the list, or None if the file is
    missing/unreadable/not a JSON list."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return data


def _atomic_write(path: str, data) -> None:
    tmp_dir = os.path.dirname(path)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=tmp_dir) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    _chmod_private(tmp_path)
    os.replace(tmp_path, path)


def _notify_admin_safely(text: str) -> None:
    # Lazy import + blanket except: a notification problem must never turn a
    # successful rollback into a crash.
    try:
        from core.notify import notify_admin
        notify_admin(text)
    except Exception:
        log.exception("could not notify admin about DB rollback")


def _rollback_from_backup() -> List[Dict]:
    """Called when DB_PATH can't be parsed. Restores the last good backup (the
    broken file is kept next to it as users.json.corrupt-<ts> for inspection)
    and returns its contents. Raises DBCorruptedError when there is nothing
    valid to restore."""
    with _lock:
        # Someone else may have fixed it while we waited for the lock.
        data = _read_valid(DB_PATH)
        if data is not None:
            return [u for u in data if isinstance(u, dict)]

        backup = _read_valid(BACKUP_PATH)
        if backup is None:
            log.error("%s is corrupted and no valid backup exists at %s", DB_PATH, BACKUP_PATH)
            _notify_admin_safely(
                "🔥 users.json повреждён, а рабочей резервной копии нет. Бот не будет "
                "ничего записывать, чтобы не затереть данные. Восстановите файл вручную "
                "(Настройки → База данных → импорт, либо скопируйте последний бэкап)."
            )
            raise DBCorruptedError(DB_PATH)

        corrupt_copy = f"{DB_PATH}.corrupt-{int(time.time())}"
        try:
            shutil.copy2(DB_PATH, corrupt_copy)
            _chmod_private(corrupt_copy)
        except OSError as e:
            log.warning("could not keep a copy of the corrupted file: %s", e)

        _atomic_write(DB_PATH, backup)
        log.error("%s was corrupted — rolled back to %s (%d records)", DB_PATH, BACKUP_PATH, len(backup))
        _notify_admin_safely(
            f"⚠️ users.json был повреждён — автоматически восстановлен из резервной копии "
            f"({len(backup)} записей). Повреждённый файл сохранён как {os.path.basename(corrupt_copy)}. "
            f"Изменения, сделанные после последнего успешного сохранения, могли потеряться."
        )
        return [u for u in backup if isinstance(u, dict)]


def load() -> List[Dict]:
    _ensure()
    data = _read_valid(DB_PATH)
    if data is None:
        log.error("failed to load %s", DB_PATH)
        return _rollback_from_backup()
    return [u for u in data if isinstance(u, dict)]


def save(data: List[Dict]):
    _ensure()

    # Refuse to write anything that isn't a list of records — the one thing
    # that must never happen is replacing the DB with garbage.
    if not isinstance(data, list) or not all(isinstance(u, dict) for u in data):
        raise ValueError("save(): data must be a list of dicts")

    # Keep the previous state (only if it is itself valid) as the rollback copy.
    if _read_valid(DB_PATH) is not None:
        try:
            shutil.copy2(DB_PATH, BACKUP_PATH)
            _chmod_private(BACKUP_PATH)
        except OSError as e:
            log.warning("could not refresh DB backup: %s", e)

    _atomic_write(DB_PATH, data)


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
        username = user.get("username")
        if any(u.get("username") == username for u in data):
            raise DuplicateUsernameError(username)
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


# ================= TELEGRAM LOOKUP =================

def get_user_by_telegram_id(tg_id: int) -> Optional[Dict]:
    tg_id = str(tg_id)

    for u in load():
        if str(u.get("telegram_id")) == tg_id:
            return u
    return None


def get_user_by_invite_token(token: str) -> Optional[Dict]:
    """See core.invite for token generation/regeneration. Only ever
    matches a token that's still the CURRENT one on that user's record —
    regenerating a user's invite link overwrites invite_token, so an old
    link (even if never used) stops resolving to anything the moment a
    new one is generated."""
    if not token:
        return None
    for u in load():
        if u.get("invite_token") == token:
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
                except (ValueError, TypeError):
                    still_fresh = False  # unparseable timestamp -- treat as stale, allow reclaim

                if still_fresh:
                    return False

            pending["ai_claimed_at"] = utcnow_naive().isoformat()
            pending.pop("ai_result", None)
            u["pending_request"] = pending

            save(data)
            return True

        return False


def pop_pending_request(username: str):
    """Atomically removes and returns username's pending_request, or None if
    there is none. Whoever gets a non-None result owns the request — used so
    that an auto-renewal, a manual approval or a second AI processor can never
    all act on the same receipt."""
    with _lock:
        data = load()
        for u in data:
            if u.get("username") != username:
                continue
            pending = u.get("pending_request")
            if not pending:
                return None
            u["pending_request"] = None
            save(data)
            return pending
        return None
