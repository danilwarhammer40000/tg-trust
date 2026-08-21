"""
Everything about HOW the admin's user lists are displayed: persistent
toggles (group-by-subscription, hide-unlimited, hide-expired), sort order,
and the per-row button label (🔔/🔸 markers). Used by every handlers/ file
that renders a "one row per user" keyboard: list_users, get_link,
mass_delete, broadcast (recipient picker), database (trial lists).
"""
import json
import os

from core.dates import is_expired, parse_expiry
from core.paths import SETTINGS_PATH

DEFAULT_SETTINGS = {
    "group_by_subscription": True,
    "hide_unlimited": False,
    "hide_expired": False,
    "hide_followers": False,
    "sort_soonest_first": False,
}


def _load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULT_SETTINGS, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def _save_settings(settings: dict):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f)


def is_grouping_enabled() -> bool:
    return _load_settings().get("group_by_subscription", True)


def toggle_grouping() -> bool:
    settings = _load_settings()
    settings["group_by_subscription"] = not settings.get("group_by_subscription", True)
    _save_settings(settings)
    return settings["group_by_subscription"]


def is_hide_unlimited_enabled() -> bool:
    return _load_settings().get("hide_unlimited", False)


def toggle_hide_unlimited() -> bool:
    settings = _load_settings()
    settings["hide_unlimited"] = not settings.get("hide_unlimited", False)
    _save_settings(settings)
    return settings["hide_unlimited"]


def is_hide_expired_enabled() -> bool:
    return _load_settings().get("hide_expired", False)


def toggle_hide_expired() -> bool:
    settings = _load_settings()
    settings["hide_expired"] = not settings.get("hide_expired", False)
    _save_settings(settings)
    return settings["hide_expired"]


def is_hide_followers_enabled() -> bool:
    return _load_settings().get("hide_followers", False)


def toggle_hide_followers() -> bool:
    settings = _load_settings()
    settings["hide_followers"] = not settings.get("hide_followers", False)
    _save_settings(settings)
    return settings["hide_followers"]


def is_sort_soonest_first_enabled() -> bool:
    return _load_settings().get("sort_soonest_first", False)


def toggle_sort_soonest_first() -> bool:
    settings = _load_settings()
    settings["sort_soonest_first"] = not settings.get("sort_soonest_first", False)
    _save_settings(settings)
    return settings["sort_soonest_first"]


def user_button_label(u: dict) -> str:
    username = u.get("username", "?")
    expires_at = u.get("expires_at")
    label = f"{username} ({expires_at or '∞'})"

    if u.get("telegram_id"):
        label = f"🔔 {label}"

    if u.get("linked_to"):
        label = f"🔗 {label}"

    return f"🔸 {label}" if is_expired(expires_at) else label


def _expiry_sort_key(u: dict, soonest_first: bool = False):
    """
    Two orderings, picked by the "Сначала истекающие" toggle:

    - Default (soonest_first=False): unlimited (no expiry) first, then
      furthest-expiring first, nearest-expiring last, broken dates at the
      very end. Good for "everything's fine, skim past the top".
    - soonest_first=True: nearest-expiring first, unlimited pushed to the
      very end (they need zero attention), broken dates still last either
      way. Good for "who do I need to deal with today" — the accounts
      that actually need a decision aren't buried behind pages of
      lifetime/far-future ones.

    Takes soonest_first as a parameter (read once by the caller) rather
    than calling is_sort_soonest_first_enabled() here — this function runs
    once per user being sorted, and re-reading settings.json from disk
    that many times per list render would be wasteful I/O for no benefit.
    """
    expires_at = u.get("expires_at")

    if not expires_at:
        return (1, 0) if soonest_first else (0, 0)

    d = parse_expiry(expires_at)
    if d is None:
        return (2, 0)  # broken dates always sort last, in either mode

    ordinal = d.date().toordinal()
    return (0, ordinal) if soonest_first else (1, -ordinal)


def sorted_users_for_display(users: list) -> list:
    """
    If grouping is enabled: subscribed (🔔, telegram_id set) users above
    unsubscribed ones. Either way, applies the expiry-date ordering from
    _expiry_sort_key (default or "soonest first", per the current setting)
    as the (secondary, or only) sort key.
    """
    soonest_first = is_sort_soonest_first_enabled()

    if is_grouping_enabled():
        return sorted(
            users,
            key=lambda u: (0 if u.get("telegram_id") else 1, *_expiry_sort_key(u, soonest_first))
        )
    return sorted(users, key=lambda u: _expiry_sort_key(u, soonest_first))


def prepare_users_for_display(users: list) -> list:
    """
    Applies all three display settings, in order: optional hide-unlimited,
    hide-expired, hide-followers filters, then sort (grouped-by-subscription
    + date, or plain date-only — see sorted_users_for_display). Use this
    everywhere a full user list gets rendered as buttons for general
    browsing — List users, Get link, mass delete, broadcast recipient
    picker, trial management.

    IMPORTANT: the leader/follower linking flows in
    bot/handlers/leader_link.py deliberately do NOT use this function —
    they need to see followers regardless of hide_followers (to re-parent
    them, or to show a leader's own current group), so they call
    sorted_users_for_display() directly instead. If you add a new list
    here, ask whether it's "general browsing" (use this) or "managing
    links" (bypass the hide-filters).
    """
    if is_hide_unlimited_enabled():
        users = [u for u in users if u.get("expires_at")]
    if is_hide_expired_enabled():
        users = [u for u in users if not is_expired(u.get("expires_at"))]
    if is_hide_followers_enabled():
        users = [u for u in users if not u.get("linked_to")]
    return sorted_users_for_display(users)
