import io
import json
import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage" if BOT_TOKEN else None
DOCUMENT_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument" if BOT_TOKEN else None
PHOTO_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto" if BOT_TOKEN else None
GET_FILE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile" if BOT_TOKEN else None
FILE_DOWNLOAD_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else None

# Optional channel the bot posts an audit trail to: every receipt
# submission (with the file) and every auto-renewal decision (automatic
# or manual) — see core/auto_renewal.py. If unset, log_to_channel() is a
# silent no-op; core/auto_renewal.py refuses to let the admin turn
# auto-renewal ON without this configured (see is_auto_renewal_enabled's
# caller in bot/handlers/auto_renewal_review.py).
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")

# MAX (max.ru) Bot API — used to reach clients who signed up through the
# MAX bot (max_bot/) instead of Telegram. Optional: if MAX_BOT_TOKEN isn't
# set, every send_max_* call below is a harmless no-op, so a deployment
# that never set up the MAX bot is unaffected.
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_API_BASE = "https://platform-api2.max.ru"


def send_message(chat_id, text: str) -> bool:
    """Plain synchronous send via raw HTTP call to Bot API.

    Used from services/cleanup.py, which is a standalone oneshot script
    with no aiogram event loop. Inside bot/bot.py itself, prefer
    `await bot.send_message(...)` directly instead of this function.
    """
    if not API_URL or not chat_id:
        return False
    try:
        r = requests.post(API_URL, json={"chat_id": chat_id, "text": text}, timeout=10)
        if not r.ok:
            log.warning("telegram sendMessage failed for %s: %s", chat_id, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("sendMessage error for %s: %s", chat_id, e)
        return False


def send_document(file_path: str, filename: str = None, caption: str = None, chat_id=None) -> bool:
    """
    Synchronous document upload, for the same reason send_message() is
    synchronous — used by services/backup.py, which has no event loop.
    """
    target = chat_id or ADMIN_ID
    if not DOCUMENT_API_URL or not target:
        return False
    try:
        with open(file_path, "rb") as f:
            files = {"document": (filename or os.path.basename(file_path), f)}
            data = {"chat_id": target}
            if caption:
                data["caption"] = caption
            r = requests.post(DOCUMENT_API_URL, data=data, files=files, timeout=30)
        if not r.ok:
            log.warning("telegram sendDocument failed for %s: %s", target, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("sendDocument error for %s: %s", target, e)
        return False


def send_photo_bytes(
    photo_bytes: bytes,
    filename: str = "photo.jpg",
    caption: str = None,
    chat_id=None,
    reply_markup: dict = None,
) -> bool:
    """
    Synchronous photo upload FROM IN-MEMORY BYTES (as opposed to
    send_document(), which needs a local file path). This is what lets
    max_bot/handlers/receipt.py relay a receipt photo it received over the
    MAX Bot API into the Telegram admin's chat, without ever writing it to
    disk: download the bytes from MAX, hand them straight to this function.

    reply_markup, if given, is a plain dict in Telegram's own JSON shape
    (see max_bot/handlers/receipt.py for how it's built) — it gets
    JSON-encoded into the multipart form field Telegram expects.
    """
    target = chat_id or ADMIN_ID
    if not PHOTO_API_URL or not target:
        return False
    try:
        files = {"photo": (filename, io.BytesIO(photo_bytes))}
        data = {"chat_id": target}
        if caption:
            data["caption"] = caption
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        r = requests.post(PHOTO_API_URL, data=data, files=files, timeout=30)
        if not r.ok:
            log.warning("telegram sendPhoto (bytes) failed for %s: %s", target, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("sendPhoto (bytes) error for %s: %s", target, e)
        return False


def send_photo_by_file_id(file_id: str, caption: str = None, chat_id=None, reply_markup: dict = None) -> bool:
    """
    Re-sends a photo Telegram already has (by file_id) to another chat —
    no download/re-upload needed. Used for forwarding a client's receipt
    to the log channel and to the admin's auto-renewal review card,
    reusing the same file_id the client originally sent instead of
    fetching the bytes again (get_file_bytes() below is only needed when
    the raw bytes genuinely have to leave Telegram, e.g. to hand to
    Gemini).
    """
    target = chat_id or ADMIN_ID
    if not PHOTO_API_URL or not target:
        return False
    try:
        data = {"chat_id": target, "photo": file_id}
        if caption:
            data["caption"] = caption
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        r = requests.post(PHOTO_API_URL, data=data, timeout=15)
        if not r.ok:
            log.warning("telegram sendPhoto (file_id) failed for %s: %s", target, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("sendPhoto (file_id) error for %s: %s", target, e)
        return False


def send_document_by_file_id(file_id: str, caption: str = None, chat_id=None, reply_markup: dict = None) -> bool:
    """Document equivalent of send_photo_by_file_id() — see its docstring."""
    target = chat_id or ADMIN_ID
    if not DOCUMENT_API_URL or not target:
        return False
    try:
        data = {"chat_id": target, "document": file_id}
        if caption:
            data["caption"] = caption
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        r = requests.post(DOCUMENT_API_URL, data=data, timeout=15)
        if not r.ok:
            log.warning("telegram sendDocument (file_id) failed for %s: %s", target, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("sendDocument (file_id) error for %s: %s", target, e)
        return False


def get_file_bytes(file_id: str):
    """
    Downloads a Telegram-hosted file's raw bytes — getFile (resolve
    file_id -> file_path) then fetch it. Used by core/auto_renewal.py to
    hand a receipt image/PDF to the Gemini API, which needs actual bytes,
    not a Telegram file_id.

    Returns (bytes, file_path) on success, (None, None) on any failure —
    callers treat that as "couldn't read the receipt" and fall back to
    manual approval, same as a Gemini failure.
    """
    if not GET_FILE_URL or not file_id:
        return None, None
    try:
        r = requests.get(GET_FILE_URL, params={"file_id": file_id}, timeout=15)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]

        r2 = requests.get(f"{FILE_DOWNLOAD_BASE}/{file_path}", timeout=30)
        r2.raise_for_status()
        return r2.content, file_path
    except (requests.RequestException, KeyError, ValueError) as e:
        log.error("get_file_bytes failed for file_id=%s: %s", file_id, e)
        return None, None


def log_to_channel(text: str, file_id: str = None, is_photo: bool = True) -> bool:
    """
    Posts to the audit-trail channel (LOG_CHANNEL_ID) — every receipt
    submission and every auto-renewal decision goes through here. Silent
    no-op if LOG_CHANNEL_ID isn't configured (auto-renewal itself refuses
    to turn on without it — see bot/handlers/auto_renewal_review.py — but
    this function stays safe to call unconditionally regardless).
    """
    if not LOG_CHANNEL_ID:
        return False

    if file_id:
        if is_photo:
            return send_photo_by_file_id(file_id, caption=text, chat_id=LOG_CHANNEL_ID)
        return send_document_by_file_id(file_id, caption=text, chat_id=LOG_CHANNEL_ID)

    return send_message(LOG_CHANNEL_ID, text)


def notify_admin(text: str) -> None:
    send_message(ADMIN_ID, text)


def notify_user(user: dict, text: str) -> None:
    """
    Sends to whichever platform(s) this user is actually bound on. A user
    normally has exactly one of telegram_id/max_chat_id set (depending on
    which bot they signed up through), but nothing stops both being sent
    if somehow both are present.
    """
    tg_id = user.get("telegram_id")
    if tg_id:
        send_message(tg_id, text)

    max_chat_id = user.get("max_chat_id")
    if max_chat_id:
        send_max_message(max_chat_id, text)


# ---------------- MAX ----------------

def send_max_message(chat_id, text: str) -> bool:
    """
    Synchronous send via the raw MAX Bot API — same rationale as
    send_message()'s Telegram equivalent: services/cleanup.py and
    services/post_disable_reminders.py are standalone scripts with no
    event loop, so they can't use the maxapi library's async Bot directly.

    NOTE: MAX requires requests to platform-api2.max.ru (NOT the older
    platform-api.max.ru) and, per MAX's own docs, that your server trusts
    the Минцифры (Ministry of Digital Development) TLS certificate for
    that domain — see README.md's MAX bot setup section.
    """
    if not MAX_BOT_TOKEN or not chat_id:
        return False
    try:
        r = requests.post(
            f"{MAX_API_BASE}/messages",
            params={"chat_id": chat_id},
            headers={"Authorization": MAX_BOT_TOKEN},
            json={"text": text},
            timeout=10,
        )
        if not r.ok:
            log.warning("MAX sendMessage failed for %s: %s", chat_id, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("MAX sendMessage error for %s: %s", chat_id, e)
        return False
