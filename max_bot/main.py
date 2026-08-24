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

from max_bot.handlers import client_menu, receipt, start

setup_logging()
log = logging.getLogger(__name__)

dp = Dispatcher()

# NOTE (unverified): maxapi's Dispatcher is documented with dp.message_created(...)
# etc. used directly as decorators in the examples, without a separate
# include_router step shown. If maxapi's Dispatcher doesn't expose
# include_router() the same way aiogram's does, switch these handler
# modules to decorate `dp` directly instead of a per-file `router = Router()`
# — test this against a real install before relying on it.
for router in (
    start.router,
    client_menu.router,
    receipt.router,
):
    dp.include_router(router)


async def main():
    log.info("Starting MAX bot (long polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
