"""AI Trade Lessons — learn from every losing trade.

After a trade closes with a loss, the AI reviews what went wrong and stores a
concise lesson. Recent lessons are injected into future analysis prompts so the
AI avoids repeating the same mistakes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
from sqlalchemy import desc, select

from .db import SessionLocal
from .models import TradeLesson, TradeRecord
from .settings import settings

logger = logging.getLogger(__name__)

# ── Lesson generation ─────────────────────────────────────────────────────────

_REVIEW_PROMPT = """You are performing a deep post-mortem on a losing day trade that YOU recommended. This is a self-review — you made this call, and now you need to understand exactly what you got wrong.

TRADE OUTCOME:
  Symbol:     {symbol}
  Direction:  {direction}
  Setup:      {setup}
  Confidence: {confidence}
  Entry:      ${entry_price}
  Exit:       ${exit_price}
  Stop Loss:  {stop_loss}
  Target:     {target}
  P&L:        ${pnl} ({pnl_pct}%)
  R-Multiple: {r_multiple}R
  Hold Time:  {hold_duration}
  Exit Reason:{exit_reason}
  Highest Price Reached: {highest_price}

YOUR REASONING WHEN YOU RECOMMENDED THIS TRADE:
{reasoning}

{analysis_context_section}
YOUR TASK — deep self-review:

1. WHAT DID YOU SEE? Look at every indicator and signal you were given (EMA, RSI, MACD, VWAP, Bollinger Bands, volume, opening range, sector momentum, news, bid-ask data, daily trend, candlestick patterns). List the key signals that made you bullish/bearish.

2. WHAT DID YOU GET WRONG? Which of those signals were you misreading? Were you giving too much weight to one signal while ignoring a contradicting one? Did you mistake noise for a pattern?

3. WHAT DID YOU MISS? What warning signs were in the data that you should have caught? Was the volume too weak? Was the trend actually against you on the daily timeframe? Was the sector dragging? Was the bid-ask imbalance warning you? Was RSI overextended?

4. WHY DID THE TRADE FAIL? Connect the dots — the specific reason the stock moved against your prediction. Don't blame the stop loss placement. Focus on why your directional call was wrong.

5. WHAT SHOULD YOU DO DIFFERENTLY? A specific, actionable rule you can apply BEFORE entering future trades to avoid this exact mistake.

Write your response as JSON with exactly these fields:
{{
  "lesson": "<3-5 sentences: your full self-review covering what you misread, what you missed, and the specific rule to apply next time. Be brutally honest about your mistake.>",
  "tags": "<comma-separated categories, e.g.: 'misread_volume,ignored_daily_trend,false_breakout,sector_divergence,overweight_single_signal'>"
}}

