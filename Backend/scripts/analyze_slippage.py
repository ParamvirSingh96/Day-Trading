#!/usr/bin/env python3
"""analyze_slippage.py — READ-ONLY slippage audit from fill-persisted trades.

Since FILL_PERSIST_ENABLED=true (2026-06-24), every clean 1:1 close stores
broker-true entry/exit fills next to the monitor prices. This script measures
the difference — the real execution cost/benefit of market-order exits on the
0.5% trailing stop — per trade and in aggregate.

Sign convention: positive = COST (fill worse than monitor), negative =
FAVORABLE (fill better than monitor). Identity check per trade:
    entry_cost + exit_cost == monitor_pnl − fill_pnl   (to the cent)

This is the evidence base for the "are limit-order exits worth it?" decision.
NO conclusions until the sample is meaningful (market orders can slip either
way — do not extrapolate from a handful of trades). Touches nothing live.

Usage:  ./venv/bin/python scripts/analyze_slippage.py [--since 2026-06-24]
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from app.db import SessionLocal
from app.models import TradeRecord

FILL_ENABLE_DATE = date(2026, 6, 24)   # flag verified live this day


def leg_costs(r: TradeRecord) -> tuple[float, float]:
    """(entry_cost, exit_cost) in dollars; + = worse than monitor price."""
    q = float(r.qty or 0)
    if r.direction == "short":
        # entry = SELL (receiving less than monitor = cost);
        # exit  = BUY  (paying more than monitor = cost).
        return ((r.entry_price - r.entry_fill_price) * q,
                (r.exit_fill_price - r.exit_price) * q)
    # long: entry = BUY, exit = SELL.
    return ((r.entry_fill_price - r.entry_price) * q,
            (r.exit_price - r.exit_fill_price) * q)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    default=FILL_ENABLE_DATE)
    a = ap.parse_args()

    db = SessionLocal()
    try:
        rows = (db.query(TradeRecord)
                  .filter(TradeRecord.status == "closed")
                  .order_by(TradeRecord.opened_at)
                  .all())
    finally:
        db.close()
    rows = [r for r in rows
            if r.opened_at is not None and r.opened_at.date() >= a.since]

    filled = [r for r in rows
              if r.entry_fill_price is not None and r.exit_fill_price is not None
              and r.entry_price is not None and r.exit_price is not None]
    print(f"closed trades since {a.since}: {len(rows)} | with both fills: {len(filled)} "
          f"| unresolved/monitor-basis: {len(rows) - len(filled)}")
    if not filled:
        print("no fill-persisted trades yet — nothing to measure.")
        return 0

    print(f"\n{'date':10} {'sym':5} {'dir':5} {'exit_reason':13} {'qty':>5} "
          f"{'entry slip':>11} {'exit slip':>11} {'total':>9} {'in R':>7}")
    tot_entry = tot_exit = 0.0
    r_costs: list[float] = []
    for r in filled:
        ec, xc = leg_costs(r)
        tot = ec + xc
        # cross-check vs pnl delta (monitor-implied − fill-derived)
        q = float(r.qty or 0)
        mon_pnl = ((r.exit_price - r.entry_price) if r.direction != "short"
                   else (r.entry_price - r.exit_price)) * q
        drift = abs((mon_pnl - (r.pnl or 0.0)) - tot)
        flag = "" if drift < 0.02 else f"  [check: identity off by ${drift:.2f}]"
        # $ per R from the record itself (guard near-zero R)
        in_r = ""
        if r.pnl and r.r_multiple and abs(r.r_multiple) > 1e-6:
            dollars_per_r = abs(r.pnl / r.r_multiple)
            if dollars_per_r > 1e-9:
                rc = tot / dollars_per_r
                r_costs.append(rc)
                in_r = f"{rc:+.3f}R"
        tot_entry += ec
        tot_exit += xc
        print(f"{str(r.opened_at)[:10]:10} {r.symbol:5} {r.direction:5} "
              f"{(r.exit_reason or '?'):13} {int(q):>5} "
              f"${ec:>+9.2f} ${xc:>+9.2f} ${tot:>+7.2f} {in_r:>7}{flag}")

    n = len(filled)
    grand = tot_entry + tot_exit
    print("-" * 78)
    print(f"{'TOTAL':>37} ${tot_entry:>+9.2f} ${tot_exit:>+9.2f} ${grand:>+7.2f}")
    print(f"\nper-trade mean: ${grand / n:+.2f}"
          + (f"  ({sum(r_costs) / len(r_costs):+.3f}R)" if r_costs else ""))
    print(f"positive = cost vs monitor price, negative = favorable fill.")
    if n < 10:
        print(f"n={n}: TOO SMALL for any conclusion — accumulate before acting "
              f"(market-order slippage cuts both ways).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
