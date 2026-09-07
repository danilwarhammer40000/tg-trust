"""
Persistent chat history between the admin and clients, grouped by
username, kept INDEFINITELY by design (explicit admin choice — no
automatic expiry or trimming of storage). Feeds
bot/handlers/settings.py's "💬 Переписки" browser.

Captures: client -> admin free-text/photo/document messages sent via
"✉️ Написать администратору" (bot/handlers/feedback.py's
client_feedback_send / feedback_media_as_message — NOT
feedback_media_as_receipt, since a payment receipt already has its own
approval/log trail and isn't "conversation"), and admin -> client
personal messages sent via "↩️ Ответить" or list_users.py's "✉️ Написать"
(feedback.py's personal_message_confirm) — both directions funnel through
add_message() below.

Storage: a flat JSON array on disk (core.paths.MESSAGES_PATH), guarded by
the same FileLock-based read-modify-write pattern core/db.py uses, so
concurrent writes from different handlers can't race and corrupt the
file.

Display-time limiting is NOT storage limiting — storage is never
automatically trimmed. get_messages()'s `limit` only bounds what gets
rendered back to the admin in one screen (see bot/handlers/settings.py).
Deleting a client's entire history is a separate, explicit admin action
(delete_conversation()) — never automatic.
"""
import json
import logging
import os
from datetime import datetime

from filelock import FileLock

from core.paths import MESSAGES_PATH

log = logging.getLogger(__name__)

_LOCK_PATH = MESSAGES_PATH + ".lock"
_lock = FileLock(_LOCK_PATH, timeout=10)


def _load() -> list:
    try:
        with open(MESSAGES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(data: list) -> None:
    os.makedirs(os.path.dirname(MESSAGES_PATH), exist_ok=True)
    # Same atomic-write pattern as core/db.py — write to a temp file in
    # the same directory, then os.replace() so a crash mid-write can
    # never leave messages.json truncated/corrupted.
    tmp_path = MESSAGES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, MESSAGES_PATH)


def add_message(username: str, direction: str, text: str = None, file_id: str = None, is_photo: bool = True) -> None:
    """direction is "in" (client -> admin) or "out" (admin -> client)."""
    with _lock:
        data = _load()
        data.append({
            "username": username,
            "direction": direction,
            "text": text,
            "file_id": file_id,
            "is_photo": is_photo,
            "timestamp": datetime.utcnow().isoformat(),
        })
        _save(data)


def get_messages(username: str, limit: int = None) -> list:
    """Chronological, oldest first. `limit`, if given, returns only the
    LAST `limit` messages for display — never trims what's on disk."""
    with _lock:
        data = [m for m in _load() if m.get("username") == username]
    if limit:
        data = data[-limit:]
    return data


def list_conversations() -> list:
    """One entry per username with at least one stored message, each
    {username, count, last_at}, newest-first by last_at — used to render
    the client picker in "💬 Переписки"."""
    with _lock:
        data = _load()

    by_user = {}
    for m in data:
        u = m.get("username")
        if not u:
            continue
        entry = by_user.setdefault(u, {"username": u, "count": 0, "last_at": None})
        entry["count"] += 1
        ts = m.get("timestamp") or ""
        if not entry["last_at"] or ts > entry["last_at"]:
            entry["last_at"] = ts

    return sorted(by_user.values(), key=lambda e: e["last_at"] or "", reverse=True)


def delete_conversation(username: str) -> int:
    """Deletes ALL stored messages for one client. Returns how many were
    removed. Admin-triggered only — there is no automatic/scheduled
    deletion of any kind, per the "храним бессрочно" requirement; this is
    the only way history for a client ever goes away."""
    with _lock:
        data = _load()
        remaining = [m for m in data if m.get("username") != username]
        removed = len(data) - len(remaining)
        if removed:
            _save(remaining)
    return removed