Be specific to THIS trade. Reference actual indicator values if available. Don't give generic advice about stop losses.
Respond with ONLY valid JSON — no markdown fences, no extra text."""


def generate_lesson(trade: dict) -> dict[str, str] | None:
    """Call Claude to analyze a losing trade and extract a lesson.

    Returns {"lesson": "...", "tags": "..."} or None on failure.
    """
    if not settings.anthropic_api_key:
        logger.warning("lessons_skip: no ANTHROPIC_API_KEY")
        return None

    hold_mins = (trade.get("hold_duration_seconds") or 0) // 60

    # Build analysis context section
    ctx = trade.get("analysis_context") or ""
    if ctx:
        analysis_context_section = (
            "FULL INDICATOR DATA YOU WERE GIVEN AT THE TIME OF YOUR RECOMMENDATION:\n"
            "─────────────────────────────────────────────────────────────────────\n"
            f"{ctx}\n"
            "─────────────────────────────────────────────────────────────────────\n"
        )
    else:
        analysis_context_section = (
            "(Full indicator data was not recorded for this trade. "
            "Base your review on the reasoning and trade details above.)\n"
        )

    highest = trade.get("highest_price_reached")
    prompt = _REVIEW_PROMPT.format(
        symbol=trade["symbol"],
        direction=trade["direction"],
        setup=trade.get("setup", "unknown"),
        confidence=f"{(trade.get('confidence') or 0):.0%}",
        entry_price=f"{trade.get('entry_price', 0):.2f}",
        exit_price=f"{trade.get('exit_price', 0):.2f}",
        stop_loss=trade.get("stop_loss", "—"),
        target=trade.get("target", "—"),
        pnl=f"{trade.get('pnl', 0):.2f}",
        pnl_pct=f"{trade.get('pnl_pct', 0):.2f}",
        r_multiple=f"{trade.get('r_multiple', 0):.2f}" if trade.get("r_multiple") is not None else "—",
        hold_duration=f"{hold_mins}m",
        exit_reason=trade.get("exit_reason", "unknown"),
        highest_price=f"${highest:.2f}" if highest else "—",
        reasoning=trade.get("reasoning", "No reasoning recorded."),
        analysis_context_section=analysis_context_section,
    )

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        import json
        result = json.loads(raw)
        return {
            "lesson": str(result.get("lesson", ""))[:4000],
            "tags": str(result.get("tags", ""))[:500],
        }
    except Exception as exc:
        logger.error("lesson_generation_failed: %s", exc)
        return None


# ── Database operations ───────────────────────────────────────────────────────

def save_lesson(
    user_id: int,
    trade_id: int,
    symbol: str,
    setup: str | None,
    direction: str,
    pnl: float,
    lesson: str,
    tags: str | None = None,
) -> int | None:
    """Persist a lesson to the database. Returns the lesson ID."""
    db = SessionLocal()
    try:
        record = TradeLesson(
            user_id=user_id,
            trade_id=trade_id,
            symbol=symbol,
            setup=setup,
            direction=direction,
            pnl=pnl,
            lesson=lesson,
            tags=tags,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info("lesson_saved id=%d symbol=%s trade_id=%d", record.id, symbol, trade_id)
        return record.id
    except Exception as exc:
        db.rollback()
        logger.error("lesson_save_failed: %s", exc)
        return None
    finally:
        db.close()


def get_lessons(
    user_id: int,
    *,
    limit: int = 20,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent lessons, newest first."""
    db = SessionLocal()
    try:
        q = select(TradeLesson).where(TradeLesson.user_id == user_id)
        if symbol:
            q = q.where(TradeLesson.symbol == symbol.upper())
        q = q.order_by(desc(TradeLesson.created_at)).limit(limit)
        records = db.scalars(q).all()
        return [
            {
                "id": r.id,
                "trade_id": r.trade_id,
                "symbol": r.symbol,
                "setup": r.setup,
                "direction": r.direction,
                "pnl": r.pnl,
                "lesson": r.lesson,
                "tags": r.tags,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ]
    finally:
        db.close()


def get_recent_lessons_for_prompt(user_id: int, *, limit: int = 10) -> str | None:
    """Build a text block of recent lessons to inject into the AI analysis prompt.

    Returns None if there are no lessons yet.
    """
    lessons = get_lessons(user_id, limit=limit)
    if not lessons:
        return None

    lines = []
    for l in lessons:
        lines.append(
            f"- [{l['symbol']}] ({l['direction']}, {l['setup'] or 'unknown'}): "
            f"{l['lesson']} [Tags: {l['tags'] or 'none'}]"
        )

    return (
        "LESSONS FROM PAST LOSING TRADES — avoid repeating these mistakes:\n"
        + "\n".join(lines)
    )


def check_and_generate_lesson(user_id: int, trade: dict) -> None:
    """If the trade was a loss, generate and save a lesson.

    Call this after a trade is closed. It checks pnl < 0 and avoids
    duplicate lessons for the same trade.
    """
    pnl = trade.get("pnl")
    if pnl is None or pnl >= 0:
        return  # Only learn from losses

    trade_id = trade.get("id")
    if not trade_id:
        return

    # Check for existing lesson on this trade
    db = SessionLocal()
    try:
        existing = db.scalars(
            select(TradeLesson).where(TradeLesson.trade_id == trade_id).limit(1)
        ).first()
        if existing:
            return  # Already analyzed
    finally:
        db.close()

    result = generate_lesson(trade)
    if not result:
        return

    save_lesson(
        user_id=user_id,
        trade_id=trade_id,
        symbol=trade["symbol"],
        setup=trade.get("setup"),
        direction=trade["direction"],
        pnl=pnl,
        lesson=result["lesson"],
        tags=result.get("tags"),
    )
