"""
haiku_screener.py — Two-stage AI pre-screen (feature #11).

Inserted between the Tier-1 Python strategy pre-screen and the Tier-2 Opus
deep analysis call in autotrade.py. Claude Haiku 4.5 looks at the matched
strategy + a minimal market snapshot and decides:

    "Is this setup valid and worth a full AI analysis, yes or no?"

Valid setups escalate to Opus (the existing analyze_chart path). Rejected
setups short-circuit — no Opus call, no news classifier call, no pending
approval ever lands on the user's screen. Rejected candidates get a
reason logged for audit so the user can spot-check Haiku's judgment
against what Opus would have said.

## Why two-stage?

- Opus + extended thinking costs ~$0.10-0.30 per call, takes 3-8s.
- Haiku costs ~$0.01 per call, returns in <1s.
- If Haiku rejects ~80% of Tier-1 matches as false alarms, the Opus
  budget drops 5× and the scan cycle speeds up ~3×. Fewer bad
  approvals reach the user, too.

## What Haiku sees

Intentionally a small payload — the whole point is to be fast + cheap:
- Symbol, current price, intended direction
- Matched strategy names (Tier-1 output: e.g. "Oversold Bounce long")
- Recent 5-min price action (last 10 bars, summarized)
- Daily context (prev close, 20d range position, today's % move)
- Structural levers (added 2026-04-25 to make the gate earn its keep):
    * ``vol_ratio`` — current bar volume / 20-bar mean. Below 0.8 is a
      low-conviction breakout.
    * ``ict_killzone`` — which ICT killzone (if any) the last bar
      lands in. Off-killzone setups are structurally weaker.
    * ``regime`` — SPY trending_up / trending_down / choppy / unknown.
      Lets Haiku reject longs in trending_down explicitly.
    * ``rs_zscore`` — stock-vs-SPY excess return in pp. Negative on a
      long (or positive on a short) is an active anti-confirmation.

What Haiku does NOT see (by design):
- Full intraday history, news articles, sector rotation, Level 2 —
  those are Opus's job. If we send them to Haiku we're paying Opus
  prices for a Haiku call.

## Output

A structured {valid, reason} verdict. "valid" is a strict yes/no — it
doesn't compose into the confidence multiplier stack (that's #6/#8/#9/#10).
"reason" is a one-sentence rationale captured on the TradeLog and shown
on the approval card, so when a signal DOES make it through, the user
sees the pre-screen rationale; when a signal is REJECTED, the reason
goes to the log where it can be audited.

## Degraded behavior

Fail-open. On API timeout, parse failure, or missing API key, we return
None. The caller treats None as "pass through to Opus" — we'd rather
waste one Opus call than drop a real signal because of transient
infra flakiness. Matches the #8/#9 degraded-no-op pattern.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import anthropic

from .settings import settings

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Haiku 4.5 — same model the news classifier (#9) uses. Fast, cheap,
# plenty smart for a "valid setup or false alarm?" binary judgment.
_MODEL = "claude-haiku-4-5"

# Caps for the two bar blocks Haiku sees. Tier-1 fetches 1-min bars
# (~480/symbol over 8h lookback), so we slice the recent 50 directly
# and aggregate the same 1-min stream into 50 5-min bars in-process.
# Why both timeframes:
#   * 50 1-min bars (50 minutes) — micro-structure for fresh-vs-stale
#     judgments. Tells Haiku whether the matched pattern fired in the
#     last 1-2 bars or is already 30 min old and blown.
#   * 50 5-min bars (~4 hours) — pre-market / extended-hours context
#     plus the broader intraday structure. Lets Haiku enforce the
#     "extended-hours match with no RTH confirmation" rejection
#     criterion the prompt already names.
# Aggregation is pure-Python on bars we already have in memory — no
# extra Alpaca call, ~sub-millisecond per symbol.
_RECENT_BARS_1MIN = 50
_RECENT_BARS_5MIN = 50
_FIVE_MIN_SECONDS = 300

# Output cap. This is a yes/no gate, not an essay. The prompt asks for a
# one-sentence reason so 200 tokens is plenty.
_MAX_OUTPUT_TOKENS = 200


# ── Public types ─────────────────────────────────────────────────────────────

@dataclass
class ScreenerVerdict:
    """Haiku's verdict on a single candidate setup."""
    valid:  bool    # True = escalate to Opus; False = drop the candidate
    reason: str     # one-line rationale (logged + shown on approval card)


# ── Public API ───────────────────────────────────────────────────────────────

