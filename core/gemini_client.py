"""
Synchronous wrapper around the Gemini API's generateContent endpoint,
used to extract structured data from a payment receipt screenshot/PDF.

Deliberately does NOT ask Gemini to decide whether to auto-renew — it
only extracts what's on the image (amount). The actual approve/reject
decision is core/auto_renewal.py's business-rule logic, in plain Python,
so a bad extraction or hallucination can't directly grant VPN access.

Synchronous by design (plain `requests`, no aiohttp) so this can be called
identically from an async bot handler (via run_in_executor) and from the
standalone services/auto_renewal_overdue_check.py script, which has no
event loop.

PROXY (Cloudflare Worker / any HTTPS reverse-proxy) — three-state, not
two, on purpose:
  1. Not configured at all (GEMINI_PROXY_URL unset in .env) ->
     proxy_configured() is False, bot/handlers/auto_renewal_review.py
     doesn't even show the toggle button, always calls Google directly.
  2. Configured AND enabled (the normal case once set up) ->
     is_proxy_enabled() True, calls go through GEMINI_PROXY_URL.
  3. Configured but temporarily DISABLED via the bot's live toggle
     (autoren:toggle_proxy) -> lets the admin fall back to a direct
     connection without touching .env or restarting the bot, e.g. to
     check whether a currently-down proxy is the actual cause of a
     Gemini failure. This ON/OFF state is separate from whether a URL is
     even set, and is persisted the same way every other auto-renewal
     setting is — see core.auto_renewal.toggle_gemini_proxy_enabled().

This exists because generativelanguage.googleapis.com enforces a region
allowlist that a Russian-hosted VPS IP typically fails
(FAILED_PRECONDITION "User location is not supported"), independent of
whether the API key itself is valid. The worker is expected to forward
whatever path+query it receives straight to Google (and may inject its
own key, in which case our own ?key= is simply redundant/ignored — either
way works).
"""
import json
import logging
import os
import base64

import requests

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# gemini-2.5-flash was retired (Gemini API now returns 404 for it, telling
# callers to switch to gemini-3.6-flash). Google's flash-tier models seem
# to get retired every few months, so this is deliberately read from
# GEMINI_MODEL first — if this breaks again, set GEMINI_MODEL in .env
# instead of needing a code change.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Cloudflare Worker (or any HTTPS reverse-proxy) base URL, e.g.
# https://your-worker.workers.dev — set in .env, changeable live via the
# bot's proxy toggle (see module docstring) but the URL itself is only
# read from .env (changing the URL, as opposed to on/off, still needs a
# .env edit + bot restart — only the enable/disable state is live).
GEMINI_PROXY_URL = os.getenv("GEMINI_PROXY_URL", "").strip()

DIRECT_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

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
  "payment_date": "YYYY-MM-DD" (дата совершения платежа) или null,
  "recipient_or_bank": "строка" (получатель, банк-отправитель — что видно) или null,
  "confidence": число от 0.0 до 1.0 (насколько ты уверен в извлечённых данных),
  "notes": "краткая причина по-русски, если readable=false или уверенность низкая, иначе пустая строка"
}

Правила:
- Если это вообще не похоже на банковский чек/перевод (случайное фото, другой \
документ, нечитаемое изображение) — readable=false, остальные поля null.
- Никогда не выдумывай значения, которых не видишь на изображении. Если что-то \
не видно чётко — используй null для этого поля и снижай confidence.
- amount — только число (например 300), без символа валюты и пробелов.
- payment_date — если год не указан на чеке явно, но дата похожа на недавнюю, \
можно принять текущий год; если совсем не понятно — null.
"""


class GeminiError(Exception):
    pass


def proxy_configured() -> bool:
    """Whether GEMINI_PROXY_URL is set in .env at all — the bot only shows
    the live enable/disable toggle when this is True (nothing to toggle
    otherwise)."""
    return bool(GEMINI_PROXY_URL)


def is_proxy_enabled() -> bool:
    """Whether outbound Gemini calls actually go through the configured
    proxy RIGHT NOW. Only meaningful when proxy_configured() is True —
    delegates the persisted on/off state to core.auto_renewal, which
    already owns every other auto-renewal setting's storage."""
    if not proxy_configured():
        return False
    from core.auto_renewal import get_setting
    return bool(get_setting("gemini_proxy_enabled"))


def toggle_proxy_enabled() -> bool:
    """Flips the live on/off state and returns the new value. Callers
    (bot/handlers/auto_renewal_review.py) are responsible for checking
    proxy_configured() first — this doesn't refuse to toggle an unset
    proxy, it just wouldn't have any visible effect (is_proxy_enabled()
    stays False regardless per the check above)."""
    from core.auto_renewal import toggle_gemini_proxy_enabled
    return toggle_gemini_proxy_enabled()


def _resolve_endpoint() -> str:
    if proxy_configured() and is_proxy_enabled():
        return f"{GEMINI_PROXY_URL.rstrip('/')}/v1beta/models/{GEMINI_MODEL}:generateContent"
    return DIRECT_URL


def extract_receipt_data(file_bytes: bytes, mime_type: str) -> dict:
    """
    Returns a dict matching EXTRACTION_PROMPT's schema. Raises GeminiError
    on any failure (missing API key, network error, malformed response) —
    callers (core/auto_renewal.py) treat that the same as "couldn't read
    the receipt" and fall back to the normal manual-approval queue.
    """
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")

    url = _resolve_endpoint()

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
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=30)
    except requests.RequestException as e:
        raise GeminiError(f"network error ({'proxy' if url != DIRECT_URL else 'direct'}): {e}") from e

    if not r.ok:
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
