"""
The async<->sync bridge between aiogram handlers and core/auto_renewal.py.
Shared by handlers/receipt.py (receipt_yes) and handlers/feedback.py
(feedback_media_as_receipt) — both are entry points for "a client just
submitted a receipt", and both need the exact same "should this trigger
auto-renewal right now, and if so, run it" logic.

Kept out of both handler files (rather than one importing from the other)
to avoid a handlers-importing-handlers dependency — this lives next to
bot/access.py, which is the same kind of small async-glue-around-core-logic
utility (run_sync() wrapping core.service.safe_sync()).
"""
import asyncio
import logging

import core.auto_renewal as auto_renewal
from core.db import claim_pending_request_for_ai, get_user

log = logging.getLogger(__name__)


async def try_auto_renewal(username: str, file_id: str, is_photo: bool) -> str:
    """
    Returns one of three strings:

    - "approved" — auto-renewal applied. The client was ALREADY notified
      synchronously inside this call (the standard renewal text, sent
      immediately — see core/auto_renewal.py's _apply_and_request_review).
      The caller must NOT send any further message to either the client
      or the admin — the admin already got their review card too.

    - "fallback" — auto-renewal was attempted (claimed the request,
      called Gemini or hit the anti-abuse lock) but did not apply. The
      admin was ALREADY sent a card by the pipeline itself
      (core/auto_renewal.py's _fallback_to_manual, or the crash-handler
      path in process_pending_request_with_ai) — the caller must NOT send
      a second admin card for the same receipt (that used to happen —
      one plain card from the caller and one fallback/anti-abuse card
      from the pipeline, both for the same submission). The caller
      SHOULD still send the client the normal "Отправлено
      администратору. Ждите подтверждения." acknowledgement, exactly as
      if auto-renewal didn't exist — the client hasn't heard anything
      about their receipt yet.

    - "skipped" — auto-renewal isn't applicable at all right now (master
      toggle off, not in the trigger window, or the request is already
      claimed by something else). Nobody has been notified about
      anything — the caller must run the FULL normal manual-approval flow
      (send the admin a card AND acknowledge the client), same as if
      auto-renewal didn't exist.
    """
    if not auto_renewal.should_attempt_now():
        return "skipped"

    if not claim_pending_request_for_ai(username):
        # Already being processed by something else (shouldn't normally
        # happen at submission time, but the overdue checker runs on its
        # own schedule) -- fall through to the normal manual path.
        return "skipped"

    loop = asyncio.get_event_loop()
    approved = await loop.run_in_executor(
        None, auto_renewal.process_pending_request_with_ai, username, "night_window"
    )
    return "approved" if approved else "fallback"


async def run_auto_renewal_in_background(
    username: str, file_id: str, is_photo: bool, caption: str, fallback_text: str,
) -> None:
    """
    Fire-and-forget wrapper around try_auto_renewal(), for a handler that
    has ALREADY acknowledged the client immediately ("чек отправлен,
    подписка скоро будет продлена") and doesn't want to block the
    callback-query handler on however long this takes — the Gemini model
    cascade (core/gemini_client.py) can legitimately run for several
    minutes in the worst case. Schedule with `asyncio.create_task(...)`
    right after sending that acknowledgement; this function does not
    return anything for the caller to await on purpose.

    Exceptions are caught and logged HERE rather than left to vanish
    silently into an orphaned asyncio Task — note that in practice
    process_pending_request_with_ai already catches everything itself
    and falls back to manual review on a crash (see its own try/except),
    so this is a last-resort net for a bug in the bridge above it, not
    the normal error path.

    - "approved": nothing more to do — the client already got the real
      renewal confirmation from inside core/auto_renewal.py itself
      (_apply_and_request_review), sent the moment it was approved.
    - "fallback": the admin already has a review card from the pipeline
      itself (_fallback_to_manual) — the client hasn't heard anything
      concrete since the immediate ack, so send `fallback_text` now.
    - "skipped" (the rare claim-lost race, or a bridge-level crash above)
      — unlike "fallback", nobody has told the admin about this receipt
      at all, so send the normal manual-flow admin card first, then the
      same `fallback_text` to the client.
    """
    try:
        result = await try_auto_renewal(username, file_id, is_photo)
    except Exception:
        log.exception("background auto-renewal task crashed for %s", username)
        result = "skipped"

    if result == "approved":
        return

    # Deferred imports: bot.config/bot.access/bot.keyboards all sit a
    # layer above this module (they're handler-facing glue, this module
    # is imported BY handlers), so importing them at module level here
    # would risk a circular import down the line. Cheap enough to import
    # on every call — this function only runs once per receipt.
    from bot.access import notify_client
    from bot.config import ADMIN_ID, bot
    from bot.keyboards import renewal_admin_kb

    if result == "skipped":
        kb = renewal_admin_kb(username)
        if is_photo:
            await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=kb)
        else:
            await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=kb)

    user = get_user(username) or {}
    if user.get("telegram_id"):
        await notify_client(bot, user["telegram_id"], fallback_text, clear_username=username)
