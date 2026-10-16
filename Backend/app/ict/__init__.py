"""
ICT (Inner Circle Trader) strategy module — public surface.
============================================================

This package owns the "smart-money" feature layer that sits alongside
``indicators.py`` / ``analyzer.py`` in the Tier-1 scanner. Each scan
cycle the pipeline calls ``compute_ict_features(bars, daily_ctx)`` to
flatten the raw zone geometry (FVGs, order blocks, swings, sweeps,
MSS events) into a small dict of booleans + scalars that slot into
the existing ``ind`` payload. The PLAYBOOK's ICT strategy predicates
read those flags the same way they read RSI / VWAP / EMA today — one
uniform ``Strategy._check(ind) -> bool`` contract, no scanner
surgery.

What lands in ``ind`` after ``ict_features()`` merges
-----------------------------------------------------
::

    ict_killzone            "london" | "ny_am" | "silver_bullet" | "ny_pm" | None
    ict_rth_minute          0–390 minute-into-RTH for the latest bar, or None outside RTH
    ict_pd_pct              0.0-1.0 position in dealing range
    ict_pd_zone             "discount" | "equilibrium" | "premium"
    ict_in_ote_long         bool — price in 62-79% discount retrace
    ict_in_ote_short        bool — price in 62-79% premium retrace
    ict_dealing_range       {"low": float, "high": float} | None
    ict_multi_day_range     {"low": float, "high": float} | None
    ict_multi_day_pd_zone   "discount" | "equilibrium" | "premium" | None (from 20-day H/L)
    ict_opening_range       {"low": float, "high": float} | None (first 15 min of RTH)
    ict_last_sweep          {direction, level_name, level_price, bars_ago} | None
    ict_last_bull_sweep     {…} | None                       (most recent BULL-implied sweep)
    ict_last_bear_sweep     {…} | None                       (most recent BEAR-implied sweep)
    ict_last_mss            {direction, bars_ago, broken_swing_price} | None
    ict_last_bull_mss       {…} | None
    ict_last_bear_mss       {…} | None
    ict_in_bull_zone        bool — price currently touching a fresh bullish FVG or OB
    ict_in_bear_zone        bool — price currently touching a fresh bearish FVG or OB
    ict_nearest_bull_zone   {low, high, kind, bars_ago} | None
    ict_nearest_bear_zone   {low, high, kind, bars_ago} | None
    ict_in_bull_breaker     bool — price touching a fresh bullish breaker block
    ict_in_bear_breaker     bool — price touching a fresh bearish breaker block
    ict_nearest_bull_breaker {low, high, bars_ago, mitigated_idx} | None
    ict_nearest_bear_breaker {…} | None
    ict_levels              flat dict: pdh/pdl/prev_close/pmh/pml/asian_high/asian_low/
                            session_high/session_low/opening_high/opening_low
    ict_zones               full geometry for frontend overlay rendering (see below)

The ``ict_zones`` field is intentionally verbose — it carries the
full list of FVGs, OBs, swings, sweeps and MSS events with their bar
indices so the frontend can draw overlays without re-running the
detectors client-side. It's a sub-dict so callers that only care
about the predicates can ignore it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .detectors import (
    KILLZONES,
    FVG,
    MSSEvent,
    OrderBlock,
    Swing,
    SweepEvent,
    dealing_range_from_swings,
    detect_fvgs,
    detect_mss,
    detect_order_blocks,
    detect_sweep,
    detect_swings,
    killzone,
    pd_array,
)


# Zones older than this get dropped from the "active" lists — keeps
# stale intraday geometry from polluting the nearest-zone search.
# 1m bars → 240 bars = 4 hours of session time.
_MAX_ZONE_AGE_BARS = 240

# When looking for "nearest fresh bullish/bearish zone touching price"
# we widen the test by this fraction of ATR so the predicate fires the
# moment price enters the neighborhood, not only on the exact bar that
# prints inside the zone.
_ZONE_TOUCH_ATR_PAD = 0.10

# US equities regular trading hours in ET. Used to split bars into
# pre-market (04:00–09:30) vs RTH (09:30–16:00) segments so the
# dealing range + session H/L anchor to the 9:30 open rather than
# leaking an overnight wick into the "session" range.
_ET = ZoneInfo("America/New_York")
_RTH_START_MIN = 9 * 60 + 30    # 09:30
_RTH_END_MIN   = 16 * 60        # 16:00
_PM_START_MIN  = 4 * 60         # 04:00 — Alpaca's earliest pre-market fill

# Opening-range window (first 15 min of RTH, 09:30–09:45 ET). Used by
# Power of 3 and the Judas Swing predicates as the "opening drive"
# reference leg. 15 min captures the classic opening-range breakout
# window without overlapping Silver Bullet.
_OR_END_MIN = 9 * 60 + 45        # 09:45

# "Asian session" for US equities is a loose fiction — US stocks don't
# trade 20:00 prev ET → 04:00 ET. But ICT's Asian Range concept still
# maps onto the overnight liquidity pool that stops hunt above/below
# when the NY session opens. We approximate it with bars whose ET time
# falls in [18:00 prev, 04:00]. That spans US after-hours (18:00–20:00)
# → Asian prime (20:00–00:00) → overnight (00:00–04:00). If the feed
# doesn't carry bars in that window the keys are simply absent.
_ASIAN_START_MIN = 18 * 60      # 18:00 ET (post-close)
_ASIAN_END_MIN   = 4  * 60      # 04:00 ET (PM start)


def _et_minutes(unix_ts: float) -> int:
    """Return the ET minute-of-day (0–1439) for a unix timestamp.

    Pulled out so the session-partition in ``_sweep_liquidity_levels``
    can share the same cheap ET conversion the classifier uses; avoids
    pulling pandas into this hot path.
    """
    dt = datetime.fromtimestamp(float(unix_ts), tz=_ET)
    return dt.hour * 60 + dt.minute


def _et_date_str(unix_ts: float) -> str:
    """ET calendar date (YYYY-MM-DD) for a unix timestamp. Used by the
    multi-day range calculation to count how many distinct trading
    days are in the bar frame."""
    dt = datetime.fromtimestamp(float(unix_ts), tz=_ET)
    return dt.strftime("%Y-%m-%d")


def _is_rth(unix_ts: float) -> bool:
    return _RTH_START_MIN <= _et_minutes(unix_ts) < _RTH_END_MIN


def _is_premarket(unix_ts: float) -> bool:
    return _PM_START_MIN <= _et_minutes(unix_ts) < _RTH_START_MIN


def _is_opening_range(unix_ts: float) -> bool:
    """First 15 min of RTH (09:30–09:45 ET)."""
    return _RTH_START_MIN <= _et_minutes(unix_ts) < _OR_END_MIN


def _is_asian(unix_ts: float) -> bool:
    """Overnight Asian-ish window, 18:00 prev ET → 04:00 ET. Spans
    midnight so the test is a disjunction rather than a range."""
    m = _et_minutes(unix_ts)
    return m >= _ASIAN_START_MIN or m < _ASIAN_END_MIN


def _nearest_zone(
    zones: list[FVG | OrderBlock],
    price: float,
    atr_pad: float,
) -> dict | None:
    """Return the zone whose [low, high] brackets ``price`` (± pad),
    preferring the most recent one. ``None`` if nothing touches.
    Accepts a mixed list of FVGs and OBs; returns a dict labeled with
    ``kind`` so callers know what they got.
    """
    touching: list[tuple[FVG | OrderBlock, int]] = [
        (z, z.created_idx) for z in zones
        if (z.low - atr_pad) <= price <= (z.high + atr_pad)
    ]
    if not touching:
        return None
    z, _ = max(touching, key=lambda pair: pair[1])
    return {
        "low":            round(z.low, 4),
        "high":           round(z.high, 4),
        "kind":           "fvg" if isinstance(z, FVG) else "ob",
        "created_idx":    z.created_idx,
        "direction":      z.direction,
    }


def _sweep_liquidity_levels(
    bars: list[dict],
    daily_ctx: dict | None,
) -> dict[str, float]:
    """Build the dict of named levels that ICT considers stop-running
    magnets. Covered pools:

        pdh / pdl        — previous day high / low
        prev_close       — yesterday's close (gap-fill magnet)
        d20_high/low     — 20-day H/L, multi-day PD anchors
        session_high/low — RTH high/low so far (09:30 ET → now)
        pmh / pml        — pre-market high / low (04:00 → 09:30 ET)
        asian_high/low   — overnight Asian-range high/low (18:00 prev → 04:00)
        opening_high/low — opening-range H/L (09:30 → 09:45 ET)

    Each level is only included when the caller supplied the value
    (daily_ctx may be partial) or the corresponding session slice
    contains at least one bar.

    Why RTH-anchoring matters
    -------------------------
    Before 2026-04-24 this function computed session high/low from the
    "last continuous block of bars with < 30-min gap". That heuristic
    silently collapsed when the feed filled pre-market bars
    continuously — a scan at 10:00 ET with 8 hours of backfill would
    see ONE unbroken bar stream from 04:00, and ``session_start_idx``
    would stay at 0. Result: "session high" was whatever the 05:30 ET
    pre-market wick had printed, which in turn poisoned the dealing
    range + the entire PD array read.

    The RTH anchor fixes that structurally: session_high/low is
    computed ONLY from bars whose ET time is in [09:30, 16:00). The
    pre-market bars aren't dropped — they're promoted to their own
    ``pmh``/``pml`` keys so the sweep detector still sees them as
    named liquidity magnets (overnight stop-runs above PMH or below
    PML are a classic ICT setup).

    Degraded path: if the bar frame has NO RTH bars yet (scanner
    firing pre-open), fall back to using the full frame for
    session_high/low. That matches the pre-RTH-fix behavior so
    pre-open probes don't null out completely.
    """
    levels: dict[str, float] = {}
    if daily_ctx:
        for key, out_key in (("prev_high", "pdh"), ("prev_low", "pdl"),
                             ("prev_close", "prev_close"),
                             ("20d_high", "d20_high"), ("20d_low", "d20_low")):
            v = daily_ctx.get(key)
            if v is not None:
                levels[out_key] = float(v)

    if bars:
        rth_highs:     list[float] = []
        rth_lows:      list[float] = []
        pm_highs:      list[float] = []
        pm_lows:       list[float] = []
        asian_highs:   list[float] = []
        asian_lows:    list[float] = []
        or_highs:      list[float] = []
        or_lows:       list[float] = []
        for b in bars:
            try:
                mins = _et_minutes(b["time"])
            except Exception:
                # Bad timestamp → count it as RTH so we don't drop
                # it silently (better to over-include than to vanish).
                mins = _RTH_START_MIN
            if _RTH_START_MIN <= mins < _RTH_END_MIN:
                rth_highs.append(b["high"])
                rth_lows.append(b["low"])
                if mins < _OR_END_MIN:
                    or_highs.append(b["high"])
                    or_lows.append(b["low"])
            elif _PM_START_MIN <= mins < _RTH_START_MIN:
                pm_highs.append(b["high"])
                pm_lows.append(b["low"])
            # Asian window wraps midnight, so test separately. A bar
            # between 00:00–04:00 ET is also ASIAN (not PM — PM starts
            # at 04:00) and a bar between 18:00–23:59 ET is ASIAN.
            if mins >= _ASIAN_START_MIN or mins < _ASIAN_END_MIN:
                asian_highs.append(b["high"])
                asian_lows.append(b["low"])

        if rth_highs:
            levels["session_high"] = max(rth_highs)
            levels["session_low"]  = min(rth_lows)
        else:
            # Pre-open / weekend edge — use the full frame so the
            # dealing range + sweep detector still have something
            # to chew on instead of zeroing out.
            levels["session_high"] = max(b["high"] for b in bars)
            levels["session_low"]  = min(b["low"]  for b in bars)

        if pm_highs:
            levels["pmh"] = max(pm_highs)
            levels["pml"] = min(pm_lows)

        if asian_highs:
            levels["asian_high"] = max(asian_highs)
            levels["asian_low"]  = min(asian_lows)

        if or_highs:
            levels["opening_high"] = max(or_highs)
            levels["opening_low"]  = min(or_lows)

    return levels


def _event_to_dict_with_age(event: Any, last_idx: int) -> dict | None:
    """Uniform "event → dict with bars_ago" serialisation. Any of the
    event classes (SweepEvent, MSSEvent) have ``idx`` + ``to_dict()``."""
    if event is None:
        return None
    d = event.to_dict()
    d["bars_ago"] = last_idx - event.idx
    return d


def _derive_breakers(
    obs: list[OrderBlock],
    mss_events: list[MSSEvent],
    last_idx: int,
) -> tuple[list[dict], list[dict]]:
    """Return (bullish_breakers, bearish_breakers).

    Classic ICT breaker: an order block that got mitigated and then had
    its polarity flipped by a structural shift in the OPPOSITE direction.

      * BULLISH breaker = a BEARISH OB that was broken (mitigated) AFTER
        a BULLISH MSS printed. The former supply zone is now a demand
        zone on retest — buyers defend where sellers failed.
      * BEARISH breaker = symmetric (BULLISH OB mitigated after a
        bearish MSS).

    Matching rule: for each mitigated OB, find the most recent MSS in
    the opposing direction whose idx lies between the OB's creation
    and its mitigation. If such an MSS exists, the OB becomes a
    breaker of the MSS's direction.

    Returns two dict lists — ``{low, high, created_idx, mitigated_idx,
    bars_ago_mitigated, mss_idx}`` — sorted by most-recent mitigation
    first. The downstream touch test mirrors ``_nearest_zone``: fire
    the predicate when price is inside [low, high] (± ATR pad).
    """
    bull: list[dict] = []
    bear: list[dict] = []

    # Sort MSS by idx for fast "most-recent-before" lookup. Not worth a
    # bisect here — intraday lists are tiny (rarely > 10).
    bull_mss = sorted([m for m in mss_events if m.direction == "bullish"],
                      key=lambda m: m.idx)
    bear_mss = sorted([m for m in mss_events if m.direction == "bearish"],
                      key=lambda m: m.idx)

    for ob in obs:
        if ob.mitigated_idx is None:
            continue  # live OB — not a breaker yet
        if ob.direction == "bearish":
            # Looking for a bullish MSS between creation and mitigation
            match = None
            for m in bull_mss:
                if ob.created_idx < m.idx <= ob.mitigated_idx:
                    match = m  # keep scanning for the most recent one
            if match is not None:
                bull.append({
                    "low":                round(ob.low, 4),
                    "high":               round(ob.high, 4),
                    "created_idx":        ob.created_idx,
                    "mitigated_idx":      ob.mitigated_idx,
                    "bars_ago_mitigated": last_idx - ob.mitigated_idx,
                    "mss_idx":            match.idx,
                    "direction":          "bullish",  # flipped polarity
                })
        else:
            # Bullish OB flipped by a bearish MSS between creation & mitigation
            match = None
            for m in bear_mss:
                if ob.created_idx < m.idx <= ob.mitigated_idx:
                    match = m
            if match is not None:
                bear.append({
                    "low":                round(ob.low, 4),
                    "high":               round(ob.high, 4),
                    "created_idx":        ob.created_idx,
                    "mitigated_idx":      ob.mitigated_idx,
                    "bars_ago_mitigated": last_idx - ob.mitigated_idx,
                    "mss_idx":            match.idx,
                    "direction":          "bearish",
                })

    bull.sort(key=lambda b: -b["mitigated_idx"])
    bear.sort(key=lambda b: -b["mitigated_idx"])
    return bull, bear


def _nearest_breaker(
    breakers: list[dict],
    price: float,
    prev_price: float | None,
    atr_pad: float,
) -> dict | None:
    """Return the most-recently-mitigated breaker whose range (±pad)
    brackets ``price``. None if nothing touches. Mirrors
    ``_nearest_zone`` for breakers.

    Adds a ``fresh_entry`` flag (2026-04-25): True when ``prev_price`` was
    OUTSIDE the zone and the current price is INSIDE — i.e. price just
    crossed into the breaker on this bar. The strategy predicate gates on
    this so we fire only on the actual retest event, not on every bar
    that price happens to be sitting inside the zone. Without it, the
    predicate fired bar-after-bar while price oscillated in the zone,
    producing 9k+ trades / -2,889 R in the v7.3 backtest. See
    `_ict_breaker_block_long/short` for the consumer side."""
    touching = [b for b in breakers
                if (b["low"] - atr_pad) <= price <= (b["high"] + atr_pad)]
    if not touching:
        return None
    # Already sorted most-recent-mitigation-first. Copy so we don't
    # mutate the underlying breaker dict that also lives in
    # ``ict_zones["breakers_*"]`` (which the frontend reads).
    nearest = dict(touching[0])
    if prev_price is None:
        # Cold start (only one bar): treat as fresh entry. Conservative
        # callers (the predicates) will further gate on volume + MSS
        # recency, so this can't fire alone.
        nearest["fresh_entry"] = True
    else:
        prev_inside = (
            (nearest["low"] - atr_pad)
            <= prev_price
            <= (nearest["high"] + atr_pad)
        )
        nearest["fresh_entry"] = not prev_inside
    return nearest


def _multi_day_range(
    bars: list[dict],
    daily_ctx: dict | None,
) -> dict | None:
    """Derive a multi-day dealing range.

    Preferred source: ``daily_ctx["20d_high"]`` / ``["20d_low"]`` — a
    true 20-session range from the daily bars. Fallback: span of the
    input bar frame IF it covers ≥ 2 distinct ET dates. If neither is
    available we return None and the multi-day predicates skip.
    """
    if daily_ctx:
        hi = daily_ctx.get("20d_high")
        lo = daily_ctx.get("20d_low")
        if hi is not None and lo is not None and float(hi) > float(lo):
            return {"low": round(float(lo), 4), "high": round(float(hi), 4),
                    "source": "daily_20d"}

    if bars:
        # Count distinct ET dates. Cheap linear scan, capped implicitly
        # by bar count.
        dates = set()
        for b in bars:
            try:
                dates.add(_et_date_str(b["time"]))
            except Exception:
                continue
            if len(dates) >= 2:
                break
        if len(dates) >= 2:
            hi = max(b["high"] for b in bars)
            lo = min(b["low"]  for b in bars)
            if hi > lo:
                return {"low": round(lo, 4), "high": round(hi, 4),
                        "source": "intraday_frame"}
    return None


def compute_ict_features(
    bars: list[dict],
    daily_ctx: dict | None = None,
) -> dict:
    """Run every ICT detector and collapse the output into the flags
    the Tier-1 scanner consumes. ``bars`` must be chronological
    (oldest → newest) with the standard ``time/open/high/low/close/volume``
    keys.

    Returns a dict. All keys are present (possibly ``None``) so the
    downstream predicates can safely ``ind.get("ict_last_mss")``
    without worrying about which features were / weren't populated.

    On too-few-bars (< 15), returns a "no-op" dict with all flags
    False / None — matches the scanner's own 15-bar minimum (both
    were dropped from 30 → 15 on 2026-04-24 to unblind the 9:45 ET
    early-session sweep/MSS window). 15 bars is structurally enough:
    swings need 2k+1=7 bars, FVGs need 3, and the session dealing
    range is cumulative from the 9:30 open.
    """
    base: dict[str, Any] = {
        "ict_killzone":             None,
        "ict_rth_minute":           None,
        "ict_pd_pct":               None,
        "ict_pd_zone":              None,
        "ict_in_ote_long":          False,
        "ict_in_ote_short":         False,
        "ict_dealing_range":        None,
        "ict_multi_day_range":      None,
        "ict_multi_day_pd_zone":    None,
        "ict_opening_range":        None,
        "ict_last_sweep":           None,
        "ict_last_bull_sweep":      None,
        "ict_last_bear_sweep":      None,
        "ict_last_mss":             None,
        "ict_last_bull_mss":        None,
        "ict_last_bear_mss":        None,
        "ict_in_bull_zone":         False,
        "ict_in_bear_zone":         False,
        "ict_nearest_bull_zone":    None,
        "ict_nearest_bear_zone":    None,
        "ict_in_bull_breaker":      False,
        "ict_in_bear_breaker":      False,
        "ict_nearest_bull_breaker": None,
        "ict_nearest_bear_breaker": None,
        "ict_levels":               None,
        "ict_zones":                None,
    }
    if not bars or len(bars) < 15:
        return base

    last_idx = len(bars) - 1
    last_bar = bars[last_idx]
    price    = float(last_bar["close"])

    # ── Killzone of the LATEST bar (the scan-cycle "now") ──────────────
    base["ict_killzone"] = killzone(last_bar["time"])

    # ── RTH minute of the latest bar ───────────────────────────────────
    # None outside RTH; 0 at the 09:30 open, 390 at 16:00 close. The
    # Judas Swing predicate uses this to gate itself to the first 30
    # min of the session without each predicate re-implementing the ET
    # clock.
    try:
        m_now = _et_minutes(last_bar["time"])
        if _RTH_START_MIN <= m_now < _RTH_END_MIN:
            base["ict_rth_minute"] = m_now - _RTH_START_MIN
    except Exception:
        base["ict_rth_minute"] = None

    # ── Liquidity levels (shared: sweep detection + dealing range) ─────
    liquidity = _sweep_liquidity_levels(bars, daily_ctx)
    # Expose a rounded flat copy for downstream predicates + the prompt.
    # Keeps strategies.py from reaching into ict_zones.liquidity.
    base["ict_levels"] = {k: round(v, 4) for k, v in liquidity.items()}

    if "opening_high" in liquidity and "opening_low" in liquidity:
        base["ict_opening_range"] = {
            "low":  round(liquidity["opening_low"],  4),
            "high": round(liquidity["opening_high"], 4),
        }

    # ── Swings → dealing range → PD array ──────────────────────────────
    # For intraday we anchor the dealing range to the SESSION high/low
    # (everything the tape has done today). The swing-based range was
    # too brittle — if the two most recent pivots are both on the same
    # side of the current price (say two HLs after a fresh up-move),
    # the "range" degenerates and the PD read becomes nonsensical.
    # Session H/L tracks the actual macro leg the ICT trader is reading.
    swings = detect_swings(bars, k=3)
    s_high = liquidity.get("session_high")
    s_low  = liquidity.get("session_low")
    if s_high is not None and s_low is not None and s_high > s_low:
        base["ict_dealing_range"] = {"low": round(s_low, 4), "high": round(s_high, 4)}
        pd = pd_array(s_low, s_high, price)
        base["ict_pd_pct"]         = pd["pct"]
        base["ict_pd_zone"]        = pd["zone"]
        base["ict_in_ote_long"]    = pd["in_ote_long"]
        base["ict_in_ote_short"]   = pd["in_ote_short"]

    # ── Multi-day dealing range + PD zone ──────────────────────────────
    # Uses the 20-day H/L from daily_ctx when available; else falls
    # back to the full intraday frame if it spans ≥ 2 calendar days.
    # Predicates that key off this (Multi-day PD L/S) noop when the
    # range is None, so a 1-day cold-start scan degrades safely.
    mdr = _multi_day_range(bars, daily_ctx)
    if mdr is not None:
        base["ict_multi_day_range"] = mdr
        base["ict_multi_day_pd_zone"] = pd_array(mdr["low"], mdr["high"], price)["zone"]

    # ── Sweeps on named liquidity levels ────────────────────────────────
    sweeps = detect_sweep(bars, liquidity, confirm_lookahead=1)
    if sweeps:
        latest_sweep  = max(sweeps, key=lambda e: e.idx)
        bull_sweeps   = [e for e in sweeps if e.direction == "bullish"]
        bear_sweeps   = [e for e in sweeps if e.direction == "bearish"]
        base["ict_last_sweep"]      = _event_to_dict_with_age(latest_sweep, last_idx)
        base["ict_last_bull_sweep"] = _event_to_dict_with_age(
            max(bull_sweeps, key=lambda e: e.idx) if bull_sweeps else None, last_idx)
        base["ict_last_bear_sweep"] = _event_to_dict_with_age(
            max(bear_sweeps, key=lambda e: e.idx) if bear_sweeps else None, last_idx)

    # ── MSS events ──────────────────────────────────────────────────────
    mss_events = detect_mss(bars, swings, lookback_swings=4)
    if mss_events:
        latest_mss = max(mss_events, key=lambda e: e.idx)
        bull_mss   = [e for e in mss_events if e.direction == "bullish"]
        bear_mss   = [e for e in mss_events if e.direction == "bearish"]
        base["ict_last_mss"]      = _event_to_dict_with_age(latest_mss, last_idx)
        base["ict_last_bull_mss"] = _event_to_dict_with_age(
            max(bull_mss, key=lambda e: e.idx) if bull_mss else None, last_idx)
        base["ict_last_bear_mss"] = _event_to_dict_with_age(
            max(bear_mss, key=lambda e: e.idx) if bear_mss else None, last_idx)

    # ── Fair Value Gaps + Order Blocks ──────────────────────────────────
    fvgs = detect_fvgs(bars)
    obs  = detect_order_blocks(bars, disp_atr_mult=1.2, disp_lookahead=3)

    # "Fresh" = unmitigated AND recent. Mitigated zones are useful for
    # frontend history overlays but shouldn't fire predicates.
    fresh_bull: list[FVG | OrderBlock] = [
        z for z in (*fvgs, *obs)
        if z.direction == "bullish"
        and z.mitigated_idx is None
        and (last_idx - z.created_idx) <= _MAX_ZONE_AGE_BARS
    ]
    fresh_bear: list[FVG | OrderBlock] = [
        z for z in (*fvgs, *obs)
        if z.direction == "bearish"
        and z.mitigated_idx is None
        and (last_idx - z.created_idx) <= _MAX_ZONE_AGE_BARS
    ]

    # Widen the touch test by a small ATR pad so predicates fire on
    # approach, not only when a 1-minute bar wick prints exactly inside
    # the zone. We re-derive ATR here rather than threading it through
    # the call surface — the detectors module already owns it.
    from .detectors import _atr as _atr_local
    atr = _atr_local(bars, 14)
    atr_pad = _ZONE_TOUCH_ATR_PAD * atr

    nearest_bull = _nearest_zone(fresh_bull, price, atr_pad)
    nearest_bear = _nearest_zone(fresh_bear, price, atr_pad)
    if nearest_bull is not None:
        nearest_bull["bars_ago"] = last_idx - nearest_bull["created_idx"]
    if nearest_bear is not None:
        nearest_bear["bars_ago"] = last_idx - nearest_bear["created_idx"]

    base["ict_nearest_bull_zone"] = nearest_bull
    base["ict_nearest_bear_zone"] = nearest_bear
    base["ict_in_bull_zone"] = nearest_bull is not None
    base["ict_in_bear_zone"] = nearest_bear is not None

    # ── Breaker blocks (mitigated OBs flipped by opposing MSS) ──────────
    # We recompute from the already-derived obs + mss_events so there's
    # no duplicate detector pass. The age cap mirrors the OB/FVG cap
    # (stale breakers pollute nearest-breaker the same way stale FVGs do).
    recent_obs = [z for z in obs if (last_idx - z.created_idx) <= _MAX_ZONE_AGE_BARS]
    bull_breakers, bear_breakers = _derive_breakers(recent_obs, mss_events, last_idx)

    # Edge detection needs the prior bar's close to know whether the
    # current bar represents a fresh entry into the breaker zone vs.
    # price merely sitting inside it. Falls back to None on a 1-bar
    # window (cold start), where _nearest_breaker treats fresh_entry
    # as True since there's no history to contradict it.
    prev_price = float(bars[last_idx - 1]["close"]) if last_idx >= 1 else None
    nearest_bull_brk = _nearest_breaker(bull_breakers, price, prev_price, atr_pad)
    nearest_bear_brk = _nearest_breaker(bear_breakers, price, prev_price, atr_pad)
    base["ict_nearest_bull_breaker"] = nearest_bull_brk
    base["ict_nearest_bear_breaker"] = nearest_bear_brk
    base["ict_in_bull_breaker"] = nearest_bull_brk is not None
    base["ict_in_bear_breaker"] = nearest_bear_brk is not None

    # ── Full zone geometry for frontend overlay ─────────────────────────
    # Cap counts so we don't ship hundreds of tiny FVGs to the browser.
    # The cap is per-list; 20 is plenty for a visible-on-screen chart.
    def _serialize(items: list, limit: int = 20) -> list[dict]:
        # Take the most recent ``limit`` by creation index
        items = sorted(items, key=lambda z: getattr(z, "created_idx", getattr(z, "idx", 0)), reverse=True)[:limit]
        items.sort(key=lambda z: getattr(z, "created_idx", getattr(z, "idx", 0)))
        return [z.to_dict() for z in items]

    base["ict_zones"] = {
        "fvgs":            _serialize(fvgs),
        "obs":             _serialize(obs),
        "swings":          _serialize(swings, limit=30),
        "sweeps":          _serialize(sweeps, limit=10),
        "mss_events":      _serialize(mss_events, limit=10),
        "liquidity":       {k: round(v, 4) for k, v in liquidity.items()},
        "breakers_bull":   bull_breakers[:10],
        "breakers_bear":   bear_breakers[:10],
        "opening_range":   base["ict_opening_range"],
        "multi_day_range": base["ict_multi_day_range"],
    }

    return base


__all__ = [
    "compute_ict_features",
    "KILLZONES",
    "killzone",
]
