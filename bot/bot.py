"""
Entrypoint. Run as: python -m bot.bot (see systemd/trustpanel-bot.service).

This file's only job is: load .env, create the Dispatcher, register every
feature router from bot/handlers/, and start polling. All actual bot logic
lives in bot/handlers/*.py — see each file's module docstring for which
FSM states it owns.

Router include order below does NOT affect correctness: every callback_data
prefix and every FSM state is owned by exactly one router (verified in
CHANGELOG.md), so there's no case where two routers could both match the
same update. The order here just roughly follows the client-menu ->
admin-menu grouping for readability.
"""
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()  # MUST run before importing bot.config (or anything that imports it)

from bot.config import bot
from core.logging_setup import setup_logging

from aiogram import Dispatcher

from bot.handlers import (
    add_user,
    broadcast,
    client_menu,
    database,
    extend,
    feedback,
    get_link,
    list_users,
    mass_delete,
    receipt,
    sorting,
    start,
    sync_deploy,
)
from bot import pagination

setup_logging()
log = logging.getLogger(__name__)

dp = Dispatcher()

for router in (
    pagination.router,
    start.router,
    client_menu.router,
    feedback.router,
    receipt.router,
    broadcast.router,
    database.router,
    sorting.router,
    sync_deploy.router,
    add_user.router,
    list_users.router,
    extend.router,
    mass_delete.router,
    get_link.router,
):
    dp.include_router(router)


async def main():
    log.info("Starting bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
