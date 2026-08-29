"""
Who's allowed to do what, and the shared "resync + restart trusttunnel"
helper. Split out on its own because almost every handlers/ file needs at
least one of these three functions.
"""
import asyncio
import functools

from aiogram.types import CallbackQuery

from bot.config import ADMIN_ID
from core.service import safe_sync


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
    Runs a blocking, network-bound core.notify.* call (log_to_channel,
    notify_admin, notify_user, send_photo_by_file_id,
    send_document_by_file_id, ...) off the event loop -- same pattern as
    run_sync() above, for the same reason.

    Every core.notify function uses plain `requests` under the hood, not
    aiohttp. Calling one directly from an aiogram handler blocks the
    ENTIRE bot process -- every user's messages and button taps, not just
    the one who triggered it -- for as long as that HTTP round-trip
    takes. This was most visible right when a client taps "✅ Да,
    отправить" on a receipt: log_to_channel() used to upload the photo to
    the log channel synchronously before anything else could run,
    stalling the whole bot for that upload's duration -- which looks
    exactly like Telegram showing a "connecting..." hiccup to everyone
    using the bot at that moment.

    Usage: await notify_bg(log_to_channel, caption, file_id=file_id)
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
