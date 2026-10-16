"""After-hours AI study session.

Reviews the full day's price action for every stock in the trading universe,
identifies setups and trends the AI missed, and saves insights that get
injected into future analysis prompts — so the AI gets smarter each day.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from sqlalchemy import desc, select

from .alpaca import AlpacaMarketDataClient, AlpacaBarsRequest, convert_alpaca_bars
from .analyzer import compute_indicators, compute_daily_context
from .db import SessionLocal
from .models import StudyInsight
from .settings import settings

# Setup-name matching — filler tokens ignored when computing overlap.
_SETUP_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
    "with", "above", "below", "after", "before", "from", "near",
}

logger = logging.getLogger("tradehack.study")

_ET = ZoneInfo("America/New_York")

# ── Study prompt ─────────────────────────────────────────────────────────────

_STUDY_PROMPT = """You are an elite day trader reviewing today's COMPLETED price action for {symbol} after the market has closed. Your goal is to study what happened and find setups you should have caught.

TODAY'S FULL-DAY DATA:
{indicator_summary}

DAILY CHART CONTEXT (higher timeframe):
{daily_summary}

LAST 30 CANDLES OF THE DAY (1-min bars, oldest → newest):
{candle_lines}

YOUR EXISTING STRATEGY PLAYBOOK:
{strategy_names}

INSTRUCTIONS:
Look at the full day's action with hindsight. Find the BEST 1-2 trade setups that formed during the day. For each, explain:

1. WHAT HAPPENED: Describe the price action pattern — the trend, breakout, reversal, or setup that formed.
2. WHEN IT FORMED: What time (ET) did the entry signal appear?
3. WHAT SIGNALS CONFIRMED IT: Which indicators (EMA, RSI, MACD, VWAP, volume, Bollinger) were confirming? What did the confluence look like?
4. WHAT THE IDEAL TRADE WAS: Direction, entry price, stop loss, target, and the approximate % gain.
5. WHY YOU MIGHT HAVE MISSED IT: What would have made this setup hard to detect in real-time? Was the confidence too low? Was it outside your playbook?
6. NEW RULE OR PATTERN: A specific, actionable insight you can apply tomorrow. This could be a new signal combination, a time-of-day pattern, a volume threshold, or a refinement to an existing strategy.

If the stock was flat/choppy with no clear setups, say so — don't force a trade where none existed.

