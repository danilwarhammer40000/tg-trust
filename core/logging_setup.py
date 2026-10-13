"""
Central logging configuration.

Replaces the scattered print("[TAG] ...") calls across the codebase.
journalctl already timestamps everything, but using `logging` gives us
levels (so warnings/errors can be filtered: `journalctl -p warning`),
consistent formatting, and the ability to add a file/Sentry handler later
without touching call sites.

Usage in any entrypoint (bot/bot.py, services/cleanup.py, services/backup.py):

    from core.logging_setup import setup_logging
    setup_logging()
    import logging
    log = logging.getLogger(__name__)
"""

import logging
import os

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Mask bot token / API keys in every record (requests puts the full URL,
    # token included, into its exception messages).
    from core.redact import RedactingFilter
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())

    # aiogram is chatty at INFO about every update; keep it at WARNING
    # unless the operator explicitly asked for DEBUG.
    if level > logging.DEBUG:
        logging.getLogger("aiogram").setLevel(logging.WARNING)

    _CONFIGURED = True
