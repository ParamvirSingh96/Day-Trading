"""
Experimental scanner (v7.15, 2026-05-18)
========================================

Second scanner that runs alongside :func:`app.scanner.scan_ict_symbols`,
reading the SAME ``bars_by_symbol`` dict the ICT scan reads — no
duplicate Alpaca fetch. Iterates :data:`app.strategies.EXPERIMENTAL_PLAYBOOK`
(ORB Long, VWAP Reclaim Long, EMA Pullback Long, Momentum Breakout Long,
Failed Breakdown Long) and returns the same hit-dict shape the ICT
scanner returns, with one new field::

    "scanner_id": "experimental_v1"

So every downstream consumer (autotrade pipeline, ``save_trade_opened``,
EOD email aggregation) can filter ICT vs experimental without a lookup.

Why a separate module instead of a flag on ``scan_ict_symbols``?

* Audit isolation. The v7.12.1 ICT baseline (+42.72R post-friction
  over 6 months) was measured against the existing predicate set;
  the experimental scanner's hits must NEVER pollute that audit lane.
  A separate module makes "is this from the ICT scanner?" a one-line
  ``isinstance``/import check, not a "decode the family field" gate.

* Different telemetry. The ICT scan writes to
  ``~/.tradehack/scan_stats/<date>.jsonl``; experimental writes to
  ``~/.tradehack/scan_stats_experimental/<date>.jsonl``.  Two folders
  means two read paths in the audit script — easier to keep cohorts
  clean than to dedupe one shared file.

* Different guardrails. ICT has no daily cap, no risk multiplier,
  no kill switch beyond ``state.running``. Experimental has all four
  (see ``_scan_and_trade`` in ``app/autotrade.py``). Encapsulating
  the experimental playbook in its own module makes the guardrail
  edges visible at the boundary instead of buried in a giant scanner.

The scanner does NOT compute any new indicators. ``compute_indicators``
already surfaces every field the 5 experimental predicates need
(see ``EXPERIMENTAL_PLAYBOOK`` docstring for the field inventory).
A ``recent_bars`` reference is threaded into ``ind`` so predicates
that need bar windows (VWAP reclaim's prev-bar lookup, EMA pullback's
5-bar low-touch scan, Failed Breakdown's 10-bar break-lookback) can
read them without a separate parameter.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.scanner_experimental")
_ET = ZoneInfo("America/New_York")


def _latest_bar_et(bars_by_symbol: dict[str, list[dict]]) -> str | None:
    latest_ts = max(
        (
            float(bars[-1]["time"])
            for bars in bars_by_symbol.values()
            if bars and bars[-1].get("time") is not None
        ),
        default=None,
    )
    if latest_ts is None:
        return None
    return datetime.fromtimestamp(latest_ts, tz=_ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def _join_symbols(symbols: list[str]) -> str:
    return ",".join(symbols) if symbols else "-"


def scan_experimental_symbols(
    bars_by_symbol: dict[str, list[dict]],
    daily_ctx_by_symbol: dict[str, dict] | None = None,
) -> list[dict]:
    """Run :data:`EXPERIMENTAL_PLAYBOOK` against ``bars_by_symbol``.

    Mirrors :func:`app.scanner.scan_ict_symbols` 1:1 (same bar-count
    floor, same per-symbol thread-pool, same hit-dict shape) so the
    autotrade pipeline can treat ICT and experimental hits the same
    way once they're tagged with ``scanner_id``.

    Returns a list of hit dicts (only symbols with ≥ 1 matching
    experimental strategy)::

        [
          {
            "symbol": "AAPL",
            "current_price": 189.42,
            "change_pct": 0.85,
            "scanner_id": "experimental_v1",
            "matched_strategies": [
                {"name": "ORB Long", "direction": "long", "family": "classic"},
                ...
            ],
            "indicators_snapshot": { ... },
          },
          ...
        ]

    Failure isolation: per-symbol exceptions are logged and skipped,
    matching ``scan_ict_symbols``'s behavior. A broken predicate
    kills one cell, never the whole cycle.
    """
    # Local imports avoid a circular at module load (the analyzer +
    # indicator + ict modules all transitively import the scanner).
    from .analyzer import compute_indicators
    from .ict import compute_ict_features
    from .strategies import EXPERIMENTAL_PLAYBOOK, experimental_screen

    daily_ctx_by_symbol = daily_ctx_by_symbol or {}

    def _process(symbol: str, bars: list[dict]) -> dict | None:
        # Same 15-bar floor as the ICT scanner (see scanner.py for the
        # rationale — 15 bars is the minimum where every indicator
        # the experimental predicates read has a sane window).
        if len(bars) < 15:
            return None
        try:
            ctx = daily_ctx_by_symbol.get(symbol) or {}
            ind = compute_indicators(
                bars,
                prev_close=ctx.get("prev_close"),
                prev_high=ctx.get("prev_high"),
                prev_low=ctx.get("prev_low"),
                avg_daily_volume=ctx.get("avg_daily_volume"),
            )
            # ICT feature layer — the experimental predicates don't
            # consume ICT zones, but we still compute them so that
            # if a future experimental rule adds an ICT confluence
            # filter the data is already in ``ind``. Failure is
            # isolated: a broken ICT feature module never breaks the
            # experimental scan.
            try:
                ind.update(compute_ict_features(bars, ctx))
            except Exception:
                logger.exception("ict_features_failed symbol=%s", symbol)

            ind["symbol"] = symbol
            # Thread the bar window into ``ind`` so predicates that
            # need recent bars (VWAP reclaim's prev_close, EMA
            # pullback's 5-bar low touch, Failed Breakdown's 10-bar
            # window) can read them without a separate parameter.
            # ``compute_indicators`` doesn't surface raw bars by
            # default, so we add them here as a scanner-local
            # affordance rather than mutating the analyzer's contract.
            ind["recent_bars"] = bars

            matched = experimental_screen(ind)
            if not matched:
                return None

            return {
                "symbol": symbol,
                "current_price": ind["current_price"],
                "change_pct": ind["change_pct"],
                # v7.15 — every experimental hit carries this so the
                # autotrade pipeline can route per-scanner without
                # any "is this from the experimental scanner?" guess.
                "scanner_id": "experimental_v1",
                "matched_strategies": [
                    {
                        "name": s.name,
                        "direction": s.direction,
                        "family": s.family,
                    }
                    for s in matched
                ],
                # No "forming_setups" on the experimental scanner.
                # Partial-state visibility is an ICT-specific UX
                # (see scanner.py v7.12 comment) — experimental
                # strategies don't have ``_partial_check`` callables
                # wired, and the v7.15 plan explicitly defers any
                # experimental "Watching" panel work to a later
                # iteration. Returning an empty list keeps the hit
                # shape parallel to ICT for downstream code.
                "forming_setups": [],
                "indicators_snapshot": {
                    "rsi":      ind.get("rsi"),
                    "vwap":     ind.get("vwap"),
                    "atr":      ind.get("atr"),
                    "ema9":     ind.get("ema9"),
                    "ema20":    ind.get("ema20"),
                    "macd":     ind.get("macd"),
                    "vol_ratio": ind.get("vol_ratio"),
                    "gap_pct":  ind.get("gap_pct"),
                    "session_high": ind.get("session_high"),
                    "opening_range": ind.get("opening_range"),
                    "prev_low": ind.get("prev_low"),
                },
            }
        except Exception:
            logger.exception("experimental_scan_error symbol=%s", symbol)
            return None

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_process, sym, bars): sym
            for sym, bars in bars_by_symbol.items()
        }
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    results.sort(key=lambda r: len(r["matched_strategies"]), reverse=True)
    matched_symbols = [r["symbol"] for r in results if r.get("matched_strategies")]
    matched_setups = sum(len(r.get("matched_strategies") or []) for r in results)
    logger.info(
        "experimental_scan_complete symbols=%d hits=%d matched_symbols=%d "
        "matched_setups=%d latest_bar_et=%s matched=%s",
        len(bars_by_symbol),
        len(results),
        len(matched_symbols),
        matched_setups,
        _latest_bar_et(bars_by_symbol) or "-",
        _join_symbols(matched_symbols),
    )
    return results
