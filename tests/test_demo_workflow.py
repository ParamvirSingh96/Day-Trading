"""One complete workflow on a clean checkout with no credentials: register the
operator, read the account, look at a chart, place and close a trade, and see
it in the history — all on the in-process fake broker (DEMO_MODE)."""
from __future__ import annotations


def test_complete_demo_workflow(operator_client, backend):
    c = operator_client

    acct = c.get("/trading/account")
    assert acct.status_code == 200, acct.text
    assert acct.json()["paper"] is True
    starting_cash = float(acct.json()["cash"])

    chart = c.get("/market/chart/AAPL", params={"timeframe": "1Min"})
    assert chart.status_code == 200, chart.text
    assert len(chart.json()["bars"]) > 50

    pulse = c.get("/market/pulse")
    assert pulse.status_code == 200, pulse.text

    order = c.post("/trading/orders", json={"symbol": "AAPL", "qty": 3, "side": "buy"})
    assert order.status_code == 200, order.text
    assert order.json()["status"] == "filled"

    positions = c.get("/trading/positions").json()
    assert [p["symbol"] for p in positions] == ["AAPL"]
    assert int(float(positions[0]["qty"])) == 3

    after_buy = float(c.get("/trading/account").json()["cash"])
    assert after_buy < starting_cash

    closed = c.delete("/trading/positions/AAPL")
    assert closed.status_code == 200, closed.text
    assert c.get("/trading/positions").json() == []

    orders = c.get("/trading/orders", params={"order_status": "all"}).json()
    assert {o["symbol"] for o in orders} == {"AAPL"}
    assert sorted(o["side"] for o in orders) == ["buy", "sell"]
    assert all(o["status"] == "filled" for o in orders)

    # Manual orders live at the broker (asserted above via /trading/orders);
    # /trading/history records autotrader-managed trades, so after a purely
    # manual round-trip it is reachable and empty — not "the trade appears".
    history = c.get("/trading/history")
    assert history.status_code == 200, history.text
    assert history.json() == {"count": 0, "trades": []}

    mode = c.get("/trading/mode").json()
    assert mode["mode"] == "paper"
    assert c.get("/trading/readiness").status_code == 200   # intentionally public
