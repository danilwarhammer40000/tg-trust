"""
Who's allowed to do what, the shared "resync + restart trusttunnel"
helper, and two small async notification wrappers. Split out on its own
because almost every handlers/ file needs at least one of these.
"""
import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery

from bot.config import ADMIN_ID
from core.db import update_user
from core.service import safe_sync

log = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def admin_only(msg_or_call) -> bool:
    """Returns True if allowed to proceed, otherwise answers politely and returns False."""
    uid = msg_or_call.from_user.id
    if is_admin(uid):
        return True

    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.answer("⛔ Недоступно", show_alert=True)
    else:
        await msg_or_call.answer("⛔ Эта команда доступна только администратору.")
    return False


async def run_sync():
    """Run the blocking systemctl-restart sync off the event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, safe_sync)


async def notify_bg(func, *args, **kwargs):
    """
    Runs a synchronous notifier (e.g. core.notify.log_to_channel, which
    does a blocking `requests` call) in a background thread so it never
    blocks the event loop. Fire-and-forget: callers that don't care about
    the return value just `await notify_bg(some_sync_func, ...)`.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def notify_client(bot, telegram_id: int, text: str, clear_username: str = None) -> bool:
    """
    Sends `text` to a client via aiogram's async Bot (as opposed to
    core.notify's synchronous raw-HTTP senders, which are for the
    no-event-loop services/ scripts). Returns True if delivered.

    If delivery fails because the client blocked the bot / deleted the
    chat (TelegramForbiddenError) or the chat_id is no longer valid
    (TelegramBadRequest), and `clear_username` is given, this also clears
    the dangling telegram_id on that user's record -- otherwise every
    future notification to them would keep silently failing against a
    chat that no longer exists.
    """
    try:
        await bot.send_message(telegram_id, text)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("notify_client: could not deliver to %s: %s", telegram_id, e)
        if clear_username:
            update_user(clear_username, telegram_id=None)
        return False
