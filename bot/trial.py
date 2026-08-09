"""
Trial tracking (survives user deletion) + trial-signup helpers.

Kept in a separate file from users.json on purpose: if an admin deletes an
expired trial account (via "🗑 Удаление пользователей"), that shouldn't let
the same Telegram account register for a second free trial.

Used by handlers/start.py (onboarding) and handlers/database.py (the
🎟 Управление триалами admin screens).
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


def has_used_trial(telegram_id: int) -> bool:
    return telegram_id in load_trial_used_ids()


def mark_trial_used(telegram_id: int):
    ids = load_trial_used_ids()
    ids.add(telegram_id)
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


def generate_username_from_name(first_name, tg_username, tg_id) -> str:
    base = slugify_name(first_name) if first_name else ""

    if not base and tg_username:
        base = slugify_name(tg_username)

    if not base:
        base = "user"

    tg_suffix = str(tg_id)

    # Reserve room for "_" + telegram_id, keep total length within the 20-char limit
    max_base_len = 20 - 1 - len(tg_suffix)

    if max_base_len < 1:
        # tg_id itself is already at the limit (not realistic today, but just in case)
        return tg_suffix[-20:]

    base = base[:max_base_len] or "u"

    candidate = f"{base}_{tg_suffix}"

    # telegram_id is unique per account, and has_used_trial() already blocks a
    # second trial from the same account, so a collision here would only
    # happen from a stale/orphaned record — this is just a paranoid fallback.
    if get_user(candidate):
        candidate = f"{base}_{tg_suffix}_{secrets.randbelow(9000) + 1000}"[:20]

    return candidate
