"""
calibration.py — Per-(strategy, symbol) confidence calibration.
===============================================================

Claude's raw confidence score is setup-pattern reasoning in the
abstract: "this looks like a 72% Oversold Bounce." But a given
stock's empirical response to a given setup can diverge sharply
from that abstract read — Oversold Bounce might be 68% accurate on
SOFI and 42% on TSLA based on a few hundred historical trades.

This module builds a lookup table of historical win rates indexed
by ``(strategy_name, symbol)`` from two sources:

  1. **Live trade history** (authoritative) — closed ``TradeRecord``
     rows in the DB, filtered to real executions with a known
     matched strategy and a realised outcome (pnl > 0 vs pnl <= 0).
  2. **Backtest CSVs** (seed data) — the per-trade CSVs written by
     ``app.strategy_backtest.save_trades_csv``. Used to seed the
     table before enough live trades accumulate. Live trades always
     take precedence over backtest trades for the same cell.

The lookup returns a *multiplier* to apply to Claude's raw
confidence score:

    adj = raw_conf × (hist_winrate / break_even_winrate)

clamped to ``[MIN_MULT, MAX_MULT]`` so a single anomalous cell
can't push a 60% signal to 95%. Below the sample-size floor
(``MIN_TRADES_PER_CELL``) we fall back to the strategy-wide win
rate; below that we fall back to 1.0 (no adjustment).

The multiplier is applied *after* Claude returns its raw score —
not baked into the prompt. We want Claude to reason on the setup
cleanly, then the calibration layer adjusts numerically using
empirical evidence the model can't see.

The lookup is cached in-process. ``refresh_calibration_table()``
rebuilds it; by default the autotrade scan loop refreshes on a
timer so newly-closed trades flow into the table within minutes.
"""

from __future__ import annotations

import csv
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import TradeRecord

logger = logging.getLogger("tradehack.calibration")

# ── Tuning knobs ─────────────────────────────────────────────────────────────

# Minimum closed trades in a (strategy, symbol) cell before we trust its
# individual win rate. Below this, fall back to the strategy-wide rate.
MIN_TRADES_PER_CELL = 20

# Strategy-wide fallback floor. Below this many total trades for the
# strategy (across all symbols), skip calibration entirely (mult=1.0) —
# the data is too thin to tell us anything useful.
MIN_TRADES_PER_STRATEGY = 50

# Clamp range on the final multiplier. Prevents a hot streak from
# shoving a 60% signal to 95% or a cold streak from drowning a
# real setup. ±30% is aggressive enough to matter, conservative
# enough not to let noise dominate.
MIN_MULT = 0.70
MAX_MULT = 1.30

# Break-even win rate assumed for the R:R used by the autotrader.
# Matches STOP_ATR_MULT=1.5 / TARGET_ATR_MULT=2.5 → reward:risk = 1.67.
# Breakeven = risk / (risk + reward) = 1 / (1 + 1.67) ≈ 0.375. Widened
# slightly to 0.40 to account for slippage + commissions eroding the
# theoretical breakeven. Tune this if the R:R template changes.
BREAK_EVEN_WINRATE = 0.40

# Cache TTL. Newly-closed trades flow into the table on the next
# refresh tick. 15 min is a good balance between staleness and cost.
_CACHE_TTL = timedelta(minutes=15)

# Where to look for backtest CSVs to seed the table. Files are only
# loaded if they exist — missing files are not an error, just skipped.
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_BACKTEST_CSV_CANDIDATES = [
    "backtest_trades_v3.csv",   # current, freshest
    "backtest_trades_v2.csv",   # previous-gen fallback
]


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class CellStats:
    """Aggregate stats for one (strategy, symbol) cell."""
    trades: int = 0
    wins: int = 0
    source: str = "backtest"   # "live" | "backtest" | "mixed"

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


@dataclass
class CalibrationTable:
    built_at: datetime
    cells: dict[tuple[str, str], CellStats] = field(default_factory=dict)
    by_strategy: dict[str, CellStats] = field(default_factory=dict)  # strategy-wide fallback

    def is_stale(self, ttl: timedelta = _CACHE_TTL) -> bool:
        return datetime.now(timezone.utc) - self.built_at > ttl


