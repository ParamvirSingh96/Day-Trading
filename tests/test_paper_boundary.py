"""The paper/live boundary is enforced at the broker client, by destination,
not by the mode label — and a paper-only build cannot reach live at all.

All checks use fake credentials and an httpx.MockTransport; nothing touches
the network.
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from app import alpaca as alpaca_mod
from app import trading_mode
from app.alpaca import AlpacaTradingClient, OrderSide, PaperBoundaryViolation, validate_trading_destination


@pytest.fixture
def real_client_mode(monkeypatch):
    """Turn DEMO_MODE off for the client under test and provide fake keys."""
    monkeypatch.setattr(alpaca_mod, "settings",
                        dataclasses.replace(alpaca_mod.settings, demo_mode=False))
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "PKFAKEPAPERKEY")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fake-paper-secret")
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "AKFAKELIVEKEY")
    monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", "fake-live-secret")
    trading_mode._mode = None


def _recording_transport():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x", "status": "accepted"}, request=request)

    return httpx.MockTransport(handler), seen


@pytest.mark.parametrize("url,mode", [
    ("https://api.alpaca.markets", "paper"),            # paper label, live host
    ("http://paper-api.alpaca.markets", "paper"),       # plain http
    ("https://paper-api.alpaca.markets:8443", "paper"), # non-443 port
    ("https://user:pw@paper-api.alpaca.markets", "paper"),
    ("https://paper-api.alpaca.markets.evil.example", "paper"),
    ("https://paper-api.alpaca.markets/v2", "paper"),   # path smuggling
    ("https://paper-api.alpaca.markets", "live"),        # live label, paper host
])
def test_destination_must_match_mode_exactly(monkeypatch, url, mode):
    monkeypatch.setattr(trading_mode, "paper_only_enabled", lambda: False)
    with pytest.raises(PaperBoundaryViolation):
        validate_trading_destination(url, mode)


def test_destination_normalizes_the_approved_host(monkeypatch):
    monkeypatch.setattr(trading_mode, "paper_only_enabled", lambda: False)
    assert validate_trading_destination("https://paper-api.alpaca.markets/", "paper") == \
        "https://paper-api.alpaca.markets"
    assert validate_trading_destination("https://api.alpaca.markets", "live") == \
        "https://api.alpaca.markets"


def test_paper_label_pointing_at_live_host_is_refused_at_construction(real_client_mode, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_URL", "https://api.alpaca.markets")
    transport, seen = _recording_transport()
    with pytest.raises(PaperBoundaryViolation):
        AlpacaTradingClient(transport=transport)
    assert seen == []


def test_paper_only_build_refuses_live_mode_everywhere(real_client_mode, monkeypatch):
    assert trading_mode.paper_only_enabled() is True
    with pytest.raises(PaperBoundaryViolation):
        trading_mode.set_mode("live")
    # even a persisted "live" state file is overridden on load
    trading_mode._write_persisted_mode("live")
    trading_mode._mode = None
    assert trading_mode.get_mode() == "paper"
    # and a live client cannot be constructed regardless of opt-ins
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "yes")
    monkeypatch.setattr(trading_mode, "get_mode", lambda: "live")
    with pytest.raises(PaperBoundaryViolation):
        AlpacaTradingClient(transport=_recording_transport()[0])


def test_live_requires_paper_only_off_and_explicit_opt_in(real_client_mode, monkeypatch):
    monkeypatch.setattr(trading_mode, "paper_only_enabled", lambda: False)
    trading_mode.set_mode("live")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    with pytest.raises(PaperBoundaryViolation):
        AlpacaTradingClient(transport=_recording_transport()[0])
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "yes")
    transport, seen = _recording_transport()
    client = AlpacaTradingClient(transport=transport)
    assert client._base_url == "https://api.alpaca.markets"


def test_mutation_is_rechecked_against_the_actual_destination(real_client_mode):
    transport, seen = _recording_transport()
    client = AlpacaTradingClient(transport=transport)
    assert client._base_url == "https://paper-api.alpaca.markets"
    # simulate drift of the underlying connection to the live host
    client._client.base_url = httpx.URL("https://api.alpaca.markets")
    with pytest.raises(PaperBoundaryViolation):
        client.submit_order("AAPL", 1, OrderSide.BUY)
    assert seen == [], "no request may leave the process after a boundary failure"


def test_mutation_env_flip_cannot_enable_live_after_construction(real_client_mode, monkeypatch):
    monkeypatch.setattr(trading_mode, "paper_only_enabled", lambda: False)
    trading_mode.set_mode("live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "yes")
    transport, seen = _recording_transport()
    client = AlpacaTradingClient(transport=transport)
    monkeypatch.delenv("LIVE_TRADING_ENABLED")        # opt-in withdrawn mid-process
    with pytest.raises(PaperBoundaryViolation):
        client.submit_order("AAPL", 1, OrderSide.BUY)
    assert seen == []


def test_paper_client_sends_mutations_only_to_the_paper_host(real_client_mode):
    transport, seen = _recording_transport()
    client = AlpacaTradingClient(transport=transport)
    client.submit_order("AAPL", 1, OrderSide.BUY)
    assert len(seen) == 1
    assert seen[0].url.host == "paper-api.alpaca.markets"
    assert seen[0].url.scheme == "https"


def test_api_mode_endpoint_refuses_live_on_paper_only_build(operator_client):
    r = operator_client.post("/trading/mode", json={"mode": "live"})
    assert r.status_code == 403
    assert r.json()["error"] == "paper_boundary"
