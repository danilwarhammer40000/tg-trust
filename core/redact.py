"""
Secret scrubbing for anything that ends up in logs or in messages to the admin.

`requests` puts the full request URL into its exception text, and Telegram's
Bot API keeps the bot token in the URL path (…/bot<TOKEN>/sendMessage). So a
plain `log.error("...: %s", e)` after a network blip would write the token to
journald. scrub() masks the known secrets (from the environment) and, as a
safety net, anything that looks like a Telegram bot token or an API key
parameter.
"""
import logging
import os
import re

_SECRET_ENV_NAMES = ("BOT_TOKEN", "GEMINI_API_KEY")

_PATTERNS = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"/file/bot[^/\s]+"), "/file/bot<redacted>"),
    (re.compile(r"([?&]key=)[^&\s'\"]+"), r"\1<redacted>"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "<redacted-key>"),
]


def scrub(text) -> str:
    text = str(text)
    for name in _SECRET_ENV_NAMES:
        secret = os.getenv(name)
        if secret and len(secret) >= 8:
            text = text.replace(secret, "<redacted>")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Applied to every log handler so no call site can leak a secret."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        if record.exc_info:
            # Pre-render the traceback so the secret can be scrubbed from it too.
            formatter = logging.Formatter()
            text = scrub(formatter.formatException(record.exc_info))
            record.exc_text = text
        return True
