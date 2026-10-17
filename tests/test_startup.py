"""The full application lifespan (startup → shutdown) against a temporary
database, including the SQLite column migration for a pre-hardening schema
and the rule that startup never promotes anyone to operator."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import OPERATOR, app, login, provision
from app import auth as auth_mod
from app.db import Base, SessionLocal, engine
from app.models import User

LEGACY_USERS_DDL = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY, name VARCHAR(80), email VARCHAR(120) UNIQUE,
  password_salt VARCHAR(255), password_hash VARCHAR(255), created_at DATETIME,
  performance_reset_at DATETIME
)
"""


def _user_columns() -> set[str]:
    with engine.connect() as conn:
        return {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}


def test_lifespan_migrates_legacy_schema_and_never_promotes():
    # A database created before the operator boundary: users table without
    # is_operator, holding the single legacy account.
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text(LEGACY_USERS_DDL))
        salt, pw_hash = auth_mod._hash_password(OPERATOR["password"])
        conn.execute(text(
            "INSERT INTO users (name, email, password_salt, password_hash, created_at) "
            "VALUES (:n, :e, :s, :h, CURRENT_TIMESTAMP)"),
            {"n": OPERATOR["name"], "e": OPERATOR["email"], "s": salt, "h": pw_hash})
    assert "is_operator" not in _user_columns()

    with TestClient(app) as c:          # runs the real lifespan: create_all + migrations + checks
        assert c.get("/health").status_code == 200
        assert "is_operator" in _user_columns()
        # the sole legacy account is NOT promoted by startup
        with SessionLocal() as s:
            assert auth_mod.operator_count(s) == 0
            legacy = s.query(User).filter_by(email=OPERATOR["email"]).one()
            assert legacy.is_operator is False
        login(c, OPERATOR)
        assert c.get("/auth/me").json()["user"]["is_operator"] is False
        assert c.get("/trading/account").status_code == 403

    # explicit provisioning, then a restart: still exactly one operator
    provision(OPERATOR)
    with TestClient(app) as c:
        with SessionLocal() as s:
            assert [u.email for u in auth_mod.list_operators(s)] == [OPERATOR["email"]]
        login(c, OPERATOR)
        assert c.get("/trading/account").status_code == 200
    with SessionLocal() as s:
        assert auth_mod.operator_count(s) == 1


def test_lifespan_is_repeatable_on_a_fresh_database():
    for _ in range(2):
        with TestClient(app) as c:
            assert c.get("/health").json()["status"] == "ok"
    with SessionLocal() as s:
        assert auth_mod.operator_count(s) == 0
