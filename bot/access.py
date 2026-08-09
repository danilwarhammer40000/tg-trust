"""
Who's allowed to do what, and the shared "resync + restart trusttunnel"
helper. Split out on its own because almost every handlers/ file needs at
least one of these three functions.
"""
import asyncio

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
