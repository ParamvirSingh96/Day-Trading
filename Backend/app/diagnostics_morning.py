"""
Setup Watch — read-only diagnostics for the live scanner (v7.15a, 2026-05-04)
=============================================================================
Pure-function module that classifies WHY a symbol isn't producing a
live-PLAYBOOK match this scan, even when it has a real catalyst.
Never reads or writes the predicate path; never feeds autotrade;
never logs trades. The output is purely a UI affordance — gives the
operator visibility on no-trade mornings ("QCOM: gap skipped prior
high, no sweep possible") instead of staring at "Setups: 0".

Design principles (per the v7.15a constraint):
  * The 7 live PLAYBOOK predicates are FROZEN. Nothing here modifies
    them, computes new ones, or relaxes any threshold.
  * Diagnostics are derived from the SAME ``ind`` dict that
    ``compute_indicators`` already produces during the scan cycle.
    No additional Alpaca calls. No additional bar processing.
  * Categories are descriptive labels, not trading signals. They
    surface in the dashboard's "Setup Watch" panel; they do NOT
    create ``SimulatedTrade`` rows or any other persistence.
  * Pure function: same ``ind`` in → same ``SymbolDiagnostic`` out.
    No side effects. Cheap (<1ms per symbol).

Diagnostic categories (the ones the operator asked for, mapped to
checks the existing ``ind`` dict supports without new computation):

  level_skipped_by_gap     — gap > 1.5% AND price gapped THROUGH the
                             prior-day extreme (no intraday sweep
                             possible because the gap ate the level)
  asian_range_intact       — asian high/low both known AND price
                             still inside the range AND no recent
                             sweep of either edge
  no_sweep_yet             — no ict_last_sweep within recent bars
  swept_no_mss             — sweep present but MSS missing (or MSS
                             too old / direction-mismatched)
  setup_forming            — broad sweep + MSS alignment, waiting for
                             predicate-specific context and/or FVG
                             retest. Close structurally, but NOT a
                             live trade recommendation by itself.
  extended_from_vwap       — price > 1.5 ATR from VWAP. Mean-
                             reversion window 11:00–13:00 may produce
                             a setup if price rotates back.
  outside_killzone         — not currently in an ICT killzone window
                             (predicates with killzone gating won't
                             fire regardless of structure).
  low_volume               — vol_ratio < 1.0 (volume confirmation
                             gates won't pass).
  trend_day_candidate      — gap > 2% AND price extended further in
                             same direction. Trend-day character;
                             current sweep-based scanner is unlikely
                             to fire all day.

A symbol can carry MULTIPLE codes. We surface them sorted by
"how close to a live match" — ``setup_forming`` first, then
``swept_no_mss``, etc.
=============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Tunables (matched to existing scanner thresholds — refer ONLY) ────────
#
# These match thresholds the SCANNER already uses internally. Listed
# here as named constants for diagnostic clarity. We do NOT define
# new thresholds — all values are mirrors of what the scanner does.

# Gap that's "big enough" to plausibly skip a level. Mirrors the
# "≥ 1.5%" threshold the scanner uses for ``gap_pct`` reason chips.
_GAP_BIG_PCT = 1.5

# Sweep recency: how recent (in 1-min bars) a sweep must be to count
# as "still active" for a setup-forming check. Matches the lookback
# used by ICT predicates' ``_partial_check`` paths.
_SWEEP_RECENT_BARS = 30

# MSS recency: same idea for market-structure shifts.
_MSS_RECENT_BARS = 20

# Distance-from-VWAP threshold (in ATRs) at which we call a stock
# "extended" — sets up the mean-reversion-later state.
_EXTENDED_VWAP_ATRS = 1.5

# Volume-confirmation floor (RVOL vs. running session). Matches the
# ``vol_ratio >= 1.0`` floor that several ICT predicates check.
_LOW_VOLUME_THRESHOLD = 1.0

# Trend-day classification: a gap-and-go signature.
_TRENDY_GAP_PCT = 2.0
_TRENDY_VWAP_ATRS = 0.7   # price further-extended in gap direction


# Display priority — lower number = closer to a live match → render
# first in the UI. ``setup_forming`` is the most actionable (operator
# might want to watch the chart); ``trend_day_candidate`` is the
# least (informational only, scanner won't fire today).
_CODE_PRIORITY = {
    "setup_forming":          1,
    "swept_no_mss":           2,
    "extended_from_vwap":     3,
    "asian_range_intact":     4,
    "no_sweep_yet":           5,
    "low_volume":             6,
    "outside_killzone":       7,
    "level_skipped_by_gap":   8,
    "trend_day_candidate":    9,
}


# Human-readable descriptions surfaced as tooltips in the UI.
DIAGNOSTIC_LABELS: dict[str, str] = {
    "setup_forming":         "Broad sweep + MSS present — not predicate-ready yet",
    "swept_no_mss":          "Sweep happened, no MSS confirmation yet",
    "extended_from_vwap":    "Far from VWAP — mean-reversion window may produce setup later",
    "asian_range_intact":    "Price still inside the asian range — no sweep attempted",
    "no_sweep_yet":          "No recent sweep of any tracked level",
    "low_volume":            "Below volume confirmation threshold",
    "outside_killzone":      "Outside ICT killzone window",
    "level_skipped_by_gap":  "Gap skipped prior-day extreme — no sweep possible from intraday action",
    "trend_day_candidate":   "Persistent directional move — sweep-based scanner unlikely to fire today",
}


@dataclass
class SymbolDiagnostic:
    """Diagnostic state for one symbol on one scan cycle.

    Pure-data return type — never mutated, never carries references
    to scanner internals. JSON-serializable for the API endpoint.
    """
    symbol: str
    codes: list[str] = field(default_factory=list)
    # Brief human summary, e.g.,
    #   "Gap +5.7% past prior-day-high; no sweep possible yet"
    # Empty when codes are empty.
    summary: str = ""
    # Raw signals captured for the UI to render numerically.
    # All optional — None when not computable from this scan's ind.
    gap_pct: float | None = None
    distance_from_vwap_atr: float | None = None
    vol_ratio: float | None = None
    last_sweep_bars_ago: int | None = None
    last_mss_bars_ago: int | None = None
    in_killzone: bool | None = None
    # v7.15.5 (2026-05-06): proximity/readiness score. Counts how many
    # broad structural prerequisites pass for this symbol on this scan.
    # This is diagnostic only: a high percentage means "worth watching",
    # NOT "the live predicate should fire".
    #
    # Gates (each contributes 20% when it passes):
    #   1. Killzone        — currently in an ICT killzone window
    #   2. Recent sweep    — last_sweep within _SWEEP_RECENT_BARS
    #   3. MSS reversal    — recent MSS in OPPOSITE direction from sweep
    #   4. Volume          — vol_ratio >= _LOW_VOLUME_THRESHOLD
    #   5. FVG/OB touch    — price is touching the directionally relevant
    #                        fresh FVG/OB zone after MSS
    #
    # ``proximity_pct`` is None when ``ind`` was missing entirely
    # (early-return path) — distinguishing "we have no data" from
    # "we have data and everything failed". A symbol whose ind exists
    # but happens to fail all 4 gates returns proximity_pct=0.
    proximity_pct: int | None = None
    proximity_passed: int | None = None
    proximity_total: int = 5


# ── Public API ─────────────────────────────────────────────────────────────

def diagnose_symbol(symbol: str, ind: dict[str, Any] | None) -> SymbolDiagnostic:
    """Classify why ``symbol`` doesn't have a full live-PLAYBOOK match
    this scan. Pure function. Never raises on missing fields — an
    empty ``ind`` produces an empty diagnostic.
    """
    if not ind or not isinstance(ind, dict):
        return SymbolDiagnostic(symbol=symbol)

    codes: list[str] = []

    # ── Capture the raw signals first (used by both checks + UI) ──
    gap_pct = _safe_float(ind.get("gap_pct"))
    vwap = _safe_float(ind.get("vwap"))
    atr = _safe_float(ind.get("atr"))
    current = _safe_float(ind.get("current_price"))
    prev_high = _safe_float(ind.get("prev_high"))
    prev_low = _safe_float(ind.get("prev_low"))
    vol_ratio = _safe_float(ind.get("vol_ratio"))

    last_sweep = ind.get("ict_last_sweep")
    last_mss = ind.get("ict_last_mss")
    killzone_raw = ind.get("ict_killzone")
    levels = ind.get("ict_levels") or {}

    last_sweep_bars_ago = _bars_ago(last_sweep)
    last_mss_bars_ago = _bars_ago(last_mss)

    # ``ict_killzone`` shape (v7.15a-fix, 2026-05-05): the ICT module
    # returns this as a STRING (e.g., 'silver_bullet', 'london', 'NY_AM',
    # 'NY_PM') when in a killzone, or an empty string / None when not.
    # NOT a dict — that was a wrong assumption in the original v7.15a
    # implementation that crashed every diagnose_symbol call.
    # Defensive shape handling so any future schema drift (string ↔ dict)
    # doesn't crash the diagnostic pipeline.
    if isinstance(killzone_raw, dict):
        in_killzone: bool | None = bool(killzone_raw.get("in_window"))
    elif isinstance(killzone_raw, str):
        in_killzone = bool(killzone_raw.strip())
    elif killzone_raw is None:
        in_killzone = None   # unknown — don't flag outside_killzone
    else:
        in_killzone = None

    distance_atr: float | None = None
    if vwap is not None and atr is not None and current is not None and atr > 0:
        distance_atr = abs(current - vwap) / atr

    # ── Check 1: level_skipped_by_gap ──
    # A big overnight gap that took price past the prior-day extreme
    # means no intraday sweep can happen on that level today — the
    # gap ate it.
    if gap_pct is not None and abs(gap_pct) >= _GAP_BIG_PCT:
        if (
            (gap_pct > 0 and prev_high is not None and current is not None and current > prev_high)
            or
            (gap_pct < 0 and prev_low is not None and current is not None and current < prev_low)
        ):
            codes.append("level_skipped_by_gap")

    # ── Check 2: asian_range_intact ──
    # Asian-range extremes known and current price between them, no
    # sweep recorded yet → predicates that need a sweep of asian
    # high/low have nothing to bind to.
    asian_high = _safe_float(levels.get("asian_high")) if isinstance(levels, dict) else None
    asian_low = _safe_float(levels.get("asian_low")) if isinstance(levels, dict) else None
    if (
        asian_high is not None
        and asian_low is not None
        and current is not None
        and asian_low <= current <= asian_high
        and not _sweep_targets_asian(last_sweep)
    ):
        codes.append("asian_range_intact")

    # ── Check 3 + 4: sweep / MSS state ──
    if last_sweep_bars_ago is None or last_sweep_bars_ago > _SWEEP_RECENT_BARS:
        codes.append("no_sweep_yet")
    else:
        # We have a recent sweep. Where are we structurally?
        sweep_dir = _sweep_direction(last_sweep)
        mss_dir = _mss_direction(last_mss)
        mss_recent = (
            last_mss_bars_ago is not None
            and last_mss_bars_ago <= _MSS_RECENT_BARS
        )
        # Setup-forming: sweep direction matched by an MSS in the
        # opposite direction (the structural reversal Judas / Turtle
        # Soup are looking for).
        if mss_recent and sweep_dir and mss_dir and sweep_dir != mss_dir:
            codes.append("setup_forming")
        else:
            codes.append("swept_no_mss")

    # ── Check 5: extended_from_vwap (mean-reversion candidate) ──
    if distance_atr is not None and distance_atr >= _EXTENDED_VWAP_ATRS:
        codes.append("extended_from_vwap")

    # ── Check 6: outside_killzone ──
    # Only fires when we definitively know we're outside a killzone
    # (in_killzone is False, not None). When killzone state is
    # unknown (no ind data, malformed feed), we suppress the code so
    # we don't surface a misleading "outside killzone" tag on data-
    # quality issues.
    if in_killzone is False:
        codes.append("outside_killzone")

    # ── Check 7: low_volume ──
    if vol_ratio is not None and vol_ratio < _LOW_VOLUME_THRESHOLD:
        codes.append("low_volume")

    # ── Check 8: trend_day_candidate ──
    # Gap >= 2% AND price is extended FURTHER in the same direction
    # (i.e., not pulling back) AND distance > 0.7 ATR. Combined with
    # a "no sweep yet" environment this is a strong tell that the
    # current scanner family won't fire today.
    if (
        gap_pct is not None and abs(gap_pct) >= _TRENDY_GAP_PCT
        and distance_atr is not None and distance_atr >= _TRENDY_VWAP_ATRS
        and current is not None and vwap is not None
    ):
        # Direction confirmation: positive gap → close > vwap;
        # negative gap → close < vwap. (If price has reversed we
        # don't tag trend_day_candidate — that's mean-reversion turf.)
        if (gap_pct > 0 and current > vwap) or (gap_pct < 0 and current < vwap):
            codes.append("trend_day_candidate")

    # ── Sort by display priority + dedupe (defensive) ──
    seen: set[str] = set()
    sorted_codes: list[str] = []
    for c in sorted(codes, key=lambda x: _CODE_PRIORITY.get(x, 999)):
        if c not in seen:
            seen.add(c)
            sorted_codes.append(c)

    summary = _build_summary(sorted_codes, gap_pct, distance_atr)

    # ── Proximity/readiness score (v7.15.5 → v7.15.6) ──
    # Broad structural prerequisites the live predicates check. Count
    # passes; the percentage is round((passed / 5) * 100). This is still
    # intentionally weaker than PLAYBOOK.matches(): it does not verify
    # exact strategy-specific level names, PD-zone context, hour-blocks,
    # blacklists, or OR reclaim/breakdown. It is a watchlist diagnostic,
    # not a trade signal.
    sweep_dir_for_score = _sweep_direction(last_sweep)
    mss_dir_for_score = _mss_direction(last_mss)

    gate_killzone = in_killzone is True
    gate_sweep = (
        last_sweep_bars_ago is not None
        and last_sweep_bars_ago <= _SWEEP_RECENT_BARS
    )
    gate_mss_reversal = (
        last_mss_bars_ago is not None
        and last_mss_bars_ago <= _MSS_RECENT_BARS
        and sweep_dir_for_score is not None
        and mss_dir_for_score is not None
        and sweep_dir_for_score != mss_dir_for_score
    )
    gate_volume = (
        vol_ratio is not None and vol_ratio >= _LOW_VOLUME_THRESHOLD
    )
    gate_entry_zone = False
    if gate_mss_reversal:
        if mss_dir_for_score == "bull":
            gate_entry_zone = bool(ind.get("ict_in_bull_zone"))
        elif mss_dir_for_score == "bear":
            gate_entry_zone = bool(ind.get("ict_in_bear_zone"))
    proximity_passed = (
        int(gate_killzone)
        + int(gate_sweep)
        + int(gate_mss_reversal)
        + int(gate_volume)
        + int(gate_entry_zone)
    )
    proximity_total = 5
    proximity_pct = int(round((proximity_passed / proximity_total) * 100))

    return SymbolDiagnostic(
        symbol=symbol,
        codes=sorted_codes,
        summary=summary,
        gap_pct=gap_pct,
        distance_from_vwap_atr=round(distance_atr, 2) if distance_atr is not None else None,
        vol_ratio=vol_ratio,
        last_sweep_bars_ago=last_sweep_bars_ago,
        last_mss_bars_ago=last_mss_bars_ago,
        in_killzone=in_killzone,
        proximity_pct=proximity_pct,
        proximity_passed=proximity_passed,
        proximity_total=proximity_total,
    )


# ── Internal helpers ──────────────────────────────────────────────────────

def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bars_ago(event: Any) -> int | None:
    """Pull ``bars_ago`` (or equivalent) from a sweep / MSS event dict.
    Returns None when the event is missing or doesn't carry a
    bars-ago field.
    """
    if not isinstance(event, dict):
        return None
    for key in ("bars_ago", "age_bars", "minutes_ago"):
        v = event.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _sweep_direction(event: Any) -> str | None:
    """Return ``"bull"`` / ``"bear"`` from a sweep dict, or None.

    The ICT sweep events carry a ``direction`` field (``"bullish"``
    or ``"bearish"``). We normalize to the short forms so MSS-vs-
    sweep direction comparison is trivial.
    """
    if not isinstance(event, dict):
        return None
    d = (event.get("direction") or "").strip().lower()
    if d in ("bullish", "bull"):
        return "bull"
    if d in ("bearish", "bear"):
        return "bear"
    return None


def _mss_direction(event: Any) -> str | None:
    """Same shape as _sweep_direction, on the MSS event dict."""
    return _sweep_direction(event)


def _sweep_targets_asian(event: Any) -> bool:
    """True iff the latest sweep targeted asian_high or asian_low."""
    if not isinstance(event, dict):
        return False
    name = (event.get("level_name") or "").lower()
    return name in ("asian_high", "asian_low")


def _build_summary(
    codes: list[str],
    gap_pct: float | None,
    distance_atr: float | None,
) -> str:
    """Compose a one-line operator summary from the highest-priority
    code + the most-relevant numerical signal.

    Empty codes → empty summary. The UI uses non-empty summaries to
    decide whether to surface a row (a symbol with no codes is just
    "structurally quiet" — not worth a row).
    """
    if not codes:
        return ""
    head = codes[0]
    label = DIAGNOSTIC_LABELS.get(head, head)

    # Append the numerical context that's most relevant to the
    # head code. Keeps the summary compact + scannable.
    if head == "level_skipped_by_gap" and gap_pct is not None:
        return f"{label} (gap {gap_pct:+.1f}%)"
    if head == "trend_day_candidate" and gap_pct is not None:
        return f"{label} (gap {gap_pct:+.1f}%)"
    if head == "extended_from_vwap" and distance_atr is not None:
        return f"{label} ({distance_atr:.1f}× ATR from VWAP)"
    return label
