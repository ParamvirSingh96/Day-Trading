#!/usr/bin/env python3
"""dry_stretch_audit.py — is the current live drought "normal"?

Builds the distribution of trade frequency + dry streaks over the validated
v7.12.1 6-month baseline, so we can judge a real-time quiet stretch against
history instead of vibes. Computes both:
  • FULL baseline (every backtest trade)
  • LIVE-reproduced subset (post-20d-fix parity flags — what live actually fires)

Session calendar comes from SPY daily bars (one bar = one trading session),
so zero-trade streaks are measured in REAL trading sessions, not calendar days.
"""
from __future__ import annotations
import csv, json, statistics
from collections import defaultdict
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

from app.alpaca import AlpacaBarsRequest, AlpacaMarketDataClient, convert_alpaca_bars

ET = ZoneInfo("America/New_York")
START, END = date(2025, 10, 27), date(2026, 4, 24)
TRADES = "data/research/parity_6mo_20260604_trades.csv"
PARITY_FIX = "data/research/parity_diag_6mo_20260604_fix20d.jsonl"


def _d(s): return datetime.fromisoformat(s)


def main():
    # 1. trading-session calendar from SPY daily bars
    c = AlpacaMarketDataClient()
    resp = c.fetch_stock_bars(AlpacaBarsRequest(
        symbols=["SPY"], timeframe="1Day",
        start=datetime(2025, 10, 27, tzinfo=ET).astimezone(timezone.utc),
        end=datetime(2026, 4, 26, tzinfo=ET).astimezone(timezone.utc)))
    sessions = sorted({
        datetime.fromtimestamp(b["time"], tz=timezone.utc).astimezone(ET).date()
        for b in convert_alpaca_bars(resp.get("SPY", []))
    })
    sessions = [s for s in sessions if START <= s <= END]

    # 2. trades — full + live-reproduced (post-fix)
    trades = list(csv.DictReader(open(TRADES)))
    parity = [json.loads(l) for l in open(PARITY_FIX)]
    pr_by = defaultdict(list)
    for r in parity:
        pr_by[(r["symbol"], r["strategy"])].append(r)

    def reproduced(t):
        et = _d(t["entry_ts_et"])
        cand = [r for r in pr_by.get((t["symbol"], t["strategy"]), [])
                if _d(r["bar_ts_et"]) <= et and (et - _d(r["bar_ts_et"])).total_seconds() <= 600]
        return max(cand, key=lambda r: _d(r["bar_ts_et"]))["live_matched"] if cand else False

    full = [(_d(t["entry_ts_et"]).date(), float(t["r_multiple"])) for t in trades]
    live = [(_d(t["entry_ts_et"]).date(), float(t["r_multiple"])) for t in trades if reproduced(t)]

    def audit(tl, label):
        by_sess = defaultdict(list)
        for d, r in tl:
            by_sess[d].append(r)
        sess_r = [sum(by_sess.get(s, [])) for s in sessions]
        traded = [1 if by_sess.get(s) else 0 for s in sessions]
        # dry streaks (consecutive zero-trade sessions)
        streaks, cur = [], 0
        for t in traded:
            if t:
                if cur: streaks.append(cur)
                cur = 0
            else: cur += 1
        if cur: streaks.append(cur)
        # trades per ISO-week (only weeks that contain >=1 session)
        wk_sessions = defaultdict(int); wk_trades = defaultdict(int)
        for s in sessions: wk_sessions[s.isocalendar()[:2]] += 1
        for d, _ in tl: wk_trades[d.isocalendar()[:2]] += 1
        weeks = sorted(wk_sessions)
        wk = [wk_trades.get(w, 0) for w in weeks]
        def worst_roll(k):
            return min((sum(sess_r[i:i+k]) for i in range(len(sess_r)-k+1)), default=0.0)
        n = len(tl)
        print(f"\n── {label}: {n} trades over {len(sessions)} sessions / {len(weeks)} weeks ──")
        print(f"  trades/week: mean {n/len(weeks):.2f}  median {statistics.median(wk):.0f}  "
              f"max {max(wk)}  |  zero-trade weeks: {wk.count(0)}/{len(weeks)} ({100*wk.count(0)/len(weeks):.0f}%)")
        print(f"  zero-trade SESSION streaks: longest {max(streaks)}  "
              f"median {statistics.median(streaks):.0f}  "
              f"#streaks>={6}: {sum(1 for x in streaks if x>=6)}  (count of dry runs >= current ~6-session drought)")
        print(f"  longest streaks (top 6): {sorted(streaks, reverse=True)[:6]}")
        print(f"  worst rolling 5-session R: {worst_roll(5):+.2f}   worst rolling 10-session R: {worst_roll(10):+.2f}")
        print(f"  total R {sum(r for _,r in tl):+.2f}  (post-friction {sum(r for _,r in tl)-0.10*n:+.2f})")

    print("=" * 74)
    print("DRY-STRETCH DISTRIBUTION AUDIT — v7.12.1 baseline (2025-10-27 → 2026-04-24)")
    print("=" * 74)
    audit(full, "FULL baseline")
    audit(live, "LIVE-reproduced (post-20d-fix)")
    print("\n" + "=" * 74)
    print("CURRENT LIVE DROUGHT for comparison: ~6 consecutive zero-trade sessions")
    print("(1 trade — ADBE 05-28, a loss — across the last ~7 live sessions).")
    print("Read: if the baseline routinely produced >=6-session dry streaks AND")
    print("multi-week zero-trade runs, the current quiet is NORMAL tail behavior.")
    print("=" * 74)


if __name__ == "__main__":
    raise SystemExit(main())
