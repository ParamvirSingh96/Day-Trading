"""Logging in must not grant control of the shared broker account."""
from __future__ import annotations

import dataclasses

import pytest

from conftest import OPERATOR, VISITOR, login, register, register_and_login
from app import auth as auth_mod


def test_first_account_is_the_bootstrap_operator(client):
    body = register(client, OPERATOR)
    assert body["user"]["is_operator"] is True
    assert client.get("/auth/me").status_code == 401     # registering never logs you in
    login(client, OPERATOR)
    me = client.get("/auth/me").json()
    assert me["user"]["is_operator"] is True


def test_registration_is_closed_after_bootstrap(client):
    register(client, OPERATOR)
    fresh = client.__class__(client.app)
    r = fresh.post("/auth/register", json=VISITOR)
    assert r.status_code == 403
    assert "closed" in r.json()["detail"].lower()


def _open_registration(monkeypatch):
    opened = dataclasses.replace(auth_mod.settings, registration_mode="open")
    monkeypatch.setattr(auth_mod, "settings", opened)


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/trading/account", None),
    ("GET", "/trading/positions", None),
    ("GET", "/trading/orders", None),
    ("POST", "/trading/orders", {"symbol": "AAPL", "qty": 1, "side": "buy"}),
    ("DELETE", "/trading/positions", None),
    ("DELETE", "/trading/orders", None),
    ("POST", "/trading/mode", {"mode": "live"}),
    ("POST", "/trading/autotrade/start", {}),
    ("POST", "/trading/autotrade/stop", None),
    ("GET", "/trading/history", None),
    ("POST", "/market/morning-universe/build", {}),
    ("GET", "/market/pulse", None),
])
def test_non_operator_is_denied_everywhere(client, monkeypatch, method, path, payload):
    register(client, OPERATOR)
    _open_registration(monkeypatch)
    visitor = client.__class__(client.app)
    body = register_and_login(visitor, VISITOR)
    assert body["user"]["is_operator"] is False

    r = visitor.request(method, path, json=payload)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}: {r.text}"
    assert r.json()["detail"] == "Operator access required."


def test_unauthenticated_is_401_not_403(client):
    assert client.get("/trading/account").status_code == 401


def test_operator_reaches_the_account(operator_client):
    r = operator_client.get("/trading/account")
    assert r.status_code == 200, r.text
    assert r.json()["paper"] is True


def test_operator_emails_promote_on_login(client, monkeypatch):
    register(client, OPERATOR)
    _open_registration(monkeypatch)
    visitor = client.__class__(client.app)
    register_and_login(visitor, VISITOR)
    assert visitor.get("/trading/account").status_code == 403

    invited = dataclasses.replace(auth_mod.settings, registration_mode="open",
                                  operator_emails=[VISITOR["email"]])
    monkeypatch.setattr(auth_mod, "settings", invited)
    body = login(visitor, VISITOR)
    assert body["user"]["is_operator"] is True
    assert visitor.get("/trading/account").status_code == 200


def test_bootstrap_promotes_single_legacy_account(client, db):
    """A pre-hardening database with one non-operator account becomes the
    operator at startup (``ensure_operator_bootstrap``)."""
    from app.models import User
    salt, pw_hash = auth_mod._hash_password("legacy-pass-123")
    db.add(User(name="Legacy", email="legacy@example.com", password_salt=salt,
                password_hash=pw_hash, is_operator=False))
    db.commit()
    auth_mod.ensure_operator_bootstrap(db)
    assert db.query(User).filter_by(email="legacy@example.com").one().is_operator is True