Respond with ONLY valid JSON — no markdown fences:
{{
  "setups": [
    {{
      "direction": "long|short",
      "setup_name": "<concise pattern name, e.g. 'VWAP reclaim + EMA9 cross above EMA20'>",
      "entry_time": "<HH:MM ET>",
      "entry_price": <float>,
      "stop_loss": <float>,
      "target": <float>,
      "potential_pnl_pct": <float, e.g. 1.5 for 1.5%>,
      "confidence": <0.0 to 1.0 — how obvious this setup was in hindsight>,
      "insight": "<3-5 sentences: what happened, what signals confirmed it, why you might have missed it, and the specific rule to apply going forward>",
      "tags": "<comma-separated, e.g.: 'vwap_reclaim,volume_surge,morning_momentum'>"
    }}
  ],
  "day_summary": "<1-2 sentences: overall market action for this stock today>"
}}"""


# ── Data fetching ────────────────────────────────────────────────────────────

def _fetch_full_day_bars(symbol: str) -> list[dict]:
    """Fetch 1-min bars for the most recent full trading day."""
    client = AlpacaMarketDataClient()
    now_et = datetime.now(_ET)

    # Use today if after 4 PM ET, otherwise use yesterday
    if now_et.hour >= 16:
        trade_date = now_et.date()
    else:
        trade_date = (now_et - timedelta(days=1)).date()
        # Skip weekends
        while trade_date.weekday() >= 5:
            trade_date -= timedelta(days=1)

    start_et = datetime(trade_date.year, trade_date.month, trade_date.day, 4, 0, tzinfo=_ET)
    end_et = datetime(trade_date.year, trade_date.month, trade_date.day, 20, 0, tzinfo=_ET)

    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)

    response = client.fetch_stock_bars(
        AlpacaBarsRequest(
            symbols=[symbol],
            timeframe="1Min",
            start=start_utc,
            end=end_utc,
        )
    )
    raw = response.get(symbol, [])
    return convert_alpaca_bars(raw) if raw else []


def _fetch_daily_bars_for_study(symbol: str) -> list[dict]:
    """Fetch 90 days of daily bars for higher-timeframe context."""
    client = AlpacaMarketDataClient()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=90)

    response = client.fetch_stock_bars(
        AlpacaBarsRequest(
            symbols=[symbol],
            timeframe="1Day",
            start=start,
            end=end,
        )
    )
    raw = response.get(symbol, [])
    return convert_alpaca_bars(raw) if raw else []


# ── Study analysis ───────────────────────────────────────────────────────────

def _build_indicator_summary(ind: dict) -> str:
    """Build a concise summary of computed indicators."""
    return f"""  Open:  ${ind['session_open']}  |  High: ${ind['session_high']}  |  Low: ${ind['session_low']}  |  Close: ${ind['current_price']}
  Change: {ind['change_pct']:+.2f}%
  EMA(9)  = ${ind['ema9']}  — price is {ind['price_vs_ema9']}
  EMA(20) = ${ind['ema20']} — price is {ind['price_vs_ema20']}
  EMA bias: {ind['ema_crossover']}
  RSI(14) = {ind['rsi']} ({ind['rsi_zone']})
  MACD: {ind['macd']:+.4f}  Signal: {ind['macd_signal']:+.4f}  Hist: {ind['macd_histogram']:+.4f}
  ATR(14) = ${ind['atr']} ({ind['atr_pct']}% of price)
  VWAP = ${ind['vwap']} — price is {ind['price_vs_vwap']}
  Bollinger: Upper ${ind['bb_upper']} | Mid ${ind['bb_mid']} | Lower ${ind['bb_lower']} (%B = {ind['bb_pct_b']:.2f})
  Volume trend: {ind['vol_trend']} ({ind['vol_ratio']}x avg)
  Structure: {ind['structure']}"""


def _build_daily_summary(daily_ctx: dict | None) -> str:
    """Build daily timeframe summary."""
    if not daily_ctx:
        return "  (No daily data available)"
    return f"""  Daily trend: {daily_ctx['trend']}
  EMA alignment: {daily_ctx['ema_alignment']}
  Daily RSI: {daily_ctx.get('rsi', '—')}
  MACD: {daily_ctx.get('macd_side', '—')}
  Prev close: ${daily_ctx.get('prev_close', '—')}
  Prev high: ${daily_ctx.get('prev_high', '—')}  |  Prev low: ${daily_ctx.get('prev_low', '—')}"""


def study_symbol(symbol: str) -> list[dict] | None:
    """Run a full after-hours study on a single symbol.

    Returns a list of setup dicts or None on failure.
    """
    if not settings.anthropic_api_key:
        logger.warning("study_skip: no ANTHROPIC_API_KEY")
        return None

    # Fetch data
    bars = _fetch_full_day_bars(symbol)
    if len(bars) < 30:
        logger.info("study_skip symbol=%s bars=%d (insufficient)", symbol, len(bars))
        return None

    daily_bars = _fetch_daily_bars_for_study(symbol)
    ind = compute_indicators(bars)
    daily_ctx = compute_daily_context(daily_bars) if len(daily_bars) >= 50 else None

    # Last 30 candles
    candle_lines = ""
    for b in bars[-30:]:
        t = datetime.fromtimestamp(b["time"], tz=timezone.utc).astimezone(_ET).strftime("%H:%M")
        bar_dir = "+" if b["close"] >= b["open"] else "-"
        candle_lines += (
            f"  {t}  O:{b['open']:.2f}  H:{b['high']:.2f}  "
            f"L:{b['low']:.2f}  C:{b['close']:.2f}  "
            f"Vol:{int(b['volume']):,}  {bar_dir}\n"
        )

    # Strategy names for context
    from .strategies import PLAYBOOK
    strategy_names = "\n".join(f"  - {s.name} ({s.direction})" for s in PLAYBOOK)

    prompt = _STUDY_PROMPT.format(
        symbol=symbol,
        indicator_summary=_build_indicator_summary(ind),
        daily_summary=_build_daily_summary(daily_ctx),
        candle_lines=candle_lines,
        strategy_names=strategy_names,
    )

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        result = json.loads(raw)
        return result.get("setups", [])

    except (anthropic.APIError, anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        logger.error("study_api_failed symbol=%s error=%s", symbol, exc)
        return None
    except json.JSONDecodeError as exc:
        logger.error("study_bad_json symbol=%s error=%s raw=%s", symbol, exc, raw[:300] if 'raw' in dir() else "<no raw>")
        return None
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        # Unexpected shape in the response (e.g. empty content list). Log and skip.
        logger.error("study_bad_shape symbol=%s error=%s", symbol, exc)
        return None


# ── Full study session ───────────────────────────────────────────────────────

def _resolve_study_date() -> str:
    now_et = datetime.now(_ET)
    if now_et.hour >= 16:
        return now_et.strftime("%Y-%m-%d")
    d = (now_et - timedelta(days=1)).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def iter_study_session(user_id: int, symbols: list[str] | None = None):
    """Generator version of the study session.

    Yields progress events as each symbol completes so callers (e.g. an SSE
    endpoint) can surface live progress to the UI. Event shapes:

      {"event": "start", "total": <int>, "study_date": "YYYY-MM-DD"}
      {"event": "progress", "index": <int 1-based>, "total": <int>,
        "symbol": "<SYM>", "status": "done" | "skipped" | "failed",
        "new": <int>, "merged": <int>, "error": "<optional>"}
      {"event": "done", "symbols_studied": <int>, "insights_found": <int>,
        "new_insights": <int>, "merged_insights": <int>, "study_date": "..."}
    """
    from .autotrade import get_active_universe

    symbols = symbols or get_active_universe()
    total = len(symbols)
    study_date = _resolve_study_date()

    logger.info("study_session_start user=%d symbols=%d", user_id, total)
    yield {"event": "start", "total": total, "study_date": study_date}

    new_count = 0
    merged_count = 0

    for idx, sym in enumerate(symbols, start=1):
        sym_new = 0
        sym_merged = 0
        status_ = "done"
        err_msg = None
        try:
            setups = study_symbol(sym)
            if not setups:
                status_ = "skipped"
            else:
                for setup in setups:
                    insight_id, was_merged = _save_or_merge_insight(
                        user_id=user_id,
                        study_date=study_date,
                        symbol=sym,
                        direction=setup.get("direction", "long"),
                        setup_name=setup.get("setup_name", "unknown"),
                        insight=setup.get("insight", ""),
                        entry_price=setup.get("entry_price"),
                        optimal_stop=setup.get("stop_loss"),
                        optimal_target=setup.get("target"),
                        potential_pnl_pct=setup.get("potential_pnl_pct"),
                        confidence=setup.get("confidence"),
                        tags=setup.get("tags"),
                    )
                    if insight_id:
                        if was_merged:
                            sym_merged += 1
                        else:
                            sym_new += 1
        except (anthropic.APIError, anthropic.APIStatusError, anthropic.APIConnectionError,
                json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError,
                ValueError) as exc:
            # Narrow set of failures we can survive and continue the session.
            # Programming errors (NameError, AssertionError, etc.) intentionally
            # propagate — they indicate bugs we want to see, not swallow.
            status_ = "failed"
            err_msg = str(exc)
            logger.error("study_symbol_failed symbol=%s error=%s", sym, exc)

        new_count += sym_new
        merged_count += sym_merged
        yield {
            "event": "progress",
            "index": idx,
            "total": total,
            "symbol": sym,
            "status": status_,
            "new": sym_new,
            "merged": sym_merged,
            **({"error": err_msg} if err_msg else {}),
        }

    logger.info(
        "study_session_done user=%d date=%s symbols=%d new=%d merged=%d",
        user_id, study_date, total, new_count, merged_count,
    )
    yield {
        "event": "done",
        "status": "completed",
        "study_date": study_date,
        "symbols_studied": total,
        "insights_found": new_count + merged_count,
        "new_insights": new_count,
        "merged_insights": merged_count,
    }


def run_study_session(user_id: int, symbols: list[str] | None = None) -> dict[str, Any]:
    """Synchronous wrapper — runs the generator to completion and returns the final summary."""
    final: dict[str, Any] = {}
    for evt in iter_study_session(user_id, symbols):
        if evt.get("event") == "done":
            final = evt
    final.pop("event", None)
    return final


# ── Database operations ──────────────────────────────────────────────────────

def _normalize_setup_tokens(name: str) -> set[str]:
    """Break a setup name into a lowercase token set with stopwords + punctuation removed."""
    if not name:
        return set()
    clean = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return {t for t in clean.split() if t and t not in _SETUP_STOPWORDS}


def _setup_similarity(a: str, b: str) -> float:
    """Jaccard similarity between two setup-name token sets."""
    ta, tb = _normalize_setup_tokens(a), _normalize_setup_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _merge_tags(existing: str | None, new: str | None) -> str | None:
    """Union of two comma-separated tag strings, de-duped, order-preserving."""
    parts: list[str] = []
    seen: set[str] = set()
    for src in (existing or "", new or ""):
        for t in src.split(","):
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
    return ", ".join(parts) if parts else None


def _merge_insight_text(existing: str | None, new: str | None, count: int) -> str:
    """Combine two insight narratives so repeated patterns get richer context."""
    new_t = (new or "").strip()
    existing_t = (existing or "").strip()
    if not existing_t:
        return new_t
    if not new_t or new_t == existing_t:
        return existing_t
    return (
        f"[Seen {count}x — latest observation below]\n"
        f"{new_t}\n\n"
        f"[Prior observation]\n"
        f"{existing_t}"
    )


def _save_or_merge_insight(
    user_id: int,
    study_date: str,
    symbol: str,
    direction: str,
    setup_name: str,
    insight: str,
    entry_price: float | None = None,
    optimal_stop: float | None = None,
    optimal_target: float | None = None,
    potential_pnl_pct: float | None = None,
    confidence: float | None = None,
    tags: str | None = None,
    similarity_threshold: float = 0.4,
) -> tuple[int | None, bool]:
    """Save a new insight OR merge it into an existing matching insight for the same stock.

    Matching logic (all three must hold):
      - same user + symbol + direction
      - setup_name token similarity >= similarity_threshold against an existing record

    When a match is found:
      - occurrence_count is incremented
      - last_seen_date is updated to today
      - created_at is refreshed so the row bubbles to the top of recency queries
      - insight text is combined (latest first, then prior)
      - entry/stop/target/confidence/pnl are updated to the latest values
      - tags are unioned

    Returns (id, merged_flag).
    """
    db = SessionLocal()
    try:
        # Look for a matching insight for this (user, symbol, direction)
        candidates = db.scalars(
            select(StudyInsight)
            .where(
                StudyInsight.user_id == user_id,
                StudyInsight.symbol == symbol,
                StudyInsight.direction == direction,
            )
            .order_by(desc(StudyInsight.occurrence_count), desc(StudyInsight.created_at))
        ).all()

        best = None
        best_score = 0.0
        for cand in candidates:
            score = _setup_similarity(cand.setup_name, setup_name)
            if score > best_score:
                best, best_score = cand, score

        if best is not None and best_score >= similarity_threshold:
            # Merge into the matching record
            best.occurrence_count = (best.occurrence_count or 1) + 1
            best.last_seen_date = study_date
            best.first_seen_date = best.first_seen_date or best.study_date
            best.study_date = study_date
            best.setup_name = setup_name
            best.insight = _merge_insight_text(best.insight, insight, best.occurrence_count)
            if entry_price is not None:
                best.entry_price = entry_price
            if optimal_stop is not None:
                best.optimal_stop = optimal_stop
            if optimal_target is not None:
                best.optimal_target = optimal_target
            if potential_pnl_pct is not None:
                best.potential_pnl_pct = potential_pnl_pct
            if confidence is not None:
                best.confidence = confidence
            best.tags = _merge_tags(best.tags, tags)
            # Bubble to the top
            best.created_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(best)
            logger.info(
                "insight_merged symbol=%s direction=%s setup=%r count=%d similarity=%.2f",
                symbol, direction, setup_name, best.occurrence_count, best_score,
            )
            return best.id, True

        # No match — insert new
        record = StudyInsight(
            user_id=user_id,
            study_date=study_date,
            symbol=symbol,
            direction=direction,
            setup_name=setup_name,
            insight=insight,
            entry_price=entry_price,
            optimal_stop=optimal_stop,
            optimal_target=optimal_target,
            potential_pnl_pct=potential_pnl_pct,
            confidence=confidence,
            tags=tags,
            occurrence_count=1,
            first_seen_date=study_date,
            last_seen_date=study_date,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record.id, False
    except Exception as exc:
        db.rollback()
        logger.error("insight_save_failed: %s", exc)
        return None, False
    finally:
        db.close()


def _insight_to_dict(r: StudyInsight) -> dict:
    return {
        "id": r.id,
        "study_date": r.study_date,
        "symbol": r.symbol,
        "direction": r.direction,
        "setup_name": r.setup_name,
        "insight": r.insight,
        "entry_price": r.entry_price,
        "optimal_stop": r.optimal_stop,
        "optimal_target": r.optimal_target,
        "potential_pnl_pct": r.potential_pnl_pct,
        "confidence": r.confidence,
        "tags": r.tags,
        "occurrence_count": r.occurrence_count or 1,
        "first_seen_date": r.first_seen_date,
        "last_seen_date": r.last_seen_date,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def get_insights(
    user_id: int,
    *,
    limit: int = 30,
    study_date: str | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """Fetch study insights, ordered by occurrence_count DESC then created_at DESC.

    Repeated patterns bubble to the top so the UI and AI both see them first.
    """
    db = SessionLocal()
    try:
        q = select(StudyInsight).where(StudyInsight.user_id == user_id)
        if study_date:
            q = q.where(StudyInsight.study_date == study_date)
        if symbol:
            q = q.where(StudyInsight.symbol == symbol.upper())
        q = q.order_by(
            desc(StudyInsight.occurrence_count),
            desc(StudyInsight.created_at),
        ).limit(limit)
        records = db.scalars(q).all()
        return [_insight_to_dict(r) for r in records]
    finally:
        db.close()


def get_recent_insights_for_prompt(
    user_id: int,
    *,
    symbol: str | None = None,
    limit: int = 6,
) -> str | None:
    """Build a text block of study insights to inject into the AI analysis prompt.

    When `symbol` is provided, only insights for that stock are pulled — so the AI
    sees patterns specifically relevant to the stock it's currently analyzing.
    Insights are ordered by occurrence_count (most-repeated first), then recency.

    Returns None if there are no insights yet.
    """
    insights = get_insights(user_id, limit=limit, symbol=symbol)
    if not insights:
        return None

    lines = []
    for i in insights:
        count = i.get("occurrence_count") or 1
        emphasis = ""
        if count >= 3:
            emphasis = f" ⚡ REPEATED {count}x — HIGH PRIORITY PATTERN"
        elif count == 2:
            emphasis = f" (seen {count}x — watch closely)"
        lines.append(
            f"- [{i['symbol']}] ({i['direction']}, {i['setup_name']}){emphasis}: "
            f"{i['insight']} [Tags: {i['tags'] or 'none'}]"
        )

    header = (
        f"PATTERNS FROM AFTER-HOURS STUDY FOR {symbol}"
        if symbol
        else "PATTERNS FROM AFTER-HOURS STUDY"
    )
    return (
        f"{header} — setups identified in hindsight. Repeated patterns (⚡) have "
        f"shown up across multiple sessions — pay extra attention to those:\n"
        + "\n".join(lines)
    )
