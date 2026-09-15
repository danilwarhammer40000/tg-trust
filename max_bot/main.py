"""
Entrypoint. Run as: python -m max_bot.main (see systemd/trustpanel-max-bot.service).

Long Polling for now (by explicit decision — MAX's own docs say Webhook is
required for production, but that needs a public HTTPS domain + cert this
deployment doesn't have yet). Revisit if/when that infra exists — see
README.md's MAX bot section.
"""
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()  # MUST run before importing max_bot.config (or anything that imports it)

from max_bot.config import bot
from core.logging_setup import setup_logging

from maxapi import Dispatcher

from max_bot.handlers import client_menu, extra_links, receipt, start

setup_logging()
log = logging.getLogger(__name__)

dp = Dispatcher()

# CONFIRMED against a real install (maxapi package, pip): the method is
# include_routers() (plural, variadic — takes every router in one call),
# NOT include_router() singular like aiogram's Dispatcher. The loop below
# was wrong before this was verified.
dp.include_routers(
    start.router,
    client_menu.router,
    extra_links.router,
    receipt.router,
)


async def main():
    log.info("Starting MAX bot (long polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
