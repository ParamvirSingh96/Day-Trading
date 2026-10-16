#!/usr/bin/env python3
"""build_edge_lookup.py — generate the edge-lookup JSON consumed by
``app/trade_quality.py::score()``.

For each strategy in the input trades CSV we compute:
  * Aggregate raw + post-friction AvgR, n, win-rate
  * Per-symbol slice: same metrics for every symbol with n >= 1 on this
    strategy (filtering n >= 5 happens at score time).
  * Per-hour slice (RTH hour-of-day in ET, 0–23): same metrics.

Default friction: 0.10R per trade (the v7.10.5 audit's "realistic"
tier — typical day on liquid US equities, bid-ask + 1-2 cent
slippage entry/exit + commission round trip).

Default input is the v7.10 18-month replay, default output is
``data/edge_lookup_v710.json``. Both are configurable.

Usage:
    venv/bin/python -m scripts.build_edge_lookup
    venv/bin/python -m scripts.build_edge_lookup \\
        --csv data/research/playbook_baseline_v710_2024-10-27_2026-04-24_trades.csv \\
        --out data/edge_lookup_v710.json \\
        --friction 0.10
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

_BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TRADES_CSV = (
    _BACKEND_DIR
    / "data"
    / "research"
    / "playbook_baseline_v710_2024-10-27_2026-04-24_trades.csv"
)
DEFAULT_OUT_JSON = _BACKEND_DIR / "data" / "edge_lookup_v710.json"
DEFAULT_FRICTION = 0.10


def _stats(group: pd.DataFrame, friction: float) -> dict:
    n = len(group)
    if n == 0:
        return {"n": 0, "raw_avg_r": 0.0, "friction_avg_r": 0.0, "win_rate": 0.0}
    raw_avg = float(group["r_multiple"].mean())
    return {
        "n": int(n),
        "raw_avg_r": round(raw_avg, 4),
        "friction_avg_r": round(raw_avg - friction, 4),
        "win_rate": round(float((group["r_multiple"] > 0).mean()), 4),
    }


def _hour_of_day_et(ts: pd.Series) -> pd.Series:
    """Extract ET hour-of-day from ``entry_ts_et`` strings.

    The CSV column already includes the ET offset (-04:00 / -05:00),
    so utc=True parsing yields a timezone-aware UTC Timestamp; we
    convert to America/New_York to read the local hour. Trades during
    DST transition still bucket to the right local hour.
    """
    utc = pd.to_datetime(ts, utc=True)
    return utc.dt.tz_convert("America/New_York").dt.hour


def build_edge_lookup(csv_path: Path, friction: float) -> dict:
    df = pd.read_csv(csv_path)
    df["et_hour"] = _hour_of_day_et(df["entry_ts_et"])

    out: dict = {
        "_meta": {
            "source_csv": str(csv_path.relative_to(_BACKEND_DIR)),
            "friction_per_trade_r": friction,
            "total_trades": int(len(df)),
            "window_start": str(pd.to_datetime(df["entry_ts_et"], utc=True).min()),
            "window_end":   str(pd.to_datetime(df["entry_ts_et"], utc=True).max()),
            "schema_version": 1,
        }
    }

    for strategy, sub in df.groupby("strategy"):
        agg = _stats(sub, friction)
        agg["by_symbol"] = {
            sym: _stats(g, friction) for sym, g in sub.groupby("symbol")
        }
        agg["by_hour"] = {
            str(h): _stats(g, friction) for h, g in sub.groupby("et_hour")
        }
        out[strategy] = agg

    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv",      type=Path, default=DEFAULT_TRADES_CSV)
    p.add_argument("--out",      type=Path, default=DEFAULT_OUT_JSON)
    p.add_argument("--friction", type=float, default=DEFAULT_FRICTION,
                   help="Per-trade R hit applied to raw_avg_r to get friction_avg_r.")
    args = p.parse_args()

    if not args.csv.exists():
        print(f"ERROR: trades CSV not found: {args.csv}")
        return 2

    print(f"Building edge lookup from {args.csv} with friction={args.friction:.2f}R …")
    lookup = build_edge_lookup(args.csv, args.friction)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        json.dump(lookup, f, indent=2, sort_keys=True)
    print(f"Wrote {args.out}")

    # Brief summary
    n_strats = len([k for k in lookup if not k.startswith("_")])
    print()
    print(f"Strategies: {n_strats}")
    print(f"{'Strategy':<35}{'n':>5}{'RawAvgR':>10}{'FricAvgR':>10}{'WR%':>7}")
    print("-" * 67)
    for strategy in sorted(k for k in lookup if not k.startswith("_")):
        s = lookup[strategy]
        print(
            f"{strategy:<35}"
            f"{s['n']:>5}"
            f"{s['raw_avg_r']:>+10.4f}"
            f"{s['friction_avg_r']:>+10.4f}"
            f"{s['win_rate']*100:>6.1f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
