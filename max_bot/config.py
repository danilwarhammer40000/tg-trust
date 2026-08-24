"""
Environment config + the shared maxapi Bot instance for the MAX bot.

Mirrors bot/config.py's pattern exactly. This is a SEPARATE process from
the Telegram bot (bot/bot.py) — the two share core/ (users.json, VPN
credential rebuilding, dates, payment text) but run as independent
systemd services, each polling its own messenger.
"""
import os

from maxapi import Bot

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")

if not MAX_BOT_TOKEN:
    raise RuntimeError("MAX_BOT_TOKEN missing")

# NOTE (unverified against a real install): the maxapi examples call
# Bot() with no arguments and it still works, suggesting it may read
# MAX_BOT_TOKEN from the environment itself. Passing it explicitly here
# is harmless either way and makes the dependency obvious.
bot = Bot(token=MAX_BOT_TOKEN)