def screen_setup(
    symbol: str,
    *,
    price: float,
    direction: str,
    matched_strategies: list[dict],
    bars: list[dict] | None,
    daily_bars: list[dict] | None,
    vol_ratio: float | None = None,
    ict_killzone: str | None = None,
    regime: str | None = None,
    rs_zscore: float | None = None,
) -> ScreenerVerdict | None:
    """Ask Haiku whether this Tier-1 match is a valid setup.

    Args:
        symbol:             Ticker, e.g. "NVDA".
        price:              Current price (for logging / prompt context).
        direction:          "long" or "short" — what the Tier-1 matcher inferred.
        matched_strategies: Output of the Tier-1 pre-screen. List of dicts with
                            ``{name, direction}``.
        bars:               Recent intraday bars (5-min), any length. We take the
                            last ``_RECENT_BARS``.
        daily_bars:         Daily bars for prev close / 20d range context. Optional.
        vol_ratio:          Current bar volume / 20-bar mean. Optional —
                            rendered as "n/a" when missing.
        ict_killzone:       Killzone label for the last bar (e.g.
                            ``"ny_am"``, ``"silver_bullet"``) or ``None``.
        regime:             SPY regime: ``trending_up`` / ``trending_down``
                            / ``choppy`` / ``unknown``.
        rs_zscore:          Stock-minus-SPY excess return in pp.

    Returns:
        ``ScreenerVerdict`` when Haiku responds with parseable JSON. ``None``
        when Claude is unreachable, the response is unparseable, or the API
        key is missing — caller fails open and proceeds to Opus.
    """
    if not settings.anthropic_api_key:
        logger.debug("haiku_screen_skip symbol=%s reason=no_api_key", symbol)
        return None
    if not matched_strategies:
        # Tier-1 didn't match anything — shouldn't be called in this case,
        # but belt-and-suspenders: pass through rather than drop silently.
        return None
    if direction not in ("long", "short"):
        return None

    prompt = _build_prompt(
        symbol=symbol,
        price=price,
        direction=direction,
        matched_strategies=matched_strategies,
        bars=bars or [],
        daily_bars=daily_bars or [],
        vol_ratio=vol_ratio,
        ict_killzone=ict_killzone,
        regime=regime,
        rs_zscore=rs_zscore,
    )

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        message = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (message.content[0].text or "").strip()
    except Exception as exc:
        # Fail-open. Never block the scan loop on a Haiku hiccup.
        logger.warning("haiku_screen_failed symbol=%s error=%s",
                       symbol, str(exc)[:200])
        return None

    verdict = _parse_verdict(raw, symbol=symbol)
    if verdict is None:
        logger.warning("haiku_screen_parse_failed symbol=%s raw=%r",
                       symbol, raw[:200])
        return None

    # Rejected candidates get the reason at INFO so the user can skim
    # logs to audit Haiku's judgment. Accepted candidates log at DEBUG
    # (the approval card itself carries the rationale).
    log_level = logging.INFO if not verdict.valid else logging.DEBUG
    logger.log(
        log_level,
        "haiku_screen symbol=%s valid=%s reason=%r",
        symbol, verdict.valid, verdict.reason[:120],
    )
    return verdict


def verdict_dict(v: ScreenerVerdict | None) -> dict | None:
    """JSON-safe serialization for the approval card + audit logs."""
    if v is None:
        return None
    return {"valid": v.valid, "reason": v.reason}


# ── Internals ────────────────────────────────────────────────────────────────

_PROMPT = """You are a day-trading setup screener. A Python pattern matcher flagged the following stock. Your job: is this a REAL setup worth running the slow, expensive deep-analysis model on, or is it a false alarm we should skip?

Symbol: {symbol}
Current price: ${price:.2f}
Intended direction: {direction}

Tier-1 matched strategies:
{strategies}

Recent 1-min price action (last 50 bars, most recent last):
{bars_1min}

Recent 5-min price action (last 50 bars, derived from the 1-min stream, most recent last):
{bars_5min}

Daily context:
{daily}

Structural context:
{structural}

## How to decide

Return VALID when the match looks clean:
- Pattern is fresh (broke out / bounced in the last 1-2 bars, not 30 min ago)
- Price action confirms the setup thesis
- Volume on the key bar is at least average
- Multiple strategies pointing the same direction is a stronger signal

Return NOT VALID (false alarm) when:
- The move has already happened and the entry is blown (pattern matched 6+ bars ago with no follow-through)
- Conflicting signals — long setup but price is making new lows on the recent bars
- Dead volume — the "breakout" is on 10% of average volume
- The pattern is triggering on a gap candle that hasn't confirmed
- Extended-hours / pre-market match with no RTH confirmation
- Price is inside a tight chop range and the strategy is a trend-follower
- vol_ratio < 0.8 — the bar driving the match has below-average volume
- Long setup in regime=trending_down (or short setup in trending_up) — fighting the tape
- ict_killzone is None — outside the active liquidity windows
- rs_zscore is meaningfully negative on a long (or positive on a short) — anti-confirmation

Default to NOT VALID unless you can articulate two specific recent-bar facts that confirm the setup.

Return ONLY valid JSON, no prose, no markdown fence:

{{
  "valid": true | false,
  "reason": "<one tight sentence, under 120 chars, no quotes>"
}}"""


