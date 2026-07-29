"""
Single source of truth for date handling.

Previously this logic was duplicated three times with subtly different
behaviour:
  - bot/bot.py used strptime(..., "%Y-%m-%d") everywhere
  - core/credentials.py used datetime.fromisoformat(exp)
  - services/cleanup.py had its own parse_expiry() with a fallback

All three now import from here, so a bugfix (like the two already recorded
in comments below) only has to be made once.
"""

import calendar
from datetime import datetime, timedelta, timezone
from typing import Optional

DATE_FORMAT = "%Y-%m-%d"


def utcnow_naive() -> datetime:
    """
    datetime.utcnow() is deprecated. This returns the same kind of value —
    a naive datetime in UTC — so it's a drop-in replacement everywhere this
    codebase compares against strptime()'d dates (which are also naive).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_expiry(date_str: Optional[str]) -> Optional[datetime]:
    """
    Safe parsing of an expires_at value. Accepts:
      - "YYYY-MM-DD" (what the bot writes)
      - full ISO 8601 (defensive, in case a value ever gets written that way)
    Returns None (never raises) if the value is empty or unparseable.
    """
    if not date_str:
        return None

    try:
        dt = datetime.fromisoformat(date_str)
    except ValueError:
        try:
            dt = datetime.strptime(date_str, DATE_FORMAT)
        except ValueError:
            return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt


def is_valid_date_string(date_str: str) -> bool:
    return parse_expiry(date_str) is not None


def is_expired(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    """None/empty expires_at means unlimited access -> never expired."""
    if not expires_at:
        return False

    dt = parse_expiry(expires_at)
    if dt is None:
        # Broken/unparseable date: treat as NOT expired rather than silently
        # disabling someone over a data-entry glitch. Surfaces as "battled
        # date" in sorting instead (see core/db._sort_key).
        return False

    reference = now or utcnow_naive()
    return dt.date() < reference.date()


def calc_new_expiry(current_expires_at: Optional[str], days: int, *, now: Optional[datetime] = None) -> str:
    """
    BUGFIX (kept from original): extending must add `days` on top of the
    CURRENT expiry date (if it's still in the future), not on top of "today".
        2026-07-30 + "extend 30" => 2026-08-29 (correct)
    not 2026-08-24 (wrong: counts from today).
    """
    reference = now or utcnow_naive()
    base = reference

    current = parse_expiry(current_expires_at)
    if current and current > reference:
        base = current

    return (base + timedelta(days=days)).strftime(DATE_FORMAT)


def add_calendar_months(base: datetime, months: int) -> datetime:
    """
    Adds `months` CALENDAR months to `base` (e.g. 2026-08-31 + 1 month =
    2026-09-30), instead of a flat +30/+60 days, which drifts the renewal
    date earlier every cycle depending on which months it crosses.

    Special case (per product decision): if the resulting month is
    February, fall back to a flat +30 days per month, since Feb only has
    28/29 days and "same day next month" doesn't map cleanly onto it.
    """
    total_month_index = base.month - 1 + months
    target_year = base.year + total_month_index // 12
    target_month = total_month_index % 12 + 1

    if target_month == 2:
        return base + timedelta(days=30 * months)

    last_day_of_target_month = calendar.monthrange(target_year, target_month)[1]
    target_day = min(base.day, last_day_of_target_month)

    return base.replace(year=target_year, month=target_month, day=target_day)


def calc_new_expiry_months(current_expires_at: Optional[str], months: int, *, now: Optional[datetime] = None) -> str:
    """Same 'extend from current expiry, not from today' rule as calc_new_expiry()."""
    reference = now or utcnow_naive()
    base = reference

    current = parse_expiry(current_expires_at)
    if current and current > reference:
        base = current

    return add_calendar_months(base, months).strftime(DATE_FORMAT)
