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

import core.auto_renewal as auto_renewal
from core.db import claim_pending_request_for_ai


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