# ── Module-level cache ───────────────────────────────────────────────────────

_cache: CalibrationTable | None = None
# RLock (reentrant), NOT Lock — 2026-05-20 deadlock fix.
# ``_get_table()`` acquires this lock and, on a cache miss/stale, calls
# ``refresh_calibration_table()`` which acquires the SAME lock again.
# With a plain ``threading.Lock`` that second acquire blocks the calling
# thread forever (a thread waiting on a lock it already holds) — a
# self-deadlock. Because ``get_multiplier`` → ``_get_table`` runs
# synchronously inside the autotrade post-match loop ON the asyncio
# event loop, that deadlock froze the whole uvicorn process every time
# a Tier-1 match needed its calibration multiplier (4 hangs: MSFT
# 2026-05-18, SOXL + ORCL 2026-05-19, JPM 2026-05-19 16:04). RLock lets
# the same thread re-acquire (recursion-counted), which is exactly the
# intended ``_get_table → refresh_calibration_table`` re-entrant path.
_cache_lock = threading.RLock()


# ── Public API ───────────────────────────────────────────────────────────────

def get_multiplier(
    strategy: str,
    symbol: str,
    *,
    raw_confidence: float | None = None,
    table: CalibrationTable | None = None,
) -> tuple[float, dict]:
    """Return (multiplier, metadata) for this (strategy, symbol).

    The multiplier is intended to be applied to ``raw_confidence`` by the
    caller — we don't do the multiplication here so the caller retains
    control over clamping to [0, 1] after the product. ``raw_confidence``
    is accepted only to surface the projected adjusted score in the
    metadata for UI display.

    ``table`` — optional explicit table to look up against, instead of
    the shared module cache. This is the walk-forward (#7) hook: pass a
    train-only snapshot (from ``refresh_calibration_table(entry_date_max=…)``)
    to score validate trades with train-fit multipliers. Leaving it None
    uses the live cache, which is the normal autotrade path.

    Returns 1.0 with a "no_data" reason when there isn't enough evidence
    to calibrate in either direction. That's the conservative default —
    absence of evidence shouldn't push Claude's score around.
    """
    table = table if table is not None else _get_table()
    key = (strategy, symbol.upper())
    cell = table.cells.get(key)

    # Cell-level path (the ideal case)
    if cell and cell.trades >= MIN_TRADES_PER_CELL:
        mult = _winrate_to_multiplier(cell.win_rate)
        return mult, {
            "reason": "cell",
            "strategy": strategy,
            "symbol": symbol,
            "n": cell.trades,
            "win_rate": round(cell.win_rate, 3),
            "source": cell.source,
            "multiplier": round(mult, 3),
            "adjusted_confidence": (
                round(min(1.0, max(0.0, raw_confidence * mult)), 3)
                if raw_confidence is not None else None
            ),
        }

    # Strategy-wide fallback (we trust a strategy's aggregate behavior
    # even when we don't have enough same-symbol trades)
    strat = table.by_strategy.get(strategy)
    if strat and strat.trades >= MIN_TRADES_PER_STRATEGY:
        mult = _winrate_to_multiplier(strat.win_rate)
        return mult, {
            "reason": "strategy_fallback",
            "strategy": strategy,
            "symbol": symbol,
            "cell_n": cell.trades if cell else 0,
            "strategy_n": strat.trades,
            "strategy_win_rate": round(strat.win_rate, 3),
            "source": strat.source,
            "multiplier": round(mult, 3),
            "adjusted_confidence": (
                round(min(1.0, max(0.0, raw_confidence * mult)), 3)
                if raw_confidence is not None else None
            ),
        }

    # No data → neutral
    return 1.0, {
        "reason": "no_data",
        "strategy": strategy,
        "symbol": symbol,
        "cell_n": cell.trades if cell else 0,
        "strategy_n": strat.trades if strat else 0,
        "multiplier": 1.0,
        "adjusted_confidence": raw_confidence,
    }


