"""
Manual per-client tariff (monthly price), set by the admin.

By default a client's price is automatic (core.payment.calc_monthly_price: base
price plus a surcharge per extra link). The admin can pin a client to a fixed
price instead; it is stored as `custom_rate` on the user record and replaces the
automatic price everywhere calc_monthly_price is used (payment screen, receipt
checks, renewal reminders).

Setting or clearing a manual tariff also moves `last_renewal_rate` to the new
value. That is deliberate: a manual correction must not make the "рассчитать /
доплатить разницу" offer (core.payment.calc_rate_gap) pop up for time the client
already paid for. The new price simply applies from the next renewal.

Followers (linked_to) have no tariff of their own: they follow their leader.
"""
from typing import Dict, Optional

from core.db import get_user, update_user
from core.payment import calc_monthly_price

MIN_RATE = 1
MAX_RATE = 100_000


def parse_rate(text: Optional[str]) -> Optional[int]:
    """'150', '150₽', '150 руб' -> 150. None for anything invalid or out of range."""
    if not text:
        return None
    clean = text.strip().lower()
    for junk in ("₽", "руб.", "руб", "р.", "р", " "):
        clean = clean.replace(junk, "")
    if not clean.isdigit():
        return None
    value = int(clean)
    return value if MIN_RATE <= value <= MAX_RATE else None


def describe(username: str) -> Dict:
    """{'custom': int|None, 'auto': int, 'current': int}"""
    user = get_user(username) or {}
    auto, _ = calc_monthly_price(username, ignore_custom=True)
    current, _ = calc_monthly_price(username)
    return {"custom": user.get("custom_rate") or None, "auto": auto, "current": current}


def _editable_user(username: str) -> Dict:
    user = get_user(username)
    if not user:
        raise ValueError("user not found")
    if user.get("linked_to"):
        raise ValueError("a follower has no tariff of its own, it follows its leader")
    return user


def set_custom_rate(username: str, rate: int) -> None:
    if not isinstance(rate, int) or isinstance(rate, bool) or not (MIN_RATE <= rate <= MAX_RATE):
        raise ValueError(f"rate must be an integer from {MIN_RATE} to {MAX_RATE}")
    _editable_user(username)
    update_user(username, custom_rate=rate, last_renewal_rate=rate)


def clear_custom_rate(username: str) -> int:
    """Back to the automatic price. Returns that price."""
    _editable_user(username)
    auto, _ = calc_monthly_price(username, ignore_custom=True)
    update_user(username, custom_rate=None, last_renewal_rate=auto)
    return auto
