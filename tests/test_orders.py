"""Order validation happens before the broker is contacted, and broker
failures become structured responses — never a generic 500 and never a
blind retry."""
from __future__ import annotations

import httpx
import pytest


def _order(**over):
    base = {"symbol": "AAPL", "qty": 1, "side": "buy", "order_type": "market"}
    base.update(over)
    return base


@pytest.mark.parametrize("payload,fragment", [
    (_order(order_type="market", limit_price=100.0), "must not carry limit_price"),
    (_order(order_type="limit"), "require limit_price"),
    (_order(order_type="limit", limit_price=-5.0), "positive finite"),
    (_order(order_type="stop", stop_price=0), "positive finite"),
    (_order(order_type="stop_limit", limit_price=100.0), "require stop_price"),
    (_order(take_profit_price=110.0), "provided together"),
    (_order(take_profit_price=90.0, stop_loss_price=110.0), "above stop_loss_price"),
    (_order(side="sell", take_profit_price=110.0, stop_loss_price=90.0), "below stop_loss_price"),
    (_order(order_type="limit", limit_price=100.0, take_profit_price=105.0, stop_loss_price=101.0),
     "between"),
    (_order(symbol="aapl; drop"), "pattern"),
    (_order(qty=0), "greater than or equal"),
])
def test_invalid_orders_are_rejected_before_the_broker(operator_client, backend, payload, fragment):
    r = operator_client.post("/trading/orders", json=payload)
    assert r.status_code == 422, r.text
    assert fragment.lower() in r.text.lower()
    assert not [q for q in backend.requests if q.url.path == "/v2/orders" and q.method == "POST"]


@pytest.mark.parametrize("token", ["Infinity", "-Infinity", "NaN", "1e999"])
def test_non_finite_prices_are_rejected(operator_client, backend, token):
    # Python's JSON parser accepts these non-standard tokens, so the request
    # reaches validation; the validator (or the JSON layer) must refuse them —
    # never pass them to the broker.
    raw = '{"symbol": "AAPL", "qty": 1, "side": "buy", "order_type": "limit", "limit_price": %s}' % token
    r = operator_client.post("/trading/orders", content=raw,
                             headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert not [q for q in backend.requests if q.url.path == "/v2/orders" and q.method == "POST"]


def test_valid_market_order_fills_on_the_fake_broker(operator_client, backend):
    r = operator_client.post("/trading/orders", json=_order(qty=2))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "filled"
    assert "AAPL" in backend.positions


def test_broker_rejection_is_a_structured_422(operator_client, backend):
    backend.faults[("POST", "/v2/orders")] = (422, {"code": 40310000, "message": "insufficient qty available"})
    r = operator_client.post("/trading/orders", json=_order())
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "order_rejected"
    assert body["broker_status"] == 422
    assert "insufficient" in body["message"]


def test_broker_server_error_is_503_not_500(operator_client, backend):
    backend.faults[("POST", "/v2/orders")] = (500, {"message": "internal"})
    r = operator_client.post("/trading/orders", json=_order())
    assert r.status_code == 503
    assert r.json()["error"] == "broker_unavailable"


def test_timeout_on_submit_is_uncertain_and_not_retried(operator_client, backend):
    backend.faults[("POST", "/v2/orders")] = httpx.ReadTimeout("simulated timeout")
    r = operator_client.post("/trading/orders", json=_order())
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "uncertain_outcome"
    assert "resubmit" in body["action"].lower()
    posts = [q for q in backend.requests if q.url.path == "/v2/orders" and q.method == "POST"]
    assert len(posts) == 1, "a mutation must be attempted exactly once"


def test_cancel_unknown_order_is_structured(operator_client):
    r = operator_client.delete("/trading/orders/does-not-exist")
    assert r.status_code == 422
    assert r.json()["error"] == "order_rejected"


def test_quick_trade_uses_entry_price_only_as_a_limit(operator_client, backend):
    r = operator_client.post("/trading/quick-trade", json={
        "symbol": "MSFT", "side": "buy", "qty": 1,
        "entry_price": 400.0, "stop_loss_price": 390.0, "take_profit_price": 420.0,
    })
    assert r.status_code == 200, r.text
    import json
    sent = [q for q in backend.requests if q.url.path == "/v2/orders" and q.method == "POST"][-1]
    body = json.loads(sent.read().decode())
    assert body["type"] == "limit" and body["order_class"] == "bracket"
    assert body["limit_price"] == "400.0"
