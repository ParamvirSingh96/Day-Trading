"""
rel_strength.py — Relative Strength / Relative Weakness multiplier (#12).
========================================================================

A per-trade soft multiplier that scores whether the setup is on a stock
actually *leading* (long) or *lagging* (short) the broader tape, and
returns a clamped factor in ``[0.70, 1.20]`` to compose with the
regime / calibration / news / Brier stack in ``autotrade.py``.

Why this matters
----------------
The v4 backtest showed every breakout-continuation strategy bleeding
(Momentum Breakout, ORB Long, Prior Day High Break, Gap-and-Go Long —
all negative). A systemic flaw: the playbook treated every stock
identically regardless of whether it was *actually* leading the tape
or just floating up with SPY on a risk-on day. When SPY rolled
intraday, those "breakouts" rolled with it.

RS filtering separates alpha from beta. A stock up +1.5% while SPY is
flat is a genuine leader with individual demand. A stock up +1.5% when
SPY is also up +1.5% is just beta — and the apparent "leadership"
vanishes the moment the tape reverses.

The multiplier is a *soft* filter (not a hard gate) for the same
reason regime weights are soft: RS is a useful signal, not a
correctness layer. A rock-solid setup on a mild laggard should still
fire, just with dampened confidence. Hard gating belongs at Tier-1.5
(Haiku) where the pipeline is already set up to short-circuit.

Degraded-no-op discipline
-------------------------
When ``stock_vs_spy`` is ``None`` (trading SPY itself, or the sector
snapshot is missing, or the feed hiccupped), ``classify`` returns
``state="unknown"`` with ``multiplier=1.0``. Same discipline as
regime/news/brier — a missing input silently no-ops rather than
suppressing trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ── Typing ───────────────────────────────────────────────────────────────────

RSState = Literal[
    "strong_leader",   # stock meaningfully ahead of SPY in counter-tape conditions
    "leader",          # stock ahead of SPY but SPY also moving our direction (partly beta)
    "mild_leader",     # stock modestly ahead
    "neutral",         # within noise of SPY
    "mild_lagger",     # stock modestly behind (damp)
    "lagger",          # stock meaningfully behind (damp harder)
    "strong_lagger",   # stock very far behind SPY in our direction (heavy damp)
    "unknown",         # no data — no-op at 1.0
]

Direction = Literal["long", "short", "wait"]


# ── Tuning knobs ─────────────────────────────────────────────────────────────

# Thresholds are in *percentage points* of (stock − SPY) return since
# session open. 1.0 pp is a strong leadership signal.
#
# MILD_LEADERSHIP_PP was 0.5 pre-v5. Tightened to 0.3 on 2026-04-21
# because the v5 post-hoc (``scripts/rs_posthoc.py``) showed a large
# dead-neutral bucket — setups sitting in [-0.5, +0.5] pp were the
# worst-performing slice of the whole book, worse than clear laggers.
# Dropping the band to ±0.3 pp shifts roughly half of the neutral
# trades into mild_leader / mild_lagger so the multiplier actually
# gets to do work on more of the book.
STRONG_LEADERSHIP_PP = 1.0
MILD_LEADERSHIP_PP   = 0.3

# SPY drift threshold: below this (for longs) the tape is flat/pulling
# back and stock leadership is genuine alpha rather than beta. Mirror
# for shorts. 0.2 pp is deliberately tight — we want a strict definition
# of "counter-tape."
COUNTER_TAPE_SPY_PP = 0.2

# Multiplier clamp. Matches the magnitude of other stack multipliers
# (regime [0,1], news [0.2,1.1], brier [0.5,1.2], calibration ~[0.7,1.3]).
# One strong RS signal can meaningfully shift a borderline trade but
# can't overpower the combined stack on its own.
MIN_MULT = 0.70
MAX_MULT = 1.20
NEUTRAL_MULT = 1.00

# Regime × neutral coupling (v5, 2026-04-21). When the broader tape has
# a direction (trending_up / trending_down) and the stock is sitting in
# the neutral RS band, it's actively failing to confirm the tape — a
# subtle laggard disguised as a non-event. v5 post-hoc shows neutral
# setups in trending regimes bleed meaningfully more than the flat 1.0
# we were handing them. Damp to 0.90 there; leave choppy/unknown alone
# (no tape direction to fail to confirm).
NEUTRAL_TRENDING_MULT = 0.90

# Strategies for which RS filtering *actively harms* expectancy.
# Currently empty — all strategies on this list were removed in v6
# (2026-04-24) when the playbook was pared to ICT-only. Kept as an
# empty frozenset so autotrade.py's composition block can continue to
# check membership without special-casing; any future re-add of a
# strategy where RS hurts expectancy just needs to land here.
RS_DISABLED_STRATEGIES: frozenset[str] = frozenset()


# ── Per-strategy RS profiles ─────────────────────────────────────────────────
# Some strategies don't fit the default trend-follow RS ladder (leader =
# good, lagger = damp). The v5 post-hoc (``scripts/rs_neutral_drag.py``
# +  ``scripts/rs_hour_crosstab_gfs.py``) surfaced two patterns worth
# modelling at the multiplier layer:
#
#   1. *Mean-reversion semantics flip* — for strategies that fade an
#      intraday EVENT (failed gap, break reject) rather than a STATE
#      (RSI extension), the "lagger" bucket for the short direction
#      (stock still holding relative strength while losing VWAP) is the
#      PRIME fade candidate, not a degraded one. Flipping the excess
#      sign turns those trades into "mean_revert leaders" and boosts
#      them correctly.
#
#      IMPORTANT: this is NOT a blanket rule for mean-reversion shorts.
#      Overbought Fade (also mean-rev, also short) does NOT benefit
#      from the flip — its cross-tab shows leaders > laggers (fading
#      an overbought stock that's already weak vs SPY = low-quality
#      rally = good fade). Only flip where the data supports it.
#
#   2. *Per-strategy neutral tax* — the neutral RS bucket isn't a
#      universal drag (9-strategy diagnostic showed only 2/9 with
#      neutral as the worst), but for the two where it IS the worst,
#      the tax is large enough to justify an explicit damp below 1.0.
#      Gap Fill Short: neutral −0.31 AvgR vs baseline −0.002 (−0.307
#      tax). ORB Short: neutral −0.35 vs baseline −0.18 (−0.175 tax).
#
# Evidence files:
#   data/rs_neutral_drag.txt         — per-strategy × RS-state curve
#   data/rs_hour_crosstab_gfs.txt    — Gap Fill Short hour × state detail
#
# Every field is optional. Unspecified strategies fall back to
# trend-follow semantics and the global NEUTRAL_MULT / NEUTRAL_TRENDING_MULT.

RSSemantics = Literal["trend_follow", "mean_revert"]


@dataclass(frozen=True)
class RSProfile:
    """Per-strategy RS multiplier overrides.

    * ``semantics`` — how to interpret ``stock_vs_spy``:
        - ``trend_follow`` (default): stock moving WITH the trade's
          direction is "leader" (boost). Right for continuation setups.
        - ``mean_revert``: stock moving AGAINST the trade's direction
          is "leader" (boost — that's the fade opportunity).

    * ``neutral_mult`` — explicit override for the neutral bucket.
      Takes precedence over ``NEUTRAL_TRENDING_MULT`` (regime coupling)
      and the default ``NEUTRAL_MULT``. Leave None to use the tiered
      fallback.
    """
    semantics:    RSSemantics   = "trend_follow"
    neutral_mult: float | None  = None


STRATEGY_RS_PROFILES: dict[str, RSProfile] = {
    # Empty in v6 (2026-04-24) — the two entries this dict used to hold
    # (Gap Fill Short, ORB Breakdown Short) were removed when the
    # playbook was pared to ICT-only. The three remaining ICT setups
    # use default trend-follow semantics for now; revisit after the
    # first month of ICT-only calibration data surfaces whether the
    # neutral bucket or the mean-revert semantics flip applies to any
    # of them.
}


# ── Types ────────────────────────────────────────────────────────────────────

@dataclass
class RSSnapshot:
    """Full RS classification plus everything needed for the UI chip."""
    state: RSState
    direction: str
    stock_vs_spy: float | None     # percentage points
    spy_change_pct: float | None   # percentage points
    multiplier: float
    label: str                     # one-line summary ("Leader · +1.2% vs SPY")
    reason: str                    # tooltip-length justification


# ── Public API ───────────────────────────────────────────────────────────────

def classify(
    stock_vs_spy: float | None,
    spy_change_pct: float | None,
    direction: str,
    regime: str | None = None,
    strategy_name: str | None = None,
) -> RSSnapshot:
    """Classify the trade's RS posture and return a snapshot.

    ``regime`` is the current market-regime label (``trending_up`` /
    ``trending_down`` / ``choppy`` / ``unknown`` / ``None``). When the
    tape has a direction AND the stock is neutral vs SPY, we damp the
    multiplier (see ``NEUTRAL_TRENDING_MULT``) — neutral in a directional
    tape is a subtle laggard, not a non-event. Other states are regime-
    independent at this layer (regime already composes multiplicatively
    via the regime-weight stack in ``autotrade.py``).

    ``strategy_name`` selects a per-strategy profile from
    ``STRATEGY_RS_PROFILES`` (overriding the default trend-follow
    semantics and / or the default neutral-bucket multiplier). Unknown
    names fall back to the default profile — safe for newly-added
    strategies that don't have calibration evidence yet.

    Degraded paths (all return ``multiplier=1.0``):
      * ``direction`` is ``"wait"`` or empty → no trade, no evaluation.
      * ``stock_vs_spy`` is ``None`` → no sector data.
    """
    profile = STRATEGY_RS_PROFILES.get(strategy_name or "", RSProfile())

    if direction not in ("long", "short"):
        return RSSnapshot(
            state="unknown",
            direction=direction,
            stock_vs_spy=stock_vs_spy,
            spy_change_pct=spy_change_pct,
            multiplier=NEUTRAL_MULT,
            label="RS n/a",
            reason="No trade direction — RS multiplier no-ops.",
        )

    if stock_vs_spy is None:
        return RSSnapshot(
            state="unknown",
            direction=direction,
            stock_vs_spy=None,
            spy_change_pct=spy_change_pct,
            multiplier=NEUTRAL_MULT,
            label="RS unknown",
            reason=(
                "Missing SPY / sector snapshot — RS multiplier no-ops "
                "rather than suppressing the trade."
            ),
        )

    # Compute signed "excess" aligned with the trade direction.
    #
    #   trend_follow semantics (default):
    #     long  → excess = +stock_vs_spy  (stock ahead of SPY = leader)
    #     short → excess = -stock_vs_spy  (stock behind SPY  = leader)
    #
    #   mean_revert semantics (e.g. Gap Fill Short):
    #     long  → excess = -stock_vs_spy  (lagging stock = fade-up target)
    #     short → excess = +stock_vs_spy  (leading stock = fade-down target)
    if profile.semantics == "trend_follow":
        excess = stock_vs_spy if direction == "long" else -stock_vs_spy
    else:  # mean_revert
        excess = -stock_vs_spy if direction == "long" else stock_vs_spy

    # "Counter-tape" = SPY is NOT moving in our favor. For a long that
    # means SPY is flat or pulling back (genuine leadership); for a
    # short it means SPY is flat or grinding up (genuine laggard). This
    # is computed from the raw direction — independent of the semantics
    # flip, because counter-tape is about SPY's move, not about how we
    # interpret stock vs SPY.
    spy = spy_change_pct if spy_change_pct is not None else 0.0
    if direction == "long":
        counter_tape = spy <= COUNTER_TAPE_SPY_PP
    else:
        counter_tape = spy >= -COUNTER_TAPE_SPY_PP

    # Classification ladder — monotone in ``excess``.
    if excess >= STRONG_LEADERSHIP_PP:
        if counter_tape:
            state, mult = "strong_leader", 1.20
        else:
            state, mult = "leader", 1.10
    elif excess >= MILD_LEADERSHIP_PP:
        state, mult = "mild_leader", 1.05
    elif excess >= -MILD_LEADERSHIP_PP:
        # Neutral bucket precedence (first match wins):
        #   1. Per-strategy override     — strongest evidence (Gap Fill
        #      Short neutral_mult=0.70, ORB Short neutral_mult=0.80)
        #   2. Regime × neutral coupling — general heuristic (0.90 in
        #      trending tape only)
        #   3. Baseline NEUTRAL_MULT     — 1.00 (no-op)
        if profile.neutral_mult is not None:
            state, mult = "neutral", profile.neutral_mult
        elif regime in ("trending_up", "trending_down"):
            state, mult = "neutral", NEUTRAL_TRENDING_MULT
        else:
            state, mult = "neutral", NEUTRAL_MULT
    elif excess >= -STRONG_LEADERSHIP_PP:
        state, mult = "mild_lagger", 0.90
    elif excess >= -2 * STRONG_LEADERSHIP_PP:
        state, mult = "lagger", 0.80
    else:
        state, mult = "strong_lagger", 0.70

    # Paranoid clamp — the ladder above already respects the bounds
    # but a future edit shouldn't be able to break the invariant.
    mult = max(MIN_MULT, min(MAX_MULT, mult))

    return RSSnapshot(
        state=state,
        direction=direction,
        stock_vs_spy=round(stock_vs_spy, 2),
        spy_change_pct=round(spy, 2) if spy_change_pct is not None else None,
        multiplier=round(mult, 3),
        label=_format_label(state, stock_vs_spy, direction),
        reason=_format_reason(
            state, stock_vs_spy, spy_change_pct, direction, counter_tape,
            regime, profile,
        ),
    )


def get_multiplier(
    stock_vs_spy: float | None,
    spy_change_pct: float | None,
    direction: str,
    regime: str | None = None,
    strategy_name: str | None = None,
) -> tuple[float, dict]:
    """Return ``(multiplier, metadata)`` for composition in ``autotrade.py``.

    Mirrors the ``(float, dict)`` signature of the regime / news /
    brier multiplier helpers so the autotrade composition block can
    slot RS in at one line without special-casing it. ``regime`` is
    forwarded to ``classify`` for the neutral-in-trend coupling, and
    ``strategy_name`` selects the per-strategy profile.
    """
    snap = classify(
        stock_vs_spy, spy_change_pct, direction,
        regime=regime, strategy_name=strategy_name,
    )
    return snap.multiplier, snapshot_dict(snap)


def snapshot_dict(snap: RSSnapshot) -> dict:
    """JSON-safe dict for websockets / UI / logging."""
    return {
        "state":           snap.state,
        "direction":       snap.direction,
        "stock_vs_spy":    snap.stock_vs_spy,
        "spy_change_pct":  snap.spy_change_pct,
        "multiplier":      snap.multiplier,
        "label":           snap.label,
        "reason":          snap.reason,
    }


# ── Label / reason formatters ────────────────────────────────────────────────

_STATE_TAG = {
    "strong_leader":  "Strong Leader",
    "leader":         "Leader",
    "mild_leader":    "Mild Leader",
    "neutral":        "Neutral vs SPY",
    "mild_lagger":    "Mild Lagger",
    "lagger":         "Lagger",
    "strong_lagger":  "Strong Lagger",
    "unknown":        "RS unknown",
}


def _format_label(state: RSState, stock_vs_spy: float | None, direction: str) -> str:
    tag = _STATE_TAG.get(state, "RS unknown")
    if stock_vs_spy is None:
        return tag
    sign = "+" if stock_vs_spy >= 0 else ""
    return f"{tag} · {sign}{stock_vs_spy:.1f}% vs SPY"


def _format_reason(
    state: RSState,
    stock_vs_spy: float | None,
    spy_change_pct: float | None,
    direction: str,
    counter_tape: bool,
    regime: str | None = None,
    profile: RSProfile | None = None,
) -> str:
    if stock_vs_spy is None:
        return "RS unknown — no SPY / sector data for this trade."
    sign = "+" if stock_vs_spy >= 0 else ""
    spy_part = (
        f"SPY {('+' if (spy_change_pct or 0) >= 0 else '')}"
        f"{(spy_change_pct or 0):.1f}%"
        if spy_change_pct is not None else "SPY flat"
    )
    dir_part = "long" if direction == "long" else "short"
    tape_note = (
        " (genuine leadership — tape not confirming)"
        if state in ("strong_leader", "leader") and counter_tape
        else ""
    )

    # Prefer the per-strategy profile note when the neutral bucket was
    # overridden explicitly; fall back to the regime coupling note only
    # when the profile didn't provide one.
    prof = profile or RSProfile()
    if state == "neutral" and prof.neutral_mult is not None:
        neutral_note = (
            f" (per-strategy neutral override {prof.neutral_mult:.2f} — "
            "dead-bucket tax from v5 post-hoc)"
        )
    elif state == "neutral" and regime in ("trending_up", "trending_down"):
        neutral_note = (
            f" (neutral in {regime.replace('_', ' ')} tape — "
            "damped for failure to confirm)"
        )
    else:
        neutral_note = ""

    semantics_note = (
        " [mean-revert RS: lagging stock = fade leader]"
        if prof.semantics == "mean_revert"
        else ""
    )

    return (
        f"{_STATE_TAG.get(state, state)}: stock {sign}{stock_vs_spy:.2f} pp "
        f"vs SPY since open, {spy_part}, trade direction {dir_part}."
        f"{tape_note}{neutral_note}{semantics_note}"
    )
