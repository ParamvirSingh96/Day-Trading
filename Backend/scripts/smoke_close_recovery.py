"""Integration test for Option A startup behavior (2026-06-12).

Option A keeps the proven close path and does NOT auto-recover in-flight closes
(that's Option B, a separate project). On startup, `_rehydrate_monitored_positions`
must therefore:

  • re-monitor open Alpaca positions that have a matching open TradeRecord
    (pre-patch behavior — including rows that happen to be status='closing';
    there is NO special routing anymore),
  • emit a PASSIVE, READ-ONLY warning for open DB rows with no matching broker
    position (manual attention) — NEVER mutating status, NEVER submitting orders,
  • do all of the above only when FILL_PERSIST_ENABLED; flag off = inert,
  • INVARIANT: rehydration never calls close_position.

Driven against a real temp SQLite DB + a fake Alpaca client.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import autotrade
from app import db as app_db
from app import trade_history
from app.models import Base, TradeRecord

OPENED = datetime(2026, 6, 11, 13, 30, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, positions):
        self._positions = positions
        self.close_calls: list[str] = []

    def list_positions(self):
        return self._positions

    def close_position(self, sym):          # INVARIANT: must never be called
        self.close_calls.append(sym)
        return {"id": f"unexpected-{sym}"}


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return path, engine, sessionmaker(bind=engine)


def seed(SL, *, symbol, status="filled"):
    db = SL()
    try:
        rec = TradeRecord(
            user_id=1, symbol=symbol, side="buy", direction="long", qty=100,
            setup="Test", confidence=0.8, order_type="market", status=status,
            order_id=f"entry-{symbol}", entry_price=100.0, stop_loss="$98.00",
            target="$104.00", scanner_id="ict", opened_at=OPENED,
        )
        db.add(rec)
        db.commit()
        return rec.id
    finally:
        db.close()


def pos(symbol):
    return {"symbol": symbol, "qty": "100", "side": "long",
            "avg_entry_price": "100", "current_price": "101"}


def run(*, positions, flag, seeds):
    path, engine, SL = make_db()
    ids = {name: seed(SL, **kw) for name, kw in seeds.items()}
    app_db.SessionLocal = SL
    trade_history.SessionLocal = SL
    client = FakeClient(positions)
    autotrade.get_shared_trading_client = lambda: client
    autotrade.get_universe_source = lambda _s: "test"
    autotrade.state.monitored_positions = []
    object.__setattr__(autotrade.settings, "fill_persist_enabled", flag)  # frozen
    warnings: list[str] = []
    orig_warn = autotrade.logger.warning
    autotrade.logger.warning = lambda msg, *a, **k: warnings.append(msg % a if a else msg)
    try:
        autotrade._rehydrate_monitored_positions(1)
    finally:
        autotrade.logger.warning = orig_warn
    assert client.close_calls == [], f"rehydration submitted a close! {client.close_calls}"

    db = SL()
    try:
        rows = {name: db.get(TradeRecord, rid) for name, rid in ids.items()}
        snaps = {n: (r.status, r.closed_at) for n, r in rows.items()}
    finally:
        db.close()
    mon = {m.symbol.upper(): m for m in autotrade.state.monitored_positions}
    engine.dispose()
    os.unlink(path)
    return snaps, mon, warnings


def main():
    # ── Flag ON ──────────────────────────────────────────────────────────────
    snaps, mon, warnings = run(
        positions=[pos("AAA"), pos("CCC")],
        flag=True,
        seeds={
            "AAA": dict(symbol="AAA", status="filled"),    # open + position
            "BBB": dict(symbol="BBB", status="filled"),    # open in DB, NO position
            "CCC": dict(symbol="CCC", status="closing"),   # 'closing' + position
        },
    )
    # Open positions re-monitored normally — NO special 'closing' routing.
    assert "AAA" in mon and not mon["AAA"].close_submitted
    assert "CCC" in mon and not mon["CCC"].close_submitted
    # DB-open / broker-flat row: warned, NOT mutated.
    assert "BBB" not in mon
    assert snaps["BBB"] == ("filled", None)                # status + closed_at unchanged
    assert any("startup_db_broker_mismatch" in w and "BBB" in w for w in warnings)
    # The matched rows did NOT trigger a mismatch warning.
    assert not any("startup_db_broker_mismatch" in w and ("AAA" in w or "CCC" in w) for w in warnings)
    print("  flag ON: normal rehydration + passive mismatch warning + no resubmit  ✓")

    # ── Flag OFF: inert ──────────────────────────────────────────────────────
    snaps, mon, warnings = run(
        positions=[pos("AAA")],
        flag=False,
        seeds={
            "AAA": dict(symbol="AAA", status="filled"),
            "BBB": dict(symbol="BBB", status="filled"),    # open in DB, no position
        },
    )
    assert "AAA" in mon and not mon["AAA"].close_submitted   # normal rehydration still works
    assert snaps["BBB"] == ("filled", None)                  # untouched
    assert not any("startup_db_broker_mismatch" in w for w in warnings)  # gated off
    print("  flag OFF: rehydration only, no mismatch warning (inert)  ✓")

    # ── Flag OFF + flat broker: pre-patch early return, no warning ───────────
    snaps, mon, warnings = run(positions=[], flag=False, seeds={"BBB": dict(symbol="BBB")})
    assert mon == {} and not any("startup_db_broker_mismatch" in w for w in warnings)
    print("  flag OFF + flat: early return, inert  ✓")

    # ── B2 (re-audit): qty validation must NOT truncate ──────────────────────
    vf = autotrade._validated_fill
    assert vf({"status": "filled", "filled_qty": "10", "filled_avg_price": "5"}, 10) == 5.0
    assert vf({"status": "filled", "filled_qty": "10.5", "filled_avg_price": "5"}, 10) is None
    assert vf({"status": "partially_filled", "filled_qty": "10", "filled_avg_price": "5"}, 10) is None
    assert vf({"status": "canceled", "filled_qty": "0", "filled_avg_price": "5"}, 10) is None
    assert vf(None, 10) is None
    print("  B2: qty validation rejects 10.5 vs 10 (no truncation)  ✓")

    # ── B1 (re-audit): fill columns + P&L stay ONE consistent basis across ───
    # repeated update_trade_closed calls (real DB, real function).
    path, engine, SL = make_db()
    rid = seed(SL, symbol="ZZZ", status="filled")  # entry 100, stop $98, qty 100, long
    app_db.SessionLocal = SL
    trade_history.SessionLocal = SL
    uc = trade_history.update_trade_closed
    common = dict(record_id=rid, status="closed", exit_reason="x", trailing_stop_final=None,
                  highest_price_reached=None, hold_duration_seconds=1)
    # First: both fills → columns set, P&L fill-derived (entry 100, exit 105 → +500, R 2.5).
    uc(exit_price=110.0, entry_fill_price=100.0, exit_fill_price=105.0, exit_order_id="oid-1", **common)
    db = SL(); r = db.get(TradeRecord, rid)
    assert (r.entry_fill_price, r.exit_fill_price, r.exit_order_id) == (100.0, 105.0, "oid-1")
    assert r.pnl == 500.0 and r.r_multiple == 2.5, (r.pnl, r.r_multiple)
    db.close()
    # Second: fills None → columns CLEARED + P&L recomputed from monitor
    # (entry 100, exit 110 → +1000, R 5.0). No contradictory state.
    uc(exit_price=110.0, entry_fill_price=None, exit_fill_price=None, exit_order_id=None, **common)
    db = SL(); r = db.get(TradeRecord, rid)
    assert (r.entry_fill_price, r.exit_fill_price, r.exit_order_id) == (None, None, None)
    assert r.pnl == 1000.0 and r.r_multiple == 5.0, (r.pnl, r.r_multiple)
    db.close(); engine.dispose(); os.unlink(path)
    print("  B1: fill columns + P&L never contradict across repeated updates  ✓")

    print("All Option-A startup-behavior checks passed.")


if __name__ == "__main__":
    raise SystemExit(main())
