"""
Environment config + the shared aiogram Bot instance.

Every other module in this package imports BOT_TOKEN/ADMIN_ID/DOMAIN/bot
from here instead of reading os.environ itself — one place to change if
a new required setting shows up, and importing this module twice never
creates a second Bot() instance (Python caches modules).

IMPORTANT: bot/bot.py (the entrypoint) calls load_dotenv() before
importing anything from this package, so by the time this module's
top-level os.getenv() calls run, .env is already loaded into os.environ.
"""
import os

from aiogram import Bot

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DOMAIN = os.getenv("TRUSTTUNNEL_DOMAIN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID missing")
if not DOMAIN:
    raise RuntimeError("TRUSTTUNNEL_DOMAIN missing")

bot = Bot(token=BOT_TOKEN)
