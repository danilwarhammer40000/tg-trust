import pytest

import core.db
from core import tariff
from core import payment
from follower_issuance import FREE_EXTRA_LINKS


@pytest.fixture
def world(monkeypatch):
    """An in-memory users table wired into core.db, core.tariff and core.payment."""
    users = {}
    followers = {}

    def get_user(name):
        return users.get(name)

    def get_followers(name):
        return [{"username": f} for f in followers.get(name, [])]

    def update_user(name, **fields):
        users[name].update(fields)
        return True

    monkeypatch.setattr(core.db, "get_user", get_user)
    monkeypatch.setattr(core.db, "get_followers", get_followers)
    monkeypatch.setattr(tariff, "get_user", get_user)
    monkeypatch.setattr(tariff, "update_user", update_user)

    class W:
        pass

    w = W()
    w.users, w.followers = users, followers
    return w


def leader_with_followers(world, name="anna", n_followers=0, **fields):
    world.users[name] = {"username": name, "status": "active", **fields}
    world.followers[name] = [f"{name}-f{i}" for i in range(n_followers)]


def expected_auto(n_followers):
    return payment.BASE_PRICE_PER_MONTH + max(0, n_followers - FREE_EXTRA_LINKS) * payment.EXTRA_LINK_SURCHARGE


# ---------------- parse_rate ----------------

@pytest.mark.parametrize("text,value", [
    ("150", 150), (" 150 ", 150), ("150₽", 150), ("150 руб", 150), ("150 р.", 150),
    ("1", 1), ("100000", 100000),
])
def test_parse_rate_accepts(text, value):
    assert tariff.parse_rate(text) == value


@pytest.mark.parametrize("text", ["", None, "abc", "0", "-5", "12.5", "100001", "1e3", "1 500 000"])
def test_parse_rate_rejects(text):
    assert tariff.parse_rate(text) is None


# ---------------- the price itself ----------------

def test_custom_rate_replaces_the_automatic_price(world):
    leader_with_followers(world, n_followers=FREE_EXTRA_LINKS + 3)
    assert payment.calc_monthly_price("anna")[0] == expected_auto(FREE_EXTRA_LINKS + 3)

    tariff.set_custom_rate("anna", 80)

    assert payment.calc_monthly_price("anna") == (80, 0)                 # surcharge is gone too
    assert payment.calc_monthly_price("anna", ignore_custom=True)[0] == expected_auto(FREE_EXTRA_LINKS + 3)


def test_describe_shows_manual_and_automatic(world):
    leader_with_followers(world, n_followers=0)
    assert tariff.describe("anna") == {"custom": None, "auto": expected_auto(0), "current": expected_auto(0)}

    tariff.set_custom_rate("anna", 250)
    assert tariff.describe("anna") == {"custom": 250, "auto": expected_auto(0), "current": 250}


def test_clear_returns_to_the_automatic_price(world):
    leader_with_followers(world, n_followers=FREE_EXTRA_LINKS + 1)
    tariff.set_custom_rate("anna", 10)

    auto = tariff.clear_custom_rate("anna")

    assert auto == expected_auto(FREE_EXTRA_LINKS + 1)
    assert payment.calc_monthly_price("anna")[0] == auto
    assert world.users["anna"]["custom_rate"] is None


def test_set_moves_last_renewal_rate_so_no_true_up_is_offered(world):
    leader_with_followers(world, expires_at="2099-01-01", last_renewal_rate=100)

    # contrast: a raw higher price WOULD open the "доплатить разницу" offer ...
    world.users["anna"]["custom_rate"] = 200
    assert payment.calc_rate_gap("anna") is not None

    # ... but going through the tariff module settles it quietly
    tariff.set_custom_rate("anna", 200)
    assert world.users["anna"]["last_renewal_rate"] == 200
    assert payment.calc_rate_gap("anna") is None


def test_clear_also_settles_last_renewal_rate(world):
    leader_with_followers(world, expires_at="2099-01-01", last_renewal_rate=300)
    tariff.set_custom_rate("anna", 300)
    auto = tariff.clear_custom_rate("anna")
    assert world.users["anna"]["last_renewal_rate"] == auto
    assert payment.calc_rate_gap("anna") is None


# ---------------- guard rails ----------------

@pytest.mark.parametrize("bad", [0, -1, 100001, 12.5, "100", True, None])
def test_set_rejects_invalid_rates_and_changes_nothing(world, bad):
    leader_with_followers(world)
    with pytest.raises(ValueError):
        tariff.set_custom_rate("anna", bad)
    assert "custom_rate" not in world.users["anna"]


def test_follower_and_unknown_user_are_rejected(world):
    leader_with_followers(world)
    world.users["bob"] = {"username": "bob", "status": "active", "linked_to": "anna"}

    with pytest.raises(ValueError):
        tariff.set_custom_rate("bob", 100)
    with pytest.raises(ValueError):
        tariff.set_custom_rate("ghost", 100)
    with pytest.raises(ValueError):
        tariff.clear_custom_rate("bob")
    assert "custom_rate" not in world.users["bob"]


def test_other_fields_are_never_touched(world):
    leader_with_followers(world, expires_at="2099-01-01", password="p", status="active")
    before = {k: v for k, v in world.users["anna"].items()}
    tariff.set_custom_rate("anna", 120)
    after = world.users["anna"]
    for key in ("expires_at", "password", "status", "username"):
        assert after[key] == before[key]
