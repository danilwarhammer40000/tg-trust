"""
Synchronous wrapper around the Gemini API's generateContent endpoint,
used to extract structured data from a payment receipt screenshot/PDF.

Deliberately does NOT ask Gemini to decide whether to auto-renew — it
only extracts what's on the image (amount, date, who/what bank). The
actual approve/reject decision is core/auto_renewal.py's business-rule
logic, in plain Python, so a bad extraction or hallucination can't
directly grant VPN access — see evaluate_receipt_extraction() there.

Synchronous by design (plain `requests`, no aiohttp) so this can be called
identically from an async bot handler (via run_in_executor, same pattern
already used for core.service.safe_sync) and from the standalone
services/auto_renewal_overdue_check.py script, which has no event loop.

PROXY SUPPORT: outbound calls to Gemini can be routed through an
admin-configurable proxy (the "🌐 Прокси для Gemini API" field under
"🤖 Автопродление -> ⚙️ Настроить условия" in the bot, or GEMINI_PROXY in
.env as the initial seed — see core.auto_renewal.DEFAULT_SETTINGS). This
exists because generativelanguage.googleapis.com enforces a region
allowlist that a Russian-hosted VPS IP typically fails
(FAILED_PRECONDITION "User location is not supported"), independent of
whether the API key itself is valid. Two proxy forms are accepted:

  - Worker / HTTPS reverse-proxy (e.g. a Cloudflare Worker forwarding to
    Google and injecting its own key): the whole googleapis.com host is
    replaced by the given base URL. requests.post gets no proxies= at
    all -- the redirection IS the proxy.
  - socks5:// / socks5h:// : the real Google endpoint is used unchanged,
    but the TCP connection goes through the given SOCKS5 proxy. Needs
    the `requests[socks]` (PySocks) extra installed -- see
    requirements.txt.

Empty/unset -> behaves exactly as before this feature existed (direct
call, no proxy).
"""
import json
import logging
import os
import base64

import requests

from core.auto_renewal import get_setting

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# gemini-2.5-flash was retired (Gemini API now returns 404 for it,
# telling callers to switch to gemini-3.6-flash). Google's flash-tier
# models seem to get retired every few months (2.0 -> 2.5 -> 3.x already),
# so this is deliberately read from GEMINI_MODEL first — if this breaks
# again, set GEMINI_MODEL in .env instead of needing a code change.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

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
    """Whether a Gemini proxy (Worker/HTTPS or SOCKS5) is currently set —
    used by bot/handlers/auto_renewal_review.py's status screen to show
    whether outbound Gemini calls are being routed through one right now."""
    return bool((get_setting("gemini_proxy") or "").strip())


def _resolve_endpoint_and_proxies():
    """
    Reads the "gemini_proxy" setting and returns (url, proxies) ready for
    requests.post(url, ..., proxies=proxies).
    """
    proxy = (get_setting("gemini_proxy") or "").strip()
    direct_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

    if not proxy:
        return direct_url, None

    if proxy.startswith("http://") or proxy.startswith("https://"):
        # Worker / HTTPS reverse-proxy: replace the host entirely. The
        # worker is expected to forward path+query to Google itself (and
        # may inject its own key), so the ?key= we still send below is
        # harmless even if the worker ignores or overwrites it.
        return f"{proxy.rstrip('/')}/v1beta/models/{GEMINI_MODEL}:generateContent", None

    if proxy.startswith("socks5://") or proxy.startswith("socks5h://"):
        # Real Google endpoint, routed through the SOCKS5 proxy.
        return direct_url, {"http": proxy, "https": proxy}

    raise GeminiError(
        f"Неизвестный формат gemini_proxy: {proxy!r} "
        "(ожидается http(s):// или socks5(h)://)"
    )


def extract_receipt_data(file_bytes: bytes, mime_type: str) -> dict:
    """
    Returns a dict matching EXTRACTION_PROMPT's schema. Raises GeminiError
    on any failure (missing API key, network error, malformed response,
    unrecognized proxy setting) — callers (core/auto_renewal.py) treat
    that the same as "couldn't read the receipt" and fall back to the
    normal manual-approval queue.
    """
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")

    url, proxies = _resolve_endpoint_and_proxies()

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
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=30, proxies=proxies)
    except requests.RequestException as e:
        raise GeminiError(f"network error: {e}") from e

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