def _bar_field(b: dict, *keys: str) -> float | None:
    """Pull the first non-None OHLCV field from a bar dict.

    Bars arrive in two shapes in this codebase:
      * raw Alpaca → ``{"o","h","l","c","v","t"}``
      * post-``convert_alpaca_bars`` → ``{"open","high","low","close","volume","time"}``

    Both shapes are accepted everywhere a bar is read. Returns ``None``
    when every candidate key is missing or unparseable so the caller can
    skip the bar instead of silently coercing missing data to 0.
    """
    for k in keys:
        v = b.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _bar_time(b: dict) -> int | None:
    """Extract a unix-second timestamp from a bar dict.

    Accepts ``time`` (int unix seconds, the canonical post-convert form),
    or ``t`` (Alpaca raw — int unix seconds OR ISO-8601 string). Returns
    ``None`` when nothing parseable is present, which makes the bar
    invisible to ``_resample_to_5min`` (it can't bucket without a clock).
    """
    raw = b.get("time")
    if raw is None:
        raw = b.get("t")
    if raw is None:
        return None
    # Already numeric (post-convert_alpaca_bars or hand-built ints).
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    # ISO-8601 string fallback (raw Alpaca shape).
    try:
        from datetime import datetime as _dt
        return int(
            _dt.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        )
    except (TypeError, ValueError):
        return None


