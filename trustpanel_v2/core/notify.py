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


def notify_admin(text: str) -> None:
    send_message(ADMIN_ID, text)


def notify_user(user: dict, text: str) -> None:
    tg_id = user.get("telegram_id")
    if tg_id:
        send_message(tg_id, text)
