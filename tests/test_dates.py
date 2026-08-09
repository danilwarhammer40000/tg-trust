"""
Run with: pytest tests/test_dates.py -v

Covers core/dates.py — the module that has already had two silent bugs
fixed in it historically (see comments in calc_new_expiry / add_calendar_months
in the original codebase). This is exactly the kind of logic that benefits
most from regression tests, since a reintroduced bug here silently shifts
every user's renewal date rather than throwing an error.
"""
from datetime import datetime

from core.dates import (
    add_calendar_months,
    calc_new_expiry,
    calc_new_expiry_months,
    is_expired,
    parse_expiry,
)


# ---------------- parse_expiry ----------------

def test_parse_expiry_valid_date():
    assert parse_expiry("2026-07-29") == datetime(2026, 7, 29)


def test_parse_expiry_none_and_empty():
    assert parse_expiry(None) is None
    assert parse_expiry("") is None


def test_parse_expiry_garbage():
    assert parse_expiry("not-a-date") is None


# ---------------- is_expired ----------------

def test_is_expired_unlimited_never_expires():
    assert is_expired(None) is False
    assert is_expired("") is False


def test_is_expired_past_date():
    now = datetime(2026, 7, 29)
    assert is_expired("2026-07-28", now=now) is True


def test_is_expired_today_not_yet_expired():
    now = datetime(2026, 7, 29)
    assert is_expired("2026-07-29", now=now) is False


def test_is_expired_future_date():
    now = datetime(2026, 7, 29)
    assert is_expired("2026-08-01", now=now) is False


def test_is_expired_garbage_date_fails_open_for_display():
    # Display-layer is_expired() treats an unparseable date as "not expired"
    # rather than silently locking someone out over a data glitch.
    assert is_expired("garbage") is False


# ---------------- calc_new_expiry (day-based extend) ----------------

def test_calc_new_expiry_extends_from_current_expiry_not_today():
    # BUGFIX regression test: extending must add on top of the CURRENT
    # expiry (if still in the future), not on top of "today".
    now = datetime(2026, 7, 29)
    result = calc_new_expiry("2026-07-30", 30, now=now)
    assert result == "2026-08-29"


def test_calc_new_expiry_from_today_when_already_expired():
    now = datetime(2026, 7, 29)
    result = calc_new_expiry("2026-01-01", 3, now=now)
    assert result == "2026-08-01"


def test_calc_new_expiry_from_today_when_no_prior_expiry():
    now = datetime(2026, 7, 29)
    result = calc_new_expiry(None, 3, now=now)
    assert result == "2026-08-01"


# ---------------- add_calendar_months ----------------

def test_add_calendar_months_same_day_next_month():
    result = add_calendar_months(datetime(2026, 7, 15), 1)
    assert result == datetime(2026, 8, 15)


def test_add_calendar_months_clamps_short_month():
    # 2026-08-31 + 1 month -> September only has 30 days
    result = add_calendar_months(datetime(2026, 8, 31), 1)
    assert result == datetime(2026, 9, 30)


def test_add_calendar_months_rolls_over_year():
    result = add_calendar_months(datetime(2026, 12, 15), 1)
    assert result == datetime(2027, 1, 15)


def test_add_calendar_months_february_uses_flat_30_days():
    # Product decision: target month == February falls back to flat +30/month
    # instead of calendar-month math, since Feb doesn't have a clean "same day".
    # Jan 5 + 30 days = Feb 4 (Jan has 31 days).
    result = add_calendar_months(datetime(2026, 1, 5), 1)
    assert result == datetime(2026, 2, 4)


# ---------------- calc_new_expiry_months ----------------

def test_calc_new_expiry_months_extends_from_current_expiry():
    now = datetime(2026, 7, 29)
    result = calc_new_expiry_months("2026-07-30", 1, now=now)
    assert result == "2026-08-30"


def test_calc_new_expiry_months_two_months():
    now = datetime(2026, 7, 29)
    result = calc_new_expiry_months(None, 2, now=now)
    assert result == "2026-09-29"