def refresh_calibration_table(
    *,
    force: bool = False,
    entry_date_max: date | None = None,
) -> CalibrationTable:
    """(Re)build the calibration table from DB + backtest CSVs.

    Returns the fresh table. Safe to call from any thread — guards a
    single rebuild at a time. When ``force=False`` and the current
    table isn't stale, returns the cached one without rebuilding.

    ``entry_date_max`` is the walk-forward (#7) knob: when set, only
    trades whose entry date is **strictly before** this date contribute
    to the table. Use this to fit per-(strategy, symbol) multipliers on
    TRAIN data only, then apply them unchanged to the validate + paper
    phases. The memo calls this out explicitly — re-fitting on validate
    bars would be lookahead bias dressed up in statistics. The filtered
    build never writes to the shared module cache (it's an explicit
    one-off snapshot), so normal live-mode calibration is unaffected.
    """
    global _cache
    with _cache_lock:
        # Date-filtered builds are ALWAYS fresh (they're explicit
        # snapshots, often invoked with different cut points), and
        # they NEVER poison the shared cache — otherwise a walk-forward
        # analysis run would leave the live trader looking at a
        # train-only table until the TTL expired.
        if entry_date_max is not None:
            return _build_table(entry_date_max=entry_date_max)

        if not force and _cache is not None and not _cache.is_stale():
            return _cache
        _cache = _build_table()
        return _cache


def _get_table() -> CalibrationTable:
    """Return a non-stale table, building if needed."""
    with _cache_lock:
        if _cache is None or _cache.is_stale():
            return refresh_calibration_table(force=True)
        return _cache


# ── Table construction ───────────────────────────────────────────────────────

def _build_table(entry_date_max: date | None = None) -> CalibrationTable:
    """Assemble cells from backtest CSVs (seed) + live DB (authoritative).

    ``entry_date_max`` — when given, filters BOTH sources to trades with
    an entry date strictly before this date. Used for walk-forward (#7)
    train-only fitting.
    """
    cells: dict[tuple[str, str], CellStats] = {}

    # 1. Seed from backtest CSVs
    seeded = _load_from_backtest_csvs(cells, entry_date_max=entry_date_max)

    # 2. Overlay / merge live closed trades. Live data is authoritative —
    #    any live observations for a (strategy, symbol) cell replace the
    #    backtest tally rather than stacking on top, because the backtest
    #    and the live scan use the same predicates, so seeing a trade
    #    both places would double-count it.
    live = _load_from_trade_history(cells, entry_date_max=entry_date_max)

    # 3. Roll up per-strategy totals (used when a cell has too few trades)
    by_strategy: dict[str, CellStats] = {}
    for (strategy, _sym), s in cells.items():
        agg = by_strategy.setdefault(strategy, CellStats())
        agg.trades += s.trades
        agg.wins += s.wins
        # Strategy totals are "mixed" if any cell is mixed, else source of cells
        if agg.source != "mixed" and agg.source != s.source and agg.trades > s.trades:
            agg.source = "mixed"
        else:
            agg.source = s.source

    table = CalibrationTable(
        built_at=datetime.now(timezone.utc),
        cells=cells,
        by_strategy=by_strategy,
    )
    logger.info(
        "calibration_built cells=%d strategies=%d seeded_from_csv=%d live=%d",
        len(cells), len(by_strategy), seeded, live,
    )
    return table


