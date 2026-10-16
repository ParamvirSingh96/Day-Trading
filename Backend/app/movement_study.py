"""
Weekly Candle Movement Study — Phase 1 (v7.16, 2026-05-07)
==============================================================================

Deterministic detection of "drastic movement" events across the last N
trading days, plus matched negative controls (failed breakouts +
nothing-happened windows). AI description and clustering are deferred
to Phase 2 — Phase 1 is purely code-driven so we can verify event
detection on real bars BEFORE spending any API tokens.

DESIGN PRINCIPLES (per operator plan, 2026-05-07):
* **Code-first event detection.** AI never decides what "interesting"
  means. Pure thresholds: 1.5x ATR single candle, 2x ATR multi-candle
  run, 2x volume spike with real move, breakout-close-near-extreme.
* **Negative controls are mandatory.** For every winner detected we
  also sample one failed_breakout and one nothing_happened window.
  Same time-of-day distribution, same symbol pool. AI sees a balanced
  mix in Phase 2 so it can't infer outcome from window-of-origin.
* **Read-only research.** Module never modifies any predicate,
  scanner config, or trade record. Output is a single JSON file
  per run for offline review.
* **Bars NOT persisted.** ``event_ts_iso + symbol`` is enough for
  any replay tool to re-fetch. We persist FEATURES across each
  window (pre/move/post) — the structural narrative the AI step
  needs, without bloating the JSON.

EVENT TYPES (deterministic):
  single_candle_expansion : one bar.range >= 1.5x ATR-14
  multi_candle_run        : sum of 3-5 consecutive close-to-close
                            moves >= 2x ATR-14, monotonic direction
  volume_spike_with_move  : volume >= 2x rolling 20-bar avg AND
                            |close - open| >= 0.7x ATR (so a doji
                            with high volume doesn't qualify)
  breakout_close_near_extreme : range >= 1.5x ATR AND close in top
                                or bottom 20% of bar (real conviction)

NEGATIVE CONTROLS:
  failed_breakout : range >= 1.5x ATR, but next 3 bars retrace >= 50%
                    of the bar's body. The "almost made it" candle
                    that hindsight pattern-matchers conflate with
                    real expansion.
  nothing_happened : random non-event index where the next 30 mins
                     have total move < 0.5x ATR AND no >= 1.5x ATR
                     single bar. Sampled in the same hour-bucket as
                     a winner so time-of-day isn't a confound.

OUTPUT: single JSON at
  Backend/data/movement_studies/movement_study_<YYYY-MM-DD>.json
==============================================================================
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .alpaca import AlpacaMarketDataClient, AlpacaBarsRequest, convert_alpaca_bars
from .analyzer import _atr, _ema, _vwap

logger = logging.getLogger("tradehack.movement_study")

_ET = ZoneInfo("America/New_York")
_BASE_DIR = Path(__file__).resolve().parent.parent
_OUT_DIR = _BASE_DIR / "data" / "movement_studies"

# ── Detection thresholds ─────────────────────────────────────────────────────
#
# Tightened 2026-05-07 after a calibration pass: 1.5x ATR on 1-min bars
# fires on roughly 25% of all bars during normal volatility — far too
# noisy for "drastic move" research. 2.5x ATR brings the per-symbol
# event count down to ~5-15 per trading day, which matches what a
# human reviewing the chart would point to.

_SINGLE_CANDLE_ATR_MULT = 2.5      # single bar range >= 2.5x ATR
_MULTI_CANDLE_ATR_MULT  = 3.0      # 3-5 candle cumulative move >= 3.0x ATR
_MULTI_CANDLE_MIN_LEN   = 3
_MULTI_CANDLE_MAX_LEN   = 5
_VOLUME_SPIKE_MULT      = 3.0      # volume >= 3x rolling 20-bar avg
_VOLUME_SPIKE_MIN_BODY  = 1.0      # AND |close-open| >= 1.0x ATR
_BREAKOUT_CLOSE_PCT     = 0.20     # close within top/bottom 20% of bar
_BREAKOUT_MIN_RANGE_ATR = 2.0      # AND range >= 2.0x ATR

# Winner-vs-loser classification: a candidate becomes a WINNER iff the
# move continued (post-window didn't retrace >= 50% of the body) AND a
# LOSER iff it reverted within 3 bars.
_LOSER_RETRACE_PCT = 0.50
_LOSER_RETRACE_BARS = 3

# Minimum gap (in bars) between consecutive winners on the same
# symbol — prevents one expansion event being counted 3-4 times across
# the bars that comprise it.
_WINNER_DEDUP_GAP = 30

# Nothing-happened: no candidate fired AND the next 30 mins had:
#  * total close-to-close move < 0.5x ATR
#  * no single bar with range >= 1.5x ATR
_NOTHING_TOTAL_MOVE_ATR = 0.5
_NOTHING_MAX_BAR_ATR    = 1.5

# Window sizing (in 1-min bars).
_PRE_MIN  = 60
_PRE_MAX  = 120
_PRE_DEFAULT = 90    # used as the "primary" pre-window length
_POST_LEN = 30

# ATR / vol-spike rolling lookbacks
_ATR_PERIOD = 14
_VOL_AVG_PERIOD = 20

# Detection-skip buffer: we ignore events where there isn't enough
# pre-window history yet (e.g., first bar of the day).
_PRE_BUFFER = max(_PRE_DEFAULT, _ATR_PERIOD, _VOL_AVG_PERIOD)


# ── Public dataclasses ───────────────────────────────────────────────────────

@dataclass
class MovementRecord:
    """A single event (winner OR negative-control) with pre/move/post features."""
    id: str
    symbol: str
    event_type: str           # see module docstring
    outcome: str              # "winner" | "loser" | "no_event"
    direction: str            # "long" | "short" | "none"
    event_ts_iso: str         # NY time of the event bar
    # Session bucket (added 2026-05-07 per operator review):
    # "open_0_10" / "morning" / "midday" / "afternoon" / "extended_hours".
    # Lets the Phase-2 clustering segment by time-of-day so opening-
    # imbalance patterns can be identified separately from intraday
    # compression-and-breakout patterns.
    event_session_bucket: str = ""
    move_atr_multiple: float | None = None
    move_candle_count: int | None = None
    pre_window: dict[str, Any] = field(default_factory=dict)
    move_window: dict[str, Any] = field(default_factory=dict)
    post_window: dict[str, Any] = field(default_factory=dict)
    # Last 30 pre-move bars in ATR-normalized form. Phase-2 Haiku
    # description prompt will format this as a compact textual
    # candle-sequence so the model can "see" the structure without
    # us having to render chart images. Each entry has:
    #   body_atr (signed) / upper_wick_atr / lower_wick_atr (>=0) /
    #   volume_ratio / close_vs_vwap (signed).
    pre_window_candles: list[dict[str, Any]] = field(default_factory=list)
    # Phase 2 will populate ai_description + ai_tags. Reserved here so
    # the schema is stable across phases.
    ai_description: str | None = None
    ai_tags: list[str] = field(default_factory=list)


# ── Bar fetching ─────────────────────────────────────────────────────────────

def _fetch_bars_for_symbol(symbol: str, days_back: int) -> list[dict]:
    """Pull 1-min bars from Alpaca covering the last `days_back` trading
    days plus a buffer. Returns the converted (time/o/h/l/c/v) shape."""
    client = AlpacaMarketDataClient()
    end_utc = datetime.now(timezone.utc)
    # Add 2 calendar days padding on the front for ATR warmup spillover
    # across weekends.
    start_utc = end_utc - timedelta(days=days_back + 4)
    raw = client.fetch_stock_bars(AlpacaBarsRequest(
        symbols=[symbol],
        timeframe="1Min",
        start=start_utc,
        end=end_utc,
    )).get(symbol, [])
    return convert_alpaca_bars(raw) if raw else []


# ── Detection helpers ────────────────────────────────────────────────────────

def _bar_iso(bar: dict[str, Any]) -> str:
    try:
        return datetime.fromtimestamp(bar["time"], tz=timezone.utc) \
            .astimezone(_ET).isoformat(timespec="seconds")
    except Exception:
        return ""


def _direction(open_p: float, close_p: float) -> str:
    if close_p > open_p: return "long"
    if close_p < open_p: return "short"
    return "none"


def _compute_atr_series(bars: list[dict]) -> list[float]:
    """Return ATR-14 per bar as a Python list (NaN-padded for warmup)."""
    if not bars:
        return []
    df = pd.DataFrame(bars)
    atr = _atr(df, _ATR_PERIOD)
    return [float(x) if pd.notna(x) else float("nan") for x in atr.tolist()]


def _compute_vol_avg_series(bars: list[dict]) -> list[float]:
    if not bars:
        return []
    s = pd.Series([b.get("volume", 0) for b in bars])
    avg = s.rolling(_VOL_AVG_PERIOD).mean()
    return [float(x) if pd.notna(x) else float("nan") for x in avg.tolist()]


def _is_rth(bar: dict) -> bool:
    """True if bar timestamp is inside 09:30-16:00 ET. Movement events
    outside RTH are excluded — the predicates we want to find apply to
    intraday structure only."""
    try:
        ny = datetime.fromtimestamp(bar["time"], tz=timezone.utc).astimezone(_ET)
    except Exception:
        return False
    minutes = ny.hour * 60 + ny.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _session_bucket(bar: dict) -> str:
    """Coarse intraday time-of-day bucket. Used as a clustering
    dimension in Phase 2 so opening-imbalance patterns separate
    cleanly from intraday compression-and-breakout patterns.

    Buckets (ET, market hours):
      open_0_10  : 09:30 – 09:40   (gap-driven opening imbalance)
      morning    : 09:40 – 11:30   (NY_AM + Silver Bullet kz)
      midday    : 11:30 – 13:30   (lunch dead zone)
      afternoon : 13:30 – 16:00   (NY_PM kz + close)
      extended_hours : anything outside RTH (defensive — shouldn't
                       happen since detection is gated on _is_rth)
    """
    try:
        ny = datetime.fromtimestamp(bar["time"], tz=timezone.utc).astimezone(_ET)
    except Exception:
        return "extended_hours"
    minutes = ny.hour * 60 + ny.minute
    if 9 * 60 + 30 <= minutes < 9 * 60 + 40:
        return "open_0_10"
    if 9 * 60 + 40 <= minutes < 11 * 60 + 30:
        return "morning"
    if 11 * 60 + 30 <= minutes < 13 * 60 + 30:
        return "midday"
    if 13 * 60 + 30 <= minutes < 16 * 60:
        return "afternoon"
    return "extended_hours"


def _normalized_candle_sequence(
    bars: list[dict],
    end_idx: int,
    atr_series: list[float],
    last_n: int = 30,
) -> list[dict]:
    """Compact ATR-normalized last-N pre-move candle sequence.

    For Phase-2 the AI gets a textual rendering of this sequence so it
    can "see" the structural pattern without us shipping chart images.
    Each bar is reduced to:
        body_atr        : (close - open)         / atr   — signed
        upper_wick_atr  : (high - max(o, c))     / atr   — non-negative
        lower_wick_atr  : (min(o, c) - low)      / atr   — non-negative
        volume_ratio    : volume / avg-of-window
        close_vs_vwap   : (close - cum_vwap)     / atr   — signed

    VWAP is anchored at the START of the returned sequence (window-local).
    ATR comes from the global atr_series. ``end_idx`` is INCLUSIVE — the
    last candle in the sequence.
    """
    if end_idx < 0 or end_idx >= len(bars):
        return []
    start = max(0, end_idx - last_n + 1)
    chunk = bars[start:end_idx + 1]
    if not chunk:
        return []

    avg_vol = sum(b.get("volume", 0) for b in chunk) / len(chunk)
    avg_vol = avg_vol if avg_vol > 0 else 1.0

    pv = 0.0
    cv = 0.0
    out: list[dict] = []
    for offset, bar in enumerate(chunk):
        i = start + offset
        atr = atr_series[i] if 0 <= i < len(atr_series) else float("nan")
        if atr != atr or atr <= 0:
            atr = 1.0   # degrade gracefully — preserve raw price diffs
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        v = bar.get("volume", 0)
        typical = (h + l + c) / 3.0
        pv += typical * v
        cv += v
        cum_vwap = (pv / cv) if cv > 0 else c
        out.append({
            "body_atr":       round((c - o) / atr, 3),
            "upper_wick_atr": round((h - max(o, c)) / atr, 3),
            "lower_wick_atr": round((min(o, c) - l) / atr, 3),
            "volume_ratio":   round(v / avg_vol, 2),
            "close_vs_vwap":  round((c - cum_vwap) / atr, 3),
        })
    return out


# ── Per-event detectors ──────────────────────────────────────────────────────

def _detect_single_candle_expansion(
    bars: list[dict], atr_series: list[float], i: int,
) -> tuple[bool, float, str]:
    """Bar i: is its range >= 1.5x ATR?"""
    atr = atr_series[i]
    if atr != atr or atr <= 0:
        return False, 0.0, "none"
    rng = bars[i]["high"] - bars[i]["low"]
    mult = rng / atr if atr > 0 else 0.0
    if mult < _SINGLE_CANDLE_ATR_MULT:
        return False, mult, "none"
    return True, mult, _direction(bars[i]["open"], bars[i]["close"])


def _detect_multi_candle_run(
    bars: list[dict], atr_series: list[float], i: int,
) -> tuple[bool, float, str, int]:
    """Bar i is the LAST bar of a 3..5 monotonic move whose total
    close-to-close magnitude exceeds 2x ATR. Returns (hit, mult, dir, len)."""
    atr = atr_series[i]
    if atr != atr or atr <= 0:
        return False, 0.0, "none", 0
    best_mult = 0.0
    best_dir = "none"
    best_len = 0
    for n in range(_MULTI_CANDLE_MIN_LEN, _MULTI_CANDLE_MAX_LEN + 1):
        start = i - n + 1
        if start < 0:
            continue
        chunk = bars[start:i + 1]
        # Monotonic check — every bar's close moves in the same direction
        # as the cumulative move (we allow one anti-direction bar as
        # noise; pure monotonic was too strict on real data).
        c0 = chunk[0]["open"]
        cN = chunk[-1]["close"]
        total = cN - c0
        if total == 0:
            continue
        # Count anti-direction closes
        anti = sum(1 for j in range(1, len(chunk))
                   if (chunk[j]["close"] - chunk[j - 1]["close"]) * total < 0)
        if anti > 1:
            continue
        mult = abs(total) / atr if atr > 0 else 0.0
        if mult >= _MULTI_CANDLE_ATR_MULT and mult > best_mult:
            best_mult = mult
            best_dir = "long" if total > 0 else "short"
            best_len = n
    if best_len == 0:
        return False, 0.0, "none", 0
    return True, best_mult, best_dir, best_len


def _detect_volume_spike_with_move(
    bars: list[dict], atr_series: list[float], vol_avg: list[float], i: int,
) -> tuple[bool, float, str]:
    avg = vol_avg[i]
    atr = atr_series[i]
    if avg != avg or avg <= 0 or atr != atr or atr <= 0:
        return False, 0.0, "none"
    bar = bars[i]
    vol_mult = bar.get("volume", 0) / avg if avg > 0 else 0.0
    if vol_mult < _VOLUME_SPIKE_MULT:
        return False, vol_mult, "none"
    body = abs(bar["close"] - bar["open"])
    body_atr = body / atr if atr > 0 else 0.0
    if body_atr < _VOLUME_SPIKE_MIN_BODY:
        return False, vol_mult, "none"
    return True, vol_mult, _direction(bar["open"], bar["close"])


def _detect_breakout_close_near_extreme(
    bars: list[dict], atr_series: list[float], i: int,
) -> tuple[bool, float, str]:
    atr = atr_series[i]
    if atr != atr or atr <= 0:
        return False, 0.0, "none"
    bar = bars[i]
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return False, 0.0, "none"
    mult = rng / atr
    if mult < _BREAKOUT_MIN_RANGE_ATR:
        return False, mult, "none"
    # Where in the bar did we close?
    pos = (bar["close"] - bar["low"]) / rng   # 0 = closed at low, 1 = closed at high
    if pos >= 1.0 - _BREAKOUT_CLOSE_PCT:
        return True, mult, "long"
    if pos <= _BREAKOUT_CLOSE_PCT:
        return True, mult, "short"
    return False, mult, "none"


# ── Outcome classification ───────────────────────────────────────────────────

def _classify_outcome(
    bars: list[dict], event_idx: int, direction: str, move_start_idx: int,
) -> str:
    """Did the move at ``event_idx`` (which started at ``move_start_idx``)
    follow through, or did it fail?

    Definitions:
      - The "move body" is from bars[move_start_idx].open → bars[event_idx].close.
      - "loser"  → ANY of the next _LOSER_RETRACE_BARS bars closes
        past the 50% retrace level (back through the body).
      - "winner" → otherwise (move held).

    Returns ``"winner"`` or ``"loser"``.
    """
    if move_start_idx < 0 or event_idx >= len(bars):
        return "winner"   # not enough info to call it a loser
    if direction not in ("long", "short"):
        return "winner"
    move_open = bars[move_start_idx]["open"]
    move_close = bars[event_idx]["close"]
    body = move_close - move_open
    if body == 0:
        return "winner"
    end = min(event_idx + 1 + _LOSER_RETRACE_BARS, len(bars))
    forward = bars[event_idx + 1:end]
    if not forward:
        return "winner"
    retrace_level = move_open + (1 - _LOSER_RETRACE_PCT) * body   # 50% retrace
    if direction == "long":
        if any(b["close"] <= retrace_level for b in forward):
            return "loser"
    else:  # short
        if any(b["close"] >= retrace_level for b in forward):
            return "loser"
    return "winner"


def _is_nothing_happened(
    bars: list[dict], atr_series: list[float], i: int,
) -> bool:
    """No event at bar i AND the next 30 mins had:
       - total close-to-close move < 0.5x ATR
       - no single bar with range >= 1.5x ATR
    """
    atr = atr_series[i]
    if atr != atr or atr <= 0:
        return False
    end = min(i + 1 + _POST_LEN, len(bars))
    if end - i < _POST_LEN // 2:   # not enough forward bars
        return False
    forward = bars[i + 1:end]
    total_move = abs(forward[-1]["close"] - bars[i]["close"]) / atr if atr > 0 else 0
    if total_move >= _NOTHING_TOTAL_MOVE_ATR:
        return False
    max_range_atr = max((b["high"] - b["low"]) / atr for b in forward) if atr > 0 else 0
    if max_range_atr >= _NOTHING_MAX_BAR_ATR:
        return False
    return True


# ── Window feature extraction ────────────────────────────────────────────────

def _window_features(
    bars: list[dict], start_idx: int, end_idx: int, atr_series: list[float],
) -> dict[str, Any]:
    """Compute structural features over [start_idx, end_idx] (inclusive).

    Returns a dict with the scalar features the Phase-2 AI prompt will
    receive. No bar arrays — just the structural narrative.
    """
    if start_idx >= len(bars) or end_idx < start_idx:
        return {}
    start_idx = max(0, start_idx)
    end_idx = min(len(bars) - 1, end_idx)
    win = bars[start_idx:end_idx + 1]
    if not win:
        return {}

    df = pd.DataFrame(win)
    closes = df["close"]
    opens = df["open"]
    highs = df["high"]
    lows = df["low"]
    vols = df["volume"]

    # VWAP across the window itself (NOT session VWAP — we want the
    # micro-structure of the lead-up).
    typical = (highs + lows + closes) / 3.0
    pv = (typical * vols).cumsum()
    cv = vols.cumsum().replace(0, float("nan"))
    win_vwap = (pv / cv).ffill().fillna(closes.iloc[0])

    # EMA-9 / EMA-20 across the window
    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)

    # ATR — use the global series at the window boundaries
    atr_start = atr_series[start_idx] if start_idx < len(atr_series) else float("nan")
    atr_end = atr_series[end_idx] if end_idx < len(atr_series) else float("nan")
    atr_mean = float(pd.Series(atr_series[start_idx:end_idx + 1]).mean()) if end_idx >= start_idx else float("nan")

    # Volume vs window average
    vol_avg = float(vols.mean()) if len(vols) else 0.0
    vol_max_ratio = float(vols.max() / vol_avg) if vol_avg > 0 else 0.0

    # EMA squeeze: avg |EMA9 - EMA20| / ATR_mean — small means tight
    ema_diff_atr = float(((ema9 - ema20).abs() / atr_mean).mean()) if atr_mean and atr_mean == atr_mean else float("nan")

    # Bullish bar percentage
    bullish_pct = float((closes > opens).sum() / len(closes)) if len(closes) else 0.0

    # Above-VWAP percentage
    above_vwap_pct = float((closes > win_vwap).sum() / len(closes)) if len(closes) else 0.0

    # Net direction
    net_change = float(closes.iloc[-1] - opens.iloc[0])
    net_change_atr = float(net_change / atr_mean) if atr_mean and atr_mean == atr_mean and atr_mean > 0 else 0.0

    return {
        "bar_count":          len(win),
        "open":               round(float(opens.iloc[0]), 4),
        "close":              round(float(closes.iloc[-1]), 4),
        "high":               round(float(highs.max()), 4),
        "low":                round(float(lows.min()), 4),
        "net_change":         round(net_change, 4),
        "net_change_atr":     round(net_change_atr, 3),
        "vwap_at_start":      round(float(win_vwap.iloc[0]), 4),
        "vwap_at_end":        round(float(win_vwap.iloc[-1]), 4),
        "above_vwap_pct":     round(above_vwap_pct, 3),
        "ema9_at_start":      round(float(ema9.iloc[0]), 4),
        "ema9_at_end":        round(float(ema9.iloc[-1]), 4),
        "ema20_at_start":     round(float(ema20.iloc[0]), 4),
        "ema20_at_end":       round(float(ema20.iloc[-1]), 4),
        "ema_diff_atr_avg":   round(ema_diff_atr, 4) if ema_diff_atr == ema_diff_atr else None,
        "atr_mean":           round(atr_mean, 4) if atr_mean == atr_mean else None,
        "vol_avg":            round(vol_avg, 1),
        "vol_max_ratio":      round(vol_max_ratio, 2),
        "total_volume":       int(vols.sum()),
        "bullish_bar_pct":    round(bullish_pct, 3),
        "ts_start":           _bar_iso(win[0]),
        "ts_end":             _bar_iso(win[-1]),
    }


# ── Top-level scan ───────────────────────────────────────────────────────────

def scan_symbol(symbol: str, days_back: int = 5) -> list[MovementRecord]:
    """Scan ``days_back`` trading days of bars for `symbol`. Returns a
    list of MovementRecord — winners + matched negative controls.

    Pure function. No AI calls. No DB writes. Caller is responsible
    for persistence (the script writes JSON).
    """
    bars = _fetch_bars_for_symbol(symbol, days_back)
    if len(bars) < _PRE_BUFFER + _POST_LEN + 10:
        logger.info("movement_study_insufficient_bars symbol=%s bars=%d", symbol, len(bars))
        return []

    atr_series = _compute_atr_series(bars)
    vol_avg = _compute_vol_avg_series(bars)

    candidates: list[MovementRecord] = []   # winners + losers (every bar that passed a threshold)
    nothing_indices: list[int] = []
    last_event_idx = -10_000

    # Single forward pass — find every bar that passed ANY threshold,
    # then classify by what happened in the post-window. A "winner" is
    # a candidate whose move continued; a "loser" is one that retraced
    # >= 50% within 3 bars. The same bar can't be both — they're
    # outcome partitions of the same trigger condition.
    for i in range(_PRE_BUFFER, len(bars) - _POST_LEN):
        if not _is_rth(bars[i]):
            continue

        # Detector priority: most-specific first.
        event = None
        move_start_idx = i

        is_breakout, brk_mult, brk_dir = _detect_breakout_close_near_extreme(bars, atr_series, i)
        is_single, single_mult, single_dir = _detect_single_candle_expansion(bars, atr_series, i)
        is_vol, vol_mult, vol_dir = _detect_volume_spike_with_move(bars, atr_series, vol_avg, i)
        is_multi, multi_mult, multi_dir, multi_len = _detect_multi_candle_run(bars, atr_series, i)

        if is_breakout:
            event = ("breakout_close_near_extreme", brk_mult, brk_dir, 1)
        elif is_single:
            event = ("single_candle_expansion", single_mult, single_dir, 1)
        elif is_vol:
            event = ("volume_spike_with_move", vol_mult, vol_dir, 1)
        elif is_multi:
            event = ("multi_candle_run", multi_mult, multi_dir, multi_len)
            move_start_idx = max(0, i - multi_len + 1)

        if event is None:
            # Not a candidate. Eligible for nothing-happened sampling.
            if _is_nothing_happened(bars, atr_series, i):
                nothing_indices.append(i)
            continue

        # Dedup: don't emit a second event within _WINNER_DEDUP_GAP bars
        # of the previous event (multiple bars of the SAME expansion
        # would otherwise count separately).
        if i - last_event_idx < _WINNER_DEDUP_GAP:
            continue
        last_event_idx = i

        etype, mult, edir, elen = event
        outcome = _classify_outcome(bars, i, edir, move_start_idx)
        candidates.append(_build_record(
            symbol=symbol,
            event_type=etype,
            outcome=outcome,
            direction=edir,
            event_idx=i,
            bars=bars,
            atr_series=atr_series,
            move_atr_multiple=mult,
            move_candle_count=elen,
        ))

    winners = [r for r in candidates if r.outcome == "winner"]
    losers = [r for r in candidates if r.outcome == "loser"]

    if not winners and not losers:
        logger.info("movement_study_no_events symbol=%s bars=%d", symbol, len(bars))
        return []

    # Sample nothing-happened windows to match the winner count
    # (stratified by hour-of-day so time isn't a confound).
    rng = random.Random(hash(symbol) & 0xFFFFFFFF)   # deterministic
    nothing_picks = _stratified_sample(nothing_indices, winners, bars, len(winners), rng)
    nothings = [
        _build_record(
            symbol=symbol, event_type="nothing_happened", outcome="no_event",
            direction="none",
            event_idx=i, bars=bars, atr_series=atr_series,
            move_atr_multiple=None, move_candle_count=None,
        )
        for i in nothing_picks
    ]

    logger.info(
        "movement_study_scanned symbol=%s bars=%d winners=%d losers=%d nothing=%d",
        symbol, len(bars), len(winners), len(losers), len(nothings),
    )
    return winners + losers + nothings


def _stratified_sample(
    candidates: list[int],
    winners: list[MovementRecord],
    bars: list[dict],
    target_count: int,
    rng: random.Random,
) -> list[int]:
    """Sample ``target_count`` candidate indices, matching the hour-of-day
    distribution of the winner records so time-of-day isn't a confound
    in the eventual AI labelling.
    """
    if not candidates or target_count <= 0:
        return []

    # Bucket candidates by NY hour
    def _hour(idx: int) -> int:
        try:
            return datetime.fromtimestamp(bars[idx]["time"], tz=timezone.utc) \
                .astimezone(_ET).hour
        except Exception:
            return -1

    cand_by_hour: dict[int, list[int]] = {}
    for c in candidates:
        cand_by_hour.setdefault(_hour(c), []).append(c)

    # Count winners per hour
    win_hour_counts: dict[int, int] = {}
    for w in winners:
        try:
            h = datetime.fromisoformat(w.event_ts_iso).hour
        except Exception:
            continue
        win_hour_counts[h] = win_hour_counts.get(h, 0) + 1

    picks: list[int] = []
    for hour, want in win_hour_counts.items():
        pool = cand_by_hour.get(hour, [])
        if not pool:
            continue
        take = min(want, len(pool))
        picks.extend(rng.sample(pool, take))

    # If stratification couldn't fill the quota, top up randomly
    if len(picks) < target_count and len(candidates) > len(picks):
        remaining = [c for c in candidates if c not in picks]
        extra = min(target_count - len(picks), len(remaining))
        picks.extend(rng.sample(remaining, extra))

    return picks[:target_count]


def _build_record(
    *,
    symbol: str,
    event_type: str,
    outcome: str,
    direction: str,
    event_idx: int,
    bars: list[dict],
    atr_series: list[float],
    move_atr_multiple: float | None,
    move_candle_count: int | None,
) -> MovementRecord:
    """Assemble a MovementRecord with pre/move/post features."""
    pre_start = max(0, event_idx - _PRE_DEFAULT)
    pre_end = event_idx - 1
    move_start = event_idx if (move_candle_count or 0) <= 1 else max(0, event_idx - (move_candle_count or 1) + 1)
    move_end = event_idx
    post_start = event_idx + 1
    post_end = min(len(bars) - 1, event_idx + _POST_LEN)

    return MovementRecord(
        id=str(uuid.uuid4()),
        symbol=symbol,
        event_type=event_type,
        outcome=outcome,
        direction=direction,
        event_ts_iso=_bar_iso(bars[event_idx]),
        event_session_bucket=_session_bucket(bars[event_idx]),
        move_atr_multiple=round(move_atr_multiple, 3) if move_atr_multiple is not None else None,
        move_candle_count=move_candle_count,
        pre_window=_window_features(bars, pre_start, pre_end, atr_series),
        move_window=_window_features(bars, move_start, move_end, atr_series),
        post_window=_window_features(bars, post_start, post_end, atr_series),
        # Last 30 pre-move candles in ATR-normalized form for the AI
        # description prompt. End at pre_end (the bar before the event)
        # so the sequence ends right where the move begins.
        pre_window_candles=_normalized_candle_sequence(
            bars, pre_end, atr_series, last_n=30,
        ),
    )


# ── Persistence ──────────────────────────────────────────────────────────────

def write_study(records: list[MovementRecord], days_back: int, universe: list[str]) -> Path:
    """Write a single JSON file summarizing the study.

    File path: ``data/movement_studies/movement_study_<NY date>.json``.
    Atomic write (.tmp + os.replace) so a crash mid-write can't leave
    a half-written file.
    """
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    today_ny = datetime.now(_ET).date().isoformat()
    out_path = _OUT_DIR / f"movement_study_{today_ny}.json"

    counts_by_outcome = {"winner": 0, "loser": 0, "no_event": 0}
    counts_by_event_type: dict[str, int] = {}
    counts_by_session: dict[str, int] = {}
    counts_session_x_outcome: dict[str, dict[str, int]] = {}
    for r in records:
        counts_by_outcome[r.outcome] = counts_by_outcome.get(r.outcome, 0) + 1
        counts_by_event_type[r.event_type] = counts_by_event_type.get(r.event_type, 0) + 1
        counts_by_session[r.event_session_bucket] = counts_by_session.get(r.event_session_bucket, 0) + 1
        counts_session_x_outcome.setdefault(r.event_session_bucket, {})
        counts_session_x_outcome[r.event_session_bucket][r.outcome] = (
            counts_session_x_outcome[r.event_session_bucket].get(r.outcome, 0) + 1
        )

    payload = {
        "schema_version":           "2",
        "generated_at":             datetime.now(_ET).isoformat(timespec="seconds"),
        "days_back":                days_back,
        "universe":                 sorted(universe),
        "counts_by_outcome":        counts_by_outcome,
        "counts_by_event_type":     counts_by_event_type,
        "counts_by_session":        counts_by_session,
        "counts_session_x_outcome": counts_session_x_outcome,
        "phase":                    "1_detection_only",   # Phase 2 will set this to "2_with_ai"
        "records":                  [asdict(r) for r in records],
    }

    tmp = out_path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    tmp.replace(out_path)

    logger.info(
        "movement_study_written path=%s records=%d winners=%d losers=%d no_events=%d",
        out_path, len(records),
        counts_by_outcome.get("winner", 0),
        counts_by_outcome.get("loser", 0),
        counts_by_outcome.get("no_event", 0),
    )
    return out_path


def run_study(symbols: list[str], days_back: int = 5) -> dict[str, Any]:
    """Top-level entry: scan all `symbols` and persist the JSON.

    Returns a summary dict (counts, output path) — used by the CLI
    driver and (eventually) a dashboard endpoint.
    """
    all_records: list[MovementRecord] = []
    per_symbol: dict[str, dict[str, int]] = {}
    for sym in symbols:
        try:
            recs = scan_symbol(sym, days_back=days_back)
        except Exception as exc:
            logger.warning("movement_study_symbol_failed symbol=%s err=%s", sym, exc)
            continue
        per_symbol[sym] = {
            "winners":  sum(1 for r in recs if r.outcome == "winner"),
            "losers":   sum(1 for r in recs if r.outcome == "loser"),
            "nothing":  sum(1 for r in recs if r.outcome == "no_event"),
        }
        all_records.extend(recs)

    out_path = write_study(all_records, days_back=days_back, universe=symbols)

    return {
        "out_path":   str(out_path),
        "n_records":  len(all_records),
        "n_winners":  sum(1 for r in all_records if r.outcome == "winner"),
        "n_losers":   sum(1 for r in all_records if r.outcome == "loser"),
        "n_nothing":  sum(1 for r in all_records if r.outcome == "no_event"),
        "per_symbol": per_symbol,
    }
