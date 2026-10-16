"""Regression checks for the monitored-position close boundary.

This script never contacts Alpaca or writes the database. It verifies the
three close-failure boundaries stay separate:
  1. broker submission failure  -> close_failed, position retryable
  2. accepted but unconfirmed   -> close_unconfirmed, read-only reconcile,
                                   never a duplicate close order
  3. post-confirmation telemetry failure -> never relabels the close
plus: delayed fills confirm within the poll window, already-flat positions
reconcile as external, and malformed opened_at metadata cannot block a close.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app import autotrade


class FakeTradingClient:
    """In-memory Alpaca stand-in.

    ``fill_after_polls`` models acceptance != fill: after close_position is
    accepted, get_position keeps reporting the position open for that many
    further lookups. ``None`` = never fills (stuck close).
    """

    def __init__(self, *, position_exists: bool = True,
                 close_error: Exception | None = None,
                 fill_after_polls: int | None = 0,
                 order_lookup_error: Exception | None = None,
                 order_status: str = "filled", order_filled_qty: str = "10"):
        self.position_exists = position_exists
        self.close_error = close_error
        self.fill_after_polls = fill_after_polls
        self.order_lookup_error = order_lookup_error
        self.order_status = order_status        # for _validated_fill exercise
        self.order_filled_qty = order_filled_qty
        self.close_calls = 0
        self.close_accepted = False

    def get_position(self, _symbol: str):
        if not self.position_exists:
            return None
        if self.close_accepted:
            if self.fill_after_polls is None:
                return object()  # never fills
            if self.fill_after_polls > 0:
                self.fill_after_polls -= 1
                return object()
            self.position_exists = False
            return None
        return object()

    def close_position(self, _symbol: str):
        self.close_calls += 1
        if self.close_error:
            raise self.close_error
        self.close_accepted = True
        # Alpaca's DELETE /v2/positions response IS the close order.
        return {"id": "fake-close-order-1"}

    def get_order(self, order_id: str):
        if self.order_lookup_error:
            raise self.order_lookup_error
        return {"id": order_id, "status": self.order_status,
                "filled_qty": self.order_filled_qty, "filled_avg_price": "101.5"}


def make_position() -> autotrade.MonitoredPosition:
    trade_log = autotrade.TradeLog(
        time=datetime.now(timezone.utc).isoformat(),
        symbol="TEST",
        side="sell",
        qty=10,
        order_type="market",
        confidence=0.8,
        setup="Synthetic Close Test",
        status="submitted",
    )
    return autotrade.MonitoredPosition(
        symbol="TEST",
        direction="short",
        entry_price=100.0,
        qty=10,
        order_id="synthetic-order",
        opened_at=datetime.now(timezone.utc).isoformat(),
        trade_log=trade_log,
        trailing_stop=101.0,
        lowest_price=99.0,
    )


async def run_checks() -> None:
    # Shrink the confirmation poll so the stuck-close case runs fast.
    autotrade._CLOSE_CONFIRM_POLL_INTERVAL_S = 0.01
    autotrade._CLOSE_CONFIRM_POLL_ATTEMPTS = 3

    # Exercise the patch (frozen settings → bypass for the test; default-off in
    # prod; the integration test covers the flag-off inert path).
    object.__setattr__(autotrade.settings, "fill_persist_enabled", True)

    events: list[str] = []
    persist_calls: list[dict] = []

    autotrade.emit_monitor_event = lambda name, _data: events.append(name)
    autotrade.emit_wallet_update = lambda: None
    autotrade._push_status = lambda: None
    autotrade.update_trade_closed = lambda **kwargs: persist_calls.append(kwargs) or True

    # 1. Accepted close + fill confirmed by polling -> closed, persisted once,
    #    with VALIDATED order-ID-linked fills (status=filled, qty reconciles).
    client = FakeTradingClient()
    autotrade.get_shared_trading_client = lambda: client
    pos = make_position()
    await autotrade._close_monitored_position(pos, "trailing_stop", 101.0)
    assert pos.closed and not pos.closing
    assert pos.trade_log.status == "closed"
    assert client.close_calls == 1
    assert len(persist_calls) == 1
    assert persist_calls[-1]["entry_fill_price"] == 101.5
    assert persist_calls[-1]["exit_fill_price"] == 101.5
    assert persist_calls[-1]["exit_order_id"] == "fake-close-order-1"

    # 2. A genuine broker failure remains open and retries successfully.
    retry_client = FakeTradingClient(close_error=RuntimeError("broker unavailable"))
    autotrade.get_shared_trading_client = lambda: retry_client
    retry_pos = make_position()
    await autotrade._close_monitored_position(retry_pos, "max_hold_time", 101.0)
    assert not retry_pos.closed and not retry_pos.closing
    assert not retry_pos.close_submitted  # rejected, not accepted -> resubmit allowed
    assert retry_pos.trade_log.status == "close_failed"
    retry_client.close_error = None
    await autotrade._close_monitored_position(retry_pos, "max_hold_time", 101.0)
    assert retry_pos.closed and retry_client.close_calls == 2

    # 3. Malformed opened_at is METADATA — must never block the broker close.
    meta_client = FakeTradingClient()
    autotrade.get_shared_trading_client = lambda: meta_client
    meta_pos = make_position()
    meta_pos.opened_at = "invalid-timestamp"
    await autotrade._close_monitored_position(meta_pos, "max_hold_time", 101.0)
    assert meta_pos.closed and meta_pos.trade_log.status == "closed"
    assert meta_pos.trade_log.hold_duration_seconds == 0  # best-effort fallback
    assert meta_client.close_calls == 1

    # 4. An already-flat position reconciles as an external close, no order.
    flat_client = FakeTradingClient(position_exists=False)
    autotrade.get_shared_trading_client = lambda: flat_client
    flat_pos = make_position()
    await autotrade._close_monitored_position(flat_pos, "trailing_stop", 101.0)
    assert flat_pos.closed and flat_pos.exit_reason == "external"
    assert flat_client.close_calls == 0

    # 5. DELAYED FILL: broker stays open for 2 polls, then flat -> confirmed
    #    within the window; closed with exactly one close order.
    slow_client = FakeTradingClient(fill_after_polls=2)
    autotrade.get_shared_trading_client = lambda: slow_client
    slow_pos = make_position()
    await autotrade._close_monitored_position(slow_pos, "trailing_stop", 101.0)
    assert slow_pos.closed and slow_pos.trade_log.status == "closed"
    assert slow_client.close_calls == 1

    # 6. UNCONFIRMED: never fills in-window -> close_unconfirmed, NOT closed,
    #    loud alert, no duplicate orders; read-only reconcile finalizes once
    #    the broker reports flat.
    stuck_client = FakeTradingClient(fill_after_polls=None)
    autotrade.get_shared_trading_client = lambda: stuck_client
    stuck_pos = make_position()
    await autotrade._close_monitored_position(stuck_pos, "max_hold_time", 101.0)
    assert not stuck_pos.closed and stuck_pos.close_submitted
    assert stuck_pos.trade_log.status == "close_unconfirmed"
    assert "close_unconfirmed" in events
    n_persist = len(persist_calls)
    # Re-entry while still open: routes to reconcile, never a second order.
    await autotrade._close_monitored_position(stuck_pos, "max_hold_time", 101.0)
    assert stuck_client.close_calls == 1 and not stuck_pos.closed
    # Monitor-loop reconcile while still open: read-only, stays unconfirmed.
    await autotrade._reconcile_unconfirmed_close(stuck_pos)
    assert stuck_client.close_calls == 1 and not stuck_pos.closed
    assert stuck_pos.trade_log.status == "close_unconfirmed"
    # Broker finally reports flat -> reconcile finalizes; still one order.
    stuck_client.fill_after_polls = 0
    await autotrade._reconcile_unconfirmed_close(stuck_pos)
    assert stuck_pos.closed and stuck_pos.trade_log.status == "closed"
    assert stuck_pos.exit_reason == "max_hold_time"  # original pending reason kept
    assert stuck_client.close_calls == 1
    assert len(persist_calls) == n_persist + 1

    # 7. Telemetry failures after fill confirmation cannot relabel the close.
    telemetry_client = FakeTradingClient()
    autotrade.get_shared_trading_client = lambda: telemetry_client
    telemetry_pos = make_position()
    autotrade.emit_monitor_event = lambda _name, _data: (_ for _ in ()).throw(
        RuntimeError("telemetry unavailable")
    )
    await autotrade._close_monitored_position(telemetry_pos, "trailing_stop", 101.0)
    assert telemetry_pos.closed and not telemetry_pos.closing
    assert telemetry_pos.trade_log.status == "closed"

    # 8. Fill-lookup failure is telemetry-only: close still finalizes as
    #    closed; fill fields persist as None (analysis falls back to monitor
    #    prices, labeled approximate).
    autotrade.emit_monitor_event = lambda name, _data: events.append(name)
    nofill_client = FakeTradingClient(order_lookup_error=RuntimeError("orders API down"))
    autotrade.get_shared_trading_client = lambda: nofill_client
    nofill_pos = make_position()
    await autotrade._close_monitored_position(nofill_pos, "trailing_stop", 101.0)
    assert nofill_pos.closed and nofill_pos.trade_log.status == "closed"
    assert persist_calls[-1]["entry_fill_price"] is None
    assert persist_calls[-1]["exit_fill_price"] is None
    assert persist_calls[-1]["exit_order_id"] == "fake-close-order-1"

    # 9. PROVEN read-only reconcile (poll-until-flat layer, unchanged by Option
    #    A): an in-flight close that confirms flat finalizes WITHOUT a resubmit,
    #    and records the validated exit fill via its in-process close-order id.
    recovered_client = FakeTradingClient(position_exists=False)
    autotrade.get_shared_trading_client = lambda: recovered_client
    recovered_pos = make_position()
    recovered_pos.close_submitted = True
    recovered_pos.close_order_id = "recovered-close-9"
    recovered_pos.pending_close_reason = "max_hold_time"
    recovered_pos.pending_close_price = 101.0
    await autotrade._reconcile_unconfirmed_close(recovered_pos)
    assert recovered_pos.closed and recovered_pos.trade_log.status == "closed"
    assert recovered_client.close_calls == 0  # never resubmits
    assert persist_calls[-1]["exit_order_id"] == "recovered-close-9"
    assert persist_calls[-1]["exit_fill_price"] == 101.5  # validated fill
    assert recovered_pos.exit_reason == "max_hold_time"

    # 10. FILL VALIDATION (P1 #3): a non-filled order, OR an aggregate close
    #     whose filled_qty != this position's qty (repeated same-symbol entries),
    #     must persist NULL fills — NEVER guess — while the trade still closes.
    for label, kw in (
        ("partial", dict(order_status="partially_filled", order_filled_qty="10")),
        ("canceled", dict(order_status="canceled", order_filled_qty="0")),
        ("qty_mismatch", dict(order_status="filled", order_filled_qty="20")),  # aggregate
    ):
        fv_client = FakeTradingClient(**kw)
        autotrade.get_shared_trading_client = lambda c=fv_client: c
        fv_pos = make_position()  # qty=10
        await autotrade._close_monitored_position(fv_pos, "trailing_stop", 101.0)
        assert fv_pos.closed and fv_pos.trade_log.status == "closed", label
        assert persist_calls[-1]["entry_fill_price"] is None, label
        assert persist_calls[-1]["exit_fill_price"] is None, label

    assert "close_failed" in events
    assert "position_closed" in events
    print("All monitored-position close lifecycle checks passed.")


if __name__ == "__main__":
    asyncio.run(run_checks())
