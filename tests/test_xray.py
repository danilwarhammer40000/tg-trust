import json
import sys
from datetime import date, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from core import xray

TODAY = date(2026, 10, 3)
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"

SERVER = {
    "decryption": "mlkem768x25519plus.native.600s.PRIVATE",
    "encryption": "mlkem768x25519plus.native.0rtt.PUBLIC",
    "domain": "idomain.test",
    "edge_addr": "198.51.100.9",
}


def user(name, status="active", expires="2026-12-01", **extra):
    u = {"username": name, "status": status, "expires_at": expires}
    u.update(extra)
    return u


def on(uid):
    return {"uuid": uid, "enabled": True}


def base_config(clients):
    return {
        "inbounds": [{
            "protocol": "vless",
            "settings": {"clients": clients, "decryption": "x"},
        }],
        "outbounds": [{"protocol": "freedom"}],
    }


@pytest.fixture
def store_file(monkeypatch, tmp_path):
    """Points the module at a throw-away xray.json and clears env overrides."""
    path = tmp_path / "xray.json"
    monkeypatch.setattr(xray, "XRAY_STORE_PATH", str(path))
    for name in ("XRAY_DOMAIN", "XRAY_EDGE_ADDR", "XRAY_ENC", "XRAY_PATH", "XRAY_PORT"):
        monkeypatch.delenv(name, raising=False)
    return path


def write_store(path, server=None, clients=None):
    path.write_text(json.dumps({"version": 1, "server": server if server is not None else SERVER,
                                "clients": clients or {}}))


# ---------------- compute_desired: opt-in + subscription state ----------------

def test_only_explicitly_enabled_clients_get_access():
    users = [user("a"), user("b"), user("c")]
    clients = {"a": on(U1), "b": {"uuid": U2, "enabled": False}}   # c has no entry at all
    assert xray.compute_desired(users, clients, today=TODAY) == {"a": U1}


def test_client_without_uuid_or_with_truthy_non_bool_is_not_enabled():
    clients = {"a": {"enabled": True}, "b": {"uuid": U2, "enabled": "yes"}}
    assert xray.compute_desired([user("a"), user("b")], clients, today=TODAY) == {}


def test_inactive_user_loses_xray_even_if_enabled():
    assert xray.compute_desired([user("a", status="inactive")], {"a": on(U1)}, today=TODAY) == {}


def test_expired_user_excluded_but_today_is_still_valid():
    users = [user("old", expires="2026-10-02"), user("edge", expires="2026-10-03")]
    clients = {"old": on(U1), "edge": on(U2)}
    assert xray.compute_desired(users, clients, today=TODAY) == {"edge": U2}


def test_unlimited_user_included_and_unparseable_date_fails_closed():
    users = [user("a", expires=None), user("b", expires="garbage")]
    clients = {"a": on(U1), "b": on(U2)}
    assert xray.compute_desired(users, clients, today=TODAY) == {"a": U1}


def test_client_for_a_user_that_no_longer_exists_is_ignored():
    assert xray.compute_desired([], {"ghost": on(U1)}, today=TODAY) == {}


# ---------------- legacy migration (xray_uuid inside users.json) ----------------

def test_adopt_legacy_copies_uuid_and_respects_opt_out():
    users = [user("a", xray_uuid=U1), user("b", xray_uuid=U2, xray_enabled=False), user("c")]
    clients = {}
    assert xray.adopt_legacy(users, clients) is True
    assert clients == {"a": {"uuid": U1, "enabled": True}, "b": {"uuid": U2, "enabled": False}}


def test_adopt_legacy_never_overwrites_an_existing_entry():
    clients = {"a": {"uuid": U3, "enabled": False}}
    assert xray.adopt_legacy([user("a", xray_uuid=U1)], clients) is False
    assert clients["a"]["uuid"] == U3


# ---------------- merge_clients ----------------

def test_manual_clients_are_preserved():
    new, changed = xray.merge_clients(base_config([{"id": U3}]), {"a": U1})
    assert changed is True
    assert [c["id"] for c in new["inbounds"][0]["settings"]["clients"]] == [U3, U1]


