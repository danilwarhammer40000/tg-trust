"""
Trial tracking (survives user deletion) + trial-signup helpers.

Kept in a separate file from users.json on purpose: if an admin deletes an
expired trial account (via "🗑 Удаление пользователей"), that shouldn't let
the same Telegram account register for a second free trial.

A trial is never issued automatically: the client files a *request*
(trial_requests.json) and the admin approves or rejects it — see
bot/handlers/trial_requests.py.
"""
import json
import logging
import os
import re
import secrets
import shutil
import string
import tempfile
from typing import Dict, Optional

from filelock import FileLock

from core.db import get_user
from core.dates import utcnow_naive
from core.paths import TRIAL_REQUESTS_PATH, TRIAL_USED_PATH

log = logging.getLogger(__name__)


# ---------------- small JSON store (atomic + locked + .bak fallback) ----------------

def _lock_for(path: str) -> FileLock:
    return FileLock(path + ".lock", timeout=10)


def _read_json(path: str, default):
    """Reads `path`; on a corrupt file falls back to the .bak copy (written on
    every save) instead of silently pretending the store is empty — an empty
    trial_used.json would hand out second free trials to everybody."""
    for candidate in (path, path + ".bak"):
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            if candidate == path and not os.path.exists(path + ".bak"):
                return default
            continue
        except (OSError, json.JSONDecodeError) as e:
            log.error("could not read %s: %s", candidate, e)
    return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            shutil.copy2(path, path + ".bak")
        except (OSError, json.JSONDecodeError):
            pass  # current file is broken — keep the older .bak as it is
    with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path), encoding="utf-8") as tmp:
        json.dump(data, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)


# ---------------- used trials ----------------

def load_trial_used_ids() -> set:
    return set(_read_json(TRIAL_USED_PATH, []))


def save_trial_used_ids(ids: set):
    with _lock_for(TRIAL_USED_PATH):
        _write_json(TRIAL_USED_PATH, sorted(ids))


def has_used_trial(account_id) -> bool:
    return account_id in load_trial_used_ids()


def mark_trial_used(account_id):
    with _lock_for(TRIAL_USED_PATH):
        ids = set(_read_json(TRIAL_USED_PATH, []))
        ids.add(account_id)
        _write_json(TRIAL_USED_PATH, sorted(ids))


# ---------------- trial requests (admin approval) ----------------

def _load_requests() -> Dict[str, dict]:
    data = _read_json(TRIAL_REQUESTS_PATH, {})
    return data if isinstance(data, dict) else {}


def get_trial_request(account_id) -> Optional[dict]:
    return _load_requests().get(str(account_id))


def add_trial_request(account_id, display_name: Optional[str], handle: Optional[str]) -> bool:
    """Records a pending request. Returns False if this account already has one."""
    with _lock_for(TRIAL_REQUESTS_PATH):
        requests_ = _load_requests()
        if str(account_id) in requests_:
            return False
        requests_[str(account_id)] = {
            "account_id": account_id,
            "display_name": display_name,
            "handle": handle,
            "requested_at": utcnow_naive().isoformat(),
        }
        _write_json(TRIAL_REQUESTS_PATH, requests_)
        return True


def pop_trial_request(account_id) -> Optional[dict]:
    """Atomically removes and returns the request (None if already handled) —
    whoever gets a non-None value owns the decision, so a double tap on
    «Одобрить» can never create two accounts."""
    with _lock_for(TRIAL_REQUESTS_PATH):
        requests_ = _load_requests()
        req = requests_.pop(str(account_id), None)
        if req is not None:
            _write_json(TRIAL_REQUESTS_PATH, requests_)
        return req


def generate_trial_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify_name(name: str) -> str:
    """Transliterates Cyrillic to Latin, then strips anything outside a-z0-9."""
    lowered = name.lower()
    transliterated = "".join(RU_TO_LAT.get(ch, ch) for ch in lowered)
    return re.sub(r"[^a-z0-9]", "", transliterated)


def generate_username_from_name(display_name, handle, account_id) -> str:
    """
    display_name: first name / full name shown by the platform.
    handle: a secondary fallback (Telegram @username).
    account_id: numeric id, used as a uniqueness suffix.
    """
    base = slugify_name(display_name) if display_name else ""

    if not base and handle:
        base = slugify_name(handle)

    if not base:
        base = "user"

    id_suffix = str(account_id)

    # Reserve room for "_" + id, keep total length within the 20-char limit
    max_base_len = 20 - 1 - len(id_suffix)

    if max_base_len < 1:
        # account_id itself is already at the limit (not realistic today, but just in case)
        return id_suffix[-20:]

    base = base[:max_base_len] or "u"

    candidate = f"{base}_{id_suffix}"

    # account_id is unique per (platform, account), and has_used_trial()
    # already blocks a second trial from the same account, so a collision
    # here would only happen from a stale/orphaned record — this is just a
    # paranoid fallback.
    if get_user(candidate):
        candidate = f"{base}_{id_suffix}_{secrets.randbelow(9000) + 1000}"[:20]

    return candidate
