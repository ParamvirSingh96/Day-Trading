"""Versioned PBKDF2 hashes at the current cost, with transparent upgrade of
legacy 120k-iteration rows on the next successful login."""
from __future__ import annotations

import base64
import hashlib
import os

from conftest import OPERATOR, register
from app import auth as auth_mod
from app.models import User


def test_new_hashes_use_the_versioned_format_and_current_cost():
    salt, encoded = auth_mod._hash_password("hunter2-but-longer")
    algo, iters, salt_b64, digest = encoded.split("$")
    assert algo == auth_mod.PBKDF2_ALGORITHM
    assert int(iters) == auth_mod.PBKDF2_ITERATIONS == 600_000
    assert salt_b64 == salt
    ok, needs = auth_mod._verify_password("hunter2-but-longer", salt, encoded)
    assert ok and not needs
    assert auth_mod._verify_password("wrong", salt, encoded) == (False, False)


def _legacy_hash(password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def test_legacy_rows_still_verify_and_flag_rehash():
    salt, legacy = _legacy_hash("old-password-123")
    ok, needs = auth_mod._verify_password("old-password-123", salt, legacy)
    assert ok and needs
    assert auth_mod._verify_password("nope", salt, legacy) == (False, False)


def test_login_upgrades_a_legacy_hash_in_place(client, db):
    salt, legacy = _legacy_hash("old-password-123")
    db.add(User(name="Legacy", email="legacy@example.com", password_salt=salt,
                password_hash=legacy, is_operator=True))
    db.commit()

    r = client.post("/auth/login", json={"email": "legacy@example.com", "password": "old-password-123"})
    assert r.status_code == 200, r.text

    db.expire_all()
    row = db.query(User).filter_by(email="legacy@example.com").one()
    assert row.password_hash.startswith(f"{auth_mod.PBKDF2_ALGORITHM}$600000$")
    assert row.password_hash.split("$")[2] == row.password_salt
    # and the upgraded hash still works
    r2 = client.post("/auth/login", json={"email": "legacy@example.com", "password": "old-password-123"})
    assert r2.status_code == 200


def test_register_then_login_round_trip(client):
    register(client, OPERATOR)
    fresh = client.__class__(client.app)
    assert fresh.get("/auth/me").status_code == 401
    assert fresh.post("/auth/login", json={"email": OPERATOR["email"],
                                           "password": "wrong-but-long-enough"}).status_code == 401
    assert fresh.post("/auth/login", json={"email": OPERATOR["email"],
                                           "password": OPERATOR["password"]}).status_code == 200
