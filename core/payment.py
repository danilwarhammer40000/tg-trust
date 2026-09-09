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

    Deliberately NOT wired into the Gemini amount->months conversion in
    core/auto_renewal.py (which still assumes a flat BASE_PRICE_PER_MONTH
    for everyone) — that's a separate, bigger change nobody asked for
    yet. This is display-only for now: shown to the client on "💳
    Реквизиты для оплаты" (bot/handlers/client_menu.py) so they know what
    they actually owe; a human (admin) still decides how many months a
    given payment covers.
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
