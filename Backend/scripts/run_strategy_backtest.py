#!/usr/bin/env python3
"""
run_strategy_backtest.py — CLI wrapper around app.strategy_backtest.

Fetches 1-min bars for the given symbols/date range (cached under
Backend/data/bars_cache), replays the live PLAYBOOK against them,
and prints per-strategy + per-(strategy, hour) stats sorted by
expectancy. Optionally dumps every simulated trade to CSV.

Examples:
    # Use the .env training_universe, 6 months back to today:
    python -m scripts.run_strategy_backtest \\
        --start 2025-10-20 --end 2026-04-20

    # Specific symbols, save per-trade CSV:
    python -m scripts.run_strategy_backtest \\
        --symbols AAPL,TSLA,NVDA,AMD,META \\
        --start 2025-10-01 --end 2026-04-01 \\
        --csv ../data/backtest_trades.csv

    # Walk-forward validation (#7) — split the result into train/validate
    # and print rank-stability + expectancy-ratio checks:
    python -m scripts.run_strategy_backtest \\
        --start 2025-10-20 --end 2026-04-17 \\
        --train-end 2026-02-17        # 2/3 train, 1/3 validate

    # Full three-phase split (train / validate / paper):
    python -m scripts.run_strategy_backtest \\
        --start 2025-10-20 --end 2026-04-17 \\
        --train-end 2026-01-20 \\
        --val-end   2026-03-10

Run from inside Backend/ with the venv activated so the app package
resolves and Alpaca credentials load from .env / environment.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path


# Let `python scripts/run_strategy_backtest.py` work from inside Backend/
# by putting Backend/ on sys.path — then `from app...` resolves.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.strategy_backtest import (  # noqa: E402
    format_report,
    run_backtest,
    save_trades_csv,
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _default_universe() -> list[str]:
    """Read the default universe from settings, avoiding autotrade's live state."""
    from app.settings import settings
    return list(settings.training_universe)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TradeHack strategy backtest — find out which of the 12 "
                    "live PLAYBOOK strategies actually make money.",
    )
    parser.add_argument(
        "--symbols", type=str, default="",
        help="Comma-separated symbols. Default: settings.training_universe.",
    )
    parser.add_argument(
        "--start", type=_parse_date, required=True,
        help="Start date (inclusive), YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end", type=_parse_date, required=True,
        help="End date (inclusive), YYYY-MM-DD.",
    )
    parser.add_argument(
        "--csv", type=Path, default=None,
        help="Optional path to save per-trade CSV.",
    )
    parser.add_argument(
        "--train-end", type=_parse_date, default=None,
        help="Walk-forward (#7): exclusive end-date of training window, YYYY-MM-DD. "
             "Trades on/after this date are held out for validation.",
    )
    parser.add_argument(
        "--val-end", type=_parse_date, default=None,
        help="Walk-forward (#7): exclusive end-date of validate window, YYYY-MM-DD. "
             "Trades on/after this date go into the paper (unseen) bucket. "
             "Only used when --train-end is also given.",
    )
    parser.add_argument(
        "--playbook", type=str, default="live",
        choices=("live", "experimental", "context"),
        help=(
            "Which playbook to replay. "
            "'live' = strategies.PLAYBOOK (default — the v7.12.1 audit baseline). "
            "'experimental' = strategies.EXPERIMENTAL_PLAYBOOK (v7.15: ORB / VWAP / "
            "EMA pullback / momentum / failed-breakdown). "
            "'context' = strategies.CONTEXT_PLAYBOOK (suspended ICT predicates, "
            "for research)."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true", help="DEBUG-level logging.",
    )
    args = parser.parse_args()

    # --val-end only makes sense alongside --train-end (it's the SECOND cut point).
    if args.val_end and not args.train_end:
        print("ERROR: --val-end requires --train-end.", file=sys.stderr)
        return 2
    if args.train_end and args.val_end and args.val_end <= args.train_end:
        print("ERROR: --val-end must be strictly after --train-end.", file=sys.stderr)
        return 2
    if args.train_end and (args.train_end <= args.start or args.train_end >= args.end):
        print(
            f"ERROR: --train-end ({args.train_end}) must be strictly "
            f"between --start ({args.start}) and --end ({args.end}).",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = _default_universe()

    if not symbols:
        print("ERROR: no symbols to backtest — pass --symbols or set training_universe.",
              file=sys.stderr)
        return 2

    # Resolve --playbook to the actual list[Strategy] passed via
    # playbook_override. The default "live" path keeps the existing
    # behavior (run_backtest defaults playbook_override=None → uses
    # PLAYBOOK), so all pre-v7.15 invocations of this CLI are unaffected.
    playbook_override = None
    if args.playbook == "experimental":
        from app.strategies import EXPERIMENTAL_PLAYBOOK as _PB
        playbook_override = list(_PB)
    elif args.playbook == "context":
        from app.strategies import CONTEXT_PLAYBOOK as _PB
        playbook_override = list(_PB)

    print(f"Backtesting {len(symbols)} symbols from {args.start} to {args.end}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Playbook: {args.playbook}" + (
        f" ({len(playbook_override)} strategies)" if playbook_override else ""
    ))
    print()

    result = run_backtest(
        symbols=symbols, start=args.start, end=args.end,
        playbook_override=playbook_override,
    )

    print(format_report(result))

    if args.csv:
        save_trades_csv(result, args.csv)
        print(f"Per-trade CSV written to: {args.csv}")

    # Walk-forward (#7) — runs on the already-computed trade list, so it's
    # a side report appended after the main backtest table.
    if args.train_end:
        from app.walk_forward import split_result, format_walk_forward_report
        wf_report = split_result(
            result, train_end=args.train_end, val_end=args.val_end,
        )
        print()
        print(format_walk_forward_report(wf_report))
        # Non-zero exit when walk-forward fails — useful for CI / scripts
        # that want to block a bad playbook from being promoted.
        if not wf_report.passes:
            return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