def test_removed_user_disappears_and_manual_client_stays():
    cfg = base_config([{"id": U3}, {"id": U1, "email": "tp:a"}, {"id": U2, "email": "tp:b"}])
    new, changed = xray.merge_clients(cfg, {"b": U2})
    assert changed is True
    assert [c["id"] for c in new["inbounds"][0]["settings"]["clients"]] == [U3, U2]


def test_no_change_detected_regardless_of_order():
    cfg = base_config([{"id": U2, "email": "tp:b"}, {"id": U1, "email": "tp:a"}])
    assert xray.merge_clients(cfg, {"a": U1, "b": U2})[1] is False


def test_uuid_change_for_same_user_is_a_change():
    assert xray.merge_clients(base_config([{"id": U1, "email": "tp:a"}]), {"a": U2})[1] is True


def test_merge_does_not_mutate_input_and_needs_a_vless_inbound():
    cfg = base_config([{"id": U1, "email": "tp:a"}])
    snapshot = json.dumps(cfg, sort_keys=True)
    xray.merge_clients(cfg, {})
    assert json.dumps(cfg, sort_keys=True) == snapshot
    with pytest.raises(RuntimeError):
        xray.merge_clients({"inbounds": [{"protocol": "socks"}]}, {"a": U1})


# ---------------- apply_identity ----------------

def test_apply_identity_restores_key_on_a_fresh_server():
    cfg = base_config([])
    cfg["inbounds"][0]["settings"]["decryption"] = "NEWSERVERKEY"
    cfg["inbounds"][0]["streamSettings"] = {
        "network": "xhttp", "security": "none",
        "xhttpSettings": {"path": "/other", "mode": "packet-up"},
    }
    new, changed = xray.apply_identity(cfg, SERVER)
    inbound = new["inbounds"][0]

    assert changed is True
    assert inbound["settings"]["decryption"] == SERVER["decryption"]
    assert inbound["streamSettings"]["xhttpSettings"] == {
        "path": "/api/v1/sync", "mode": "packet-up",
        "xPaddingObfsMode": True, "xPaddingKey": "pref", "xPaddingPlacement": "cookie",
    }


def test_apply_identity_is_idempotent_pure_and_noop_without_key():
    cfg = base_config([])
    snapshot = json.dumps(cfg, sort_keys=True)
    once, _ = xray.apply_identity(cfg, SERVER)
    assert json.dumps(cfg, sort_keys=True) == snapshot
    twice, changed = xray.apply_identity(once, SERVER)
    assert changed is False and twice == once
    new, changed = xray.apply_identity(cfg, {"domain": "x"})
    assert changed is False and new == cfg


# ---------------- store: the separate xray.json ----------------

def test_missing_store_means_xray_is_off(store_file):
    assert xray.is_enabled() is False
    assert xray.load_store() == xray.empty_store()


def test_store_with_server_section_enables_xray_without_env(store_file):
    write_store(store_file)
    assert xray.is_enabled() is True


def test_corrupt_store_disables_reading_but_refuses_mutation(store_file):
    store_file.write_text("{not json")
    assert xray.is_enabled() is False
    with pytest.raises(xray.StoreError):
        xray.load_store(strict=True)
    with pytest.raises(xray.StoreError):
        xray.enable_client("anna")
    assert store_file.read_text() == "{not json"      # nothing was overwritten


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes do not exist on Windows")
def test_save_store_is_private(store_file):
    xray.save_store({"version": 1, "server": SERVER, "clients": {}})
    assert oct(store_file.stat().st_mode & 0o777) == "0o600"


def test_store_clients_hold_only_uuid_and_enabled(store_file):
    write_store(store_file, clients={"a": on(U1)})
    assert set(xray.load_store()["clients"]["a"]) == {"uuid", "enabled"}


# ---------------- admin mutations ----------------

def test_enable_creates_uuid_and_is_stable(store_file):
    write_store(store_file)
    first = xray.enable_client("anna")
    assert xray.enable_client("anna") == first                       # same link on a second tap
    assert xray.load_store()["clients"]["anna"] == {"uuid": first, "enabled": True}


