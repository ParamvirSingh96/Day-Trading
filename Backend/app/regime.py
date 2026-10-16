"""
regime.py — Feature #8: Market regime detector.
================================================

A daily-cadence classifier that reads SPY daily bars and labels the
current tape as one of three regimes:

  • ``trending_up``   — SPY above its 20-day SMA, slope positive,
                        realized vol < 20% annualized.
  • ``trending_down`` — SPY below 20-SMA, slope negative, vol < 25%.
  • ``choppy``        — high vol (≥ 25%) OR SPY crosses the 20-SMA
                        three or more times in the last 5 sessions.

Why this matters
----------------
The current playbook is ICT-only (v6, 2026-04-24). ICT Sweep+MSS
setups work in both trending tape (continuation after an inner-
liquidity sweep) and chop (reversion back through equilibrium after
an old-level sweep), but bleed against a strong OPPOSITE-direction
trend — shorting a sweep into a screaming up-tape is fighting the
index. The regime table dampens each ICT direction in its hostile
trend and leaves it at full weight elsewhere.

The earlier 9-strategy playbook had sharper regime dependencies —
mean-reversion vs trend-following vs breakouts — and that logic is
preserved in git history for if/when those strategies get re-added.

The detector doesn't block trades outright (except optionally at the
scanner tier). Instead it returns a per-(strategy, regime) weight in
[0, 1] that the autotrader multiplies into Claude's confidence score,
composing with the per-(strategy, symbol) calibration from feature #6.

The thresholds here are deliberately simple. Refine them only after
walk-forward validation (#7) shows the regime-aware variant beats
regime-blind on held-out data.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.regime")

_ET = ZoneInfo("America/New_York")

Regime = Literal["trending_up", "trending_down", "choppy", "unknown"]
ALL_REGIMES: tuple[Regime, ...] = (
    "trending_up",
    "trending_down",
    "choppy",
    "unknown",
)


# ── Tuning knobs ─────────────────────────────────────────────────────────────

SMA_LOOKBACK = 20        # bars used for the moving average
VOL_LOOKBACK = 20        # bars used for realized vol
SLOPE_LOOKBACK = 5       # how far back we compare the SMA to itself for slope
CROSS_LOOKBACK = 5       # sessions to count 20-SMA crosses over

# Annualized realized-vol thresholds (0.20 = 20% annualized).
TRENDING_UP_VOL_MAX = 0.20
TRENDING_DOWN_VOL_MAX = 0.25
CHOPPY_VOL_MIN = 0.25

# If SPY crosses its 20-SMA this many times in CROSS_LOOKBACK sessions,
# classify as choppy regardless of the vol / slope reading. Three or more
# crosses in a week is the textbook "can't make up its mind" tape.
CHOPPY_CROSS_MIN = 3


# ── Types ────────────────────────────────────────────────────────────────────

@dataclass
class RegimeSnapshot:
    """Everything needed to explain a regime decision to the user."""
    regime: Regime
    # Inputs / diagnostics — surfaced to the UI so the user can sanity-check
    spy_close: float
    sma_20: float
    price_vs_sma_pct: float          # (close - sma) / sma; +0.012 = 1.2% above
    sma_slope_pct_5d: float           # (sma_today - sma_5d_ago) / sma_5d_ago
    realized_vol_20d: float           # annualized, as a fraction
    crosses_last_5d: int              # how many times price crossed 20-SMA
    bars_used: int                    # how many daily bars classified from
    computed_at_date: date            # ET date of the last bar
    label: str                        # one-line human-readable summary


# ── Per-strategy regime weights ──────────────────────────────────────────────
# Values are Claude-confidence multipliers in [0, 1]. 1.0 = full weight,
# 0.0 = would skip the trade entirely (we soft-multiply rather than
# hard-filter, but a 0.0 would drag any raw confidence to zero).
#
# "unknown" is the safety net for days with insufficient SPY history —
# it must not be stricter than the strategy's best regime, or we'd
# silently suppress trades every time the feed hiccups.
#
# The assignments below come from the planned-features memo and from
# first-principles reasoning about each setup's edge:
#   • Mean-reversion: home is chop. Trends murder the thesis.
#   • Trend-following / momentum: home is trends. Chop chops them up.
#   • Breakouts (ORB, Prior-Day-Break): trend-biased but decisive
#     breaks still work in chop, so only mildly dampened.
#
# This table is kept ROUGHLY in sync with the active PLAYBOOK in
# ``strategies.py``, but with one deliberate exception: rows for
# *suspended* strategies (BB L/S in v7.6, ARB L/S in v7.7, Sweep+MSS
# Short and Multi-Day PD Short in v7.9, Silver Bullet Long in v7.3)
# are intentionally retained. The predicate functions for those
# strategies stay callable from research drivers via
# ``playbook_override``, and re-promotion is a one-line
# ``Strategy(...)`` re-insertion in strategies.py — nothing in the
# regime layer needs to change. Removing the rows here would force a
# coupled edit on every re-promotion, which is exactly the friction
# we don't want in research-mode.
#
# After the v4 backtest pruning (2026-04-21) the original
# non-ICT loser strategies were removed; subsequent ICT additions
# (v7), suspensions (v7.3 / v7.6 / v7.7 / v7.9) and the live count
# (now 7) are all documented in ``strategies.py`` module docstring.
# ``get_weight`` falls back to 1.0 for any unknown name, so re-adding
# a strategy to the playbook without updating this table still works,
# it just runs at full weight in every regime.

_DEFAULT_WEIGHTS: dict[str, dict[Regime, float]] = {
    # ICT Sweep + MSS — smart-money liquidity-raid setups. These fire
    # on structural events (sweep → MSS → zone retest) that the classic
    # regime taxonomy doesn't cleanly classify: they work in both
    # trending tape (sweep → continuation of the trend after a
    # pullback that took inner liquidity) and in chop (sweep of an old
    # level → reversion back through equilibrium). The only regime
    # where they structurally bleed is a strong trend in the OPPOSITE
    # direction — shorting a sweep-and-MSS setup into a screaming
    # trending_up tape is fighting the index. Keep the table
    # asymmetric by direction, neutral-to-mild elsewhere.
    #
    # No backtest baseline yet — these ship on structural priors from
    # the ICT playbook. After the first month of ICT-only calibration
    # data, replace with evidence-derived weights the same way every
    # other strategy's weights were set.
    #
    # 2026-04-25: the long-side ``ICT Sweep + MSS (Long)`` entry was
    # removed when the playbook strategy itself was deleted as a
    # duplicate of ``ICT Silver Bullet (Long)``. Only the short side
    # survives as a standalone playbook strategy now; the long-side
    # regime profile lives on under Silver Bullet (below).
    "ICT Sweep + MSS (Short)":
        {"trending_up": 0.7, "trending_down": 1.0, "choppy": 1.0, "unknown": 1.0},

    # Silver Bullet — the long-side Sweep+MSS profile. Originally a
    # time-of-day gated variant (10:00–11:00 ET killzone), but the
    # killzone gate was removed 2026-04-25 along with all other
    # time-of-day predicate gates, so it now just IS the long-side
    # Sweep+MSS strategy. Slightly tighter counter-trend damp than the
    # short side because the morning-tape data we have so far favored
    # reversal setups when the early-session move was aligned.
    #
    # 2026-04-25 evening: the strategy is currently SUSPENDED in the
    # PLAYBOOK pending an investigation into a 23.8% win rate at 10:00
    # ET on n=80 trades. The weights here are kept (a) so they're not
    # lost from git history when re-enabled, and (b) so the smoke
    # test's "every name has at least one non-1.0 regime cell" check
    # still passes if the suspended predicate is ever wired back in
    # for backtesting.
    "ICT Silver Bullet (Long)":
        {"trending_up": 1.0, "trending_down": 0.6, "choppy": 1.0, "unknown": 1.0},

    # ── v7 additions (2026-04-24) ────────────────────────────────────
    # No backtest baseline — these ship on structural priors. Rule of
    # thumb: reversal setups (Judas, Turtle Soup, Breaker) do their
    # best work in chop + counter-trend regimes, and worst in the
    # SAME-direction trend (fighting the trend is a bad idea). Breakout
    # / continuation setups (Asian-Range, Power of 3, Multi-day PD)
    # do their best work in the aligned trend and worst in the
    # opposite. Retune after the first calibration window.

    # Judas Swing — a reversal of the opening fakeout. The reversal
    # side wants the OPPOSITE regime of the move it's reversing. For
    # the long variant that means chop > trending_up > trending_down.
    "ICT Judas Swing (Long)":
        {"trending_up": 0.9, "trending_down": 0.7, "choppy": 1.0, "unknown": 1.0},
    "ICT Judas Swing (Short)":
        {"trending_up": 0.7, "trending_down": 0.9, "choppy": 1.0, "unknown": 1.0},

    # Turtle Soup — false break of PDH/PDL. Classic mean-reversion
    # behavior; prefers chop, tolerates the direction-neutral regime,
    # bleeds hardest when the tape is screaming AWAY from the sweep.
    "ICT Turtle Soup (Long)":
        {"trending_up": 0.9, "trending_down": 0.6, "choppy": 1.1, "unknown": 1.0},
    "ICT Turtle Soup (Short)":
        {"trending_up": 0.6, "trending_down": 0.9, "choppy": 1.1, "unknown": 1.0},

    # Asian-Range Breakout — continuation setup. Wants the aligned
    # trend; chop fades breakouts; opposite trend is a trap.
    "ICT Asian-Range Breakout (Long)":
        {"trending_up": 1.1, "trending_down": 0.7, "choppy": 0.8, "unknown": 1.0},
    "ICT Asian-Range Breakout (Short)":
        {"trending_up": 0.7, "trending_down": 1.1, "choppy": 0.8, "unknown": 1.0},

    # Power of 3 — opening manipulation → expansion. The expansion
    # leg is a continuation move, so same profile as a breakout.
    "ICT Power of 3 (Long)":
        {"trending_up": 1.1, "trending_down": 0.7, "choppy": 0.9, "unknown": 1.0},
    "ICT Power of 3 (Short)":
        {"trending_up": 0.7, "trending_down": 1.1, "choppy": 0.9, "unknown": 1.0},

    # Breaker Block — polarity-flip retest. Structurally a reversal
    # setup; behaves like Turtle Soup but with tighter requirements.
    "ICT Breaker Block (Long)":
        {"trending_up": 0.9, "trending_down": 0.6, "choppy": 1.0, "unknown": 1.0},
    "ICT Breaker Block (Short)":
        {"trending_up": 0.6, "trending_down": 0.9, "choppy": 1.0, "unknown": 1.0},

    # Multi-Day PD — higher-timeframe-aligned swing. Multi-day
    # discount + long fires against a weekly downtrend context by
    # design, so the "trending_down" damp here is LIGHTER than the
    # intraday Sweep+MSS Long — this strategy is explicitly looking
    # to catch falling knives at the bottom of the 20-day range.
    "ICT Multi-Day PD (Long)":
        {"trending_up": 1.0, "trending_down": 0.85, "choppy": 1.0, "unknown": 1.0},
    "ICT Multi-Day PD (Short)":
        {"trending_up": 0.85, "trending_down": 1.0, "choppy": 1.0, "unknown": 1.0},
}

# Threshold below which the live UI should say "regime-suppressed" in the
# approval card. Purely cosmetic — affects how things are labeled, not whether
# they fire. 0.5 means "less than half weight" = noticeably dampened.
SUPPRESSION_THRESHOLD = 0.5


# ── Hard direction-vs-regime VETO (2026-04-25) ──────────────────────────────
# Tier-1 post-mortem on the low win rate showed that long trades during a
# confirmed downtrend (and short trades during a confirmed uptrend) bled out
# even when the per-strategy soft multiplier was applied — the multiplier
# wasn't strong enough to drag those trades below the autotrade firing
# threshold. We add a hard veto on top of the soft multiplier: regardless
# of which strategy matched, a long trade in ``trending_down`` or a short
# trade in ``trending_up`` is rejected outright before any further scoring.
#
# The ``choppy`` and ``unknown`` regimes do NOT veto — choppy is regime-
# neutral (both directions are plausible reversion plays), and unknown is
# the cold-start safety net (vetoing here would silently eat trades every
# time the SPY feed hiccups).

def is_regime_vetoed(direction: str, regime: Regime) -> bool:
    """Return True iff a trade in ``direction`` should be hard-vetoed
    by the current ``regime``.

      * regime == "trending_down" + direction == "long"  → veto
      * regime == "trending_up"   + direction == "short" → veto
      * everything else                                  → not vetoed

    Direction is the trade direction Tier-2 picked, NOT the strategy's
    bias. We veto by trade direction so a long-side trade vetoed by
    a downtrend regime is rejected even if the matching Tier-1
    strategy was a "short-counter" set-up that Tier-2 happened to
    flip into a long. (In practice the matching strategies and the
    trade share direction by construction; the veto is direction-
    correct either way.)
    """
    d = (direction or "").lower()
    if d == "long" and regime == "trending_down":
        return True
    if d == "short" and regime == "trending_up":
        return True
    return False


# ── Public API ───────────────────────────────────────────────────────────────

def classify(daily_bars: list[dict]) -> RegimeSnapshot:
    """Compute the regime snapshot from a list of SPY daily bars.

    ``daily_bars`` is expected to be the internal-format list
    (``{"time": <unix_s>, "open", "high", "low", "close", "volume"}``)
    sorted chronologically ascending. At least ``SMA_LOOKBACK +
    SLOPE_LOOKBACK`` bars are required to produce a valid signal;
    below that the result is an ``unknown`` snapshot that keeps all
    strategies on their weight-1.0 fallback.
    """
    if not daily_bars:
        return _unknown_snapshot(0)

    # Sort defensively — callers shouldn't have to trust the feed order.
    bars = sorted(daily_bars, key=lambda b: b.get("time", 0))
    closes = [float(b["close"]) for b in bars if "close" in b]

    if len(closes) < SMA_LOOKBACK + SLOPE_LOOKBACK:
        return _unknown_snapshot(len(closes))

    today_close = closes[-1]
    sma_20 = _sma(closes, SMA_LOOKBACK, offset=0)
    sma_5d_ago = _sma(closes, SMA_LOOKBACK, offset=SLOPE_LOOKBACK)
    price_vs_sma_pct = (today_close - sma_20) / sma_20 if sma_20 else 0.0
    sma_slope_pct_5d = (sma_20 - sma_5d_ago) / sma_5d_ago if sma_5d_ago else 0.0
    vol = _realized_vol(closes, VOL_LOOKBACK)
    crosses = _count_sma_crosses(closes, SMA_LOOKBACK, CROSS_LOOKBACK)

    # Classification ladder — chop check comes first so a high-vol
    # uptrend doesn't get mis-labeled as trending.
    if vol >= CHOPPY_VOL_MIN or crosses >= CHOPPY_CROSS_MIN:
        regime: Regime = "choppy"
    elif today_close > sma_20 and sma_slope_pct_5d > 0 and vol < TRENDING_UP_VOL_MAX:
        regime = "trending_up"
    elif today_close < sma_20 and sma_slope_pct_5d < 0 and vol < TRENDING_DOWN_VOL_MAX:
        regime = "trending_down"
    else:
        # Mixed signals (above SMA but slope flat, or vol in the gap
        # between the trending ceilings and the choppy floor) → chop as
        # the conservative default.
        regime = "choppy"

    bar_date = _bar_et_date(bars[-1])

    return RegimeSnapshot(
        regime=regime,
        spy_close=today_close,
        sma_20=sma_20,
        price_vs_sma_pct=price_vs_sma_pct,
        sma_slope_pct_5d=sma_slope_pct_5d,
        realized_vol_20d=vol,
        crosses_last_5d=crosses,
        bars_used=len(closes),
        computed_at_date=bar_date,
        label=_format_label(regime, price_vs_sma_pct, vol),
    )


def get_weight(strategy_name: str, regime: Regime) -> float:
    """Return the [0, 1] multiplier for (strategy, regime).

    Unknown strategies get 1.0 (no effect) — so newly-added PLAYBOOK
    entries don't silently get zero-weighted until someone remembers
    to update the table.
    """
    weights = _DEFAULT_WEIGHTS.get(strategy_name)
    if not weights:
        return 1.0
    return weights.get(regime, 1.0)


def snapshot_dict(snap: RegimeSnapshot) -> dict:
    """JSON-safe dict for websockets / UI / logging."""
    return {
        "regime":               snap.regime,
        "label":                snap.label,
        "spy_close":            round(snap.spy_close, 2),
        "sma_20":               round(snap.sma_20, 2),
        "price_vs_sma_pct":     round(snap.price_vs_sma_pct, 4),
        "sma_slope_pct_5d":     round(snap.sma_slope_pct_5d, 4),
        "realized_vol_20d":     round(snap.realized_vol_20d, 4),
        "crosses_last_5d":      snap.crosses_last_5d,
        "bars_used":            snap.bars_used,
        "computed_at_date":     snap.computed_at_date.isoformat(),
    }


# ── Math helpers ─────────────────────────────────────────────────────────────

def _sma(closes: list[float], window: int, *, offset: int = 0) -> float:
    """Simple moving average.

    ``offset=0`` → SMA of the last ``window`` bars (today's SMA).
    ``offset=5`` → SMA as of 5 bars ago. Used to compute slope.
    """
    end = len(closes) - offset
    start = end - window
    if start < 0 or end <= start:
        return 0.0
    return sum(closes[start:end]) / window


def _realized_vol(closes: list[float], window: int) -> float:
    """Annualized realized volatility of daily log-returns.

    Uses the last ``window`` log-returns (so ``window+1`` closes).
    Annualized with √252 — the standard day-trader convention.
    """
    if len(closes) < window + 1:
        return 0.0
    log_returns: list[float] = []
    tail = closes[-(window + 1):]
    for i in range(1, len(tail)):
        if tail[i - 1] > 0 and tail[i] > 0:
            log_returns.append(math.log(tail[i] / tail[i - 1]))
    if len(log_returns) < 2:
        return 0.0
    return statistics.stdev(log_returns) * math.sqrt(252)


def _count_sma_crosses(
    closes: list[float],
    sma_window: int,
    lookback: int,
) -> int:
    """Count how many times price crossed the rolling SMA in the last ``lookback`` sessions.

    A "cross" is any pair of consecutive bars where the (close > SMA)
    relationship flips. Same-side bars don't count. The SMA is
    re-computed at each point so the cross count reflects the
    then-current mean, not a look-ahead mean.
    """
    if len(closes) < sma_window + lookback:
        return 0

    # Need `lookback + 1` observation points to count `lookback` transitions.
    signs: list[bool] = []
    for i in range(len(closes) - lookback, len(closes) + 1):
        # SMA over [i - sma_window, i) — strictly past bars at point i.
        if i - sma_window < 0:
            continue
        sma = sum(closes[i - sma_window:i]) / sma_window
        # Observation: did the *most-recent* bar (index i-1) close above SMA?
        signs.append(closes[i - 1] > sma)

    crosses = 0
    for i in range(1, len(signs)):
        if signs[i] != signs[i - 1]:
            crosses += 1
    return crosses


def _bar_et_date(bar: dict) -> date:
    """Best-effort ET date for a bar. Used only for UI surfacing."""
    t = bar.get("time")
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(_ET).date()
    return date.today()


def _unknown_snapshot(bars_used: int) -> RegimeSnapshot:
    return RegimeSnapshot(
        regime="unknown",
        spy_close=0.0,
        sma_20=0.0,
        price_vs_sma_pct=0.0,
        sma_slope_pct_5d=0.0,
        realized_vol_20d=0.0,
        crosses_last_5d=0,
        bars_used=bars_used,
        computed_at_date=date.today(),
        label=f"Unknown regime (need ≥{SMA_LOOKBACK + SLOPE_LOOKBACK} SPY daily bars, have {bars_used})",
    )


def _format_label(regime: Regime, price_vs_sma: float, vol: float) -> str:
    name = {
        "trending_up":   "Trending Up",
        "trending_down": "Trending Down",
        "choppy":        "Choppy",
        "unknown":       "Unknown",
    }[regime]
    return f"{name} · SPY {price_vs_sma:+.1%} vs 20-SMA · vol {vol:.0%}"
