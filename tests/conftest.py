"""Shared test setup.

Everything runs against a throw-away SQLite database and the in-process fake
broker (DEMO_MODE) — no network, no real credentials, and the developer's
``Backend/.env`` is never loaded (TRADEHACK_SKIP_DOTENV).

The environment MUST be configured before ``app`` is imported because
``app.settings`` builds a frozen Settings object at import time.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "Backend"
sys.path.insert(0, str(BACKEND))

_TMP = Path(tempfile.mkdtemp(prefix="tradehack-tests-"))
os.environ["TRADEHACK_SKIP_DOTENV"] = "1"
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["PAPER_ONLY"] = "true"
os.environ["DEMO_MODE"] = "true"
os.environ["REGISTRATION_MODE"] = "closed"
os.environ["CORS_ORIGINS"] = "http://testserver,http://127.0.0.1:3000"
os.environ["SESSION_COOKIE_SECURE"] = "false"
for _k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER_API_KEY",
           "ALPACA_PAPER_SECRET_KEY", "ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY",
           "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_APP_PASSWORD", "LIVE_TRADING_ENABLED",
           "TRADING_MODE_DEFAULT", "SCHEDULER_USER_EMAIL"):
    os.environ.pop(_k, None)
os.environ["AUTO_START_TRADING"] = "false"
os.environ["AUTONOMOUS_TRADING_ENABLED"] = "false"
os.environ["EXPERIMENTAL_SCANNER_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import trading_mode  # noqa: E402
from app import alpaca as alpaca_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.fake_broker import reset_shared_backend, shared_backend  # noqa: E402
from app.main import app  # noqa: E402
from app.settings import settings  # noqa: E402

# Keep trading-mode persistence out of the real data/ directory, and every
# ~/.tradehack/* telemetry/state directory out of the real home directory
# (the lifespan tests start the scheduler, which records per-day state).
trading_mode._STATE_DIR = _TMP
trading_mode._STATE_FILE = _TMP / "trading_mode.json"
from app import autotrade, matched_outcomes, scan_stats, scan_stats_experimental, scheduler_state  # noqa: E402
scheduler_state._STATE_DIR = _TMP / "state"
scan_stats._OUT_DIR = _TMP / "scan_stats"
scan_stats_experimental._OUT_DIR = _TMP / "scan_stats_experimental"
matched_outcomes._OUT_DIR = _TMP / "matched_outcomes"
autotrade._HANG_DUMP_DIR = _TMP / "hang_dumps"
# Rate limits are per-IP and would trip across a test session.
app.state.limiter.enabled = False

OPERATOR = {"name": "Operator One", "email": "operator@example.com", "password": "correct-horse-battery"}
VISITOR = {"name": "Visitor Two", "email": "visitor@example.com", "password": "another-strong-pass"}


@pytest.fixture(autouse=True)
def _fresh_state():
    """Fresh tables + fresh fake broker + paper mode for every test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    reset_shared_backend()
    alpaca_mod.reset_shared_trading_client()
    trading_mode._mode = None
    if trading_mode._STATE_FILE.exists():
        trading_mode._STATE_FILE.unlink()
    yield
    alpaca_mod.reset_shared_trading_client()
    reset_shared_backend()


@pytest.fixture
def client() -> TestClient:
    # No ``with`` block on purpose: the lifespan (scheduler, trade stream,
    # universe restore) is not part of what these tests exercise.
    return TestClient(app)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def backend():
    return shared_backend()


def register(client: TestClient, user: dict) -> dict:
    r = client.post("/auth/register", json=user)
    assert r.status_code == 200, r.text
    return r.json()


def login(client: TestClient, user: dict) -> dict:
    r = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert r.status_code == 200, r.text
    return r.json()


def session_cookie_header(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(settings.session_cookie_name)
    assert token, "no session cookie on the test client"
    return {"Cookie": f"{settings.session_cookie_name}={token}"}


def register_and_login(client: TestClient, user: dict) -> dict:
    """Registration never issues a session (the UI routes through the
    login page), so tests that need an authenticated client do both.
    Only works while REGISTRATION_MODE is open (see ``open_registration``)."""
    register(client, user)
    return login(client, user)


def provision(user: dict) -> None:
    """Provision an operator the way the administrator does: locally,
    through ``auth.provision_operator`` (what scripts/manage_operator.py
    calls) — never through an HTTP request."""
    from app.auth import provision_operator
    with SessionLocal() as s:
        provision_operator(s, user["email"], name=user["name"], password=user["password"])


def open_registration(monkeypatch) -> None:
    import dataclasses
    from app import auth as auth_mod
    monkeypatch.setattr(auth_mod, "settings",
                        dataclasses.replace(auth_mod.settings, registration_mode="open"))


@pytest.fixture
def operator_client(client: TestClient) -> TestClient:
    provision(OPERATOR)
    login(client, OPERATOR)
    return client
