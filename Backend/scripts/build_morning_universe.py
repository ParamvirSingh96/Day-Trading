"""
Morning Universe Builder — CLI driver (v7.13 Phase 1, 2026-05-01)
=============================================================================
Runs the morning screener at the operator's command (or a cron at
~08:30 ET each trading day) and writes a JSON snapshot to
``Backend/data/morning_universe_<date>.json``. The frontend dashboard
reads that snapshot to render the "Today's universe" panel.

Usage
-----
From ``Backend/``::

    venv/bin/python -m scripts.build_morning_universe                     # today, defaults
    venv/bin/python -m scripts.build_morning_universe --date 2026-05-01    # specific date
    venv/bin/python -m scripts.build_morning_universe --n-dynamic 15      # more candidates
    venv/bin/python -m scripts.build_morning_universe \
        --candidate-pool META,AVGO,COIN --n-dynamic 5                     # focused pool

Output
------
* ``data/morning_universe_<date>.json`` — full snapshot (base + dynamic
  + provenance config). Stable schema; the 30-day audit reads this.
* stdout — short human summary (top dynamic candidates with reason
  tags) so an operator running this manually sees what shipped.

Exit codes
----------
* 0 — snapshot written successfully (even with zero dynamic candidates;
  that just means nothing in the pool met the gates today).
* 1 — Alpaca credentials missing or invalid.
* 2 — Internal error (bubbled-up exception logged).
=============================================================================
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Make Backend/ importable when running as ``-m scripts.build_morning_universe``.
_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

from app.alpaca import AlpacaMarketDataClient
from app.screener_morning import (
    DEFAULT_CANDIDATE_POOL,
    DEFAULT_MIN_PM_DOLLAR_VOL,
    DEFAULT_N_DYNAMIC,
    MorningCandidate,
    ScreenerConfig,
    build_morning_universe,
    write_snapshot_json,
)
from app.settings import settings


_ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet HTTP layer — we do our own structured progress logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("morning_builder")


def _print_summary(candidates: list[MorningCandidate], cfg: ScreenerConfig) -> None:
    """Print a compact human-readable summary to stdout.

    Layout::

        Morning universe — 2026-05-01
        Base (always-on, audited):  AAPL NVDA AMD ... (23 symbols)
        Trading universe (base + top N dynamic):
            1. META  score=2.31  gap +4.2%, earnings, XLK +0.8%
            ...
        Watchlist (informational, not traded):
            11. SHOP  score=0.71  gap +1.0%, pm $35M
            ...
    """
    base = [c for c in candidates if c.source == "base"]
    dyn  = [c for c in candidates if c.source == "morning_dynamic"]
    wl   = [c for c in candidates if c.source == "watchlist_only"]

    print(f"\nMorning universe — {datetime.now(_ET).date().isoformat()}")
    print(f"Feed:  {cfg.feed}")
    print(f"Base (always-on, audited):  {' '.join(c.symbol for c in base)}  ({len(base)} symbols)")
    print(f"Trading universe — top {cfg.n_dynamic} dynamic (from a pool of {cfg.candidate_pool_size}):")
    if not dyn:
        print("    (none — no candidate met the liquidity / signal gates today)")
    else:
        for c in dyn:
            tags = ", ".join(c.reason_tags) if c.reason_tags else "(no tags)"
            print(f"    {c.rank:>2}. {c.symbol:<6}  score={c.score:.3f}  {tags}")
    if wl:
        print(f"Watchlist — informational, NOT in trading universe ({len(wl)} more):")
        for c in wl[:10]:  # cap printed list at 10 for readability
            tags = ", ".join(c.reason_tags) if c.reason_tags else "(no tags)"
            print(f"    {c.rank:>2}. {c.symbol:<6}  score={c.score:.3f}  {tags}")
        if len(wl) > 10:
            print(f"    ... +{len(wl) - 10} more (see JSON snapshot)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build today's Morning Universe snapshot.",
    )
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="ET date to build the universe for. Default: today (ET).",
    )
    parser.add_argument(
        "--n-dynamic", type=int, default=DEFAULT_N_DYNAMIC,
        help=f"Max morning_dynamic candidates beyond the base. Default: {DEFAULT_N_DYNAMIC}.",
    )
    parser.add_argument(
        "--min-pm-dollar-vol", type=float, default=DEFAULT_MIN_PM_DOLLAR_VOL,
        help=(
            "Minimum premarket dollar volume to consider a candidate. "
            f"Default: ${DEFAULT_MIN_PM_DOLLAR_VOL:,.0f}."
        ),
    )
    parser.add_argument(
        "--candidate-pool", type=str, default=None,
        help=(
            "Comma-separated override for the pool of candidate symbols. "
            "Default: the curated DEFAULT_CANDIDATE_POOL in screener_morning.py "
            f"({len(DEFAULT_CANDIDATE_POOL)} liquid US large-caps + sector/index ETFs)."
        ),
    )
    parser.add_argument(
        "--base-symbols", type=str, default=None,
        help=(
            "Comma-separated override for the always-on base. Default: "
            "TRAINING_UNIVERSE from .env (the v7.12.1 audited 23-symbol set)."
        ),
    )
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path("data"),
        help="Where to write the JSON snapshot. Default: data/",
    )
    args = parser.parse_args()

    today_et = args.date or datetime.now(_ET).date()
    base_symbols = (
        [s.strip().upper() for s in args.base_symbols.split(",") if s.strip()]
        if args.base_symbols
        else list(settings.training_universe)
    )
    candidate_pool = (
        [s.strip().upper() for s in args.candidate_pool.split(",") if s.strip()]
        if args.candidate_pool
        else list(DEFAULT_CANDIDATE_POOL)
    )
    cfg = ScreenerConfig(
        n_dynamic=args.n_dynamic,
        min_pm_dollar_vol=args.min_pm_dollar_vol,
    )

    logger.info(
        "morning_builder_start date=%s base=%d pool=%d n_dynamic=%d feed=%s",
        today_et, len(base_symbols), len(candidate_pool), cfg.n_dynamic,
        settings.alpaca_data_feed,
    )

    try:
        client = AlpacaMarketDataClient()
    except ValueError as exc:
        # Missing/invalid Alpaca credentials — give a clean error
        # rather than letting a RuntimeError propagate.
        logger.error("morning_builder_credentials_failed err=%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        candidates, cfg_used = build_morning_universe(
            today_et,
            base_symbols=base_symbols,
            candidate_pool=candidate_pool,
            config=cfg,
            market_data_client=client,
        )
        out_path = write_snapshot_json(today_et, candidates, cfg_used, args.out_dir)
        _print_summary(candidates, cfg_used)
        print(f"Snapshot:  {out_path}")
        return 0
    except Exception:
        logger.exception("morning_builder_failed")
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
