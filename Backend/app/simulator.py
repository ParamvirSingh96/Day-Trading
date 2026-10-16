"""
Paper Trade Performance Tracker — hypothetical trade simulation.
================================================================
Logs every AI recommendation (regardless of user approval) and tracks
what *would* have happened using live market data. Computes hypothetical
P&L so you can answer: "If I followed every AI recommendation this week,
what would my P&L be?"

Open simulated trades are monitored by the autotrade monitor loop which
calls `check_simulated_exits()` periodically with fresh prices.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import SimulatedTrade

logger = logging.getLogger("tradehack.simulator")

_ET = ZoneInfo("America/New_York")


# ── Record a new simulated trade ──────────────────────────────────────────────

def log_simulated_trade(
    user_id: int,
    *,
    symbol: str,
    direction: str,
    setup: str,
    confidence: float,
    entry_price: float,
    stop_loss: float | None,
    target: float | None,
    entry_zone: str | None,
    risk_reward: str | None,
    reasoning: str | None,
    qty: int = 1,
    was_approved: str | None = None,
) -> int | None:
    """Log an AI recommendation as a simulated trade. Returns the DB record id."""
    db = SessionLocal()
    try:
        record = SimulatedTrade(
            user_id=user_id,
            symbol=symbol,
            direction=direction,
            setup=setup,
            confidence=confidence,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target,
            entry_zone=entry_zone,
            risk_reward=risk_reward,
            reasoning=reasoning[:2000] if reasoning else None,
            qty=qty,
            was_approved=was_approved,
            status="open",
            highest_price=entry_price if direction == "long" else None,
            lowest_price=entry_price if direction == "short" else None,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info("sim_trade_logged id=%d symbol=%s dir=%s conf=%.2f entry=%.2f",
                     record.id, symbol, direction, confidence, entry_price)
        return record.id
    except Exception as exc:
        db.rollback()
        logger.error("sim_trade_log_failed symbol=%s error=%s", symbol, exc)
        return None
    finally:
        db.close()


# ── Monitor open simulated trades against live prices ──────────────────────────

def check_simulated_exits(prices: dict[str, float], max_hold_minutes: int = 60) -> list[dict]:
    """Check all open simulated trades against current prices.

    Called periodically from the autotrade monitor loop.
    Returns a list of trades that were closed this cycle.
    """
    db = SessionLocal()
    closed_trades: list[dict] = []
    try:
        open_trades = db.scalars(
            select(SimulatedTrade).where(SimulatedTrade.status == "open")
        ).all()

        if not open_trades:
            return closed_trades

        now = datetime.now(timezone.utc)
        now_et = datetime.now(_ET)

        for trade in open_trades:
            current_price = prices.get(trade.symbol)
            if current_price is None:
                continue

            # Update high/low water marks
            if trade.direction == "long":
                if trade.highest_price is None or current_price > trade.highest_price:
                    trade.highest_price = current_price
            else:
                if trade.lowest_price is None or current_price < trade.lowest_price:
                    trade.lowest_price = current_price

            exit_reason = None
            exit_price = current_price

            # 1. Check stop loss
            if trade.stop_loss:
                if trade.direction == "long" and current_price <= trade.stop_loss:
                    exit_reason = "stop_hit"
                    exit_price = trade.stop_loss  # assume filled at stop
                elif trade.direction == "short" and current_price >= trade.stop_loss:
                    exit_reason = "stop_hit"
                    exit_price = trade.stop_loss

            # 2. Check target
            if not exit_reason and trade.target:
                if trade.direction == "long" and current_price >= trade.target:
                    exit_reason = "target_hit"
                    exit_price = trade.target
                elif trade.direction == "short" and current_price <= trade.target:
                    exit_reason = "target_hit"
                    exit_price = trade.target

            # 3. Max hold time
            if not exit_reason:
                # SQLite drops tz info — ensure opened_at is UTC-aware before math
                opened = trade.opened_at
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                elapsed = (now - opened).total_seconds() / 60
                if elapsed >= max_hold_minutes:
                    exit_reason = "time_exit"

            # 4. EOD exit (15 min before market close)
            if not exit_reason and now_et.weekday() < 5:
                market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
                cutoff = market_close - timedelta(minutes=15)
                if now_et >= cutoff:
                    exit_reason = "eod_exit"

            if exit_reason:
                _close_simulated_trade(trade, exit_price, exit_reason)
                closed_trades.append(_sim_to_dict(trade))

        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("sim_check_exits_failed error=%s", exc)
    finally:
        db.close()

    return closed_trades


def _close_simulated_trade(trade: SimulatedTrade, exit_price: float, reason: str):
    """Close a simulated trade and compute P&L."""
    trade.exit_price = exit_price
    trade.exit_reason = reason
    trade.closed_at = datetime.now(timezone.utc)

    if trade.direction == "long":
        trade.pnl = (exit_price - trade.entry_price) * trade.qty
        trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
    else:
        trade.pnl = (trade.entry_price - exit_price) * trade.qty
        trade.pnl_pct = (trade.entry_price - exit_price) / trade.entry_price * 100

    trade.status = "won" if trade.pnl > 0 else "lost"

    logger.info(
        "sim_trade_closed id=%d symbol=%s reason=%s pnl=$%.2f (%.2f%%)",
        trade.id, trade.symbol, reason, trade.pnl, trade.pnl_pct,
    )


def mark_simulated_approval(trade_id: int, approved: str):
    """Update a simulated trade with approval status ('yes', 'no', 'expired')."""
    db = SessionLocal()
    try:
        trade = db.get(SimulatedTrade, trade_id)
        if trade:
            trade.was_approved = approved
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ── Query simulated trade history ──────────────────────────────────────────────

def get_open_simulated_symbols(user_id: int | None = None) -> list[str]:
    """Return symbols with open simulated trades (for price fetching)."""
    db = SessionLocal()
    try:
        q = select(SimulatedTrade.symbol).where(SimulatedTrade.status == "open")
        if user_id:
            q = q.where(SimulatedTrade.user_id == user_id)
        q = q.distinct()
        return list(db.scalars(q).all())
    finally:
        db.close()


def get_simulated_trades(
    user_id: int,
    *,
    days: int = 7,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Fetch simulated trade records."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        q = select(SimulatedTrade).where(
            SimulatedTrade.user_id == user_id,
            SimulatedTrade.opened_at >= cutoff,
        )
        if status:
            q = q.where(SimulatedTrade.status == status)

        q = q.order_by(desc(SimulatedTrade.opened_at)).limit(limit)
        records = db.scalars(q).all()
        return [_sim_to_dict(r) for r in records]
    finally:
        db.close()


def get_simulated_performance(user_id: int, *, days: int = 7) -> dict[str, Any]:
    """Compute aggregate performance for simulated trades."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        records = db.scalars(
            select(SimulatedTrade).where(
                SimulatedTrade.user_id == user_id,
                SimulatedTrade.opened_at >= cutoff,
            ).order_by(desc(SimulatedTrade.opened_at))
        ).all()

        if not records:
            return _empty_sim_stats(days)

        closed = [r for r in records if r.status in ("won", "lost")]
        open_trades = [r for r in records if r.status == "open"]

        wins = [r for r in closed if r.pnl and r.pnl > 0]
        losses = [r for r in closed if r.pnl is not None and r.pnl <= 0]
        total_pnl = sum(r.pnl or 0 for r in closed)
        total_trades = len(closed)
        win_rate = len(wins) / total_trades * 100 if total_trades else 0

        avg_win = sum(r.pnl for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r.pnl for r in losses) / len(losses) if losses else 0
        profit_factor = abs(sum(r.pnl for r in wins) / sum(r.pnl for r in losses)) if losses and sum(r.pnl for r in losses) != 0 else (float("inf") if wins else 0)

        # Max drawdown
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in sorted(closed, key=lambda x: x.opened_at):
            running += r.pnl or 0
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        # Approval breakdown
        approved_count = sum(1 for r in records if r.was_approved == "yes")
        rejected_count = sum(1 for r in records if r.was_approved == "no")
        expired_count = sum(1 for r in records if r.was_approved == "expired")
        unapproved_count = sum(1 for r in records if r.was_approved is None or r.was_approved not in ("yes", "no", "expired"))

        # P&L of trades that were approved vs not
        approved_pnl = sum(r.pnl or 0 for r in closed if r.was_approved == "yes")
        missed_pnl = sum(r.pnl or 0 for r in closed if r.was_approved != "yes")

        # Per-symbol breakdown
        symbol_stats: dict[str, dict] = {}
        for r in closed:
            s = symbol_stats.setdefault(r.symbol, {"trades": 0, "wins": 0, "pnl": 0.0})
            s["trades"] += 1
            if r.pnl and r.pnl > 0:
                s["wins"] += 1
            s["pnl"] += r.pnl or 0
        for s in symbol_stats.values():
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)

        # Per-setup breakdown
        setup_stats: dict[str, dict] = {}
        for r in closed:
            s = setup_stats.setdefault(r.setup, {"trades": 0, "wins": 0, "pnl": 0.0})
            s["trades"] += 1
            if r.pnl and r.pnl > 0:
                s["wins"] += 1
            s["pnl"] += r.pnl or 0
        for s in setup_stats.values():
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)

        # Daily P&L series
        daily_pnl: dict[str, float] = {}
        for r in closed:
            day = r.opened_at.strftime("%Y-%m-%d")
            daily_pnl[day] = daily_pnl.get(day, 0) + (r.pnl or 0)
        daily_series = [{"date": d, "pnl": round(v, 2)} for d, v in sorted(daily_pnl.items())]

        cumulative = 0.0
        cumulative_series = []
        for pt in daily_series:
            cumulative += pt["pnl"]
            cumulative_series.append({"date": pt["date"], "pnl": round(cumulative, 2)})

        return {
            "period_days": days,
            "total_recommendations": len(records),
            "total_closed": total_trades,
            "open_trades": len(open_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
            "max_drawdown": round(max_dd, 2),
            # Approval analysis
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "expired_count": expired_count,
            "unapproved_count": unapproved_count,
            "approved_pnl": round(approved_pnl, 2),
            "missed_pnl": round(missed_pnl, 2),
            # Breakdowns
            "by_symbol": dict(sorted(symbol_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)),
            "by_setup": dict(sorted(setup_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)),
            "daily_pnl": daily_series,
            "cumulative_pnl": cumulative_series,
        }
    finally:
        db.close()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _empty_sim_stats(days: int) -> dict:
    return {
        "period_days": days,
        "total_recommendations": 0,
        "total_closed": 0,
        "open_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
        "total_pnl": 0,
        "avg_win": 0,
        "avg_loss": 0,
        "profit_factor": None,
        "max_drawdown": 0,
        "approved_count": 0,
        "rejected_count": 0,
        "expired_count": 0,
        "unapproved_count": 0,
        "approved_pnl": 0,
        "missed_pnl": 0,
        "by_symbol": {},
        "by_setup": {},
        "daily_pnl": [],
        "cumulative_pnl": [],
    }


def _sim_to_dict(r: SimulatedTrade) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "direction": r.direction,
        "setup": r.setup,
        "confidence": r.confidence,
        "entry_price": r.entry_price,
        "stop_loss": r.stop_loss,
        "target": r.target,
        "entry_zone": r.entry_zone,
        "risk_reward": r.risk_reward,
        "reasoning": r.reasoning,
        "status": r.status,
        "exit_price": r.exit_price,
        "exit_reason": r.exit_reason,
        "highest_price": r.highest_price,
        "lowest_price": r.lowest_price,
        "pnl": r.pnl,
        "pnl_pct": r.pnl_pct,
        "qty": r.qty,
        "was_approved": r.was_approved,
        "opened_at": r.opened_at.isoformat() if r.opened_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    }
