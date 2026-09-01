"""
Synchronous wrapper around the Gemini API's generateContent endpoint,
used to extract structured data from a payment receipt screenshot/PDF.

Deliberately does NOT ask Gemini to decide whether to auto-renew — it
only extracts what's on the image (amount, who/what bank, how confident
it is). The actual approve/reject decision is core/auto_renewal.py's
business-rule logic, in plain Python, so a bad extraction or
hallucination can't directly grant VPN access — see
evaluate_receipt_extraction() there.

NOTE: the extraction schema deliberately does NOT include a payment date.
Auto-renewal only cares how much money was transferred — see
core/auto_renewal.py's module docstring for why the date check was
removed.

Synchronous by design (plain `requests`, no aiohttp) so this can be called
identically from an async bot handler (via run_in_executor, same pattern
already used for core.service.safe_sync) and from the standalone
services/auto_renewal_overdue_check.py script, which has no event loop.
"""
import json
import logging
import os
import base64

import requests

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# gemini-2.5-flash was retired (Gemini API now returns 404 for it,
# telling callers to switch to gemini-3.6-flash). Google's flash-tier
# models seem to get retired every few months (2.0 -> 2.5 -> 3.x already),
# so this is deliberately read from GEMINI_MODEL first — if this breaks
# again, set GEMINI_MODEL in .env instead of needing a code change.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Google's Generative Language API rejects requests by the caller's
# apparent geographic location (HTTP 400 FAILED_PRECONDITION, "User
# location is not supported for the API use") -- unrelated to the API
# key itself, and can differ between two servers even at the "same"
# host/location if they sit on different subnets/ASNs. If that happens,
# set GEMINI_PROXY_URL in .env (e.g. "http://user:pass@proxy-host:port"
# or "socks5://host:port") to route ONLY the Gemini call through an exit
# point in a supported region -- deliberately NOT the generic
# HTTP_PROXY/HTTPS_PROXY env vars, since those would silently redirect
# every other outbound call this bot makes too (Telegram Bot API, MAX Bot
# API, etc.), which almost certainly isn't wanted.
GEMINI_PROXY_URL = os.getenv("GEMINI_PROXY_URL")
GEMINI_PROXIES = {"http": GEMINI_PROXY_URL, "https": GEMINI_PROXY_URL} if GEMINI_PROXY_URL else None

# Keep this in Russian: the receipts themselves are Russian bank transfer
# screenshots, and error/notes fields written in Russian are what end up
# quoted straight into the admin's log-channel messages.
EXTRACTION_PROMPT = """\
Ты анализируешь скриншот или PDF банковского перевода (чек об оплате подписки).
Верни СТРОГО валидный JSON, без markdown-разметки, без пояснений вне JSON, \
со следующими полями:

{
  "readable": true или false,
  "amount": число (сумма перевода в рублях) или null,
  "recipient_or_bank": "строка" (получатель, банк-отправитель — что видно) или null,
  "confidence": число от 0.0 до 1.0 (насколько ты уверен в извлечённых данных),
  "notes": "краткая причина по-русски, если readable=false или уверенность низкая, иначе пустая строка"
}

Правила:
- Нас интересует ТОЛЬКО сумма перевода — дата платежа не проверяется и не важна,
  можно её игнорировать, даже если она нечитаема или отсутствует.
- Если это вообще не похоже на банковский чек/перевод (случайное фото, другой \
документ, нечитаемое изображение) — readable=false, остальные поля null.
- Никогда не выдумывай значения, которых не видишь на изображении. Если что-то \
не видно чётко — используй null для этого поля и снижай confidence.
- amount — только число (например 300), без символа валюты и пробелов.
"""


class GeminiError(Exception):
    pass


def extract_receipt_data(file_bytes: bytes, mime_type: str) -> dict:
    """
    Returns a dict matching EXTRACTION_PROMPT's schema. Raises GeminiError
    on any failure (missing API key, network error, malformed response) —
    callers (core/auto_renewal.py) treat that the same as "couldn't read
    the receipt" and fall back to the normal manual-approval queue.
    """
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")

    payload = {
        "contents": [{
            "parts": [
                {"text": EXTRACTION_PROMPT},
                {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(file_bytes).decode("ascii")}},
            ]
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0,
        },
    }

    try:
        r = requests.post(
            GEMINI_API_URL,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=60,
            proxies=GEMINI_PROXIES,
        )
    except requests.RequestException as e:
        raise GeminiError(f"network error: {e}") from e

    if not r.ok:
        if r.status_code == 400 and "location is not supported" in r.text.lower():
            raise GeminiError(
                "Google заблокировал запрос по геолокации сервера (не по ключу API). "
                "Настройте GEMINI_PROXY_URL в .env на прокси в поддерживаемом регионе — "
                f"см. комментарий в core/gemini_client.py. Исходный ответ: {r.text[:200]}"
            )
        raise GeminiError(f"HTTP {r.status_code}: {r.text[:300]}")

    try:
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        extraction = json.loads(text)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        raise GeminiError(f"unexpected response shape: {e}") from e

    if not isinstance(extraction, dict):
        raise GeminiError("response was valid JSON but not an object")

    return extraction
