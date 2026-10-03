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

MODEL CASCADE: Google's flash-tier models get retired/rate-limited often
enough that hardcoding one model name isn't reliable (see the
GEMINI_MODEL comment below for one such retirement already hit in
practice). GEMINI_MODELS (plural, comma-separated in .env) is an ORDERED
list, most capable first, that extract_receipt_data() below works through
on failure: RETRIES_PER_MODEL attempts against one model, RETRY_DELAY_SECONDS
apart, before moving on to the next, less-loaded/less-advanced model in
the list and repeating. Only a TRANSIENT failure (network blip, HTTP 429
rate-limit, HTTP 5xx "model overloaded") triggers this -- a model that's
outright gone (HTTP 404) is skipped immediately with no retries wasted on
it, and an auth failure (HTTP 401/403, i.e. a bad API key) aborts the
whole cascade immediately rather than failing identically against every
model in the list one by one. Falls back to a single-model list built
from GEMINI_MODEL when GEMINI_MODELS isn't set, so an existing
single-model .env keeps working unchanged.

This can legitimately take several minutes in the worst case (every model
exhausting its retries) -- that's by design here, not a bug. See
bot/auto_renewal_hook.py and bot/handlers/receipt.py for how the client
is kept informed (an immediate "we got it, hang tight" acknowledgement)
while this runs to completion in a background thread.
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

# Ordered cascade of models to try, most capable first — see the MODEL
# CASCADE section of the module docstring above. "gemini-3.6-flash,
# gemini-2.5-flash,gemini-2.0-flash" is the kind of thing to put here;
# the exact names depend on what's actually available on the account at
# deploy time, so there's no hardcoded fallback list beyond GEMINI_MODEL
# itself — an admin who wants the cascade behaviour has to list the
# models explicitly.
_GEMINI_MODELS_RAW = os.getenv("GEMINI_MODELS", "").strip()
GEMINI_MODELS = [m.strip() for m in _GEMINI_MODELS_RAW.split(",") if m.strip()] or [GEMINI_MODEL]

# How many attempts a single model gets before the cascade moves on, and
# how long to wait between those attempts — see extract_receipt_data().
RETRIES_PER_MODEL = 3
RETRY_DELAY_SECONDS = 180  # 3 minutes

# Cloudflare Worker (or any HTTPS reverse-proxy) base URL, e.g.
# https://your-worker.workers.dev — set in .env as the default/fallback.
# The EFFECTIVE url is proxy_url() below, which prefers a live override
# set from the bot's Settings screen over this .env value.
GEMINI_PROXY_URL = os.getenv("GEMINI_PROXY_URL", "").strip()

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
    """Base class for every failure extract_receipt_data() can raise.
    Callers (core/auto_renewal.py) treat any instance of this the same
    way: "couldn't read the receipt", fall back to manual review."""
    pass


class _TransientGeminiError(GeminiError):
    """Worth retrying the SAME model after RETRY_DELAY_SECONDS — a
    network blip, an HTTP 429 rate-limit, or an HTTP 5xx ("model
    overloaded", Google's own wording for the most common case)."""
    pass


class _ModelUnavailableError(GeminiError):
    """This specific model is gone or misconfigured (HTTP 404) — retrying
    it won't help, so the cascade skips straight to the next model with
    no attempts wasted."""
    pass


# HTTP statuses worth retrying against the SAME model before falling
# through to the next one — Google's own "this is temporary, try again"
# signals (503 UNAVAILABLE / overloaded model is the most common one in
# practice, 429 is a rate limit).
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

# A bad/expired API key fails identically against every model in the
# cascade — no point working through the whole list one by one.
_AUTH_STATUS_CODES = {401, 403}

# The model name itself doesn't exist (retired, typo'd in .env, ...).
_MODEL_UNAVAILABLE_STATUS_CODES = {404}


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


