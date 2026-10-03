"""
Trial tracking (survives user deletion) + trial-signup helpers.

Moved here from bot/trial.py: this logic was never actually Telegram-
specific (it just needs a numeric account id + a display name), and the
MAX bot (max_bot/) needs the exact same trial-abuse tracking and username
generation. One shared trial_used.json, one place to fix bugs in either.

Kept in a separate file from users.json on purpose: if an admin deletes an
expired trial account (via "🗑 Удаление пользователей"), that shouldn't let
the same account register for a second free trial.
"""
import json
import os
import re
import secrets
import string

from core.db import get_user
from core.paths import TRIAL_USED_PATH


def load_trial_used_ids() -> set:
    try:
        with open(TRIAL_USED_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_trial_used_ids(ids: set):
    os.makedirs(os.path.dirname(TRIAL_USED_PATH), exist_ok=True)
    with open(TRIAL_USED_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


def has_used_trial(account_id) -> bool:
    """account_id: a Telegram user id (int) or a MAX chat id (int) — the two
    platforms use separate id spaces, but both are just ints stored in the
    same set here. A person trying both messengers can still get one trial
    per platform; that's an accepted edge case, not a bug."""
    return account_id in load_trial_used_ids()


def mark_trial_used(account_id):
    ids = load_trial_used_ids()
    ids.add(account_id)
    save_trial_used_ids(ids)


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
    handle: a secondary fallback (Telegram @username, or None on MAX).
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
