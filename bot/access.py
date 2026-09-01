"""
Who's allowed to do what, and the shared "resync + restart trusttunnel"
helper. Split out on its own because almost every handlers/ file needs at
least one of these three functions.
"""
import asyncio
import functools
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery

from bot.config import ADMIN_ID
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


async def notify_client(bot, chat_id, text: str, *, clear_username: str = None) -> bool:
    """
    Sends a plain text message to a CLIENT's chat_id via the aiogram Bot
    object, swallowing the two errors that mean "this id doesn't actually
    reach a live chat" (a stale/wrong telegram_id -- Telegram raises
    "chat not found" -- or the client blocked the bot) instead of letting
    them crash the whole handler.

    Without this, e.g. tapping an "Extend" button for a user whose stored
    telegram_id is stale raises TelegramBadRequest INSIDE the handler,
    BEFORE it ever reaches call.answer(). Telegram then shows a spinning
    button on the admin's side for the full callback-query timeout --
    which looks exactly like "buttons react very slowly", even though the
    underlying change (e.g. the extension itself) already went through to
    the database fine, since that update happens before this notify call.

    If clear_username is given AND the failure is TelegramBadRequest (the
    id itself is invalid -- "chat not found" and similar -- a permanent,
    not-fixing-itself problem), telegram_id is wiped from that user's
    record automatically. This is what stops the SAME bad id from
    repeatedly causing this exact failure on every future notification,
    and from tripping up other places that build UI around telegram_id
    (e.g. bot/handlers/list_users.py's "💬 Открыть чат" button, which has
    its own separate guard for the same underlying problem).

    Deliberately NOT cleared on TelegramForbiddenError (the client
    blocked the bot) -- that's reversible if they unblock it later, so
    the id is still worth keeping around; only a confirmed-bad id gets
    wiped.

    Returns True if the message was actually delivered, False otherwise
    -- most callers don't need to check this (the point is just "don't
    crash"), but can use it to tell the admin the id turned out stale.
    """
    try:
        await bot.send_message(chat_id, text)
        return True
    except TelegramForbiddenError as e:
        log.warning("client chat_id=%s has blocked the bot (telegram_id kept): %s", chat_id, e)
        return False
    except TelegramBadRequest as e:
        log.warning("could not notify client chat_id=%s (invalid id): %s", chat_id, e)
        if clear_username:
            from core.db import update_user
            update_user(clear_username, telegram_id=None)
            log.warning("cleared invalid telegram_id for user %r", clear_username)
        return False
