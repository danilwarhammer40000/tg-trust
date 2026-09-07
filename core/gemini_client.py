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
  1. Not configured at all (no GEMINI_PROXY_URL in .env AND no live
     override set) -> proxy_configured() is False,
     bot/handlers/auto_renewal_review.py doesn't even show the toggle
     button, always calls Google directly.
  2. Configured AND enabled (the normal case once set up) ->
     is_proxy_enabled() True, calls go through the effective URL (live
     override if one is set, else GEMINI_PROXY_URL from .env).
  3. Configured but temporarily DISABLED via the bot's live toggle
     (autoren:toggle_proxy) -> lets the admin fall back to a direct
     connection without touching .env or restarting the bot, e.g. to
     check whether a currently-down proxy is the actual cause of a
     Gemini failure. This ON/OFF state is separate from whether a URL is
     even set, and is persisted the same way every other auto-renewal
     setting is — see core.auto_renewal.toggle_gemini_proxy_enabled().

The URL itself can now ALSO be changed live, from the bot's Settings
screen ("🌐 Прокси-адрес") — see
core.auto_renewal.get/set_gemini_proxy_url_override(). GEMINI_PROXY_URL
from .env is only the fallback/default when no override has been set.

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
import time

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
# https://your-worker.workers.dev — set in .env as the default/fallback.
# The EFFECTIVE url is proxy_url() below, which prefers a live override
# set from the bot's Settings screen over this .env value.
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


# HTTP statuses worth retrying before giving up and falling back to manual
# review — all of these are Google's own "this is temporary, try again"
# signals (503 UNAVAILABLE / overloaded model is the most common one in
# practice), as opposed to e.g. 400 (bad request/malformed image) or 403
# (bad API key), which won't fix themselves on a retry.
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def proxy_configured() -> bool:
    """Whether a proxy URL is available from EITHER source (live override
    or GEMINI_PROXY_URL in .env) — the bot only shows the live
    enable/disable toggle when this is True (nothing to toggle
    otherwise)."""
    return bool(proxy_url())


def proxy_url() -> str:
    """The EFFECTIVE Worker/reverse-proxy base URL right now: a live
    override set via the bot's Settings screen takes precedence over
    GEMINI_PROXY_URL from .env — see
    core.auto_renewal.get_gemini_proxy_url_override(). For display too
    (e.g. bot/handlers/auto_renewal_review.py's status screen, so the
    admin can see exactly where the on/off toggle is pointing). Empty
    string if nothing is configured from either source; callers should
    check proxy_configured() first rather than treat "" as a real value."""
    from core.auto_renewal import get_gemini_proxy_url_override
    return get_gemini_proxy_url_override() or GEMINI_PROXY_URL


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
        return f"{proxy_url().rstrip('/')}/v1beta/models/{GEMINI_MODEL}:generateContent"
    return DIRECT_URL


def extract_receipt_data(file_bytes: bytes, mime_type: str, retries: int = 3, retry_delay: float = 2.0) -> dict:
    """
    Returns a dict matching EXTRACTION_PROMPT's schema. Raises GeminiError
    on any failure (missing API key, network error, malformed response) —
    callers (core/auto_renewal.py) treat that the same as "couldn't read
    the receipt" and fall back to the normal manual-approval queue.

    Retries up to `retries` times (with a growing pause) for network
    errors and for the transient HTTP statuses in _TRANSIENT_STATUS_CODES
    — chiefly 503 "model overloaded", which Google's own error message
    explicitly calls temporary. A permanent failure (bad API key, 400 for
    a malformed request, an unparseable response) is NOT retried — those
    fail on the first attempt exactly as before. Synchronous
    time.sleep() is fine here (see module docstring — this always runs
    via run_in_executor or the standalone script, never on the event
    loop thread directly), and the retry math is bounded on purpose:
    worst case is a handful of extra seconds, not a new multi-minute
    hang, so this doesn't undermine the timeout=60 above.
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

    r = None
    last_network_error = None

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=60)
        except requests.RequestException as e:
            last_network_error = e
            if attempt < retries:
                time.sleep(retry_delay * attempt)
                continue
            raise GeminiError(f"network error ({'proxy' if url != DIRECT_URL else 'direct'}): {e}") from e

        if r.ok:
            break

        if r.status_code in _TRANSIENT_STATUS_CODES and attempt < retries:
            log.warning(
                "Gemini transient error (attempt %d/%d): HTTP %d, retrying in %.1fs",
                attempt, retries, r.status_code, retry_delay * attempt,
            )
            time.sleep(retry_delay * attempt)
            continue

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
