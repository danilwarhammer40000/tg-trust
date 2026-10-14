"""Regression tests for the revision fixes (DB rollback, atomic pending request,
auto-renewal trigger, secret scrubbing, trial requests)."""
import importlib
import json
import os
from datetime import timedelta

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("TRUSTTUNNEL_DOMAIN", "example.com")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUSTPANEL_DB_PATH", str(tmp_path / "users.json"))
    import core.db as db_module
    importlib.reload(db_module)
    monkeypatch.setattr(db_module, "_notify_admin_safely", lambda text: None)
    return db_module


def _u(name, **kw):
    return {"username": name, "password": "p", "status": "active", "expires_at": "2099-01-01", **kw}


# ---------- DB: rollback instead of wipe ----------

def test_corrupted_db_rolls_back_to_backup(db):
    db.add_user(_u("a"))
    db.add_user(_u("b"))          # second save -> .bak now holds [a]
    with open(db.DB_PATH, "w") as f:
        f.write('[{"username": "a"')   # broken JSON

    users = db.load()
    assert [u["username"] for u in users] == ["a"]      # restored, not []
    # file on disk is valid again, corrupt copy kept for inspection
    assert json.load(open(db.DB_PATH))[0]["username"] == "a"
    assert any(".corrupt-" in n for n in os.listdir(os.path.dirname(db.DB_PATH)))


def test_corrupted_db_without_backup_raises_and_never_wipes(db):
    with open(db.DB_PATH, "w") as f:
        f.write("not json")
    with pytest.raises(db.DBCorruptedError):
        db.add_user(_u("c"))
    assert open(db.DB_PATH).read() == "not json"          # untouched


def test_save_refuses_garbage(db):
    with pytest.raises(ValueError):
        db.save({"not": "a list"})


# ---------- atomic pending request ----------

def test_pop_pending_request_only_once(db):
    db.add_user(_u("a", pending_request={"type": "renewal"}))
    assert db.pop_pending_request("a") == {"type": "renewal"}
    assert db.pop_pending_request("a") is None            # second processor gets nothing
    assert db.get_user("a")["pending_request"] is None


def test_claim_tolerates_tz_aware_timestamp(db):
    db.add_user(_u("a", pending_request={"ai_claimed_at": "2026-01-01T00:00:00+00:00"}))
    assert db.claim_pending_request_for_ai("a") is True   # no TypeError


# ---------- auto-renewal trigger ----------

@pytest.fixture
def ar(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUSTPANEL_DB_PATH", str(tmp_path / "users.json"))
    import core.db, core.paths, core.auto_renewal
    importlib.reload(core.db)
    importlib.reload(core.paths)
    importlib.reload(core.auto_renewal)
    return core.auto_renewal


def test_enabled_means_no_time_conditions(ar):
    assert ar.should_attempt_now() is False
    ar.set_auto_renewal_enabled(True)
    assert ar.should_attempt_now() is True


def test_stuck_request_threshold(ar):
    from core.dates import utcnow_naive
    old = (utcnow_naive() - timedelta(minutes=ar.STUCK_REQUEST_MINUTES + 1)).isoformat()
    new = (utcnow_naive() - timedelta(minutes=1)).isoformat()
    assert ar.is_request_overdue(old) and not ar.is_request_overdue(new)


# ---------- secrets ----------

def test_scrub_masks_bot_token_and_api_key():
    from core.redact import scrub
    text = ("HTTPSConnectionPool: url: /bot123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/sendMessage "
            "and /v1beta/models/x:generateContent?key=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    out = scrub(text)
    assert "AAAAAAAAAAAAAAAAAAAA" not in out and "AIzaSy" not in out


# ---------- trial requests ----------

@pytest.fixture
def trial(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUSTPANEL_DB_PATH", str(tmp_path / "users.json"))
    import core.db, core.paths, core.trial
    importlib.reload(core.db)
    importlib.reload(core.paths)
    importlib.reload(core.trial)
    return core.trial


def test_trial_request_is_single_and_popped_once(trial):
    assert trial.add_trial_request(42, "Ivan", "ivan") is True
    assert trial.add_trial_request(42, "Ivan", "ivan") is False   # no duplicates
    assert trial.pop_trial_request(42)["display_name"] == "Ivan"
    assert trial.pop_trial_request(42) is None                    # double tap on «Одобрить»


def test_trial_used_survives_corruption_via_backup(trial):
    trial.mark_trial_used(1)
    trial.mark_trial_used(2)      # .bak now holds {1}
    with open(trial.TRIAL_USED_PATH, "w") as f:
        f.write("{broken")
    assert trial.load_trial_used_ids() == {1}


def test_username_validation(trial):
    assert trial.USERNAME_RE.match("ivan_123")
    for bad in ("-c", "a b", "иван", "x", "a:b", "x" * 21):
        assert not trial.USERNAME_RE.match(bad)


def test_trial_card_profile_link():
    from bot.handlers.trial_requests import profile_url
    assert profile_url(5, "ivan") == "https://t.me/ivan"
    assert profile_url(5, None) == "tg://user?id=5"


# ---------- forged / repeated receipts ----------

def test_months_cap_sends_huge_receipt_to_manual(ar):
    ok, months, _ = ar.evaluate_receipt_extraction({"readable": True, "confidence": 0.99, "amount": 1200}, rate=100)
    assert ok and months == 12
    ok, months, reason = ar.evaluate_receipt_extraction({"readable": True, "confidence": 0.99, "amount": 100000}, rate=100)
    assert not ok and months == 0 and "лимит" in reason


def test_max_months_is_validated_and_editable(ar):
    assert ar.set_setting_validated("max_months", "0")[0] is False
    assert ar.set_setting_validated("max_months", "61")[0] is False
    assert ar.set_setting_validated("max_months", "3")[0] is True
    ok, months, _ = ar.evaluate_receipt_extraction({"readable": True, "confidence": 0.99, "amount": 400}, rate=100)
    assert not ok


def test_same_receipt_file_accepted_only_once(ar):
    assert ar._receipt_seen("abc") is None
    assert ar._remember_receipt("abc", "ivan") is True
    assert ar._remember_receipt("abc", "petr") is False
    assert ar._receipt_seen("abc") == "ivan"
