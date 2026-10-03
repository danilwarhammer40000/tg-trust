"""
One-time invite links: an admin-generated deep-link
(t.me/<bot>?start=invite_<token>) that binds a pre-created DB user to
whoever taps it, without the client ever pasting a connection card
themselves — see bot/handlers/start.py's CommandStart() handler for the
consuming side.

"Infinite but one-time" (per admin's spec): the link itself never expires
on a timer — it stays valid until the moment someone actually uses it
(i.e. until that user's telegram_id gets set). Regenerating a link
(generate_invite_token again) makes a brand new token that's ALSO
infinite-until-used, and immediately invalidates whatever the previous
token was, even if that previous one was never used —
core.db.get_user_by_invite_token only ever matches the CURRENT token on
the record, so the old link simply stops resolving to anything.

Reachable from two places, both producing the exact same kind of link:
bot/handlers/add_user.py (right after creating a brand-new user) and
bot/handlers/list_users.py (from an existing user's card, e.g. for
someone created before this feature existed, or to regenerate a link
that was shared with the wrong person).
"""
import secrets

from core.db import update_user


def generate_invite_token(username: str) -> str:
    """Generates a fresh token, overwrites any previous one on this
    user's record, and returns it."""
    token = secrets.token_urlsafe(9)
    update_user(username, invite_token=token)
    return token


def build_invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start=invite_{token}"
