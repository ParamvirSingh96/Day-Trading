"""
smoke_rth_split.py — Verify the RTH-only indicator + PM liquidity fix.
======================================================================

Synthetic-bar smoke test for the 2026-04-24 RTH-anchoring change.
Constructs a bar series that spans pre-market + RTH with deliberately
distinct high/low ranges, then asserts:

  * compute_indicators() reports session_high/low from the RTH slice
    only (not the wider PM wicks).
  * compute_ict_features() publishes ``pmh`` / ``pml`` in
    ict_zones.liquidity as the pre-market range.
  * ict_dealing_range is the RTH range (inherits from session H/L).
  * The pre-open edge case (no RTH bars yet) falls back cleanly.

Run from Backend/:
    venv/bin/python -m scripts.smoke_rth_split
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.analyzer import compute_indicators
from app.ict import compute_ict_features


_ET = ZoneInfo("America/New_York")


def _make_bar(ts: int, price: float, volume: int = 10_000) -> dict:
    """One-minute bar with a flat body at ``price``. Variants override
    high/low via the caller-provided price; ``_inject_wick`` mutates a
    specific bar to carry the deliberate wick used by the assertion."""
    return {
        "time":   ts,
        "open":   price,
        "high":   price,
        "low":    price,
        "close":  price,
        "volume": volume,
    }


def _inject_wick(bar: dict, high: float | None = None, low: float | None = None) -> dict:
    if high is not None:
        bar["high"] = high
    if low is not None:
        bar["low"] = low
    return bar


def _unix(dt_et: datetime) -> int:
    """Convert a naive ET datetime to a unix timestamp."""
    return int(dt_et.replace(tzinfo=_ET).timestamp())


def _build_frame(
    date: datetime,
    pm_start_hour: int = 4,
    rth_end_hour: int = 10,   # partial RTH — stops at 10:00 ET
    pm_wick_high: float = 105.0,
    pm_wick_low:  float = 99.0,
    rth_wick_high: float = 103.0,
    rth_wick_low:  float = 101.0,
    base_price:    float = 102.0,
) -> list[dict]:
    """Build a minute-bar frame from ``pm_start_hour`` ET until
    ``rth_end_hour`` ET. Injects a PM wick (105/99) around 06:00 and a
    distinct RTH wick (103/101) around 09:45.
    """
    bars: list[dict] = []
    cur = date.replace(hour=pm_start_hour, minute=0, second=0, microsecond=0)
    end = date.replace(hour=rth_end_hour,  minute=0, second=0, microsecond=0)
    while cur < end:
        bars.append(_make_bar(_unix(cur), base_price))
        cur += timedelta(minutes=1)

    # Inject wicks at specific minutes (find by ET clock time).
    def _at(h: int, m: int) -> int:
        return _unix(date.replace(hour=h, minute=m, second=0, microsecond=0))

    for b in bars:
        if b["time"] == _at(6, 0):
            _inject_wick(b, high=pm_wick_high)
        if b["time"] == _at(6, 30):
            _inject_wick(b, low=pm_wick_low)
        if b["time"] == _at(9, 45):
            _inject_wick(b, high=rth_wick_high)
        if b["time"] == _at(9, 50):
            _inject_wick(b, low=rth_wick_low)
    return bars


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"SMOKE FAIL: {msg}")
    print(f"  ok  {msg}")


def main() -> int:
    # Use a fixed weekday in 2026 so the test is deterministic.
    test_day = datetime(2026, 4, 23, tzinfo=None)  # a Thursday

    # ── Scenario A: pre-market + partial RTH ────────────────────────────
    print("Scenario A: PM 04:00–09:29 + RTH 09:30–10:00, distinct wicks")
    bars = _build_frame(
        test_day,
        pm_start_hour=4,
        rth_end_hour=10,
        pm_wick_high=105.0,
        pm_wick_low=99.0,
        rth_wick_high=103.0,
        rth_wick_low=101.0,
        base_price=102.0,
    )
    ind = compute_indicators(bars)
    ict = compute_ict_features(bars)

    # session_high/low must be from RTH slice only
    _assert(ind["session_high"] == 103.0,
            f"indicators.session_high == 103.0 (got {ind['session_high']})")
    _assert(ind["session_low"] == 101.0,
            f"indicators.session_low  == 101.0 (got {ind['session_low']})")

    # ICT liquidity dict should carry pmh/pml and RTH session_high/low
    liq = (ict.get("ict_zones") or {}).get("liquidity") or {}
    _assert(liq.get("pmh") == 105.0, f"liquidity.pmh == 105.0 (got {liq.get('pmh')})")
    _assert(liq.get("pml") ==  99.0, f"liquidity.pml ==  99.0 (got {liq.get('pml')})")
    _assert(liq.get("session_high") == 103.0,
            f"liquidity.session_high == 103.0 (got {liq.get('session_high')})")
    _assert(liq.get("session_low") == 101.0,
            f"liquidity.session_low  == 101.0 (got {liq.get('session_low')})")

    # Dealing range inherits RTH session H/L
    dr = ict.get("ict_dealing_range") or {}
    _assert(dr.get("high") == 103.0, f"dealing_range.high == 103.0 (got {dr.get('high')})")
    _assert(dr.get("low")  == 101.0, f"dealing_range.low  == 101.0 (got {dr.get('low')})")

    # ── Scenario B: pre-open only (no RTH bars yet) ─────────────────────
    print("\nScenario B: PM 04:00–08:00 only, no RTH bars — fallback path")
    pm_only = _build_frame(
        test_day,
        pm_start_hour=4,
        rth_end_hour=8,
        pm_wick_high=105.0,
        pm_wick_low=99.0,
        rth_wick_high=0.0,  # doesn't matter — no RTH bars
        rth_wick_low=0.0,
        base_price=102.0,
    )
    ind2 = compute_indicators(pm_only)
    ict2 = compute_ict_features(pm_only)

    # Fallback: session H/L uses the full (PM) frame since RTH slice is empty
    _assert(ind2["session_high"] == 105.0,
            f"fallback session_high == 105.0 (got {ind2['session_high']})")
    _assert(ind2["session_low"] == 99.0,
            f"fallback session_low  ==  99.0 (got {ind2['session_low']})")
    liq2 = (ict2.get("ict_zones") or {}).get("liquidity") or {}
    _assert(liq2.get("pmh") == 105.0,
            f"pre-open liquidity.pmh == 105.0 (got {liq2.get('pmh')})")
    _assert(liq2.get("pml") == 99.0,
            f"pre-open liquidity.pml ==  99.0 (got {liq2.get('pml')})")

    # ── Scenario C: RTH only (no pre-market bars) ───────────────────────
    print("\nScenario C: RTH 09:30–15:00, no pre-market — no pmh/pml keys")
    rth_only_day = datetime(2026, 4, 23)
    rth_only_bars: list[dict] = []
    cur = rth_only_day.replace(hour=9, minute=30)
    end = rth_only_day.replace(hour=15, minute=0)
    while cur < end:
        rth_only_bars.append(_make_bar(_unix(cur), 102.0))
        cur += timedelta(minutes=1)
    # Inject a single RTH wick at 10:00
    _inject_wick(rth_only_bars[30], high=104.0)
    _inject_wick(rth_only_bars[60], low=100.5)

    ind3 = compute_indicators(rth_only_bars)
    ict3 = compute_ict_features(rth_only_bars)
    _assert(ind3["session_high"] == 104.0,
            f"rth-only session_high == 104.0 (got {ind3['session_high']})")
    _assert(ind3["session_low"]  == 100.5,
            f"rth-only session_low  == 100.5 (got {ind3['session_low']})")
    liq3 = (ict3.get("ict_zones") or {}).get("liquidity") or {}
    _assert("pmh" not in liq3, "rth-only: no pmh key")
    _assert("pml" not in liq3, "rth-only: no pml key")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