def _endpoint_for(model: str) -> str:
    if proxy_configured() and is_proxy_enabled():
        return f"{proxy_url().rstrip('/')}/v1beta/models/{model}:generateContent"
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _call_gemini_once(model: str, payload: dict) -> dict:
    """
    One raw HTTP call against `model`. Returns the parsed extraction dict
    on success. Raises:
      - _TransientGeminiError for a network error or one of
        _TRANSIENT_STATUS_CODES — worth retrying the SAME model later.
      - _ModelUnavailableError for HTTP 404 — this model name doesn't
        exist, skip it entirely.
      - plain GeminiError for everything else (auth failure, a malformed
        request, an unparseable response) — not worth retrying at all.
    """
    url = _endpoint_for(model)

    try:
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=60)
    except requests.RequestException as e:
        raise _TransientGeminiError(f"network error on {model}: {e}") from e

    if not r.ok:
        if r.status_code in _AUTH_STATUS_CODES:
            raise GeminiError(f"HTTP {r.status_code} (auth) on {model}: {r.text[:300]}")
        if r.status_code in _MODEL_UNAVAILABLE_STATUS_CODES:
            raise _ModelUnavailableError(f"HTTP {r.status_code} (model unavailable) on {model}: {r.text[:300]}")
        if r.status_code in _TRANSIENT_STATUS_CODES:
            raise _TransientGeminiError(f"HTTP {r.status_code} (transient) on {model}: {r.text[:300]}")
        raise GeminiError(f"HTTP {r.status_code} on {model}: {r.text[:300]}")

    try:
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        extraction = json.loads(text)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        raise GeminiError(f"unexpected response shape on {model}: {e}") from e

    if not isinstance(extraction, dict):
        raise GeminiError(f"response was valid JSON but not an object on {model}")

    return extraction


def extract_receipt_data(file_bytes: bytes, mime_type: str) -> dict:
    """
    Returns a dict matching EXTRACTION_PROMPT's schema. Raises GeminiError
    (see its subclasses above) once every model in GEMINI_MODELS has been
    exhausted — callers (core/auto_renewal.py) treat that as "couldn't
    read the receipt" and fall back to the normal manual-approval queue.

    Works through GEMINI_MODELS in order (see the module docstring's
    MODEL CASCADE section): up to RETRIES_PER_MODEL attempts against one
    model, RETRY_DELAY_SECONDS apart, but only when the failure is
    transient (network blip, rate limit, "model overloaded") — a model
    that doesn't exist at all (HTTP 404) is skipped with zero attempts
    wasted on it. An auth failure (bad API key) raises immediately and
    aborts the WHOLE cascade — retrying or trying other models can't fix
    a bad key.

    Deliberately synchronous, with real time.sleep() calls that can add
    up to several minutes in the worst case — see the module docstring
    for why that's fine here (always called via run_in_executor or a
    standalone script, never on the event loop thread).
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

    last_error = None

    for model in GEMINI_MODELS:
        for attempt in range(1, RETRIES_PER_MODEL + 1):
            try:
                return _call_gemini_once(model, payload)

            except _ModelUnavailableError as e:
                log.warning("Gemini model %s unavailable, skipping to next model: %s", model, e)
                last_error = e
                break  # no retries for a model that doesn't exist — next model now

            except _TransientGeminiError as e:
                last_error = e
                is_last_attempt_for_model = attempt == RETRIES_PER_MODEL

                if is_last_attempt_for_model:
                    log.warning(
                        "Gemini model %s exhausted %d attempt(s), moving to next model: %s",
                        model, RETRIES_PER_MODEL, e,
                    )
                    break  # on to the next model, no extra wait

                log.warning(
                    "Gemini model %s attempt %d/%d failed (transient), retrying in %ds: %s",
                    model, attempt, RETRIES_PER_MODEL, RETRY_DELAY_SECONDS, e,
                )
                time.sleep(RETRY_DELAY_SECONDS)

            # Any other GeminiError (auth failure, malformed response) is
            # permanent and not model-specific in a way retrying or
            # switching models helps — let it propagate and abort the
            # whole cascade immediately (no `except GeminiError` here on
            # purpose: this is the fall-through case).

    raise GeminiError(
        f"all Gemini models exhausted ({', '.join(GEMINI_MODELS)}): {last_error}"
    )
