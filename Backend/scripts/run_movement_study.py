"""
Weekly Candle Movement Study — CLI driver (Phase 1, v7.16, 2026-05-07)
=============================================================================

Scans the last N trading days of 1-min bars across the active universe
(or a caller-supplied symbol list) for big-movement events PLUS matched
negative controls (failed breakouts + nothing-happened windows).
Persists everything to a single JSON file under
``Backend/data/movement_studies/movement_study_<YYYY-MM-DD>.json``.

Phase 1 is purely deterministic — NO AI calls. Phase 2 (when the
detection rules are validated) will add per-record AI descriptions
and clustering.

Usage
-----
From ``Backend/`` on the dev folder::

    # Default: 5 trading days, active universe
    env PYTHONPATH=. venv/bin/python -m scripts.run_movement_study

    # Custom universe + 7 days
    env PYTHONPATH=. venv/bin/python -m scripts.run_movement_study \\
        --symbols AAPL,NVDA,MU --days 7

    # Just count events without writing the JSON (useful while
    # iterating on detection thresholds)
    env PYTHONPATH=. venv/bin/python -m scripts.run_movement_study \\
        --symbols AAPL --dry-run

What gets reported
------------------
Per-symbol counts of winners / losers / nothing-happened windows,
plus the output JSON path. Summary printed to stdout; full
per-record detail goes to the JSON (the AI step in Phase 2 reads
the same JSON).

Pass criterion for Phase 1 (before Phase 2 ships)
-------------------------------------------------
* Every active-universe symbol returns ≥ 1 record across 5 days
  (otherwise our thresholds are too tight)
* Winner-to-loser ratio is roughly balanced after stratified sampling
  (otherwise the negative-control sampler is broken)
* Spot-checking a handful of winners shows they look like real
  expansion candles (not flag-day noise)

If those check out, Phase 2 (AI description) is safe to build.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.logging_config import configure_logging
from app.movement_study import run_study


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Weekly Candle Movement Study (Phase 1)")
    p.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated symbol list. Default: the active autotrade universe.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=5,
        help="Trading days to scan back from today (default: 5).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary but skip writing the JSON file.",
    )
    return p.parse_args()


def _resolve_universe(symbols_arg: str | None) -> list[str]:
    if symbols_arg:
        syms = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
        return syms
    # Fall through to the active universe.
    from app.autotrade import get_active_universe
    return get_active_universe()


def main() -> int:
    configure_logging()
    args = _parse_args()

    symbols = _resolve_universe(args.symbols)
    if not symbols:
        print("ERROR: empty universe — pass --symbols or apply a morning universe first.", file=sys.stderr)
        return 1

    print(f"Scanning {len(symbols)} symbols × {args.days} days...")
    print(f"Symbols: {', '.join(symbols)}")
    print()

    result = run_study(symbols, days_back=args.days)

    print("Per-symbol counts:")
    print(f"  {'symbol':<8s} {'winners':>8s} {'losers':>8s} {'nothing':>8s}")
    print("  " + "-" * 36)
    for sym, counts in sorted(result["per_symbol"].items()):
        print(f"  {sym:<8s} {counts['winners']:>8d} {counts['losers']:>8d} {counts['nothing']:>8d}")
    print()
    print(f"Total records: {result['n_records']}")
    print(f"  winners:  {result['n_winners']}")
    print(f"  losers:   {result['n_losers']}")
    print(f"  nothing:  {result['n_nothing']}")
    print()

    if args.dry_run:
        print("DRY RUN — no JSON written.")
    else:
        print(f"Wrote JSON to: {result['out_path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
