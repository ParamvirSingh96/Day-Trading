"""
hour_weights.py — Hour-of-day multiplier (DISABLED).
====================================================

This module used to apply a per-strategy hour-of-day soft multiplier
that boosted strategies during their "edge" hours and damped them
during their "drag" hours (e.g. lunch chop). It was removed on
2026-04-25 at the user's request — every strategy now passes through
at 1.0 regardless of the entry hour.

The module is intentionally kept (rather than deleted) because the
multiplier is composed into the confidence stack in ``autotrade.py``
and into the TradeLog persistence schema. A no-op shim here keeps
those call sites working without ripping plumbing out of seven other
files. If you want to bring time-of-day weighting back, repopulate
``_HOUR_WEIGHTS`` below — the public API is unchanged.

Public API (preserved):
  * ``get_hour_weight(strategy_name, hour_et) -> float`` — always 1.0.
  * ``get_multiplier(aligned_strategies, hour_et) -> (float, dict)`` —
    always (1.0, {…label/per_strategy metadata…}).

The metadata dict still surfaces ``hour_et`` and a ``label`` so the
approval-card UI can render an informational "Hour 11 ET" chip; the
chip just always reads ×1.00 now.
"""

from __future__ import annotations

from typing import Optional


# Empty by design — every strategy/hour resolves to NEUTRAL_HOUR_MULT.
# Repopulate this dict to re-enable the multiplier.
_HOUR_WEIGHTS: dict[str, dict[int, float]] = {}


# ── Clamp + threshold constants ──────────────────────────────────────────────

MIN_HOUR_MULT  = 0.80
MAX_HOUR_MULT  = 1.20
NEUTRAL_HOUR_MULT = 1.00

# Kept for back-compat with any caller that imports it. The "suppressed"
# flag in the metadata dict is now always False because every weight
# is 1.0.
SUPPRESSION_THRESHOLD = 0.90


# ── Public API ───────────────────────────────────────────────────────────────

def get_hour_weight(strategy_name: str, hour_et: int) -> float:
    """Return the hour-of-day multiplier for (strategy, hour).

    Always returns ``NEUTRAL_HOUR_MULT`` (1.0) — the time-of-day
    weighting system is disabled. See the module docstring.
    """
    return NEUTRAL_HOUR_MULT


def get_multiplier(
    aligned_strategies: list[dict],
    hour_et: Optional[int],
) -> tuple[float, dict]:
    """Return ``(1.0, metadata)`` for the aligned matches at this hour.

    Mirrors the original signature so the autotrade composition stack
    keeps working unchanged. The multiplier is always 1.0; the metadata
    dict still carries the hour label + a per-strategy breakdown so the
    UI chip can render an honest "Hour XX ET · ×1.00" badge.
    """
    if hour_et is None or not aligned_strategies:
        return NEUTRAL_HOUR_MULT, {
            "hour_et":      hour_et,
            "label":        "Hour n/a" if hour_et is None else f"Hour {hour_et:02d} ET",
            "multiplier":   NEUTRAL_HOUR_MULT,
            "per_strategy": [],
        }

    per_strategy: list[dict] = []
    for m in aligned_strategies:
        name = m.get("name") or ""
        if not name:
            continue
        per_strategy.append({
            "strategy":   name,
            "weight":     NEUTRAL_HOUR_MULT,
            "suppressed": False,
        })

    return NEUTRAL_HOUR_MULT, {
        "hour_et":      hour_et,
        "label":        f"Hour {hour_et:02d} ET",
        "multiplier":   NEUTRAL_HOUR_MULT,
        "per_strategy": per_strategy,
    }
