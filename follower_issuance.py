"""
Shared helper for creating a brand-new linked sub-account ("follower")
under an existing user — used by:
  - the admin's "➕ Выпустить нового" button (handlers/leader_link.py)
  - a client's self-service "📱 Подключить ещё устройства" request, once
    an admin approves it (handlers/extra_links.py)

Deliberately NOT in core/db.py: this needs core.generator.generate_link()
(bot-layer config — DOMAIN) to hand back a ready-to-send connection card,
which core/db.py has no business depending on. core/db.py itself is left
completely untouched by this feature — every DB operation here goes
through its existing add_user()/link_user()/get_followers() functions.

IMPORTANT ORDERING RULE (see build_connection_card()'s docstring):
issue_follower() only touches the DB. It deliberately does NOT build a
connection card / call generate_link(). generate_link() shells out to the
trusttunnel_endpoint binary, which reads its user list from
vpn.toml/hosts.toml — files that only get rebuilt by
bot.access.run_sync(). Callers MUST call run_sync() (when
leader_is_active()) BEFORE calling build_connection_card(), or the binary
won't know about the brand-new username yet and generate_link() silently
falls back to a non-functional placeholder URL.
"""
import re

from bot.config import DOMAIN
from bot.formatting import format_full_instructions_message
from core.dates import utcnow_naive
from core.db import add_user, get_followers, get_user, link_user
from core.generator import generate_link

# Followers of a leader are named "{leader}-2", "{leader}-3", etc. — the
# leader account itself is implicitly "slot 1". Chosen over "{leader}/2"
# specifically because the username ends up in a URL path
# (core/generator.py's fallback link is /connect/{username}) and gets
# passed as a CLI argument to the trusttunnel_endpoint binary — "-" is
# safe in both contexts, "/" risked being read as an extra path segment
# by code we don't control.
_SUFFIX_RE = re.compile(r"^-(\d+)$")


def next_follower_username(leader_username: str, existing_followers: list) -> str:
    """
    Picks the lowest unused slot number >= 2 among CURRENT followers of
    leader_username whose name actually matches the "{leader}-N" pattern
    — a follower linked in some other way (e.g. an existing independent
    account manually attached via "🔗 Сделать ведомым") doesn't occupy a
    numbered slot, so it's simply skipped rather than colliding.
    """
    used = set()
    prefix_len = len(leader_username)
    for f in existing_followers:
        name = f.get("username", "")
        if name.startswith(leader_username):
            m = _SUFFIX_RE.match(name[prefix_len:])
            if m:
                used.add(int(m.group(1)))

    n = 2
    while n in used:
        n += 1
    return f"{leader_username}-{n}"


def issue_follower(leader_username: str, existing_followers: list = None):
    """
    Creates one new sub-account linked to leader_username in the DB only:
    SAME password as the leader (by design — these are the same person's
    extra devices, not separate clients), expires_at/status synced from
    the leader via link_user() (identical to manually linking an existing
    account).

    existing_followers can be passed in to avoid a redundant
    get_followers() call when issuing several in a row (see
    handlers/extra_links.py, which issues multiple and must account for
    each new one before picking the next slot number) — the CALLER is
    responsible for keeping that list current between calls in that case.

    Returns the new username on success, or None if leader_username
    doesn't exist.

    Does NOT generate a connection link/card — see this module's
    docstring for why that has to happen separately, after a resync. Call
    build_connection_card() for that, once run_sync() (if needed) has run.
    """
    leader = get_user(leader_username)
    if not leader:
        return None

    if existing_followers is None:
        existing_followers = get_followers(leader_username)

    new_username = next_follower_username(leader_username, existing_followers)

    add_user({
        "username": new_username,
        "password": leader.get("password"),
        "created_at": utcnow_naive().strftime("%Y-%m-%d"),
        "expires_at": leader.get("expires_at"),  # placeholder — link_user() below syncs it for real
        "status": leader.get("status", "active"),
        "telegram_id": None,
        "notified_days": [],
        "post_disable_notified": [],
        "pending_request": None,
    })

    link_user(new_username, leader_username)

    return new_username


def build_connection_card(username: str) -> str:
    """
    Generates the actual connection link (via generate_link(), which
    shells out to the trusttunnel_endpoint binary) and formats the full
    instructions message for `username`.

    MUST be called AFTER a run_sync() that included this user — i.e. never
    call this in the same breath as issue_follower() without a run_sync()
    in between (when the leader is active; see leader_is_active() below).
    Calling it too early hands back a broken fallback link because the
    binary hasn't been told about `username` yet. See this module's
    docstring for the full explanation.

    Returns "" if username somehow doesn't exist (shouldn't happen if
    called right after issue_follower() succeeded).
    """
    user = get_user(username)
    if not user:
        return ""

    link = generate_link(username, DOMAIN)
    return format_full_instructions_message(username, user.get("password"), user.get("expires_at"), link)


def leader_is_active(leader: dict) -> bool:
    """Whether issuing a new follower right now actually needs a
    credentials.toml resync — only true if the leader (and therefore the
    new follower, which syncs to the same status) is currently active."""
    from core.dates import is_expired
    return leader.get("status") == "active" and not is_expired(leader.get("expires_at"))
