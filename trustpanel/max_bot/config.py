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

# CONFIRMED against the installed maxapi package: Bot(token=...) is a
# real, explicit keyword argument (not just "examples happen to work
# without it") — Bot.__init__ accepts token as its first parameter.
bot = Bot(token=MAX_BOT_TOKEN)
