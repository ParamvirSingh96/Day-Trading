"""Smoke test for the v7 ICT playbook additions (2026-04-24).

Verifies four things:
  1. The stack imports cleanly with the 7-entry live PLAYBOOK
     (count history: 13 → 11 (v7.6 BB L/S suspended) → 9 (v7.7 ARB
     L/S suspended) → 7 (v7.9 MD-PD Short and Sweep+MSS Short
     suspended). ``ICT Silver Bullet (Long)`` was also SUSPENDED in
     v7.3 — see strategies.py module docstring for the full chain).
  2. compute_ict_features() populates every v7 key without blowing up.
  3. Each v7 predicate's structural skeleton can be nudged into a True
     read by a carefully constructed synthetic ``ind`` dict (this is
     NOT a backtest — it's a "predicate wiring works" check). The
     suspended predicates (BB, ARB, MD-PD Short, Sweep+MSS Short,
     Silver Bullet Long) remain importable for ``playbook_override``-
     driven research and are still exercised by these wiring checks
     even though the named ``Strategy(...)`` blocks are gone.
  4. The new v7.3 win-rate fixes (volume floor, sweep-magnitude floor,
     hard regime veto) actually gate the predicates / autotrade logic
     when their thresholds aren't met.

We also round-trip the regime + hour-weight tables to make sure the
strategy-name keys match exactly (a typo here would silently apply 1.0
everywhere). Suspended-strategy regime-weight rows are intentionally
preserved (consistent with the BB / ARB pattern) so re-promoting a
predicate is a one-line ``Strategy(...)`` re-insertion, not a regime-
table edit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.strategies import CONTEXT_PLAYBOOK, PLAYBOOK, context_screen, screen
from app.ict import compute_ict_features
from app.ict.detectors import killzone, KILLZONES
from app.regime import get_weight, ALL_REGIMES, is_regime_vetoed
from app.hour_weights import get_hour_weight

_ET = ZoneInfo("America/New_York")


def _ts(y, m, d, h, mi) -> int:
    """Build a unix-seconds timestamp from ET components."""
    return int(datetime(y, m, d, h, mi, tzinfo=_ET).timestamp())


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    assert cond, label


# ── 1. Structural checks ────────────────────────────────────────────────────

print("== PLAYBOOK structure ==")
names = [s.name for s in PLAYBOOK]
check(f"PLAYBOOK has 7 strategies (got {len(PLAYBOOK)})", len(PLAYBOOK) == 7)
for nm in [
    "ICT Judas Swing (Long)",
    "ICT Judas Swing (Short)",
    "ICT Turtle Soup (Long)",
    "ICT Turtle Soup (Short)",
    "ICT Power of 3 (Long)",
    "ICT Power of 3 (Short)",
    "ICT Multi-Day PD (Long)",
]:
    check(f"PLAYBOOK contains {nm!r}", nm in names)

# ``ICT Sweep + MSS (Long)`` was deleted 2026-04-25 (afternoon) as a
# duplicate of Silver Bullet. Lock that in so a future re-add has to
# pass through this assertion.
check("ICT Sweep + MSS (Long) NOT in PLAYBOOK (deduped into Silver Bullet)",
      "ICT Sweep + MSS (Long)" not in names)

# ``ICT Silver Bullet (Long)`` was SUSPENDED 2026-04-25 (evening) pending
# investigation. Lock the suspension in so a careless re-add doesn't go
# unnoticed during code review.
check("ICT Silver Bullet (Long) NOT in PLAYBOOK (suspended pending review)",
      "ICT Silver Bullet (Long)" not in names)

# v7.6 (2026-04-26 evening): Breaker Block (L/S) suspended. Predicates
# remain callable for playbook_override research. Lock the suspension.
check("ICT Breaker Block (Long) NOT in PLAYBOOK (v7.6 suspended)",
      "ICT Breaker Block (Long)" not in names)
check("ICT Breaker Block (Short) NOT in PLAYBOOK (v7.6 suspended)",
      "ICT Breaker Block (Short)" not in names)

# v7.7 (2026-04-27): Asian-Range Breakout (L/S) suspended after the
# factorial-experiment round failed to clear the (a)-(e) gates.
check("ICT Asian-Range Breakout (Long) NOT in PLAYBOOK (v7.7 suspended)",
      "ICT Asian-Range Breakout (Long)" not in names)
check("ICT Asian-Range Breakout (Short) NOT in PLAYBOOK (v7.7 suspended)",
      "ICT Asian-Range Breakout (Short)" not in names)

# v7.9 (2026-04-28): Multi-Day PD (Short) and Sweep+MSS (Short)
# suspended after the post-cooldown-fix 18-month re-baseline showed a
# stable counter-edge in both walk-forward halves on each. Long-side
# variants stay in the live book.
check("ICT Multi-Day PD (Short) NOT in PLAYBOOK (v7.9 suspended)",
      "ICT Multi-Day PD (Short)" not in names)
check("ICT Sweep + MSS (Short) NOT in PLAYBOOK (v7.9 suspended)",
      "ICT Sweep + MSS (Short)" not in names)

# SMT explicitly NOT in PLAYBOOK (deferred).
check("SMT Divergence NOT in PLAYBOOK (deferred)",
      not any("SMT" in n for n in names))

context_names = [s.name for s in CONTEXT_PLAYBOOK]
check(f"CONTEXT_PLAYBOOK has 10 strategies (got {len(CONTEXT_PLAYBOOK)})",
      len(CONTEXT_PLAYBOOK) == 10)
for nm in [
    "ICT Sweep + MSS (Short)",
    "ICT Multi-Day PD (Short)",
    "ICT Breaker Block (Long)",
    "ICT Breaker Block (Short)",
    "ICT Asian-Range Breakout (Long)",
    "ICT Asian-Range Breakout (Short)",
    "ICT Failed-ARB (Long)",
    "ICT Failed-ARB (Short)",
    "ICT Retest-ARB (Long)",
    "ICT Retest-ARB (Short)",
]:
    check(f"CONTEXT_PLAYBOOK contains {nm!r}", nm in context_names)


# ── 2. Killzones ────────────────────────────────────────────────────────────

print("\n== Killzones ==")
check("London killzone registered", "london" in KILLZONES)
# Pick unambiguous timestamps inside each window
check("03:00 ET → 'london'",
      killzone(_ts(2026, 4, 22, 3, 0)) == "london")
check("09:45 ET → 'ny_am'",
      killzone(_ts(2026, 4, 22, 9, 45)) == "ny_am")
check("10:30 ET → 'silver_bullet'",
      killzone(_ts(2026, 4, 22, 10, 30)) == "silver_bullet")
check("14:00 ET → 'ny_pm'",
      killzone(_ts(2026, 4, 22, 14, 0)) == "ny_pm")
check("06:30 ET → None (outside all)",
      killzone(_ts(2026, 4, 22, 6, 30)) is None)


# ── 3. compute_ict_features populates v7 keys ─────────────────────────────

print("\n== compute_ict_features populates v7 keys ==")

# Build a mixed Asian + PM + RTH bar frame. We just need enough bars
# for the feature builder not to early-out (>= 15) and bars across
# multiple ET-time windows. Prices are monotone-up to keep it simple.
bars = []
# Previous day 18:00-23:59 ET (Asian start) — 6 bars
for i in range(6):
    bars.append({
        "time": _ts(2026, 4, 21, 18 + i, 0),
        "open": 100 + i * 0.1, "high": 100 + i * 0.1 + 0.3,
        "low": 100 + i * 0.1 - 0.2, "close": 100 + i * 0.1 + 0.1,
        "volume": 1000,
    })
# Overnight 00:00-03:59 — 4 bars (still in Asian window)
for i in range(4):
    bars.append({
        "time": _ts(2026, 4, 22, i, 0),
        "open": 100.5 + i * 0.1, "high": 100.5 + i * 0.1 + 0.2,
        "low": 100.5 + i * 0.1 - 0.2, "close": 100.5 + i * 0.1,
        "volume": 800,
    })
# Pre-market 04:00-09:29 — 6 bars
for i in range(6):
    bars.append({
        "time": _ts(2026, 4, 22, 4 + i, 0),
        "open": 101 + i * 0.1, "high": 101 + i * 0.1 + 0.3,
        "low": 101 + i * 0.1 - 0.2, "close": 101 + i * 0.1 + 0.1,
        "volume": 1500,
    })
# RTH 09:30-10:15 — 46 one-minute bars
for i in range(46):
    h = 9 if (30 + i) < 60 else 10
    mi = (30 + i) % 60
    price = 102 + i * 0.05
    bars.append({
        "time": _ts(2026, 4, 22, h, mi),
        "open": price, "high": price + 0.2,
        "low": price - 0.15, "close": price + 0.1,
        "volume": 3000,
    })

daily_ctx = {
    "prev_high": 102.5,
    "prev_low":  99.5,
    "prev_close": 101.0,
    "20d_high": 110.0,
    "20d_low":  95.0,
}

feat = compute_ict_features(bars, daily_ctx=daily_ctx)

# v7 new keys present
for k in [
    "ict_rth_minute",
    "ict_multi_day_range",
    "ict_multi_day_pd_zone",
    "ict_opening_range",
    "ict_in_bull_breaker",
    "ict_in_bear_breaker",
    "ict_nearest_bull_breaker",
    "ict_nearest_bear_breaker",
    "ict_levels",
]:
    check(f"feature dict has {k!r}", k in feat)

# Levels surface
levels = feat["ict_levels"] or {}
check("levels includes pmh (pre-market H)", "pmh" in levels)
check("levels includes pml (pre-market L)", "pml" in levels)
check("levels includes asian_high (overnight H)", "asian_high" in levels)
check("levels includes asian_low (overnight L)", "asian_low" in levels)
check("levels includes opening_high (09:30-09:45 H)", "opening_high" in levels)
check("levels includes opening_low (09:30-09:45 L)", "opening_low" in levels)
check("levels includes d20_high", "d20_high" in levels)
check("levels includes d20_low", "d20_low" in levels)

# Opening range shape
op = feat["ict_opening_range"]
check("opening_range is a dict with low<high",
      op is not None and op["low"] < op["high"])

# Multi-day range derived from daily_ctx
mdr = feat["ict_multi_day_range"]
check("multi_day_range derived from daily_ctx 20d", mdr is not None and mdr["source"] == "daily_20d")

# RTH minute resolvable on the last bar (10:15 ET → 45 minutes into RTH)
check("rth_minute = 45 for 10:15 ET last bar",
      feat["ict_rth_minute"] == 45)


# ── 4. Each v7 predicate fires on a crafted ind dict ───────────────────────

print("\n== v7 predicates fire on structured synthetic inputs ==")

from app.strategies import (
    _ict_judas_swing_long, _ict_judas_swing_short,
    _ict_turtle_soup_long, _ict_turtle_soup_short,
    _ict_asian_range_breakout_long, _ict_asian_range_breakout_short,
    _ict_power_of_3_long, _ict_power_of_3_short,
    _ict_breaker_block_long, _ict_breaker_block_short,
    _ict_multi_day_pd_long, _ict_multi_day_pd_short,
    _ict_smt_not_implemented,
    _MIN_VOL_RATIO, _MIN_SWEEP_ATR_MULT,
)

# v7.3 (2026-04-25 evening) — every ICT predicate now requires
# ``vol_ratio >= 0.8`` and ``atr > 0`` with sweep magnitude (wick
# excursion past level) ≥ 0.25× ATR. Synthetic ind dicts now embed
# the v7.3 baseline; each per-strategy dict spreads on top of it so
# the structural assertions read identical to v7.0/v7.1/v7.2 except
# for the gate values surfaced at the top.

# Baseline v7.3 floor: comfortably above the 0.8 vol gate, ATR set
# so a 0.5-magnitude wick (default in the sweep helper) is just over
# the 0.25× ATR sweep-magnitude floor.
_BASE_IND = {
    "vol_ratio": 1.2,   # > _MIN_VOL_RATIO (0.8)
    "atr":       1.0,   # 0.5-mag sweep / 1.0 ATR = 0.5 (> 0.25)
}

# Shared structural helpers
def sweep(name, bars_ago, direction, *, magnitude=0.5, level_price=100.0):
    """Build a synthetic SweepEvent.to_dict()-shaped record.

    The default ``magnitude=0.5`` is well above the 0.25× ATR floor
    when paired with the default ``_BASE_IND["atr"]=1.0``. Override
    ``magnitude`` to test the rejection path.
    """
    # wick_extreme is set such that abs(wick_extreme - level_price) == magnitude.
    # For bullish sweep we wick BELOW; for bearish we wick ABOVE — same
    # absolute distance either way for the magnitude calc.
    if direction == "bullish":
        wick_extreme = level_price - magnitude
    else:
        wick_extreme = level_price + magnitude
    return {
        "level_name":   name,
        "bars_ago":     bars_ago,
        "direction":    direction,
        "level_price":  level_price,
        "wick_extreme": wick_extreme,
        "magnitude":    magnitude,
    }

def mss(bars_ago, direction):
    return {"bars_ago": bars_ago, "direction": direction, "broken_swing_price": 100.0}

# Judas Long: first 15 min of RTH, bull sweep of pml, bull MSS, in bull zone
judas_long_ind = {
    **_BASE_IND,
    "ict_rth_minute": 10,
    "ict_last_bull_sweep": sweep("pml", 5, "bullish"),
    "ict_last_bull_mss":   mss(2, "bullish"),
    "ict_in_bull_zone": True,
}
check("Judas Long fires on structured ind",
      _ict_judas_swing_long(judas_long_ind))

# Judas Short
judas_short_ind = {
    **_BASE_IND,
    "ict_rth_minute": 5,
    "ict_last_bear_sweep": sweep("pmh", 4, "bearish"),
    "ict_last_bear_mss":   mss(1, "bearish"),
    "ict_in_bear_zone": True,
}
check("Judas Short fires on structured ind",
      _ict_judas_swing_short(judas_short_ind))

# Judas time-of-day gate (30-min open window) is enforced — the
# predicate REJECTS when rth_minute is past 30 or undefined. The gate
# was briefly removed 2026-04-25 and then restored the same day; the
# checks below lock the restored behavior in.
judas_late_in_session = {**judas_long_ind, "ict_rth_minute": 45}
check("Judas Long REJECTS when past 30-min open window",
      not _ict_judas_swing_long(judas_late_in_session))
judas_outside_rth = {**judas_long_ind, "ict_rth_minute": None}
check("Judas Long REJECTS when rth_minute is None (outside RTH)",
      not _ict_judas_swing_long(judas_outside_rth))

# Turtle Soup Long — specifically sweep level = pdl
ts_long_ind = {
    **_BASE_IND,
    "ict_last_bull_sweep": sweep("pdl", 5, "bullish"),
    "ict_last_bull_mss":   mss(2, "bullish"),
    "ict_pd_zone": "discount",
    "ict_in_bull_zone": True,
}
check("Turtle Soup Long fires",
      _ict_turtle_soup_long(ts_long_ind))
check("Turtle Soup Long REJECTS sweep of pml (must be pdl)",
      not _ict_turtle_soup_long({**ts_long_ind,
                                 "ict_last_bull_sweep": sweep("pml", 5, "bullish")}))

# Turtle Soup Short — sweep level = pdh
ts_short_ind = {
    **_BASE_IND,
    "ict_last_bear_sweep": sweep("pdh", 5, "bearish"),
    "ict_last_bear_mss":   mss(2, "bearish"),
    "ict_pd_zone": "premium",
    "ict_in_bear_zone": True,
}
check("Turtle Soup Short fires",
      _ict_turtle_soup_short(ts_short_ind))

# Asian Range Breakout Long
asian_long_ind = {
    **_BASE_IND,
    "ict_rth_minute": 20,
    "ict_levels": {"asian_high": 100.0, "asian_low": 98.0},
    "current_price": 100.5,
    "ict_last_bull_mss":   mss(5, "bullish"),
    "ict_last_bull_sweep": sweep("asian_high", 6, "bullish"),
}
check("Asian-Range Breakout Long fires",
      _ict_asian_range_breakout_long(asian_long_ind))
check("Asian-Range Breakout Long REJECTS when price < asian_high",
      not _ict_asian_range_breakout_long({**asian_long_ind, "current_price": 99.5}))

asian_short_ind = {
    **_BASE_IND,
    "ict_rth_minute": 20,
    "ict_levels": {"asian_high": 100.0, "asian_low": 98.0},
    "current_price": 97.5,
    "ict_last_bear_mss":   mss(5, "bearish"),
    "ict_last_bear_sweep": sweep("asian_low", 6, "bearish"),
}
check("Asian-Range Breakout Short fires",
      _ict_asian_range_breakout_short(asian_short_ind))

# Power of 3 Long — above OR high, with manipulation sweep + MSS.
# rth_minute=90 (= 11:00 ET) is past the v7.8 morning block and
# before the v7.10 afternoon block. Inside either blocked window the
# predicates short-circuit to False regardless of structural inputs.
po3_long_ind = {
    **_BASE_IND,
    "ict_rth_minute": 90,
    "ict_opening_range": {"low": 99.0, "high": 100.0},
    "current_price": 100.5,
    "ict_last_bull_sweep": sweep("opening_low", 30, "bullish"),
    "ict_last_bull_mss":   mss(10, "bullish"),
}
check("Power of 3 Long fires",
      _ict_power_of_3_long(po3_long_ind))
check("Power of 3 Long REJECTS when price below OR high",
      not _ict_power_of_3_long({**po3_long_ind, "current_price": 99.5}))
check("Power of 3 Long REJECTS inside v7.8 hour-block window (rth_minute=60)",
      not _ict_power_of_3_long({**po3_long_ind, "ict_rth_minute": 60}))
check("Power of 3 Long REJECTS inside v7.10 afternoon block (rth_minute=300)",
      not _ict_power_of_3_long({**po3_long_ind, "ict_rth_minute": 300}))
check("Power of 3 Long fires at 13:00 ET before afternoon block (rth_minute=210)",
      _ict_power_of_3_long({**po3_long_ind, "ict_rth_minute": 210}))

po3_long_strategy = next(s for s in PLAYBOOK if s.name == "ICT Power of 3 (Long)")
check("Power of 3 Long Strategy.matches allows AAPL",
      po3_long_strategy.matches({**po3_long_ind, "symbol": "AAPL"}))
check("Power of 3 Long Strategy.matches blocks MS",
      not po3_long_strategy.matches({**po3_long_ind, "symbol": "MS"}))

po3_short_ind = {
    **_BASE_IND,
    "ict_rth_minute": 90,
    "ict_opening_range": {"low": 99.0, "high": 100.0},
    "current_price": 98.5,
    "ict_last_bear_sweep": sweep("opening_high", 30, "bearish"),
    "ict_last_bear_mss":   mss(10, "bearish"),
}
check("Power of 3 Short fires",
      _ict_power_of_3_short(po3_short_ind))
check("Power of 3 Short REJECTS inside v7.8 hour-block window (rth_minute=60)",
      not _ict_power_of_3_short({**po3_short_ind, "ict_rth_minute": 60}))

# Breaker Block Long. fresh_entry=True is required (added 2026-04-25):
# the predicate now fires only on the bar where price first crosses
# INTO the zone, not every bar inside it. v7.3 backtest had 4,377 long
# trades / -1,463 R because the predicate was bar-sticky.
breaker_long_ind = {
    **_BASE_IND,
    "ict_in_bull_breaker": True,
    "ict_nearest_bull_breaker": {"low": 99.5, "high": 100.0,
                                 "bars_ago_mitigated": 20,
                                 "mss_idx": 30,
                                 "fresh_entry": True},
    "ict_pd_zone": "discount",
    "ict_last_bull_mss": mss(25, "bullish"),
}
check("Breaker Block Long fires on fresh entry",
      _ict_breaker_block_long(breaker_long_ind))
check("Breaker Block Long REJECTS when mitigation is stale (>60 bars)",
      not _ict_breaker_block_long({**breaker_long_ind,
                                   "ict_nearest_bull_breaker": {"low": 99.5, "high": 100.0,
                                                                 "bars_ago_mitigated": 120,
                                                                 "mss_idx": 30,
                                                                 "fresh_entry": True}}))
check("Breaker Block Long REJECTS when price was already inside zone (fresh_entry False)",
      not _ict_breaker_block_long({**breaker_long_ind,
                                   "ict_nearest_bull_breaker": {"low": 99.5, "high": 100.0,
                                                                 "bars_ago_mitigated": 20,
                                                                 "mss_idx": 30,
                                                                 "fresh_entry": False}}))
check("Breaker Block Long REJECTS when fresh_entry is missing (defensive default)",
      not _ict_breaker_block_long({**breaker_long_ind,
                                   "ict_nearest_bull_breaker": {"low": 99.5, "high": 100.0,
                                                                 "bars_ago_mitigated": 20,
                                                                 "mss_idx": 30}}))

breaker_short_ind = {
    **_BASE_IND,
    "ict_in_bear_breaker": True,
    "ict_nearest_bear_breaker": {"low": 100.0, "high": 100.5,
                                 "bars_ago_mitigated": 20,
                                 "mss_idx": 30,
                                 "fresh_entry": True},
    "ict_pd_zone": "premium",
    "ict_last_bear_mss": mss(25, "bearish"),
}
check("Breaker Block Short fires on fresh entry",
      _ict_breaker_block_short(breaker_short_ind))
check("Breaker Block Short REJECTS when fresh_entry False",
      not _ict_breaker_block_short({**breaker_short_ind,
                                    "ict_nearest_bear_breaker": {"low": 100.0, "high": 100.5,
                                                                  "bars_ago_mitigated": 20,
                                                                  "mss_idx": 30,
                                                                  "fresh_entry": False}}))

# Multi-Day PD Long
mdpd_long_ind = {
    **_BASE_IND,
    "ict_multi_day_pd_zone": "discount",
    "ict_pd_zone": "discount",
    "ict_last_bull_sweep": sweep("pdl", 5, "bullish"),
    "ict_last_bull_mss":   mss(2, "bullish"),
    "ict_in_bull_zone": True,
}
check("Multi-Day PD Long fires",
      _ict_multi_day_pd_long(mdpd_long_ind))
check("Multi-Day PD Long REJECTS when multi-day PD is premium",
      not _ict_multi_day_pd_long({**mdpd_long_ind,
                                  "ict_multi_day_pd_zone": "premium"}))

mdpd_short_ind = {
    **_BASE_IND,
    "ict_multi_day_pd_zone": "premium",
    "ict_pd_zone": "premium",
    "ict_last_bear_sweep": sweep("pdh", 5, "bearish"),
    "ict_last_bear_mss":   mss(2, "bearish"),
    "ict_in_bear_zone": True,
}
check("Multi-Day PD Short fires",
      _ict_multi_day_pd_short(mdpd_short_ind))

# SMT placeholder always False
check("SMT stub always returns False (infra gap, deferred)",
      _ict_smt_not_implemented({}) is False)


# ── 5. v7.3 win-rate gates ──────────────────────────────────────────────────
#
# Verify the new gates each fire (a) on a green-path ind dict and (b)
# reject when their threshold isn't met. Reuses the structural setups
# above; one rep per gate is enough to lock in the wiring.

print("\n== v7.3 win-rate gates ==")

# Volume gate: drop vol_ratio below the floor and the same Judas Long
# setup that fired above must now reject.
low_vol = {**judas_long_ind, "vol_ratio": _MIN_VOL_RATIO - 0.01}
check(f"Judas Long REJECTS when vol_ratio < {_MIN_VOL_RATIO}",
      not _ict_judas_swing_long(low_vol))
zero_vol = {**judas_long_ind, "vol_ratio": 0}
check("Judas Long REJECTS when vol_ratio == 0",
      not _ict_judas_swing_long(zero_vol))
missing_vol = {k: v for k, v in judas_long_ind.items() if k != "vol_ratio"}
check("Judas Long REJECTS when vol_ratio is missing entirely",
      not _ict_judas_swing_long(missing_vol))

# Sweep-magnitude gate: shrink the sweep magnitude under 0.25× ATR (with
# atr=1.0 the floor is 0.25). 0.1 < 0.25 should reject.
tiny_sweep_long = {**judas_long_ind,
                   "ict_last_bull_sweep": sweep("pml", 5, "bullish", magnitude=0.1)}
check(f"Judas Long REJECTS when sweep magnitude < {_MIN_SWEEP_ATR_MULT}× ATR",
      not _ict_judas_swing_long(tiny_sweep_long))
# Boundary: a magnitude exactly == 0.25× ATR should pass (>= compare).
exact_mag_long = {**judas_long_ind,
                  "ict_last_bull_sweep": sweep("pml", 5, "bullish", magnitude=0.25)}
check(f"Judas Long FIRES when sweep magnitude == {_MIN_SWEEP_ATR_MULT}× ATR (boundary)",
      _ict_judas_swing_long(exact_mag_long))
# Missing ATR also rejects (conservative — can't size without ATR).
no_atr = {**judas_long_ind, "atr": 0}
check("Judas Long REJECTS when atr == 0 (sweep-magnitude gate fails)",
      not _ict_judas_swing_long(no_atr))

# Breaker Block predicates don't touch the sweep-magnitude gate
# directly (they don't read sweep events at all) — only the volume
# gate. Confirm both: green path fires (sanity), low-vol rejects.
low_vol_breaker = {**breaker_long_ind, "vol_ratio": _MIN_VOL_RATIO - 0.01}
check(f"Breaker Block Long REJECTS when vol_ratio < {_MIN_VOL_RATIO}",
      not _ict_breaker_block_long(low_vol_breaker))

# ── _nearest_breaker direct unit tests (2026-04-25) ─────────────────────────
# Verify the edge-detection helper that powers ``fresh_entry``. The
# breaker zone is [99.5, 100.0], atr_pad=0.05 (so effective range is
# [99.45, 100.05]). We walk price across the zone boundary and verify
# fresh_entry flips correctly.
print("\n== _nearest_breaker fresh_entry edge detection ==")
from app.ict import _nearest_breaker

_breakers = [{"low": 99.5, "high": 100.0, "bars_ago_mitigated": 20,
              "mss_idx": 30, "mitigated_idx": 50}]
_pad = 0.05

# Cold start: no prev_price → fresh_entry must default True (don't
# permanently lock out a brand-new symbol).
b = _nearest_breaker(_breakers, price=99.8, prev_price=None, atr_pad=_pad)
check("_nearest_breaker cold start (prev_price=None) → fresh_entry True",
      b is not None and b.get("fresh_entry") is True)

# Prev outside (above), current inside → fresh entry from above.
b = _nearest_breaker(_breakers, price=99.8, prev_price=100.5, atr_pad=_pad)
check("_nearest_breaker prev above zone, current inside → fresh_entry True",
      b is not None and b.get("fresh_entry") is True)

# Prev outside (below), current inside → fresh entry from below.
b = _nearest_breaker(_breakers, price=99.8, prev_price=99.0, atr_pad=_pad)
check("_nearest_breaker prev below zone, current inside → fresh_entry True",
      b is not None and b.get("fresh_entry") is True)

# Prev inside, current still inside → NOT a fresh entry (the bug we're
# fixing — predicate was firing on every bar this case held).
b = _nearest_breaker(_breakers, price=99.8, prev_price=99.7, atr_pad=_pad)
check("_nearest_breaker prev inside, current inside → fresh_entry False",
      b is not None and b.get("fresh_entry") is False)

# Prev outside, current also outside → returns None (no zone touch).
b = _nearest_breaker(_breakers, price=101.0, prev_price=101.5, atr_pad=_pad)
check("_nearest_breaker prev outside, current outside → returns None",
      b is None)

# Prev inside, current outside → returns None (price has left the
# zone). We don't want fresh_entry False echoing through to the
# predicate — the predicate must short-circuit on ict_in_*_breaker.
b = _nearest_breaker(_breakers, price=101.0, prev_price=99.8, atr_pad=_pad)
check("_nearest_breaker prev inside, current outside → returns None",
      b is None)

# Edge of pad: price at exactly low - pad (boundary touch) is INSIDE.
# atr_pad makes 99.45 the effective lower edge.
b = _nearest_breaker(_breakers, price=99.45, prev_price=99.0, atr_pad=_pad)
check("_nearest_breaker boundary touch (low - pad) → fresh_entry True",
      b is not None and b.get("fresh_entry") is True)

# Empty breaker list → None regardless of price/prev_price.
b = _nearest_breaker([], price=99.8, prev_price=99.0, atr_pad=_pad)
check("_nearest_breaker on empty list → None",
      b is None)

# Returned dict must be a COPY, not the underlying breaker. Otherwise
# we'd mutate ict_zones["breakers_*"] which is shipped to the frontend.
underlying = _breakers[0]
b = _nearest_breaker(_breakers, price=99.8, prev_price=100.5, atr_pad=_pad)
check("_nearest_breaker returns a COPY (no mutation of underlying)",
      b is not underlying and "fresh_entry" not in underlying)

# Hard regime VETO. is_regime_vetoed takes (direction, regime). We
# don't go through autotrade.py here — the veto wiring lives in
# autotrade._analyze_one's per-symbol scoring loop and is exercised
# by the integration tests — but this checks that the helper returns
# the right answer and won't silently get inverted.
check("Veto: long + trending_down → True",
      is_regime_vetoed("long", "trending_down") is True)
check("Veto: short + trending_up → True",
      is_regime_vetoed("short", "trending_up") is True)
check("No veto: long + trending_up → False",
      is_regime_vetoed("long", "trending_up") is False)
check("No veto: short + trending_down → False",
      is_regime_vetoed("short", "trending_down") is False)
check("No veto: long + choppy → False",
      is_regime_vetoed("long", "choppy") is False)
check("No veto: short + choppy → False",
      is_regime_vetoed("short", "choppy") is False)
check("No veto: long + unknown → False (cold-start safety)",
      is_regime_vetoed("long", "unknown") is False)
check("No veto: short + unknown → False (cold-start safety)",
      is_regime_vetoed("short", "unknown") is False)
# Direction case-insensitivity (autotrade may pass "Long" or "LONG"
# depending on upstream casing).
check("Veto: 'LONG' uppercased + trending_down → True (case-insensitive)",
      is_regime_vetoed("LONG", "trending_down") is True)


# ── 6. screen() against an empty ind returns [] cleanly ────────────────────

print("\n== screen() stability ==")
result = screen({})
check("screen({}) returns [] without exception", result == [])
check("context_screen({}) returns [] without exception", context_screen({}) == [])


# ── 6b. v7.11 tier + Trade Quality Score sanity ──────────────────────────
# v7.11 added a `tier` field on Strategy with audit-locked
# per-strategy assignments + a score function in app/trade_quality.py.
# Pure metadata — does NOT change the backtest trade count. These
# asserts catch regressions where someone re-runs a backtest, the
# new edge lookup contradicts the audit, and the scanner UI starts
# showing wrong tier badges.

print("\n== v7.11 tier + Trade Quality Score ==")

def _by_name(strategy_list, n):
    return next(s for s in strategy_list if s.name == n)

# Per-strategy tier assignments (locked from the v7.10.5 audit).
check("Judas Long is A-tier",
      _by_name(PLAYBOOK, "ICT Judas Swing (Long)").tier == "A")
check("Judas Short is A-tier",
      _by_name(PLAYBOOK, "ICT Judas Swing (Short)").tier == "A")
check("Turtle Soup Long is A-tier",
      _by_name(PLAYBOOK, "ICT Turtle Soup (Long)").tier == "A")
check("Turtle Soup Short is context-tier (live but never primary)",
      _by_name(PLAYBOOK, "ICT Turtle Soup (Short)").tier == "context")
check("Power of 3 (Long) is B-tier",
      _by_name(PLAYBOOK, "ICT Power of 3 (Long)").tier == "B")
check("Power of 3 (Short) is B-tier",
      _by_name(PLAYBOOK, "ICT Power of 3 (Short)").tier == "B")
check("Multi-Day PD (Long) is B-tier",
      _by_name(PLAYBOOK, "ICT Multi-Day PD (Long)").tier == "B")

# All CONTEXT_PLAYBOOK entries default to context tier.
check("All CONTEXT_PLAYBOOK strategies are tier=context",
      all(s.tier == "context" for s in CONTEXT_PLAYBOOK))

# Score function loads + returns the expected schema. Edge lookup
# JSON must exist (regenerated by scripts/build_edge_lookup.py).
from app.trade_quality import score, load_edge_lookup
edge = load_edge_lookup()
check("edge lookup loaded with at least one strategy entry",
      any(not k.startswith("_") for k in edge.keys()))

# Score a known A-grade match with a populated symbol cell.
result = score(
    strategy_name="ICT Judas Swing (Long)",
    symbol="AAPL",
    rth_hour_et=9,
    ind={"vol_ratio": 1.2},
    edge_lookup=edge,
    base_tier="A",
)
check("score returns dict with score/tier/reasons keys",
      set(result.keys()) >= {"score", "tier", "reasons", "post_friction_avg_r"})
check("score is in 0-100 range",
      0 <= result["score"] <= 100)
check("Judas Long A-grade match keeps tier=A",
      result["tier"] == "A")
check("score reasons array is non-empty",
      isinstance(result["reasons"], list) and len(result["reasons"]) >= 1)

# Score a B-grade strategy and confirm tier-floor behavior.
result_b = score(
    strategy_name="ICT Power of 3 (Long)",
    symbol="AMD",   # known gold cell on P3 Long
    rth_hour_et=9,
    ind={},
    edge_lookup=edge,
    base_tier="B",
)
check("P3 Long with gold AMD cell stays B (never promoted above base tier)",
      result_b["tier"] in ("B", "suppress"))

# Unknown strategy graceful fallback.
result_unknown = score(
    strategy_name="Made-Up Strategy Name",
    symbol="AAPL", rth_hour_et=10, ind={}, edge_lookup=edge, base_tier="B",
)
check("unknown strategy falls back to base_tier",
      result_unknown["tier"] == "B")
check("unknown strategy returns a reasons explanation",
      len(result_unknown["reasons"]) >= 1)


# ── 6c. v7.12 partial_match — Watching-panel forming-state sanity ─────────
# v7.12 added `_partial_check` field + `partial_match()` wrapper on
# Strategy, mirroring the structural_stop_fn pattern. Pure metadata —
# matches() output is unchanged. These asserts catch regressions
# where someone accidentally wires partial_match into matches() or
# changes the forming-state output schema (which the frontend's
# Watching panel depends on).

print("\n== v7.12 partial_match (forming-state) ==")

# All 7 live PLAYBOOK strategies have _partial_check wired.
for s in PLAYBOOK:
    check(f"{s.name} has _partial_check wired",
          s._partial_check is not None)

# CONTEXT_PLAYBOOK predicates are context-only and don't need
# partial-state visibility. Lock that in.
for s in CONTEXT_PLAYBOOK:
    check(f"{s.name} has _partial_check=None (context-only)",
          s._partial_check is None)

# partial_match returns None on empty ind (volume gate fails).
for s in PLAYBOOK:
    check(f"{s.name} partial_match({{}}) returns None (volume gate)",
          s.partial_match({}) is None)

# Functional check: build a synthetic ind with a fresh bullish sweep
# but no MSS → Judas Long should report sweep_only stage.
judas_long = next(s for s in PLAYBOOK if s.name == "ICT Judas Swing (Long)")
sweep_only_ind = {
    "vol_ratio": 1.0,
    "atr": 1.0,
    "ict_rth_minute": 15,  # in Judas window
    "ict_last_bull_sweep": {
        "level_name": "asian_low",
        "bars_ago": 4,
        "magnitude": 0.5,
    },
    "ict_last_bull_mss": None,
    "ict_in_bull_zone": False,
}
result_partial = judas_long.partial_match(sweep_only_ind)
check("Judas Long partial_match with sweep-only ind returns dict",
      result_partial is not None)
check("Judas Long partial_match stage == 'sweep_only'",
      result_partial.get("stage") == "sweep_only")
check("Judas Long partial_match missing includes 'mss'",
      "mss" in (result_partial.get("missing") or []))
check("Judas Long partial_match returns structural_notes list",
      isinstance(result_partial.get("structural_notes"), list)
      and len(result_partial.get("structural_notes")) >= 1)

# Critical guardrail: matches() output is unchanged after _partial_check
# was added. The sweep_only_ind should NOT match (no MSS = matches()
# returns False). If a future refactor accidentally wired partial_match
# into matches(), this assert catches it.
check("Judas Long matches() with sweep-only ind returns False (matches() is unaffected by _partial_check)",
      judas_long.matches(sweep_only_ind) is False)

# Stage progression: adding the MSS advances to sweep_and_mss.
sweep_mss_ind = {**sweep_only_ind,
                 "ict_last_bull_mss": {"bars_ago": 2, "direction": "bullish"}}
result_progressed = judas_long.partial_match(sweep_mss_ind)
check("Judas Long partial_match with sweep+MSS advances to sweep_and_mss",
      result_progressed.get("stage") == "sweep_and_mss")
check("Judas Long partial_match still missing fvg_touch",
      "fvg_touch" in (result_progressed.get("missing") or []))


# ── 6d. v7.12 catalyst boosts — capped + anti-overpowering ───────────────
# Track C added catalyst boosts (RVOL, liquidity stacking, MTF
# alignment) capped at +12 of 100. The score must remain anchored to
# post-friction edge — a friction-negative cell with maxed catalysts
# CANNOT reach an A-grade-looking score.

print("\n== v7.12 catalyst boosts ==")

baseline = score(
    strategy_name="ICT Judas Swing (Long)", symbol="AAPL",
    rth_hour_et=9, ind={}, edge_lookup=edge, base_tier="A",
)
maxed_ind = {
    "rvol_vs_20d": 2.5,           # +2
    "touches_prev_high": 2,
    "touches_session_high": 2,    # 4 touches → +6
    "rsi_alignment": "long_aligned",
    "ema_alignment_mtf": "long_aligned",  # +3
    "vol_ratio": 1.5,
}
maxed = score(
    strategy_name="ICT Judas Swing (Long)", symbol="AAPL",
    rth_hour_et=9, ind=maxed_ind, edge_lookup=edge, base_tier="A",
)
check("catalyst boost adds points above baseline",
      maxed["score"] >= baseline["score"])
check("catalyst boost is capped (max ≤ baseline + 12)",
      maxed["score"] - baseline["score"] <= 12)

# Anti-overpowering: maxed-catalyst B-tier (friction-negative) cannot
# beat clean A-tier (friction-positive). Use unknown symbols so the
# strategy aggregate friction is used.
b_max = score(
    strategy_name="ICT Power of 3 (Long)", symbol="UNKNOWNSYMBOL",
    rth_hour_et=13, ind=maxed_ind, edge_lookup=edge, base_tier="B",
)
a_clean = score(
    strategy_name="ICT Judas Swing (Long)", symbol="UNKNOWNSYMBOL",
    rth_hour_et=11, ind={}, edge_lookup=edge, base_tier="A",
)
check("anti-overpowering: A-tier clean beats B-tier (friction-negative) with max catalysts",
      a_clean["score"] > b_max["score"])

# Negative catalyst (low RVOL) should subtract a few points.
weak_ind = {"rvol_vs_20d": 0.5, "vol_ratio": 0.8}
weak = score(
    strategy_name="ICT Judas Swing (Long)", symbol="AAPL",
    rth_hour_et=9, ind=weak_ind, edge_lookup=edge, base_tier="A",
)
check("low RVOL produces small negative boost",
      weak["score"] < baseline["score"])
check("negative boost is also capped (min ≥ baseline - 12)",
      baseline["score"] - weak["score"] <= 12)


# ── 7. regime coverage + hour-weight neutralization ────────────────────────
#
# Hour weights were stripped on 2026-04-25 — every (strategy, hour)
# pair now returns 1.0. Regime weights are unchanged (regime is a
# market-state input, not a time input) so we still verify each v7
# name carries at least one non-neutral regime cell.
#
# Note: this list includes suspended-but-research-only predicates
# (BB L/S, ARB L/S, MD-PD Short, Sweep+MSS Short). The regime-weight
# rows for those names are intentionally preserved (consistent with
# the BB / ARB convention) so re-promotion is a one-line
# ``Strategy(...)`` re-insertion in app/strategies.py, not a regime-
# table edit. If a row is missing here the smoke test fails — that's
# the desired behavior.

print("\n== regime coverage + hour-weight neutralization ==")
v7_names = [
    # Live (live PLAYBOOK members):
    "ICT Judas Swing (Long)",
    "ICT Judas Swing (Short)",
    "ICT Turtle Soup (Long)",
    "ICT Turtle Soup (Short)",
    "ICT Power of 3 (Long)",
    "ICT Power of 3 (Short)",
    "ICT Multi-Day PD (Long)",
    # Suspended (research-only via playbook_override; rows preserved):
    "ICT Sweep + MSS (Short)",       # v7.9
    "ICT Asian-Range Breakout (Long)",   # v7.7
    "ICT Asian-Range Breakout (Short)",  # v7.7
    "ICT Breaker Block (Long)",      # v7.6
    "ICT Breaker Block (Short)",     # v7.6
    "ICT Multi-Day PD (Short)",      # v7.9
]
for nm in v7_names:
    # Regime table lookup — at least one regime should have a non-1.0
    # weight (otherwise the name is actually missing from the table).
    weights = {r: get_weight(nm, r) for r in ALL_REGIMES}
    check(f"{nm} has at least one regime weight != 1.0",
          any(w != 1.0 for w in weights.values()))
    # Hour-weight lookup — must be neutral 1.0 for every hour now.
    check(f"{nm} hour weight is neutral (==1.0) for every hour",
          all(get_hour_weight(nm, h) == 1.0 for h in range(24)))

print("\nAll v7 smoke checks passed.")
