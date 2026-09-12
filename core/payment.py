# Реквизиты для оплаты — единственное место, где это нужно менять.
# Используется и ботом (кнопка "Реквизиты для оплаты"), и cleanup.py
# (уведомления T-7/T-3/T-0).

BASE_PRICE_PER_MONTH = 100

# Надбавка за каждую доп. ссылку СВЕРХ бесплатного лимита
# (follower_issuance.FREE_EXTRA_LINKS). Применяется только к ссылкам,
# выданным через обычное "✅ Выдать" в bot/handlers/extra_links.py —
# админ может выдать те же ссылки кнопкой "💚 Без наценки", тогда они не
# увеличивают цену (см. calc_monthly_price ниже).
EXTRA_LINK_SURCHARGE = 30

PAYMENT_INFO = (
    f"💳 Для продления переведите клубный донат из расчета {BASE_PRICE_PER_MONTH}руб/мес "
    "(без комиссии и из любого банка):\n"
    "https://finance.ozon.ru/apps/sbp/ozonbankpay/019deec7-3a90-7887-aed0-13b23f04ee5b\n"
    "Даниил П.\n\n"
    "Чек или скрин для проверки отправьте прямо сюда в чат ✅.\n"
)

# Показывается клиенту, когда доступ истёк: и в момент автоотключения
# (services/cleanup.py), и по кнопке "🔗 Моя ссылка" (bot/bot.py) — если
# клиент истёк, но ещё не был формально отключён (окно между истечением
# и следующим запуском cleanup/sync), либо уже отключён. Один текст в
# одном месте, чтобы формулировки не разъезжались.
ACCESS_EXPIRED_MESSAGE = (
    "❌ Ваш доступ истёк и был отключён.\n"
    "Пришлите чек об оплате в этот чат, чтобы продлить доступ.\n\n"
    f"{PAYMENT_INFO}"
)


def calc_monthly_price(username: str):
    """
    Returns (price_per_month, surcharged_link_count) for `username`'s
    leader account. Base price + EXTRA_LINK_SURCHARGE for each follower
    beyond follower_issuance.FREE_EXTRA_LINKS, MINUS whichever of those
    were granted via "💚 Без наценки" in bot/handlers/extra_links.py
    (tracked as surcharge_exempt_links on the user record — a plain
    count, not which specific follower, since individual follower
    accounts are otherwise interchangeable).

    This is the rate a NEW receipt gets evaluated against (frozen at
    submission time into pending_request["rate_at_submission"] — see
    core/auto_renewal.py's evaluate_receipt_extraction). It does NOT
    retroactively change a client's CURRENT expires_at — someone who
    prepaid several months at a lower rate keeps that paid-for time
    in full; see calc_rate_gap/calc_recalculated_expiry/calc_topup_amount
    below for the two ways a client can optionally true up early instead
    of just waiting for their next renewal.
    """
    # Local imports to avoid a circular import — core.db and
    # follower_issuance don't import core.payment, but importing them at
    # module load time here would still make core.payment's import order
    # matter more than it needs to for a couple of functions only used
    # inside this one helper.
    from core.db import get_followers, get_user
    from follower_issuance import FREE_EXTRA_LINKS

    user = get_user(username) or {}
    followers = get_followers(username)

    paid_links = max(0, len(followers) - FREE_EXTRA_LINKS)
    exempt = min(paid_links, user.get("surcharge_exempt_links") or 0)
    surcharged = paid_links - exempt

    price = BASE_PRICE_PER_MONTH + surcharged * EXTRA_LINK_SURCHARGE
    return price, surcharged


# Used for the proportional day-math below — matches the same "1 month =
# 30 days" convention core.dates.add_calendar_months already falls back
# to for its February edge case, rather than introducing a second
# assumption about what "a month" means.
DAYS_PER_MONTH = 30


def get_last_renewal_rate(username: str) -> int:
    """
    The price-per-month that was ACTUALLY in effect the last time this
    client's expires_at was extended by a real payment — set by
    core/auto_renewal.py's _apply_and_request_review (auto-renewal) and
    bot/handlers/receipt.py's approve_renewal (manual "➕1 мес"/"➕2 мес").
    This is the baseline calc_rate_gap compares today's calc_monthly_price
    against. Falls back to BASE_PRICE_PER_MONTH for accounts that last
    renewed before this field existed (nothing breaks — it just means no
    gap is detected until their next real renewal sets it explicitly).
    """
    from core.db import get_user
    user = get_user(username) or {}
    return user.get("last_renewal_rate") or BASE_PRICE_PER_MONTH


def calc_rate_gap(username: str):
    """
    Returns None if there's nothing to reconcile — no remaining paid time,
    or today's rate isn't actually higher than what their current
    expires_at was paid at. Otherwise returns
    (remaining_days, old_rate, new_rate).

    Deliberately one-directional: if a client's rate went DOWN since
    their last renewal (e.g. an extra link was removed, or more of their
    links became surcharge-exempt), nothing is offered here — they just
    quietly benefit at their next renewal. This only surfaces when they'd
    otherwise be getting MORE service than they paid for.
    """
    from core.dates import is_expired, parse_expiry, utcnow_naive
    from core.db import get_user

    user = get_user(username) or {}
    expires_at = user.get("expires_at")
    if not expires_at or is_expired(expires_at):
        return None

    old_rate = get_last_renewal_rate(username)
    new_rate, _ = calc_monthly_price(username)
    if new_rate <= old_rate:
        return None

    expiry_dt = parse_expiry(expires_at)
    if not expiry_dt:
        return None

    remaining_days = (expiry_dt.date() - utcnow_naive().date()).days
    if remaining_days <= 0:
        return None

    return remaining_days, old_rate, new_rate


def calc_recalculated_expiry(username: str):
    """
    "🔄 Пересчитать срок": shortens the CURRENT expires_at so the money
    already paid (at old_rate) buys the same amount of VALUE at the new,
    higher rate instead of the same amount of TIME — the client keeps
    every rouble's worth of service, just compressed into fewer days.
    Returns None if there's nothing to recalculate (see calc_rate_gap).
    Otherwise returns the new expires_at as a "YYYY-MM-DD" string;
    caller is responsible for actually applying it (and for updating
    last_renewal_rate to the new rate, since after this the gap is
    settled).
    """
    from core.dates import calc_new_expiry
    from core.db import get_user

    gap = calc_rate_gap(username)
    if not gap:
        return None
    remaining_days, old_rate, new_rate = gap

    paid_value = remaining_days * old_rate
    new_remaining_days = int(paid_value / new_rate)
    days_to_cut = remaining_days - new_remaining_days
    if days_to_cut <= 0:
        return None

    user = get_user(username) or {}
    return calc_new_expiry(user.get("expires_at"), -days_to_cut)


def calc_topup_amount(username: str):
    """
    "💰 Доплатить сейчас": the amount needed to keep the CURRENT
    expires_at completely unchanged despite the rate increase — the gap
    between what was already paid (at old_rate) and what the remaining
    days are actually worth at new_rate. Returns None if there's nothing
    to top up (see calc_rate_gap). Rounds UP so an accepted top-up always
    covers the full gap, never leaves the client a rouble short.
    """
    import math

    gap = calc_rate_gap(username)
    if not gap:
        return None
    remaining_days, old_rate, new_rate = gap

    gap_per_day = (new_rate - old_rate) / DAYS_PER_MONTH
    amount = math.ceil(remaining_days * gap_per_day)
    return amount if amount > 0 else None
