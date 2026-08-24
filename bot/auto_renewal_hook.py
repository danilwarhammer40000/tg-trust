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


async def try_auto_renewal(username: str, file_id: str, is_photo: bool) -> bool:
    """
    Returns True if auto-renewal actually applied (caller should skip
    sending the normal manual-approval notification) — False if
    auto-renewal isn't applicable right now, or it was attempted and fell
    back to manual (in the fallback case, the normal manual notification
    with ➕1мес/➕2мес/✍️/❌ buttons still needs to go out, exactly as if
    auto-renewal didn't exist).
    """
    if not auto_renewal.should_attempt_now():
        return False

    if not claim_pending_request_for_ai(username):
        # Already being processed by something else (shouldn't normally
        # happen at submission time, but the overdue checker runs on its
        # own schedule) -- fall through to the normal manual path.
        return False

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, auto_renewal.process_pending_request_with_ai, username, "night_window")
