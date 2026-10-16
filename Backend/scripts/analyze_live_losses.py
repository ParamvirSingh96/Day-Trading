#!/usr/bin/env python3
"""analyze_live_losses.py — read-only post-mortem of every live scanner trade.

Per trade: hold-window MAE/MFE, exit-capture quality, post-exit price path
(did our stop/target hit AFTER we bailed?), and a validated-exit counterfactual
(same entry, but exited by the BACKTEST's rules: first-touch stop/target else
60-min close — no trailing). Plus Alpaca fill reconciliation: recorded monitor
prices vs actual fills vs account equity.

Read-only: market-data GETs + order GETs. No orders, no DB writes.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.alpaca import (AlpacaBarsRequest, AlpacaMarketDataClient,
                        AlpacaTradingClient, convert_alpaca_bars)

ET = ZoneInfo("America/New_York")
DB = "data/tradehack-dev.db"
EOD_ET = (15, 45)  # both engines force-close 15:45


def _px(v) -> float | None:
    """Parse a price that may be stored as float or AI-formatted string."""
    if v is None or isinstance(v, (int, float)):
        return v
    import re
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def fetch_day_bars(client, symbol: str, day: str) -> list[dict]:
    d = datetime.fromisoformat(day).date()
    start = datetime(d.year, d.month, d.day, 9, 25, tzinfo=ET).astimezone(timezone.utc)
    end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).astimezone(timezone.utc)
    resp = client.fetch_stock_bars(AlpacaBarsRequest(
        symbols=[symbol], timeframe="1Min", start=start, end=end, feed="sip"))
    return convert_alpaca_bars(resp.get(symbol, []))


def r_of(direction: str, entry: float, px: float, risk: float) -> float:
    return ((px - entry) if direction == "long" else (entry - px)) / risk


def first_touch(bars, direction, stop, target):
    """Scan bars: which level is touched first? Returns (what, bar)."""
    for b in bars:
        if direction == "long":
            hit_stop = stop is not None and b["low"] <= stop
            hit_tgt = target is not None and b["high"] >= target
        else:
            hit_stop = stop is not None and b["high"] >= stop
            hit_tgt = target is not None and b["low"] <= target
        if hit_stop and hit_tgt:
            return "both_same_bar", b
        if hit_stop:
            return "stop", b
        if hit_tgt:
            return "target", b
    return "neither", None


def ts_et(b) -> str:
    return datetime.fromtimestamp(b["time"], tz=timezone.utc).astimezone(ET).strftime("%H:%M")


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    trades = con.execute(
        "SELECT * FROM trade_records WHERE status='closed' ORDER BY opened_at").fetchall()
    mkt = AlpacaMarketDataClient()
    trd = AlpacaTradingClient()
    orders = trd.list_orders(status="all", limit=500)

    print("=" * 78)
    print(f"LIVE-TRADE LOSS POST-MORTEM — {len(trades)} closed trades (read-only)")
    print("=" * 78)

    fill_pnl_by_day: dict[str, float] = {}
    for t in trades:
        sym, direction = t["symbol"], t["direction"]
        entry, exitp = _px(t["entry_price"]), _px(t["exit_price"])
        stop, target = _px(t["stop_loss"]), _px(t["target"])
        risk = abs(entry - stop) if stop else None
        opened_utc = datetime.fromisoformat(t["opened_at"]).replace(tzinfo=timezone.utc)
        opened_et = opened_utc.astimezone(ET)
        day = opened_et.strftime("%Y-%m-%d")
        hold_min = (t["hold_duration_seconds"] or 0) // 60
        exit_et = opened_et + timedelta(seconds=t["hold_duration_seconds"] or 0)

        bars = fetch_day_bars(mkt, sym, day)
        hold = [b for b in bars if opened_utc.timestamp() <= b["time"] <= exit_et.timestamp()]
        post = [b for b in bars if b["time"] > exit_et.timestamp()
                and datetime.fromtimestamp(b["time"], tz=timezone.utc).astimezone(ET).hour * 60
                + datetime.fromtimestamp(b["time"], tz=timezone.utc).astimezone(ET).minute
                <= EOD_ET[0] * 60 + EOD_ET[1]]
        w60 = [b for b in bars if opened_utc.timestamp() <= b["time"]
               <= (opened_utc + timedelta(minutes=60)).timestamp()]

        print(f"\n── #{t['id']} {sym} {direction.upper()}  {day} {opened_et.strftime('%H:%M')} ET "
              f"— {t['setup']} (conf {t['confidence']})")
        print(f"   entry {entry}  exit {exitp} ({t['exit_reason']}, {hold_min}m)  "
              f"stop {stop}  target {target}  R {t['r_multiple']:+.2f}  P&L ${t['pnl']:+.2f}")

        if risk and hold:
            if direction == "long":
                mfe = max(r_of(direction, entry, b["high"], risk) for b in hold)
                mae = min(r_of(direction, entry, b["low"], risk) for b in hold)
                best = max(b["high"] for b in hold)
            else:
                mfe = max(r_of(direction, entry, b["low"], risk) for b in hold)
                mae = min(r_of(direction, entry, b["high"], risk) for b in hold)
                best = min(b["low"] for b in hold)
            print(f"   HOLD: MFE {mfe:+.2f}R  MAE {mae:+.2f}R  "
                  f"best px {best}  (exit captured {r_of(direction, entry, exitp, risk):+.2f}R)")

        if risk and post:
            what, b = first_touch(post, direction, stop, target)
            if direction == "long":
                post_best = max(x["high"] for x in post); post_worst = min(x["low"] for x in post)
            else:
                post_best = min(x["low"] for x in post); post_worst = max(x["high"] for x in post)
            eod_px = post[-1]["close"]
            print(f"   POST-EXIT→15:45: first touch = {what}"
                  + (f" at {ts_et(b)} ET" if b is not None else "")
                  + f"; best {post_best} ({r_of(direction, entry, post_best, risk):+.2f}R)"
                    f"  worst {post_worst} ({r_of(direction, entry, post_worst, risk):+.2f}R)"
                    f"  15:45 close {eod_px} ({r_of(direction, entry, eod_px, risk):+.2f}R)")

        # Counterfactual: SAME entry, validated exit engine (stop/target first-touch
        # in 60 min, else 60-min close). Uses the trade's own recorded levels —
        # an approximation of the backtest's ATR-stop, labeled as such.
        if risk and w60:
            what, b = first_touch(w60, direction, stop, target)
            if what == "stop":
                cf = (-1.0, f"stop at {ts_et(b)}")
            elif what == "target":
                cf = (r_of(direction, entry, target, risk), f"target at {ts_et(b)}")
            elif what == "both_same_bar":
                cf = (-1.0, f"both in same bar {ts_et(b)} (worst-case: stop)")
            else:
                cf = (r_of(direction, entry, w60[-1]["close"], risk), "60-min close (no touch)")
            print(f"   VALIDATED-EXIT counterfactual (no trail): {cf[0]:+.2f}R via {cf[1]}"
                  f"   [Δ vs live {cf[0] - t['r_multiple']:+.2f}R]")

        # Fill reconciliation — order-ID-linked when available (persisted DB
        # fills post-migration, or exact order-id lookup), else timestamp-
        # proximity matching (±3 min, side-aware). NEVER first/last-of-day
        # pairing: same-day repeat trades (MU/COIN 2026-06-11) misreport that.
        def _find_order(oid):
            return next((o for o in orders
                         if o.get("id") == oid and o.get("status") == "filled"), None)

        def _nearest(ts_utc, want_side):
            best, best_d = None, 181.0
            for o in orders:
                if (o.get("symbol") != sym or o.get("status") != "filled"
                        or o.get("side") != want_side):
                    continue
                ots = str(o.get("filled_at") or o.get("submitted_at") or "")
                try:
                    od = datetime.fromisoformat(ots.replace("Z", "+00:00"))
                except ValueError:
                    continue
                d = abs((od - ts_utc).total_seconds())
                if d < best_d:
                    best, best_d = o, d
            return best

        cols = t.keys()
        db_fe = t["entry_fill_price"] if "entry_fill_price" in cols else None
        db_fx = t["exit_fill_price"] if "exit_fill_price" in cols else None
        eo = _find_order(t["order_id"]) if t["order_id"] else None
        xo = (_find_order(t["exit_order_id"])
              if "exit_order_id" in cols and t["exit_order_id"] else None)
        side_in = "buy" if direction == "long" else "sell"
        side_out = "sell" if direction == "long" else "buy"
        exit_utc = opened_utc + timedelta(seconds=t["hold_duration_seconds"] or 0)
        if eo is None and db_fe is None:
            eo = _nearest(opened_utc, side_in)
        if xo is None and db_fx is None:
            xo = _nearest(exit_utc, side_out)
        fe = db_fe if db_fe is not None else (
            float(eo["filled_avg_price"]) if eo and eo.get("filled_avg_price") else None)
        fx = db_fx if db_fx is not None else (
            float(xo["filled_avg_price"]) if xo and xo.get("filled_avg_price") else None)
        if fe is not None and fx is not None:
            qty = t["qty"] or (int(float(eo["filled_qty"])) if eo else 0)
            fill_pnl = ((fx - fe) if direction == "long" else (fe - fx)) * qty
            fill_pnl_by_day[day] = fill_pnl_by_day.get(day, 0.0) + fill_pnl
            linked = "order-ID" if (db_fe is not None or db_fx is not None
                                    or t["order_id"]) else "time-matched"
            print(f"   FILLS [{linked}]: entry {fe} vs recorded {entry} (Δ{fe - entry:+.4f})  "
                  f"exit {fx} vs recorded {exitp} (Δ{fx - exitp:+.4f})  "
                  f"fill-true P&L ${fill_pnl:+.2f} vs recorded ${t['pnl']:+.2f} "
                  f"(Δ${fill_pnl - t['pnl']:+.2f})")
        else:
            print("   FILLS: unresolved (no order-ID link and ambiguous time match) — "
                  "skipped rather than misattributed")

    print("\n── fill-true P&L by day (vs account equity is ground truth) ──")
    for d, v in sorted(fill_pnl_by_day.items()):
        print(f"   {d}: ${v:+.2f}")

    # Simulated (blocked/below-floor) trades — the counterfactual ledger
    sims = con.execute("SELECT * FROM simulated_trades ORDER BY id").fetchall()
    print(f"\n── simulated_trades ({len(sims)} rows — blocked-signal counterfactuals) ──")
    for s in sims:
        risk = abs(s["entry_price"] - s["stop_loss"]) if s["stop_loss"] else None
        r = (((s["exit_price"] - s["entry_price"]) if s["direction"] == "long"
              else (s["entry_price"] - s["exit_price"])) / risk
             if (risk and s["exit_price"] is not None) else None)
        print(f"   {s['symbol']:<5} {s['direction']:<5} {s['setup'][:32]:<32} "
              f"status={s['status']:<9} entry={s['entry_price']} exit={s['exit_price']} "
              f"reason={s['exit_reason']} R={f'{r:+.2f}' if r is not None else 'n/a'}")


if __name__ == "__main__":
    raise SystemExit(main())