def _load_from_backtest_csvs(
    cells: dict[tuple[str, str], CellStats],
    *,
    entry_date_max: date | None = None,
) -> int:
    """Populate ``cells`` from any backtest CSVs on disk. Returns rows loaded.

    ``entry_date_max`` — when given, only rows whose ``entry_ts_et`` date
    is strictly before this date are counted. Used for walk-forward
    train-only fitting.
    """
    loaded = 0
    for filename in _BACKTEST_CSV_CANDIDATES:
        path = _DATA_DIR / filename
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    strategy = row.get("strategy", "").strip()
                    symbol = row.get("symbol", "").strip().upper()
                    if not strategy or not symbol:
                        continue
                    try:
                        r_mult = float(row.get("r_multiple", "") or 0)
                    except ValueError:
                        continue
                    if entry_date_max is not None:
                        ts_str = row.get("entry_ts_et", "")
                        if not ts_str:
                            continue
                        try:
                            entry_d = datetime.fromisoformat(ts_str).date()
                        except ValueError:
                            continue
                        if entry_d >= entry_date_max:
                            continue
                    cell = cells.setdefault((strategy, symbol), CellStats(source="backtest"))
                    cell.trades += 1
                    if r_mult > 0:
                        cell.wins += 1
                    loaded += 1
        except Exception as exc:
            logger.warning("backtest_csv_load_failed path=%s err=%s", path, exc)
            continue
        # Only need the freshest CSV we find — stop after the first success
        logger.info(
            "calibration_seeded path=%s rows=%d entry_date_max=%s",
            path, loaded, entry_date_max,
        )
        break
    return loaded


def _load_from_trade_history(
    cells: dict[tuple[str, str], CellStats],
    *,
    entry_date_max: date | None = None,
) -> int:
    """Populate/overlay ``cells`` with closed live trades. Returns rows loaded.

    A trade is counted when:
      • it has a non-null ``matched_strategies`` tag (new column — old
        rows have NULL and are ignored here, they'll fall into the
        strategy-wide fallback via setup-name matching in a future pass),
      • it has a realised outcome (``closed_at`` set and ``pnl`` present),
      • ``matched_strategies`` is a comma-separated list of PLAYBOOK
        names — we credit each strategy on the list (Claude may have
        been shown multiple candidates, all of which were "in play" for
        this trade's outcome). Most trades only have one match.

    ``entry_date_max`` — when given, only trades whose ``opened_at`` date
    is strictly before this date are counted. Used for walk-forward
    train-only fitting.
    """
    db: Session = SessionLocal()
    loaded = 0
    seen_live_cells: set[tuple[str, str]] = set()
    try:
        stmt = select(TradeRecord).where(
            TradeRecord.closed_at.is_not(None),
            TradeRecord.pnl.is_not(None),
        )
        records = db.scalars(stmt).all()
        for r in records:
            tag = getattr(r, "matched_strategies", None)
            if not tag:
                continue
            if entry_date_max is not None and r.opened_at is not None:
                # opened_at is UTC; for the coarse train/validate cut we
                # compare on the UTC date. If a trade lives on the border
                # it's a rounding issue either way — the 20-trade floor
                # in the cell mask dominates this noise.
                if r.opened_at.date() >= entry_date_max:
                    continue
            strategies = [s.strip() for s in tag.split(",") if s.strip()]
            if not strategies:
                continue
            won = (r.pnl or 0) > 0
            for strategy in strategies:
                key = (strategy, r.symbol.upper())
                # First live observation for this cell blows away the
                # backtest seed — live is authoritative. After that,
                # accumulate additional live trades into the same cell.
                if key not in seen_live_cells:
                    cells[key] = CellStats(source="live")
                    seen_live_cells.add(key)
                cell = cells[key]
                cell.trades += 1
                if won:
                    cell.wins += 1
                loaded += 1
        if loaded:
            logger.info("calibration_live_overlay rows=%d cells=%d",
                        loaded, len(seen_live_cells))
    except Exception as exc:
        logger.warning("trade_history_load_failed err=%s", exc)
    finally:
        db.close()
    return loaded


# ── Math ─────────────────────────────────────────────────────────────────────

def _winrate_to_multiplier(win_rate: float) -> float:
    """Map observed win rate → confidence multiplier, clamped.

    Ratio of observed to break-even. At break-even the multiplier is
    exactly 1.0 (Claude's raw score passes through). Above break-even
    we scale up; below, we scale down. Clamped to [MIN_MULT, MAX_MULT].
    """
    if BREAK_EVEN_WINRATE <= 0:
        return 1.0
    raw = win_rate / BREAK_EVEN_WINRATE
    return max(MIN_MULT, min(MAX_MULT, raw))
