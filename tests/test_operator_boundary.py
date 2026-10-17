"""Who becomes an operator, and what a non-operator can reach.

Operator status is granted only by the local administrative command
(``scripts/manage_operator.py`` → ``auth.provision_operator``). It is never
granted over HTTP: not to the first account on an empty database, not by
registering with a particular email, not at login, and not at startup.
"""
from __future__ import annotations

import io

import pytest

from conftest import OPERATOR, VISITOR, login, open_registration, provision, register, register_and_login
from app import auth as auth_mod
from app.db import SessionLocal
from scripts import manage_operator


# ── claiming the role over HTTP is impossible ────────────────────────────────

def test_empty_database_closed_registration_cannot_claim_operator(client):
    """An unauthenticated request to a fresh instance gets nothing."""
    r = client.post("/auth/register", json=OPERATOR)
    assert r.status_code == 403
    assert "closed" in r.json()["detail"].lower()
    with SessionLocal() as s:
        assert auth_mod.operator_count(s) == 0


def test_open_registration_never_grants_operator(client, monkeypatch):
    """Even with registration open — and even for an email the admin will
    later provision — a self-registered account is ordinary."""
    open_registration(monkeypatch)
    body = register_and_login(client, OPERATOR)
    assert body["user"]["is_operator"] is False
    assert client.get("/auth/me").json()["user"]["is_operator"] is False
    assert client.get("/trading/account").status_code == 403


def test_login_never_promotes(client, monkeypatch):
    open_registration(monkeypatch)
    register(client, VISITOR)
    for _ in range(2):                     # repeated logins change nothing
        assert login(client, VISITOR)["user"]["is_operator"] is False
    with SessionLocal() as s:
        assert auth_mod.operator_count(s) == 0


# ── explicit provisioning works, and is idempotent ───────────────────────────

def test_admin_provisioning_enables_the_account(client, backend):
    provision(OPERATOR)
    body = login(client, OPERATOR)
    assert body["user"]["is_operator"] is True
    assert client.get("/trading/account").status_code == 200
    r = client.post("/trading/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"})
    assert r.status_code == 200, r.text
    assert "AAPL" in backend.positions


def test_provisioning_is_idempotent_and_promotion_keeps_the_password(client, monkeypatch):
    open_registration(monkeypatch)
    register(client, VISITOR)              # self-registered, ordinary account
    with SessionLocal() as s:
        _, action = auth_mod.provision_operator(s, VISITOR["email"])
        assert action == "promoted"
        _, action = auth_mod.provision_operator(s, VISITOR["email"])
        assert action == "unchanged"
        _, action = auth_mod.provision_operator(s, OPERATOR["email"], name=OPERATOR["name"],
                                                password=OPERATOR["password"])
        assert action == "created"
        _, action = auth_mod.provision_operator(s, OPERATOR["email"], name="x", password="y" * 8)
        assert action == "unchanged"
        assert auth_mod.operator_count(s) == 2
    # the promoted account still logs in with the password IT chose
    assert login(client, VISITOR)["user"]["is_operator"] is True


def test_creating_an_operator_requires_name_and_strong_password():
    with SessionLocal() as s:
        with pytest.raises(ValueError):
            auth_mod.provision_operator(s, "nobody@example.com")
        with pytest.raises(ValueError):
            auth_mod.provision_operator(s, "nobody@example.com", name="N", password="short")
        assert auth_mod.operator_count(s) == 0


def test_cli_create_list_promote_demote(client, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(OPERATOR["password"] + "\n"))
    assert manage_operator.main(["create", "--email", OPERATOR["email"], "--name", OPERATOR["name"],
                                 "--password-stdin"]) == 0
    assert manage_operator.main(["list"]) == 0
    out = capsys.readouterr().out
    assert OPERATOR["email"] in out and OPERATOR["password"] not in out
    assert manage_operator.main(["promote", "--email", "ghost@example.com"]) == 2

    login(client, OPERATOR)
    assert client.get("/trading/account").status_code == 200
    # demotion takes effect immediately: sessions are invalidated
    assert manage_operator.main(["demote", "--email", OPERATOR["email"]]) == 0
    assert client.get("/trading/account").status_code == 401
    assert login(client, OPERATOR)["user"]["is_operator"] is False
    assert client.get("/trading/account").status_code == 403
    assert manage_operator.main(["demote", "--email", OPERATOR["email"]]) == 0   # idempotent


# ── authorization, not just authentication ───────────────────────────────────

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
    open_registration(monkeypatch)
    register_and_login(client, VISITOR)
    r = client.request(method, path, json=payload)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}: {r.text}"
    assert r.json()["detail"] == "Operator access required."


def test_unauthenticated_is_401_not_403(client):
    assert client.get("/trading/account").status_code == 401
