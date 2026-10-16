"""
Trade history — persistent storage and performance analytics.
=============================================================
Saves every AI auto-trade to the database and computes performance
metrics (win rate, P&L, drawdown, etc.) across configurable time windows.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import TradeRecord, User

logger = logging.getLogger("tradehack.trade_history")


# ── Persistence helpers (called from autotrade.py) ──────────────────────────

def save_trade_opened(
    user_id: int,
    *,
    symbol: str,
    side: str,
    direction: str,
    qty: int,
    setup: str,
    confidence: float,
    order_type: str,
    order_id: str | None,
    status: str,
    entry_price: float | None,
    prediction_price: float | None,
    confirmation_price: float | None,
    price_move_pct: float | None,
    entry_zone: str | None,
    stop_loss: str | None,
    target: str | None,
    risk_reward: str | None,
    reasoning: str | None,
    analysis_context: str | None = None,
    matched_strategies: str | None = None,
    # Feature #10 — structured probabilities. Optional so pre-#10 callers
    # and legacy Claude responses (confidence-only) continue to work;
    # brier.py's calibration aggregator simply skips rows where any of
    # these is None.
    p_target: float | None = None,
    p_stop: float | None = None,
    p_neither: float | None = None,
    # v7.13 Phase 1.6 — universe provenance tag. None when the caller
    # doesn't know (e.g. legacy code path); the autotrade scan loop
    # populates it via ``get_universe_source(symbol)``. The 30-day
    # audit reads this column as the primary group-by key.
    universe_source: str | None = None,
    # v7.14a (2026-05-03) — JSON-serialized scan-time context dossier
    # built via ``trade_exporter.build_context_at_firing(...)``. Stored
    # as a TEXT blob (≤ 32 KB by construction). Used by the per-trade
    # exporter at close-time to assemble the friend-test dossier.
    # Setting None on legacy callers is fine; the exporter handles
    # missing context gracefully.
    context_at_firing: str | None = None,
    # v7.14 (2026-05-18) — scanner identity tag. Defaults to "ict" so
    # pre-rename call sites (and any code path that doesn't yet know
    # about the multi-scanner split) continue to land in the ICT
    # bucket, preserving v7.12.1 audit baseline parity. v7.15 will
    # pass "experimental_v1" from the second scanner's hits.
    scanner_id: str = "ict",
) -> int | None:
    """Persist a newly executed trade. Returns the DB record id.

    ``matched_strategies`` is a comma-separated list of PLAYBOOK names from the
    Tier-1 scan. Keyed by calibration.py to look up per-(strategy, symbol)
    historical win rates.
    """
    db = SessionLocal()
    try:
        record = TradeRecord(
            user_id=user_id,
            symbol=symbol,
            side=side,
            direction=direction,
            qty=qty,
            setup=setup,
            confidence=confidence,
            order_type=order_type,
            order_id=order_id,
            status=status,
            entry_price=entry_price,
            prediction_price=prediction_price,
            confirmation_price=confirmation_price,
            price_move_pct=price_move_pct,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            target=target,
            risk_reward=risk_reward,
            reasoning=reasoning[:2000] if reasoning else None,
            analysis_context=analysis_context,
            matched_strategies=matched_strategies[:500] if matched_strategies else None,
            p_target=p_target,
            p_stop=p_stop,
            p_neither=p_neither,
            universe_source=universe_source,
            context_at_firing=context_at_firing,
            scanner_id=scanner_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info(
            "trade_saved id=%d symbol=%s side=%s scanner_id=%s",
            record.id, symbol, side, scanner_id,
        )
        return record.id
    except Exception as exc:
        db.rollback()
        logger.error("trade_save_failed symbol=%s error=%s", symbol, exc)
        return None
    finally:
        db.close()


def update_trade_closed(
    order_id: str | None = None,
    record_id: int | None = None,
    *,
    status: str,
    exit_price: float | None,
    exit_reason: str | None,
    trailing_stop_final: float | None,
    highest_price_reached: float | None,
    hold_duration_seconds: int | None,
    # Feature #10 — outcome_class is filled in by brier.classify_outcome
    # at close-time in autotrade.py. None means "could not classify"
    # (e.g. stop_loss unparseable, or the classifier raised) — Brier
    # scoring skips unclassified rows rather than pretending 0 or 1.
    outcome_class: str | None = None,
    # v7.16.x (2026-06-12) — order-ID-linked broker fills (telemetry-only;
    # None = lookup unavailable/failed, the row keeps monitor prices only).
    entry_fill_price: float | None = None,
    exit_fill_price: float | None = None,
    exit_order_id: str | None = None,
) -> bool:
    """Update a trade record when the position is closed."""
    db = SessionLocal()
    try:
        record = None
        if record_id:
            record = db.get(TradeRecord, record_id)
        elif order_id:
            record = db.scalar(
                select(TradeRecord).where(TradeRecord.order_id == order_id).order_by(desc(TradeRecord.id))
            )

        if not record:
            logger.warning("trade_update_not_found order_id=%s record_id=%s", order_id, record_id)
            return False

        record.status = status
        record.exit_price = exit_price
        record.exit_reason = exit_reason
        record.trailing_stop_final = trailing_stop_final
        record.highest_price_reached = highest_price_reached
        record.hold_duration_seconds = hold_duration_seconds
        record.closed_at = datetime.now(timezone.utc)
        if outcome_class is not None:
            record.outcome_class = outcome_class

        # P&L basis AND the fill columns are set TOGETHER so a row can never
        # show broker fills beside a monitor-derived P&L (review 2026-06-12):
        #   • both fills present → fill basis + persist the fill trio.
        #   • otherwise          → monitor basis + CLEAR the fill trio to None,
        #     so the columns and pnl always agree, on every write, regardless
        #     of how many times update_trade_closed runs for a record.
        # Never mix one fill with one monitor price.
        if entry_fill_price is not None and exit_fill_price is not None:
            entry_basis, exit_basis = entry_fill_price, exit_fill_price
            record.entry_fill_price = entry_fill_price
            record.exit_fill_price = exit_fill_price
            record.exit_order_id = exit_order_id
        else:
            entry_basis, exit_basis = record.entry_price, exit_price
            record.entry_fill_price = None
            record.exit_fill_price = None
            record.exit_order_id = None
        if entry_basis and exit_basis:
            if record.direction == "long":
                record.pnl = (exit_basis - entry_basis) * record.qty
                record.pnl_pct = (exit_basis - entry_basis) / entry_basis * 100
            else:
                record.pnl = (entry_basis - exit_basis) * record.qty
                record.pnl_pct = (entry_basis - exit_basis) / entry_basis * 100

            # R-multiple: P&L relative to risk (entry → stop loss distance),
            # measured on the same basis as the P&L.
            stop_price = _parse_price(record.stop_loss)
            if stop_price and entry_basis:
                risk_per_share = abs(entry_basis - stop_price)
                if risk_per_share > 0:
                    pnl_per_share = record.pnl / record.qty if record.qty else 0
                    record.r_multiple = round(pnl_per_share / risk_per_share, 2)

        db.commit()
        logger.info(
            "trade_updated id=%d symbol=%s pnl=$%.2f exit=%s",
            record.id, record.symbol, record.pnl or 0, exit_reason,
        )
        return True
    except Exception as exc:
        db.rollback()
        logger.error("trade_update_failed order_id=%s error=%s", order_id, exc)
        return False
    finally:
        db.close()


# ── Query helpers (called from API endpoints) ────────────────────────────────

def get_trade_history(
    user_id: int,
    *,
    days: int = 30,
    symbol: str | None = None,
    setup: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Fetch trade records with optional filters."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        q = select(TradeRecord).where(
            TradeRecord.user_id == user_id,
            TradeRecord.opened_at >= cutoff,
        )
        if symbol:
            q = q.where(TradeRecord.symbol == symbol.upper())
        if setup:
            q = q.where(TradeRecord.setup.ilike(f"%{setup}%"))

        q = q.order_by(desc(TradeRecord.opened_at)).offset(offset).limit(limit)
        records = db.scalars(q).all()
        return [_record_to_dict(r) for r in records]
    finally:
        db.close()


def get_performance_stats(
    user_id: int,
    *,
    days: int = 30,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Compute aggregate performance metrics over a time window."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Respect per-user performance reset — stats only count trades from the
        # reset point forward, so switching broker/API-key accounts gives a
        # clean performance slate while trade history remains intact.
        user = db.get(User, user_id)
        reset_at = getattr(user, "performance_reset_at", None) if user else None
        if reset_at is not None:
            # SQLite strips tzinfo on read — normalize to UTC for a valid compare.
            if reset_at.tzinfo is None:
                reset_at = reset_at.replace(tzinfo=timezone.utc)
            if reset_at > cutoff:
                cutoff = reset_at

        base = select(TradeRecord).where(
            TradeRecord.user_id == user_id,
            TradeRecord.opened_at >= cutoff,
        )
        if symbol:
            base = base.where(TradeRecord.symbol == symbol.upper())

        records = db.scalars(base.order_by(desc(TradeRecord.opened_at))).all()

        if not records:
            return _empty_stats(days)

        closed = [r for r in records if r.closed_at is not None and r.pnl is not None]
        open_trades = [r for r in records if r.closed_at is None]

        wins = [r for r in closed if r.pnl > 0]
        losses = [r for r in closed if r.pnl <= 0]
        total_pnl = sum(r.pnl for r in closed)
        total_trades = len(closed)
        win_rate = len(wins) / total_trades * 100 if total_trades else 0

        avg_win = sum(r.pnl for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r.pnl for r in losses) / len(losses) if losses else 0
        profit_factor = abs(sum(r.pnl for r in wins) / sum(r.pnl for r in losses)) if losses and sum(r.pnl for r in losses) != 0 else float("inf") if wins else 0

        avg_hold = sum(r.hold_duration_seconds or 0 for r in closed) / total_trades if total_trades else 0
        avg_r = sum(r.r_multiple or 0 for r in closed if r.r_multiple is not None) / len([r for r in closed if r.r_multiple is not None]) if any(r.r_multiple is not None for r in closed) else None

        # Max drawdown (running P&L)
        running = 0
        peak = 0
        max_dd = 0
        for r in sorted(closed, key=lambda x: x.opened_at):
            running += r.pnl
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        # Best and worst trades
        best = max(closed, key=lambda r: r.pnl) if closed else None
        worst = min(closed, key=lambda r: r.pnl) if closed else None

        # Per-symbol breakdown
        symbol_stats: dict[str, dict] = {}
        for r in closed:
            s = symbol_stats.setdefault(r.symbol, {"trades": 0, "wins": 0, "pnl": 0})
            s["trades"] += 1
            if r.pnl > 0:
                s["wins"] += 1
            s["pnl"] += r.pnl
        for s in symbol_stats.values():
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)

        # Per-setup breakdown
        setup_stats: dict[str, dict] = {}
        for r in closed:
            s = setup_stats.setdefault(r.setup, {"trades": 0, "wins": 0, "pnl": 0})
            s["trades"] += 1
            if r.pnl > 0:
                s["wins"] += 1
            s["pnl"] += r.pnl
        for s in setup_stats.values():
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)

        # Per-hour-of-day breakdown (Eastern Time)
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
        hour_stats: dict[str, dict] = {}
        for r in closed:
            et_time = r.opened_at.astimezone(_ET)
            hour_label = et_time.strftime("%I %p").lstrip("0")  # e.g. "9 AM", "1 PM"
            h = hour_stats.setdefault(hour_label, {"trades": 0, "wins": 0, "pnl": 0.0, "hour_24": et_time.hour})
            h["trades"] += 1
            if r.pnl > 0:
                h["wins"] += 1
            h["pnl"] += r.pnl
        for h in hour_stats.values():
            h["win_rate"] = round(h["wins"] / h["trades"] * 100, 1) if h["trades"] else 0
            h["pnl"] = round(h["pnl"], 2)
        # Sort by hour of day
        hour_sorted = dict(sorted(hour_stats.items(), key=lambda x: x[1]["hour_24"]))
        # Remove the sort key from output
        for h in hour_sorted.values():
            del h["hour_24"]

        # Daily P&L series
        daily_pnl: dict[str, float] = {}
        for r in closed:
            day = r.opened_at.strftime("%Y-%m-%d")
            daily_pnl[day] = daily_pnl.get(day, 0) + r.pnl
        daily_series = [{"date": d, "pnl": round(v, 2)} for d, v in sorted(daily_pnl.items())]

        # Cumulative P&L series
        cumulative = 0
        cumulative_series = []
        for pt in daily_series:
            cumulative += pt["pnl"]
            cumulative_series.append({"date": pt["date"], "pnl": round(cumulative, 2)})

        return {
            "period_days": days,
            "total_trades": total_trades,
            "open_trades": len(open_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
            "avg_r_multiple": round(avg_r, 2) if avg_r is not None else None,
            "avg_hold_seconds": round(avg_hold),
            "max_drawdown": round(max_dd, 2),
            "best_trade": _record_to_dict(best) if best else None,
            "worst_trade": _record_to_dict(worst) if worst else None,
            "by_symbol": dict(sorted(symbol_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)),
            "by_setup": dict(sorted(setup_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)),
            "by_hour": hour_sorted,
            "daily_pnl": daily_series,
            "cumulative_pnl": cumulative_series,
        }
    finally:
        db.close()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _empty_stats(days: int) -> dict:
    return {
        "period_days": days,
        "total_trades": 0,
        "open_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
        "total_pnl": 0,
        "avg_win": 0,
        "avg_loss": 0,
        "profit_factor": None,
        "avg_r_multiple": None,
        "avg_hold_seconds": 0,
        "max_drawdown": 0,
        "best_trade": None,
        "worst_trade": None,
        "by_symbol": {},
        "by_setup": {},
        "by_hour": {},
        "daily_pnl": [],
        "cumulative_pnl": [],
    }


def _record_to_dict(r: TradeRecord) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "side": r.side,
        "direction": r.direction,
        "qty": r.qty,
        "setup": r.setup,
        "confidence": r.confidence,
        "order_type": r.order_type,
        "order_id": r.order_id,
        "status": r.status,
        "entry_price": r.entry_price,
        "exit_price": r.exit_price,
        "prediction_price": r.prediction_price,
        "confirmation_price": r.confirmation_price,
        "price_move_pct": r.price_move_pct,
        "entry_zone": r.entry_zone,
        "stop_loss": r.stop_loss,
        "target": r.target,
        "risk_reward": r.risk_reward,
        "reasoning": r.reasoning,
        "analysis_context": r.analysis_context,
        "exit_reason": r.exit_reason,
        "trailing_stop_final": r.trailing_stop_final,
        "highest_price_reached": r.highest_price_reached,
        "hold_duration_seconds": r.hold_duration_seconds,
        "pnl": r.pnl,
        "pnl_pct": r.pnl_pct,
        "r_multiple": r.r_multiple,
        "matched_strategies": getattr(r, "matched_strategies", None),
        # Feature #10 — structured probabilities + graded outcome. Nullable
        # on legacy rows opened before the schema migration.
        "p_target":      getattr(r, "p_target", None),
        "p_stop":        getattr(r, "p_stop", None),
        "p_neither":     getattr(r, "p_neither", None),
        "outcome_class": getattr(r, "outcome_class", None),
        # Order-ID-linked broker fills (v7.16.x). NULL = monitor-priced row
        # (pre-deploy, or fills unresolved). When both are non-NULL, pnl/R above
        # are fill-derived; exit_order_id is the close-order provenance.
        "entry_fill_price": getattr(r, "entry_fill_price", None),
        "exit_fill_price":  getattr(r, "exit_fill_price", None),
        "exit_order_id":    getattr(r, "exit_order_id", None),
        "opened_at": r.opened_at.isoformat() if r.opened_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    }


def _parse_price(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"[\d]+(?:\.[\d]+)?", str(value))
    return float(match.group()) if match else None
