"""
ICT detector primitives — stateless functions over a bar sequence.
===================================================================

Everything here operates on a list of bar dicts with keys
``time`` (unix seconds), ``open``, ``high``, ``low``, ``close``,
``volume`` — the same shape the rest of the scanner uses
(see ``alpaca.convert_alpaca_bars``).

Design rules
------------
1. **Stateless.** Every function takes a full bar series and derives
   its result fresh. We recompute on every scan cycle; the cost is
   small (O(N) per bar for most detectors) and it avoids a per-symbol
   state file that would desynchronize the moment anything crashes.
2. **Pure.** No I/O, no logging that isn't explicit, no mutation of
   the input list. Everything returns plain dicts / dataclasses that
   JSON-serialise cleanly for the ``ind`` payload and the frontend
   overlay endpoint.
3. **Degraded gracefully.** Too few bars → returns ``[]`` or ``None``
   rather than raising. The Tier-1 predicates that consume these
   outputs should all be ``ind.get(...)`` guarded.
4. **Indices, not timestamps.** Zones reference bar indices into the
   input list; callers translate to timestamps when they need to
   render or log. Keeps internal math tight.

Concepts covered
----------------
  • Swing pivots  (``detect_swings``)
  • Market structure shift (``detect_mss``)
  • Liquidity sweeps of named levels (``detect_sweep``)
  • Fair value gaps — 3-candle imbalances (``detect_fvgs``)
  • Order blocks — last opposing candle before displacement (``detect_order_blocks``)
  • Premium/Discount within a dealing range (``pd_array``)
  • Killzones (NY AM / Silver Bullet / NY PM), see ``killzone()``

All of these are the minimum viable ICT toolkit; higher-order
concepts (breaker blocks, Judas swing, SMT, Power-of-3) would build
on top of this and are deliberately OUT of scope for the Option 1
MVP the scanner wires up.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, time, timezone
from typing import Iterable, Literal
from zoneinfo import ZoneInfo


# ── Timezone shared with the rest of the backend ────────────────────────────

_ET = ZoneInfo("America/New_York")


# ── Data classes for zones / events ─────────────────────────────────────────
# Every zone carries both its geometry (price range) AND its origin
# bar index. The frontend draws from origin to "now" (or to mitigated
# bar if we set one) so the box extends across the chart the way
# TradingView users expect.

Direction = Literal["bullish", "bearish"]


@dataclass
class Swing:
    idx: int
    price: float
    kind: Literal["high", "low"]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FVG:
    """A 3-candle Fair Value Gap.

    Bullish FVG (buyside imbalance): ``candle[i-2].high < candle[i].low``
    ⇒ price "skipped" the range [c[i-2].high, c[i].low] going up.
    The gap sits below current price and is a demand zone on revisit.

    Bearish FVG (sellside imbalance): ``candle[i-2].low > candle[i].high``
    ⇒ price skipped [c[i].high, c[i-2].low] going down.
    """
    low:       float        # bottom of the gap range
    high:      float        # top of the gap range
    direction: Direction    # "bullish" = demand zone below, "bearish" = supply zone above
    created_idx: int        # the index of the middle (c[i-1]) creation bar
    mitigated_idx: int | None = None  # None = still live; else first bar that traded back into it

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderBlock:
    """The last opposing candle before a displacement in the trade direction.

    Bullish OB: last DOWN-close candle whose body is then swept away
    by a strong up-move that takes out its high and keeps going.
    We measure displacement as "next ``k`` bars' total range ≥
    ``disp_atr_mult`` × ATR" AND the next bar closes above the OB high.

    Bearish OB: mirror — last UP-close candle before a strong down-move
    that takes out its low.
    """
    low:       float
    high:      float
    direction: Direction
    created_idx: int
    mitigated_idx: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MSSEvent:
    """Market Structure Shift: the first close that breaks the most recent
    opposing swing pivot AFTER a string of same-direction pivots.

    ``bullish`` MSS = we had LH / LL swings, then a close > the most
    recent LH price. Shifts structure from bearish to bullish.
    """
    idx: int
    direction: Direction
    broken_swing_price: float
    broken_swing_idx: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SweepEvent:
    """A liquidity sweep: a bar's wick breaches a named level and the
    SAME bar (or next one) closes back on the starting side, leaving
    an accumulated stop-run pool behind. ``direction`` is the
    *implication*, not the wick direction:

      wicked below a low and closed back up → direction = "bullish"
      wicked above a high and closed back down → direction = "bearish"

    ``wick_extreme`` is the price the wick reached (low for a bullish
    sweep, high for a bearish sweep) — i.e. the deepest excursion past
    the swept level. ``magnitude`` (the absolute distance from
    ``level_price`` to ``wick_extreme``) is what downstream predicates
    use to size the sweep against ATR: a 1-tick wick through PDL is
    noise; a 0.5×ATR wick is a genuine stop-run.
    """
    idx: int
    level_name: str      # "pdh" / "pdl" / "session_high" / "session_low" / "asian_high" / ...
    level_price: float
    direction: Direction
    wick_extreme: float  # bar.low for "bullish", bar.high for "bearish"

    def to_dict(self) -> dict:
        d = asdict(self)
        # Surface the derived ``magnitude`` so consumers don't have to
        # recompute it. abs() guards against feed sanity issues where
        # wick_extreme accidentally lands on the wrong side of level.
        d["magnitude"] = abs(self.wick_extreme - self.level_price)
        return d


# ── Small helpers ───────────────────────────────────────────────────────────

def _atr(bars: list[dict], period: int = 14) -> float:
    """Simple Wilder-ish ATR over the last ``period`` completed bars.

    For ICT we only need a scalar to size "what counts as displacement"
    — not a full series — so this is cheaper than the pandas ATR in
    ``analyzer.py``. Returns 0.0 if there aren't enough bars.
    """
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]
        l = bars[i]["low"]
        pc = bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    tail = trs[-period:]
    return sum(tail) / len(tail)


def _body_high_low(b: dict) -> tuple[float, float]:
    """Return (body_high, body_low) — useful for OB body-based zones.
    ICT usage splits: body-only OBs vs wick-inclusive OBs. We use the
    more conservative wick-inclusive range (high/low) for OBs because
    at 1-minute resolution bodies are too tight to catch retests.
    """
    o, c = b["open"], b["close"]
    return (max(o, c), min(o, c))


# ── Swings ──────────────────────────────────────────────────────────────────

def detect_swings(bars: list[dict], k: int = 3) -> list[Swing]:
    """Return swing highs and lows in chronological order.

    A swing high at bar ``i`` = ``bars[i].high`` is strictly greater
    than the high of the preceding ``k`` bars AND strictly greater
    than the high of the following ``k`` bars. Symmetric for lows.

    ``k=3`` gives a noise/sensitivity tradeoff that works reasonably
    well on 1-minute equities. For a quieter chart bump to k=5.

    Note: this is the classic "fractal" pivot — NOT a windowed
    high-of-last-N detector. A pivot here must be surrounded on BOTH
    sides, so the most recent ``k`` bars can never produce a pivot
    (they might become one later once more bars print). Downstream
    MSS detection takes this into account.
    """
    out: list[Swing] = []
    n = len(bars)
    if n < (2 * k + 1):
        return out
    for i in range(k, n - k):
        hi = bars[i]["high"]
        lo = bars[i]["low"]
        left  = bars[i - k: i]
        right = bars[i + 1: i + k + 1]
        if hi > max(b["high"] for b in left) and hi > max(b["high"] for b in right):
            out.append(Swing(idx=i, price=hi, kind="high"))
        if lo < min(b["low"]  for b in left) and lo < min(b["low"]  for b in right):
            out.append(Swing(idx=i, price=lo, kind="low"))
    # A single bar can't be both a swing high and low under ``>``/``<``
    # strict comparisons, so ordering by idx is sufficient (no ties).
    out.sort(key=lambda s: s.idx)
    return out


# ── Fair Value Gaps ─────────────────────────────────────────────────────────

def detect_fvgs(bars: list[dict]) -> list[FVG]:
    """Find all 3-candle FVGs in the series, tagging mitigation when seen.

    For a triple (c[i-2], c[i-1], c[i]):
      * Bullish FVG: c[i-2].high < c[i].low — gap = [c[i-2].high, c[i].low]
      * Bearish FVG: c[i-2].low  > c[i].high — gap = [c[i].high,  c[i-2].low]

    Mitigation: the first later bar ``j`` where the bar's low/high
    crosses back into the gap range. For a bullish gap, mitigation =
    first ``j > i`` with ``bars[j].low <= gap.high``. Symmetric for
    bearish. We record the FIRST touch; subsequent touches are
    implicitly "already mitigated".

    Caveat: at 1-minute resolution, FVGs are noisy — you'll see many
    small ones. Downstream callers filter by minimum size (``min_size_atr``)
    when they care about tradeable ones. We return everything here so
    the frontend can paint them all and the filtering can stay in one
    spot (the feature builder).
    """
    out: list[FVG] = []
    if len(bars) < 3:
        return out

    for i in range(2, len(bars)):
        a = bars[i - 2]
        c = bars[i]
        if a["high"] < c["low"]:
            gap = FVG(
                low=a["high"], high=c["low"],
                direction="bullish",
                created_idx=i - 1,
            )
            out.append(gap)
        elif a["low"] > c["high"]:
            gap = FVG(
                low=c["high"], high=a["low"],
                direction="bearish",
                created_idx=i - 1,
            )
            out.append(gap)

    # Mitigation pass: for each FVG, walk forward from its creation
    # and record the first bar that trades back into the gap. This is
    # a separate pass because we want the full FVG list even if all
    # of them are unmitigated.
    for fvg in out:
        for j in range(fvg.created_idx + 2, len(bars)):
            b = bars[j]
            if fvg.direction == "bullish":
                # price dipped back into the demand gap
                if b["low"] <= fvg.high:
                    fvg.mitigated_idx = j
                    break
            else:
                # price rallied back into the supply gap
                if b["high"] >= fvg.low:
                    fvg.mitigated_idx = j
                    break

    return out


# ── Order Blocks ────────────────────────────────────────────────────────────

def detect_order_blocks(
    bars: list[dict],
    *,
    disp_atr_mult: float = 1.2,
    disp_lookahead: int = 3,
    atr_period: int = 14,
) -> list[OrderBlock]:
    """Find bullish/bearish order blocks across the series.

    Definition used here (intraday-tight):
      Bullish OB at bar ``i`` (down-close candle) if:
        * ``close[i] < open[i]`` (down candle)
        * The next ``disp_lookahead`` bars' cumulative range
          (max(high[i+1..]) − min(low[i+1..])) ≥
          ``disp_atr_mult`` × ATR(14), AND
        * ``max(high[i+1..]) > high[i]`` — i.e. the displacement
          actually took out the OB high in the UP direction.

      Bearish OB: mirror (up-close candle, subsequent displacement
      takes out the low).

    Mitigation: first bar after the OB whose range touches back
    into [low, high]. Same rule for both directions.

    We keep the full list (including mitigated ones). Downstream the
    feature builder filters to "fresh" (unmitigated) OBs when the
    predicates look for retest entries.
    """
    out: list[OrderBlock] = []
    n = len(bars)
    if n < (disp_lookahead + 2):
        return out

    atr = _atr(bars, atr_period)
    if atr <= 0:
        return out
    displacement_threshold = disp_atr_mult * atr

    for i in range(n - disp_lookahead - 1):
        b  = bars[i]
        look = bars[i + 1: i + 1 + disp_lookahead]
        look_high = max(x["high"] for x in look)
        look_low  = min(x["low"]  for x in look)
        cum_range = look_high - look_low
        if cum_range < displacement_threshold:
            continue

        # Bullish OB: down candle, subsequent impulse takes out its high
        if b["close"] < b["open"] and look_high > b["high"]:
            out.append(OrderBlock(
                low=b["low"], high=b["high"],
                direction="bullish",
                created_idx=i,
            ))
        # Bearish OB: up candle, subsequent impulse takes out its low
        if b["close"] > b["open"] and look_low < b["low"]:
            out.append(OrderBlock(
                low=b["low"], high=b["high"],
                direction="bearish",
                created_idx=i,
            ))

    # Mitigation pass — start search AFTER the displacement window
    # so we don't flag the displacement itself as the mitigation.
    for ob in out:
        start = ob.created_idx + disp_lookahead + 1
        for j in range(start, n):
            b = bars[j]
            if b["low"] <= ob.high and b["high"] >= ob.low:
                ob.mitigated_idx = j
                break

    return out


# ── Market Structure Shift ──────────────────────────────────────────────────

def detect_mss(
    bars: list[dict],
    swings: list[Swing],
    *,
    lookback_swings: int = 4,
) -> list[MSSEvent]:
    """Detect MSS events across the bar series.

    Algorithm:
      Walk the swings in order. Keep a rolling picture of the most
      recent ``lookback_swings`` highs and lows. Two patterns fire:

        Bullish MSS: after at least one lower-high (LH) forms relative
        to an earlier high, a subsequent bar CLOSES above that most
        recent LH. The close bar is the MSS event; the "broken swing"
        is the LH that was taken out.

        Bearish MSS: symmetric — after a higher-low (HL), a close
        below it.

    We report one MSS per rolling window; once an MSS fires in a
    direction, we reset its anchor so we don't re-fire on the same
    level. This matches how ICT traders read it on a chart: one
    structural break per leg, not a cluster of them.
    """
    if not bars or not swings:
        return []

    out: list[MSSEvent] = []

    # Build an index of close prices for fast "did price close above X" checks.
    closes = [b["close"] for b in bars]

    highs = [s for s in swings if s.kind == "high"]
    lows  = [s for s in swings if s.kind == "low"]

    # Bullish MSS — loop through consecutive high pairs looking for LH,
    # then scan forward for a close above the LH price.
    last_fired_bull = -1
    for j in range(1, len(highs)):
        prev_h = highs[j - 1]
        lh     = highs[j]
        if lh.price >= prev_h.price:
            continue  # not a lower high, no setup here
        if lh.idx <= last_fired_bull:
            continue  # already fired past this anchor
        # Scan forward from the bar after the LH for the first close > LH.price
        for i in range(lh.idx + 1, len(bars)):
            if closes[i] > lh.price:
                out.append(MSSEvent(
                    idx=i, direction="bullish",
                    broken_swing_price=lh.price, broken_swing_idx=lh.idx,
                ))
                last_fired_bull = i
                break

    # Bearish MSS — mirror.
    last_fired_bear = -1
    for j in range(1, len(lows)):
        prev_l = lows[j - 1]
        hl     = lows[j]
        if hl.price <= prev_l.price:
            continue  # not a higher low
        if hl.idx <= last_fired_bear:
            continue
        for i in range(hl.idx + 1, len(bars)):
            if closes[i] < hl.price:
                out.append(MSSEvent(
                    idx=i, direction="bearish",
                    broken_swing_price=hl.price, broken_swing_idx=hl.idx,
                ))
                last_fired_bear = i
                break

    out.sort(key=lambda e: e.idx)
    return out


# ── Liquidity sweeps ────────────────────────────────────────────────────────

def detect_sweep(
    bars: list[dict],
    liquidity_levels: dict[str, float],
    *,
    confirm_lookahead: int = 1,
) -> list[SweepEvent]:
    """Flag bars that wick through a named level and close back inside.

    ``liquidity_levels`` = ``{"pdh": 101.2, "pdl": 99.0, ...}``. For
    each level we scan the series and emit a SweepEvent when:

      * Wicked ABOVE: ``bar.high > level`` AND
        ``bar.close < level`` (same bar) OR the next ``confirm_lookahead``
        bars close below the level. Direction = "bearish" (swept
        buyside liquidity, implied downside).

      * Wicked BELOW: ``bar.low < level`` AND same-bar close above
        OR next ``confirm_lookahead`` bars close above. Direction =
        "bullish".

    We dedupe per-level so the first wick is the event; subsequent
    retests are NOT re-emitted as fresh sweeps (they're retest
    entries, a different pattern).
    """
    out: list[SweepEvent] = []
    if not bars:
        return out

    # Process each level independently; within a level we emit at most
    # one sweep per direction so a level that gets wicked twice from
    # opposite sides produces two events (one of each).
    for name, level in liquidity_levels.items():
        if level is None:
            continue
        fired_bull = False
        fired_bear = False
        for i, b in enumerate(bars):
            # Bearish sweep: wick above, close back below
            if not fired_bear and b["high"] > level:
                close_back = b["close"] < level
                if not close_back and confirm_lookahead > 0:
                    nxt = bars[i + 1: i + 1 + confirm_lookahead]
                    close_back = any(x["close"] < level for x in nxt)
                if close_back:
                    out.append(SweepEvent(
                        idx=i, level_name=name, level_price=float(level),
                        direction="bearish",
                        wick_extreme=float(b["high"]),
                    ))
                    fired_bear = True
            # Bullish sweep: wick below, close back above
            if not fired_bull and b["low"] < level:
                close_back = b["close"] > level
                if not close_back and confirm_lookahead > 0:
                    nxt = bars[i + 1: i + 1 + confirm_lookahead]
                    close_back = any(x["close"] > level for x in nxt)
                if close_back:
                    out.append(SweepEvent(
                        idx=i, level_name=name, level_price=float(level),
                        direction="bullish",
                        wick_extreme=float(b["low"]),
                    ))
                    fired_bull = True
            if fired_bull and fired_bear:
                break

    out.sort(key=lambda e: e.idx)
    return out


# ── Premium / Discount ──────────────────────────────────────────────────────

def pd_array(
    dealing_low: float,
    dealing_high: float,
    price: float,
) -> dict:
    """Position ``price`` within the ``[dealing_low, dealing_high]`` range.

    Returns ``{"pct", "zone", "in_ote_long", "in_ote_short"}``:
      * ``pct`` — 0.0 at the low, 1.0 at the high.
      * ``zone`` — "discount" (< 0.5), "equilibrium" ([0.45, 0.55]),
        "premium" (> 0.5). Equilibrium band is ± 5% around the mid so
        the UI can paint a visibly-wider neutral stripe without every
        touch flicking between labels.
      * ``in_ote_long``  — True when price is in [0.21, 0.38] — i.e.
        a 62–79% discount retracement of the dealing range. This is
        the classic ICT "Optimal Trade Entry" zone for longs.
      * ``in_ote_short`` — True when price is in [0.62, 0.79] — the
        equivalent premium retrace for shorts.
    """
    if dealing_high <= dealing_low:
        return {"pct": 0.5, "zone": "equilibrium",
                "in_ote_long": False, "in_ote_short": False}
    pct = (price - dealing_low) / (dealing_high - dealing_low)
    pct_clamped = max(0.0, min(1.0, pct))
    if pct_clamped < 0.45:
        zone = "discount"
    elif pct_clamped > 0.55:
        zone = "premium"
    else:
        zone = "equilibrium"
    return {
        "pct":            round(pct_clamped, 3),
        "zone":           zone,
        "in_ote_long":    0.21 <= pct_clamped <= 0.38,
        "in_ote_short":   0.62 <= pct_clamped <= 0.79,
    }


def dealing_range_from_swings(swings: list[Swing]) -> tuple[float, float] | None:
    """Pick the dealing range = most recent significant swing-low to the
    most recent significant swing-high (or vice versa), whichever came
    last. This is the simplest reading of "the current leg" — more
    sophisticated PD analysis uses multi-timeframe anchored ranges,
    which is Phase 2.

    Returns ``(low, high)`` or ``None`` if we can't find one of each.
    """
    last_high = next((s for s in reversed(swings) if s.kind == "high"), None)
    last_low  = next((s for s in reversed(swings) if s.kind == "low"),  None)
    if last_high is None or last_low is None:
        return None
    return (last_low.price, last_high.price)


# ── Killzones ───────────────────────────────────────────────────────────────
#
# Windows are defined in Eastern Time because US stock sessions + ICT
# convention are both ET-anchored. Times are LOCAL to America/New_York,
# which handles DST automatically via zoneinfo.
#
# * London       — 02:00-05:00 ET: European institutional session. US
#                  equities barely trade here so there's no live US-
#                  equity strategy gated to London — but we still label
#                  the window so a futures/FX extension can filter on it
#                  and so the UI can colour-code pre-open scans.
# * NY AM        — 09:30-11:00 ET: the high-conviction morning window.
# * Silver Bullet— 10:00-11:00 ET: a tighter subset. ICT claims the
#                  cleanest FVG-retrace setups fire inside this hour.
# * NY PM        — 13:30-16:00 ET: the afternoon session, second most
#                  active window for ICT intraday setups.

KILLZONES: dict[str, tuple[time, time]] = {
    "london":        (time(2,  0),  time(5,   0)),
    "ny_am":         (time(9, 30),  time(11,  0)),
    "silver_bullet": (time(10, 0),  time(11,  0)),
    "ny_pm":         (time(13, 30), time(16,  0)),
}


def killzone(ts_utc: datetime | int | float) -> str | None:
    """Return the most specific killzone name, or None if outside all.

    ``ts_utc`` may be a ``datetime`` (aware or naive — naive is
    assumed UTC) or a unix-seconds number. We convert to ET before
    testing.

    Ordering matters: Silver Bullet is a subset of NY AM, so we check
    it FIRST and return the more specific label when both match.
    """
    if isinstance(ts_utc, (int, float)):
        dt = datetime.fromtimestamp(ts_utc, tz=timezone.utc)
    else:
        dt = ts_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(_ET).time()

    # Silver Bullet first — most specific label.
    sb_start, sb_end = KILLZONES["silver_bullet"]
    if sb_start <= et <= sb_end:
        return "silver_bullet"
    am_start, am_end = KILLZONES["ny_am"]
    if am_start <= et <= am_end:
        return "ny_am"
    pm_start, pm_end = KILLZONES["ny_pm"]
    if pm_start <= et <= pm_end:
        return "ny_pm"
    ln_start, ln_end = KILLZONES["london"]
    if ln_start <= et <= ln_end:
        return "london"
    return None