def _resample_to_5min(bars: list[dict]) -> list[dict]:
    """Aggregate 1-min OHLCV bars into 5-min OHLCV bars by UTC bucket.

    Bucketing rule: ``floor(time / 300) * 300`` — i.e. the 5-min bucket
    a 1-min bar belongs to. Within a bucket:
        * ``open``   = first input bar's open (relies on chronological input)
        * ``high``   = max
        * ``low``    = min
        * ``close``  = last input bar's close
        * ``volume`` = sum
        * ``time``   = bucket start (unix seconds)

    The last bucket may be partial (1-4 of the 5 minutes filled) — kept
    on purpose so Haiku sees the freshest context. The 1-min block sits
    right next to the 5-min block in the prompt, so any partial-bar
    ambiguity is disambiguated there.

    Bars without a parseable timestamp are skipped (we can't bucket).
    Bars missing OHLC are also skipped. Returns ``[]`` when nothing
    survives — the 1-min block alone still renders.

    Pure-Python, ~sub-millisecond per symbol on the typical 480-bar
    8-hour lookback. No extra Alpaca call.
    """
    if not bars:
        return []
    buckets: dict[int, dict] = {}
    for b in bars:
        t = _bar_time(b)
        if t is None:
            continue
        o = _bar_field(b, "o", "open")
        h = _bar_field(b, "h", "high")
        l = _bar_field(b, "l", "low")
        c = _bar_field(b, "c", "close")
        v = _bar_field(b, "v", "volume")
        if o is None or h is None or l is None or c is None:
            continue
        if v is None:
            v = 0.0
        bucket_t = (t // _FIVE_MIN_SECONDS) * _FIVE_MIN_SECONDS
        agg = buckets.get(bucket_t)
        if agg is None:
            buckets[bucket_t] = {
                "time":   bucket_t,
                "open":   o,
                "high":   h,
                "low":    l,
                "close":  c,
                "volume": v,
            }
        else:
            # ``open`` already stamped from the first bar in the bucket;
            # rolling close keeps the latest 1-min close as the bucket
            # close, matching standard OHLCV resampling semantics.
            if h > agg["high"]:
                agg["high"] = h
            if l < agg["low"]:
                agg["low"] = l
            agg["close"] = c
            agg["volume"] += v
    return [buckets[k] for k in sorted(buckets)]


def _format_bar_lines(window: list[dict]) -> str:
    """Render a list of OHLCV bars as the indented block Haiku reads.

    Used for both the 1-min and 5-min sections — same format keeps the
    prompt scan-line consistent so the model can compare timeframes
    visually. Skips bars that fail to coerce to floats; returns the
    "(no bar data)" sentinel when nothing survives so the prompt never
    has a blank section.
    """
    lines: list[str] = []
    for b in window:
        o = _bar_field(b, "o", "open")
        h = _bar_field(b, "h", "high")
        l = _bar_field(b, "l", "low")
        c = _bar_field(b, "c", "close")
        v = _bar_field(b, "v", "volume")
        if o is None or h is None or l is None or c is None:
            continue
        v_int = int(v) if v is not None else 0
        lines.append(f"  O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f} V={v_int:,}")
    return "\n".join(lines) if lines else "  (no bar data)"


def _build_prompt(
    *,
    symbol: str,
    price: float,
    direction: str,
    matched_strategies: list[dict],
    bars: list[dict],
    daily_bars: list[dict],
    vol_ratio: float | None = None,
    ict_killzone: str | None = None,
    regime: str | None = None,
    rs_zscore: float | None = None,
) -> str:
    strat_lines = []
    for s in matched_strategies:
        name = str(s.get("name", "")).strip()
        d    = str(s.get("direction", "")).strip()
        if name:
            strat_lines.append(f"  - {name} ({d})")
    strat_block = "\n".join(strat_lines) if strat_lines else "  (none)"

    bars_1min_block = _format_bar_lines((bars or [])[-_RECENT_BARS_1MIN:])
    bars_5min_block = _format_bar_lines(
        _resample_to_5min(bars or [])[-_RECENT_BARS_5MIN:]
    )

    daily_block = "  (no daily data)"
    if daily_bars:
        try:
            prev  = daily_bars[-2] if len(daily_bars) >= 2 else daily_bars[-1]
            today = daily_bars[-1]
            prev_close  = float(prev.get("c") or prev.get("close") or 0)
            today_close = float(today.get("c") or today.get("close") or 0)
            move_pct    = ((today_close - prev_close) / prev_close * 100) if prev_close else 0
            window      = daily_bars[-20:]
            highs       = [float(d.get("h") or d.get("high") or 0) for d in window]
            lows        = [float(d.get("l") or d.get("low")  or 0) for d in window]
            hi20, lo20  = (max(highs) if highs else 0), (min(lows) if lows else 0)
            position = (
                (today_close - lo20) / (hi20 - lo20) * 100
                if hi20 > lo20 else 0
            )
            daily_block = (
                f"  Prev close ${prev_close:.2f} | "
                f"Today move {move_pct:+.2f}% | "
                f"20d range ${lo20:.2f}-${hi20:.2f} "
                f"(price at {position:.0f}% of range)"
            )
        except (TypeError, ValueError, IndexError):
            pass

    # ── Structural context block ──
    # Each line is rendered even when the underlying value is missing
    # so Haiku sees the consistent shape and can treat "n/a" as a
    # genuine signal (e.g. no killzone = outside liquidity windows).
    def _fmt_vol(v: float | None) -> str:
        if v is None:
            return "n/a"
        try:
            return f"{float(v):.2f}"
        except (TypeError, ValueError):
            return "n/a"

    def _fmt_rs(v: float | None) -> str:
        if v is None:
            return "n/a"
        try:
            return f"{float(v):+.2f} pp"
        except (TypeError, ValueError):
            return "n/a"

    structural_block = (
        f"  vol_ratio: {_fmt_vol(vol_ratio)} (current bar vol / 20-bar mean; <0.8 is weak)\n"
        f"  ict_killzone: {ict_killzone or 'none'} (None = outside active liquidity windows)\n"
        f"  regime: {regime or 'unknown'} (SPY classification)\n"
        f"  rs_zscore: {_fmt_rs(rs_zscore)} (stock − SPY excess return since open)"
    )

    return _PROMPT.format(
        symbol=symbol,
        price=price,
        direction=direction,
        strategies=strat_block,
        bars_1min=bars_1min_block,
        bars_5min=bars_5min_block,
        daily=daily_block,
        structural=structural_block,
    )


def _parse_verdict(raw: str, *, symbol: str) -> ScreenerVerdict | None:
    """Parse Haiku's JSON output. Returns None on any parse issue."""
    text = raw.strip()
    # Strip markdown fences defensively — some models wrap JSON in ```json.
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text.lstrip("json").strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(parsed, dict):
        return None

    raw_valid = parsed.get("valid")
    # Accept bool, "true"/"false" strings, and 1/0 ints — models drift.
    if isinstance(raw_valid, bool):
        valid = raw_valid
    elif isinstance(raw_valid, (int, float)):
        valid = bool(raw_valid)
    elif isinstance(raw_valid, str):
        valid = raw_valid.strip().lower() in ("true", "yes", "1", "valid")
    else:
        return None

    reason = str(parsed.get("reason") or "").strip()[:200]
    if not reason:
        reason = "(no reason given)"

    return ScreenerVerdict(valid=valid, reason=reason)