def test_disable_keeps_uuid_so_reenabling_restores_the_same_link(store_file):
    write_store(store_file)
    uid = xray.enable_client("anna")
    xray.disable_client("anna")
    assert xray.load_store()["clients"]["anna"] == {"uuid": uid, "enabled": False}
    assert xray.enable_client("anna") == uid


def test_disable_unknown_client_is_a_noop(store_file):
    write_store(store_file)
    xray.disable_client("nobody")
    assert xray.load_store()["clients"] == {}


def test_rotate_changes_uuid_and_keeps_state(store_file):
    write_store(store_file)
    old = xray.enable_client("anna")
    new = xray.rotate_client("anna")
    assert new != old and xray.load_store()["clients"]["anna"] == {"uuid": new, "enabled": True}


def test_mutations_keep_the_server_section(store_file):
    write_store(store_file)
    xray.enable_client("anna")
    assert xray.load_store()["server"] == SERVER


def test_empty_username_is_rejected(store_file):
    write_store(store_file)
    with pytest.raises(ValueError):
        xray.enable_client("")


# ---------------- admin card state ----------------

def test_client_state_variants(store_file):
    write_store(store_file, clients={"a": on(U1), "b": {"uuid": U2, "enabled": False}})
    future = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")

    assert xray.client_state(user("a", expires=future))["reachable"] is True
    s = xray.client_state(user("a", status="inactive"))
    assert s["enabled"] is True and s["reachable"] is False and "отключён" in s["reason"]
    assert "истёк" in xray.client_state(user("a", expires="2020-01-01"))["reason"]
    assert xray.client_state(user("b"))["enabled"] is False
    assert xray.client_state(user("nobody"))["enabled"] is False


def test_client_state_when_not_configured(store_file):
    assert xray.client_state(user("a"))["configured"] is False


# ---------------- link / block ----------------

def test_build_link_shape_from_the_store(store_file):
    write_store(store_file)
    parsed = urlparse(xray.build_link("anna k", U1))
    q = parse_qs(parsed.query)

    assert parsed.scheme == "vless" and parsed.username == U1
    assert parsed.hostname == "198.51.100.9" and parsed.port == 443
    assert q["sni"] == ["idomain.test"] and q["host"] == ["idomain.test"]
    assert q["type"] == ["xhttp"] and q["mode"] == ["packet-up"] and q["path"] == ["/api/v1/sync"]
    assert q["encryption"] == [SERVER["encryption"]]
    assert json.loads(q["extra"][0]) == {
        "xPaddingObfsMode": True, "xPaddingKey": "pref", "xPaddingPlacement": "cookie",
    }
    assert unquote(parsed.fragment) == "anna k"


def test_env_overrides_the_store(store_file, monkeypatch):
    write_store(store_file)
    monkeypatch.setenv("XRAY_EDGE_ADDR", "203.0.113.1")
    assert urlparse(xray.build_link("a", U1)).hostname == "203.0.113.1"


def test_edge_defaults_to_domain(store_file):
    write_store(store_file, server={"decryption": "d", "encryption": "e", "domain": "only.test"})
    assert urlparse(xray.build_link("a", U1)).hostname == "only.test"


def test_block_is_empty_unless_the_admin_enabled_the_client(store_file):
    future = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
    write_store(store_file, clients={"b": {"uuid": U2, "enabled": False}})
    assert xray.xray_block(user("a", expires=future)) == ""           # no entry
    assert xray.xray_block(user("b", expires=future)) == ""           # switched off
    assert xray.xray_block(None) == ""


def test_block_escapes_the_link_and_flags_an_expired_subscription(store_file):
    write_store(store_file, clients={"a": on(U1)})
    future = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")

    good = xray.xray_block(user("a", expires=future))
    assert "<code>vless://" in good and "&amp;security=tls" in good and "&security" not in good
    assert "Сейчас не работает" not in good

    expired = xray.xray_block(user("a", expires="2020-01-01"))
    assert "истёк" in expired                                         # admin still sees the link, with a warning


def test_block_empty_when_not_configured(store_file):
    assert xray.xray_block(user("a")) == ""
