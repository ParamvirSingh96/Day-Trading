from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.scanner")
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

# ── Live Strategy Scanner (Tier 1 — fast, no AI) ─────────────────────────────

def scan_ict_symbols(
    bars_by_symbol: dict[str, list[dict]],
    daily_ctx_by_symbol: dict[str, dict] | None = None,
) -> list[dict]:
    """
    Run the ICT strategy playbook pre-screen across multiple symbols.

    v7.14 (2026-05-18) — renamed from ``scan_live_symbols`` so a second
    "experimental" scanner (v7.15) can live alongside the ICT one. Behavior
    is byte-identical to the prior implementation; the rename is pure
    labeling. ``scan_live_symbols`` is preserved below as a compat alias for
    callers that still import the old name (no behavior change either way).

    Args:
        bars_by_symbol: {symbol: [bar_dicts]}  (time/open/high/low/close/volume)
        daily_ctx_by_symbol: optional {symbol: {"prev_close", "prev_high",
            "prev_low", "avg_daily_volume"}} map. When supplied the values
            are forwarded to compute_indicators so gap-based and prior-day-
            level strategies (Gap-and-Go, Prior Day High/Low Break) can
            pre-screen and the RVOL reading has a baseline. When absent
            those strategies self-skip via ind.get(...) and RVOL is None
            — the remaining playbook runs unchanged, so older callers stay
            backwards compatible.

    Returns a list of dicts (only symbols with ≥ 1 matching strategy):
        [
          {
            "symbol": "AAPL",
            "current_price": 189.42,
            "change_pct": 0.85,
            "matched_strategies": [{"name": "Oversold Bounce (Mean Reversion)", "direction": "long"}, ...],
            "indicators_snapshot": { "rsi": 52.1, ... }
          },
          ...
        ]
    """
    from .analyzer import compute_indicators
    from .ict import compute_ict_features
    from .strategies import PLAYBOOK, screen

    daily_ctx_by_symbol = daily_ctx_by_symbol or {}

    def _process(symbol: str, bars: list[dict]) -> dict | None:
        # Minimum bar count before we try to classify a setup. Lowered
        # from 30 → 15 on 2026-04-24 as part of the ICT-only pivot: the
        # 30-bar floor meant the scanner couldn't classify anything
        # before ~10:00 ET on a 1-minute feed, which blinded us to the
        # 9:45–10:00 ET sweet spot where a lot of opening-liquidity
        # sweeps and first-MSS events occur. 15 bars is enough for the
        # ICT feature layer (swings need 2k+1=7, FVGs need 3, session
        # dealing range is cumulative) and the classic indicators
        # (EMA9, RSI(14), ATR(14)) are all usable at 15 bars even if
        # EMA20 / Bollinger(20) won't have a full window yet — fine,
        # since the playbook is ICT-only and those classic indicators
        # aren't driving the strategy matching anymore. compute_ict_features
        # has a matching floor that was lowered in lockstep.
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
            # ICT feature layer — runs the sweep/MSS/FVG/OB/killzone
            # detectors and merges the resulting flags into the ``ind``
            # payload so ICT strategy predicates read them the same
            # way every other strategy reads RSI / VWAP / EMA today.
            # Failure is isolated: if this blows up, we log and keep
            # the classic playbook live instead of failing the scan.
            try:
                ind.update(compute_ict_features(bars, ctx))
            except Exception:
                logger.exception("ict_features_failed symbol=%s", symbol)

            ind["symbol"] = symbol
            matched = screen(ind)

            # v7.12: collect "forming" partial-state setups (sweep+MSS
            # before the final entry trigger fires). These never auto-
            # trade and are surfaced to the frontend's Watching panel.
            # We compute partial state on every PLAYBOOK strategy whose
            # _partial_check is wired AND that didn't already fully
            # match — otherwise we'd double-render the same setup as
            # both "matched" and "fully_formed forming."
            matched_names = {s.name for s in matched}
            forming_setups: list[dict] = []
            for s in PLAYBOOK:
                if s._partial_check is None:
                    continue
                if s.name in matched_names:
                    continue
                state = s.partial_match(ind)
                if state is None:
                    continue
                if state.get("stage") == "fully_formed":
                    # The structural sequence is complete but matches()
                    # didn't fire — likely symbol_blacklist gate. Don't
                    # surface as "watching" since the user can't act on
                    # a blacklisted setup anyway.
                    continue
                forming_setups.append({
                    "name": s.name,
                    "direction": s.direction,
                    "tier": "forming",
                    "stage": state.get("stage"),
                    "missing": state.get("missing", []),
                    "structural_notes": state.get("structural_notes", []),
                })

            # v7.12: return a hit when EITHER matched_strategies OR
            # forming_setups is non-empty. Without this, forming-only
            # setups would be computed but never surfaced to the
            # analyzer / frontend.
            if not matched and not forming_setups:
                return None

            return {
                "symbol": symbol,
                "current_price": ind["current_price"],
                "change_pct": ind["change_pct"],
                # v7.14 (2026-05-18) — scanner identity tag. Every hit
                # carries the producing scanner's id so downstream
                # pipelines (autotrade, telemetry, EOD email) never
                # have to guess. The v7.15 experimental scanner will
                # emit "experimental_v1" via its own return dict.
                "scanner_id": "ict",
                "matched_strategies": [
                    {"name": s.name, "direction": s.direction} for s in matched
                ],
                "forming_setups": forming_setups,
                "indicators_snapshot": {
                    "rsi":      ind["rsi"],
                    "vwap":     ind["vwap"],
                    "atr":      ind["atr"],
                    "ema9":     ind["ema9"],
                    "ema20":    ind["ema20"],
                    "macd":     ind["macd"],
                    "bb_pct_b": ind["bb_pct_b"],
                    "vol_ratio": ind["vol_ratio"],
                    "gap_pct":  ind.get("gap_pct"),
                    # ICT fields (optional; None when ICT features
                    # couldn't be computed). Kept shallow — Tier-2
                    # Opus reads the full zone geometry from the
                    # analyzer path, not from this snapshot.
                    "ict_killzone":     ind.get("ict_killzone"),
                    "ict_pd_zone":      ind.get("ict_pd_zone"),
                    "ict_last_mss":     ind.get("ict_last_mss"),
                    "ict_last_sweep":   ind.get("ict_last_sweep"),
                },
            }
        except Exception:
            logger.exception("scan_error symbol=%s", symbol)
            return None

    results = []
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
    matched_symbols = [
        r["symbol"] for r in results if r.get("matched_strategies")
    ]
    forming_symbols = [
        r["symbol"]
        for r in results
        if r.get("forming_setups") and not r.get("matched_strategies")
    ]
    matched_setups = sum(len(r.get("matched_strategies") or []) for r in results)
    forming_setups = sum(len(r.get("forming_setups") or []) for r in results)
    logger.info(
        "ict_scan_complete symbols=%d hits=%d matched_symbols=%d "
        "matched_setups=%d forming_symbols=%d forming_setups=%d "
        "latest_bar_et=%s matched=%s forming=%s",
        len(bars_by_symbol),
        len(results),
        len(matched_symbols),
        matched_setups,
        len(forming_symbols),
        forming_setups,
        _latest_bar_et(bars_by_symbol) or "-",
        _join_symbols(matched_symbols),
        _join_symbols(forming_symbols),
    )
    return results


# v7.14 compat alias: pre-rename callers (`from .scanner import scan_live_symbols`)
# continue to work without behavior change. New code should use
# ``scan_ict_symbols`` so it's clear which scanner is being invoked once
# v7.15 ships the second (experimental) scanner.
scan_live_symbols = scan_ict_symbols
