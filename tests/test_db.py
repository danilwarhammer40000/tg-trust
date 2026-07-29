"""
Run with: pytest tests/test_db.py -v

Uses the TRUSTPANEL_DB_PATH env override (added in core/db.py) to point
at a throwaway file instead of /opt/trustpanel/data/users.json, so tests
never touch a real deployment's data.
"""
import importlib
import os

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUSTPANEL_DB_PATH", str(tmp_path / "users.json"))
    import core.db as db_module
    importlib.reload(db_module)  # pick up the env var into DB_PATH
    yield db_module


def test_add_and_get_user(db):
    db.add_user({"username": "alice", "password": "pw1", "status": "active"})
    user = db.get_user("alice")
    assert user is not None
    assert user["password"] == "pw1"


def test_get_missing_user_returns_none(db):
    assert db.get_user("nobody") is None


def test_update_user_returns_true_on_success(db):
    db.add_user({"username": "bob", "password": "pw2", "status": "active"})
    ok = db.update_user("bob", status="inactive")
    assert ok is True
    assert db.get_user("bob")["status"] == "inactive"


def test_update_user_returns_false_when_missing(db):
    assert db.update_user("ghost", status="inactive") is False


def test_delete_user(db):
    db.add_user({"username": "carol", "password": "pw3", "status": "active"})
    db.delete_user("carol")
    assert db.get_user("carol") is None


def test_get_user_by_telegram_id(db):
    db.add_user({"username": "dave", "password": "pw4", "telegram_id": 12345})
    user = db.get_user_by_telegram_id(12345)
    assert user is not None
    assert user["username"] == "dave"


def test_list_users_sorts_unlimited_first(db):
    db.add_user({"username": "z_expiring", "expires_at": "2026-08-01"})
    db.add_user({"username": "a_unlimited", "expires_at": None})
    users = db.list_users()
    assert users[0]["username"] == "a_unlimited"
