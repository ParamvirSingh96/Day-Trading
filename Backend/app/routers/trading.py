"""
Trading router — paper & live order management via Alpaca.
==========================================================
All endpoints require authentication.  A safety guard prevents
live-trading calls when ALPACA_PAPER=true (the default).
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from typing import Any

import math

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..alpaca import AlpacaTradingClient, OrderSide, OrderType, TimeInForce, get_shared_trading_client
from ..analyzer import analysis_cache
from ..trade_history import get_trade_history, get_performance_stats
from ..lessons import get_lessons, generate_lesson, save_lesson
from ..study import run_study_session, iter_study_session, get_insights as get_study_insights
from ..models import TradeRecord, TradeLesson, StudyInsight
from ..db import SessionLocal
from sqlalchemy import select, delete
from ..simulator import get_simulated_trades, get_simulated_performance
from ..autotrade import (
    state as autotrade_state,
    get_status as autotrade_status,
    start as autotrade_start,
    stop as autotrade_stop,
    approve_trade as autotrade_approve,
    reject_trade as autotrade_reject,
    close_monitored as autotrade_close_monitored,
    set_autonomous_mode as autotrade_set_autonomous,
    get_active_universe,
    set_trading_universe,
)
from ..deps import require_operator
from ..settings import settings
from .. import trading_mode
from ..trading_mode import (
    PaperBoundaryViolation,
    mode_snapshot,
    set_mode,
    is_configured as _mode_is_configured,
)

logger = logging.getLogger("tradehack.trading")
router = APIRouter(prefix="/trading", tags=["trading"])


# ── Request / response models ────────────────────────────────────────────────

_SYMBOL_PATTERN = "^[A-Z]{1,10}$"


def _positive_finite(v: float | None, name: str) -> float | None:
    if v is None:
        return None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(v)


def _check_bracket_legs(side: str, take_profit: float | None, stop_loss: float | None,
                        reference: float | None) -> None:
    """Bracket legs must come together and sit on the correct side of the
    entry: for a buy the take-profit is above and the stop below (and vice
    versa for a sell). ``reference`` is the limit/entry price when known."""
    if (take_profit is None) != (stop_loss is None):
        raise ValueError("take_profit_price and stop_loss_price must be provided together")
    if take_profit is None:
        return
    if side == "buy":
        if not take_profit > stop_loss:
            raise ValueError("for a buy, take_profit_price must be above stop_loss_price")
        if reference is not None and not (stop_loss < reference < take_profit):
            raise ValueError("for a buy, the entry price must sit between stop_loss_price and take_profit_price")
    else:
        if not take_profit < stop_loss:
            raise ValueError("for a sell, take_profit_price must be below stop_loss_price")
        if reference is not None and not (take_profit < reference < stop_loss):
            raise ValueError("for a sell, the entry price must sit between take_profit_price and stop_loss_price")


class PlaceOrderRequest(BaseModel):
    symbol: str = Field(pattern=_SYMBOL_PATTERN)
    qty: int = Field(ge=1, le=10_000)
    side: str = Field(pattern="^(buy|sell)$")
    order_type: str = Field(default="market", pattern="^(market|limit|stop|stop_limit)$")
    time_in_force: str = Field(default="day", pattern="^(day|gtc|ioc|fok)$")
    limit_price: float | None = None
    stop_price: float | None = None
    # Bracket legs (auto stop-loss + take-profit)
    take_profit_price: float | None = None
    stop_loss_price: float | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _upper_symbol(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("limit_price", "stop_price", "take_profit_price", "stop_loss_price")
    @classmethod
    def _prices_positive(cls, v: float | None, info) -> float | None:
        return _positive_finite(v, info.field_name)

    @model_validator(mode="after")
    def _cross_field(self) -> "PlaceOrderRequest":
        needs_limit = self.order_type in ("limit", "stop_limit")
        needs_stop = self.order_type in ("stop", "stop_limit")
        if needs_limit and self.limit_price is None:
            raise ValueError(f"{self.order_type} orders require limit_price")
        if not needs_limit and self.limit_price is not None:
            raise ValueError(f"{self.order_type} orders must not carry limit_price")
        if needs_stop and self.stop_price is None:
            raise ValueError(f"{self.order_type} orders require stop_price")
        if not needs_stop and self.stop_price is not None:
            raise ValueError(f"{self.order_type} orders must not carry stop_price")
        _check_bracket_legs(self.side, self.take_profit_price, self.stop_loss_price,
                            self.limit_price)
        return self


class QuickTradeRequest(BaseModel):
    """One-click trade from the analysis page — takes the AI recommendation directly."""
    symbol: str = Field(pattern=_SYMBOL_PATTERN)
    side: str = Field(pattern="^(buy|sell)$")
    qty: int = Field(ge=1, le=10_000)
    entry_price: float | None = None       # limit price (None = market order)
    stop_loss_price: float | None = None
    take_profit_price: float | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _upper_symbol(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("entry_price", "stop_loss_price", "take_profit_price")
    @classmethod
    def _prices_positive(cls, v: float | None, info) -> float | None:
        return _positive_finite(v, info.field_name)

    @model_validator(mode="after")
    def _cross_field(self) -> "QuickTradeRequest":
        _check_bracket_legs(self.side, self.take_profit_price, self.stop_loss_price,
                            self.entry_price)
        return self


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client() -> AlpacaTradingClient:
    # Reuses a process-wide shared client so we don't pay TCP/TLS setup
    # per request AND don't duplicate Alpaca rate-limit pressure with
    # multiple independent connection pools.
    try:
        return get_shared_trading_client()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        ) from exc


# ── Small in-process TTL cache for hot GET endpoints ─────────────────────────
# The frontend only polls /account and /positions when something actually
# changes (WebSocket `wallet_update` is the primary trigger) but we still
# want a safety net: if multiple browser tabs / background tasks request
# the same data within a few seconds, one Alpaca round-trip should cover
# all of them. Every order / close mutation invalidates the cache, so
# stale reads after a user action are never possible.
_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.Lock()
_cache_store: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _cache_store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            _cache_store.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: Any, ttl: float = _CACHE_TTL_SECONDS) -> None:
    with _cache_lock:
        _cache_store[key] = (time.monotonic() + ttl, value)


def _cache_invalidate(*keys: str) -> None:
    with _cache_lock:
        if not keys:
            _cache_store.clear()
            return
        for k in keys:
            _cache_store.pop(k, None)


# ── Manual-order position-size guard ─────────────────────────────────────────
# Autotrade applies risk-based sizing (position_size_pct of portfolio, capped
# at 95% of spendable cash). Manual /orders and /quick-trade endpoints must
# not bypass those caps — a fat-finger or hostile client could otherwise
# submit a 100%-portfolio order that would never survive the autotrader.

# Max fraction of (cash, buying_power) a single manual order may consume.
_MANUAL_MAX_CASH_FRACTION = 0.95
# Max fraction of portfolio value a single manual order may represent.
# Intentionally generous (2× the autotrader's default 5%/trade × 1.5x confidence
# boost = 7.5%) so discretionary trades aren't unduly blocked, but still
# catches runaway positions.
_MANUAL_MAX_POSITION_PCT = 0.20
# Hard share cap (defence-in-depth against a Pydantic bypass).
_MANUAL_MAX_SHARES = 10_000


def _estimate_order_price(
    client: AlpacaTradingClient,
    symbol: str,
    limit_price: float | None,
    order_type: str = "market",
) -> float:
    """Best-effort reference price for notional sizing.

    Uses the explicit limit price only for order types that actually carry
    one (limit / stop_limit); market and stop orders are sized off the latest
    trade. Returns 0 if no price can be determined — callers must treat that
    as "cannot validate" and reject.
    """
    if order_type in ("limit", "stop_limit") and limit_price and limit_price > 0:
        return float(limit_price)
    try:
        from ..alpaca import AlpacaMarketDataClient
        mkt = AlpacaMarketDataClient()
        snaps = mkt.fetch_snapshots([symbol])
        snap = snaps.get(symbol.upper(), {}) or snaps.get(symbol, {})
        trade = snap.get("latestTrade") or snap.get("latest_trade") or {}
        p = float(trade.get("p") or trade.get("price") or 0)
        if p > 0:
            return p
        bar = snap.get("minuteBar") or snap.get("minute_bar") or {}
        return float(bar.get("c", 0) or 0)
    except Exception as exc:
        logger.warning("manual_cap_price_lookup_failed symbol=%s error=%s", symbol, exc)
        return 0.0


def _existing_long_qty(client: AlpacaTradingClient, symbol: str) -> int:
    """Return current long qty held in `symbol` (0 if none or short)."""
    try:
        pos = client.get_position(symbol)
        if pos is None:
            return 0
        q = int(float(pos.get("qty", 0)))
        return max(0, q)  # negative = short; treat short as "no long" for close-check
    except Exception:
        return 0


# ── Pattern Day Trader (PDT) guard ────────────────────────────────────────────
# PDT rule: a margin account with equity < $25k that executes 4+ day-trades in
# a rolling 5-business-day window gets flagged PDT and restricted to closing
# only until equity is brought above $25k. Cash accounts are exempt from PDT
# (they have GFV rules instead) — this guard is a defensive check in case the
# account ever gets converted to margin, or the user adds another account.
_PDT_MIN_EQUITY = 25_000.0
_PDT_DAYTRADE_LIMIT = 3  # block on the 4th attempt


def _pdt_would_trigger(acct: dict[str, Any]) -> tuple[bool, str]:
    """Return (would_trigger, reason) for a hypothetical new day-trade."""
    # Cash accounts (multiplier=1, shorting disabled) are PDT-exempt.
    try:
        multiplier = float(acct.get("multiplier", 1) or 1)
    except (TypeError, ValueError):
        multiplier = 1.0
    if multiplier <= 1.0:
        return (False, "")

    # Already flagged? Alpaca will auto-reject further day-trades.
    if acct.get("pattern_day_trader"):
        return (
            True,
            "Account is already flagged as Pattern Day Trader. Opening a "
            "new position would be blocked by Alpaca. Deposit funds to bring "
            "equity above $25,000 or wait for the flag to expire.",
        )

    equity = float(acct.get("equity", 0) or 0)
    daytrade_count = int(acct.get("daytrade_count", 0) or 0)
    if equity >= _PDT_MIN_EQUITY:
        return (False, "")
    if daytrade_count >= _PDT_DAYTRADE_LIMIT:
        return (
            True,
            f"Blocked to avoid Pattern Day Trader flag — you have "
            f"{daytrade_count} day-trades in the last 5 business days and "
            f"account equity (${equity:,.2f}) is below the $25,000 PDT "
            "threshold. One more round-trip today would trigger the PDT "
            "flag and restrict your account.",
        )
    return (False, "")


def _enforce_manual_position_cap(
    client: AlpacaTradingClient,
    symbol: str,
    side: str,
    qty: int,
    limit_price: float | None,
    order_type: str = "market",
) -> None:
    """Reject the request if it would breach cash or portfolio-fraction caps.

    Sells that are fully covered by an existing long position are treated as
    closes and skipped — the cap only guards NEW exposure.
    """
    # Hard share cap first (cheap check).
    if qty > _MANUAL_MAX_SHARES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order qty {qty} exceeds max shares per order ({_MANUAL_MAX_SHARES}).",
        )

    # Sell that fully closes an existing long is not a new exposure — but on a
    # live account, closing a same-day-opened position may still trigger PDT
    # (it completes a day-trade) or GFV (if it was bought with unsettled funds),
    # so we still need to gate those cases.
    if side == "sell":
        held = _existing_long_qty(client, symbol)
        if qty <= held:
            if not trading_mode.is_paper():
                try:
                    acct = client.get_account()
                except Exception as exc:
                    logger.warning("sell_gate_account_lookup_failed error=%s", exc)
                    acct = {}
                # PDT: closing a same-day open = day-trade. Block if it'd be #4.
                try:
                    opened_today = symbol.upper() in client.get_todays_buy_symbols()
                except Exception:
                    opened_today = False
                if opened_today:
                    pdt_blocked, pdt_reason = _pdt_would_trigger(acct)
                    if pdt_blocked:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"Selling {symbol.upper()} now would be a day-trade "
                                f"(opened today). {pdt_reason}"
                            ),
                        )
                    # GFV: closing a same-day-opened position while unsettled
                    # proceeds exist is the classic "sold with unsettled funds"
                    # pattern. Block rather than risk a violation.
                    try:
                        unsettled_today = client.get_todays_sell_proceeds()
                    except Exception:
                        unsettled_today = 0.0
                    if unsettled_today > 0:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"Selling {symbol.upper()} now would likely trigger "
                                f"a Good Faith Violation — it was opened today and "
                                f"${unsettled_today:,.2f} of today's sell proceeds "
                                "are still unsettled (T+1). Hold overnight or wait "
                                "for proceeds to settle."
                            ),
                        )
            return
        # A sell that goes beyond held qty = opening a short. Cash accounts
        # cannot short, so block this before it hits Alpaca with a reject.
        if not trading_mode.is_paper():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot short {symbol.upper()} — short trades are not "
                    "supported on a cash account. Reduce qty to at most "
                    f"{held} shares to close your existing long."
                ),
            )

    price = _estimate_order_price(client, symbol, limit_price, order_type)
    if price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot validate position size for {symbol.upper()} — no live "
                "price available. Try again or set a limit price."
            ),
        )

    notional = qty * price

    try:
        acct = client.get_account()
        portfolio_value = float(acct.get("portfolio_value", 0)) or float(acct.get("equity", 0))
        cash = float(acct.get("cash", 0))
        buying_power = float(acct.get("buying_power", 0))
        non_marginable_bp = float(acct.get("non_marginable_buying_power", 0))
        # On live cash accounts, also compute unsettled proceeds from today's
        # sells — Alpaca's non_marginable_bp can lag / report 0 even when real
        # unsettled cash exists, and we need a hard local guard against GFVs.
        unsettled_today = (
            client.get_todays_sell_proceeds() if not trading_mode.is_paper() else 0.0
        )
    except Exception as exc:
        logger.warning("manual_cap_account_lookup_failed error=%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read account to validate order size.",
        ) from exc

    # PDT guard (margin accounts < $25k only). On cash this is a no-op.
    if not trading_mode.is_paper():
        pdt_blocked, pdt_reason = _pdt_would_trigger(acct)
        if pdt_blocked:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=pdt_reason,
            )

    # On a live cash account, cap by the stricter of non_marginable_buying_power
    # and (cash - today's unsettled sell proceeds), so an unsettled sale can't
    # trigger a Good Faith Violation today.
    spendable = min(cash, buying_power) if cash > 0 else buying_power
    if not trading_mode.is_paper():
        live_caps = [spendable]
        if non_marginable_bp > 0:
            live_caps.append(non_marginable_bp)
        live_caps.append(max(0.0, cash - unsettled_today))
        spendable = min(live_caps)
        # Hard block: if there's nothing truly settled left, refuse outright
        # with a GFV-aware error rather than a generic "over cash cap".
        if spendable <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot open a new position — ${unsettled_today:,.2f} of today's "
                    "sell proceeds are still unsettled (T+1). Buying with unsettled "
                    "cash and then selling before it settles would trigger a Good "
                    "Faith Violation. Hold existing positions overnight or wait "
                    "until proceeds settle."
                ),
            )
    cash_cap = spendable * _MANUAL_MAX_CASH_FRACTION
    pos_cap = portfolio_value * _MANUAL_MAX_POSITION_PCT

    if spendable > 0 and notional > cash_cap:
        max_shares = max(1, int(cash_cap // price))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Order total ${notional:,.2f} exceeds {int(_MANUAL_MAX_CASH_FRACTION * 100)}% "
                f"of spendable cash (${spendable:,.2f}). Reduce qty to at most {max_shares} shares."
            ),
        )

    if portfolio_value > 0 and notional > pos_cap:
        max_shares = max(1, int(pos_cap // price))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Order total ${notional:,.2f} exceeds the "
                f"{int(_MANUAL_MAX_POSITION_PCT * 100)}% per-trade cap on a "
                f"${portfolio_value:,.2f} portfolio. Reduce qty to at most {max_shares} shares."
            ),
        )

    logger.info(
        "manual_order_cap_ok symbol=%s side=%s qty=%d price=%.2f notional=%.2f cash=%.2f portfolio=%.2f",
        symbol, side, qty, price, notional, cash, portfolio_value,
    )


# ── Account ───────────────────────────────────────────────────────────────────

@router.get("/account")
def get_account(
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return the Alpaca account summary (cash, equity, buying power, P&L)."""
    cached = _cache_get("account")
    if cached is not None:
        return cached
    client = _get_client()
    acct = client.get_account()
    cash = float(acct.get("cash", 0))
    non_marginable_bp = float(acct.get("non_marginable_buying_power", 0))
    # Compute today's unsettled sell proceeds ourselves — Alpaca's
    # non_marginable_buying_power sometimes reports 0 even on a cash account
    # that actually has unsettled sales, which makes GFV protection silently
    # fail. Doing it locally is authoritative.
    try:
        unsettled_today = client.get_todays_sell_proceeds() if not trading_mode.is_paper() else 0.0
    except Exception:
        unsettled_today = 0.0
    # True settled cash: the stricter of what Alpaca reports (if > 0) and
    # (cash - today's unsettled proceeds). This is what's safe to spend on
    # anything you might flip before T+1.
    if not trading_mode.is_paper():
        candidates = [max(0.0, cash - unsettled_today)]
        if non_marginable_bp > 0:
            candidates.append(non_marginable_bp)
        settled_cash = min(candidates)
    else:
        settled_cash = cash
    unsettled_cash = max(0.0, cash - settled_cash)

    # PDT status — informational for the UI. On a cash account this is always
    # safe; on margin accounts under $25k, show how close we are to the flag.
    try:
        multiplier = float(acct.get("multiplier", 1) or 1)
    except (TypeError, ValueError):
        multiplier = 1.0
    equity_val = float(acct.get("equity", 0) or 0)
    daytrade_count = int(acct.get("daytrade_count", 0) or 0)
    is_margin = multiplier > 1.0
    pdt_applies = is_margin and equity_val < 25_000
    pdt_remaining = max(0, 3 - daytrade_count) if pdt_applies else None
    pdt_at_risk = pdt_applies and daytrade_count >= 2
    result = {
        "paper":             trading_mode.is_paper(),
        "account_id":        acct.get("id"),
        "status":            acct.get("status"),
        "currency":          acct.get("currency", "USD"),
        "cash":              cash,
        "portfolio_value":   float(acct.get("portfolio_value", 0)),
        "equity":            float(acct.get("equity", 0)),
        # v7.16.x (2026-06-10): prior-close equity → frontend "Today's P&L"
        # tile computes equity − last_equity (ground truth, incl. open
        # positions). Staged: inert until the next boundary restart.
        "last_equity":       float(acct.get("last_equity", 0)),
        "buying_power":      float(acct.get("buying_power", 0)),
        "non_marginable_buying_power": non_marginable_bp,
        "settled_cash":      round(settled_cash, 2),
        "unsettled_cash":    round(unsettled_cash, 2),
        "unsettled_today":   round(unsettled_today, 2),
        "long_market_value":  float(acct.get("long_market_value", 0)),
        "short_market_value": float(acct.get("short_market_value", 0)),
        "unrealized_pl":     float(acct.get("unrealized_pl", 0)),
        "unrealized_plpc":   float(acct.get("unrealized_plpc", 0)),
        "daytrade_count":    daytrade_count,
        "pattern_day_trader": bool(acct.get("pattern_day_trader", False)),
        "is_margin_account": is_margin,
        "pdt_applies":       pdt_applies,
        "pdt_daytrades_remaining": pdt_remaining,
        "pdt_at_risk":       pdt_at_risk,
    }
    _cache_set("account", result)
    return result


# ── Trading mode (paper ↔ live toggle) ───────────────────────────────────────
#
# The mode is persisted in ``data/trading_mode.json`` and exposed via the
# ``trading_mode`` singleton. ``GET /trading/mode`` returns the current
# state plus a preflight (are the other mode's credentials configured?
# does the user need to stop autotrade / close positions before flipping?);
# ``POST /trading/mode`` performs the flip after validating those
# preconditions. On a successful flip we:
#   1. Reset the shared Alpaca trading client so the next call rebuilds
#      against the new credentials.
#   2. Invalidate every account / positions cache entry — those are
#      per-mode and MUST NOT leak across accounts.
#   3. Clear the in-memory autotrade day-state (trades_today, confirming,
#      pending_approvals). Monitored positions are already required to be
#      empty by the preflight, so nothing to purge there.
#   4. Broadcast ``trading_mode_changed`` over WebSocket so the wallet
#      badge re-renders immediately.
# The WebSocket trade-updates stream (``AlpacaTradeStream``) re-reads
# credentials on every reconnect cycle — the next backoff loop picks up
# the new mode on its own, so we don't need to tear it down here.

class TradingModeRequest(BaseModel):
    mode:       str  = Field(..., pattern="^(paper|live)$")
    # Opt-in bypass for the "autotrade is running" guard. If True we
    # stop autotrade + reject pending approvals as part of the flip
    # instead of returning 409. Open positions are NEVER bypassed —
    # those require manual close.
    force_stop: bool = False


def _mode_flip_preflight(target_mode: str) -> list[str]:
    """Return a list of human-readable reasons the flip is blocked.

    Empty list → safe to flip. Never raises — the endpoint converts
    the returned reasons into a 409 response with the full list so
    the UI can show them all at once.
    """
    reasons: list[str] = []

    current = trading_mode.get_mode()
    if target_mode == current:
        reasons.append(f"Already in {current} mode.")

    if not _mode_is_configured(target_mode):
        upper = target_mode.upper()
        reasons.append(
            f"No credentials configured for {target_mode}. Set "
            f"ALPACA_{upper}_API_KEY and ALPACA_{upper}_SECRET_KEY in .env "
            "and restart the backend."
        )

    open_positions = [
        m.symbol for m in autotrade_state.monitored_positions if not m.closed
    ]
    if open_positions:
        reasons.append(
            "Close all monitored autotrade positions first: "
            + ", ".join(open_positions)
        )

    return reasons


@router.get("/mode")
def get_trading_mode(
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return the active trading mode and a flip preflight.

    Response shape:
        {
          "mode": "paper"|"live",
          "paper": {"configured": bool, "base_url": str},
          "live":  {"configured": bool, "base_url": str},
          "autotrade_running": bool,
          "pending_approvals": int,
          "open_positions":    [<symbol>, ...],
        }
    The ``paper`` / ``live`` entries never include credentials — just
    whether each mode is configured. ``open_positions`` is the hard
    block; ``autotrade_running`` + ``pending_approvals`` can be
    force-stopped via POST.
    """
    snap = mode_snapshot()
    snap["autotrade_running"]  = bool(autotrade_state.running)
    snap["pending_approvals"]  = len(autotrade_state.pending_approvals)
    snap["open_positions"]     = [
        m.symbol for m in autotrade_state.monitored_positions if not m.closed
    ]
    return snap


@router.post("/mode")
async def post_trading_mode(
    req: TradingModeRequest,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Flip the active trading mode (paper ↔ live).

    Fails with 409 when:
      * Already in the target mode.
      * Target mode has no configured credentials.
      * Any monitored autotrade position is open (hard block — close
        those first, this endpoint won't auto-liquidate live positions).
      * Autotrade is running, UNLESS ``force_stop=True``, in which
        case we stop autotrade + reject pending approvals as part
        of the flip.
    """
    from ..alpaca import reset_shared_trading_client

    current = trading_mode.get_mode()
    target  = req.mode

    # Paper-only builds refuse live before any other preflight so the
    # response is unambiguous (403 paper_boundary, not a 409 config hint).
    if target == "live" and trading_mode.paper_only_enabled():
        raise PaperBoundaryViolation(
            "This build is paper-only (PAPER_ONLY=true): live trading mode is not available."
        )

    reasons = _mode_flip_preflight(target)
    # Autotrade-running is a soft block — the user can force_stop.
    if autotrade_state.running and not req.force_stop:
        reasons.append(
            "Autotrade is currently running. Stop it first, or POST again "
            "with force_stop=true to stop it as part of the flip."
        )

    if reasons:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Cannot switch trading mode", "reasons": reasons},
        )

    # Optional: stop autotrade + reject pending approvals before flipping.
    if autotrade_state.running:
        logger.info("trading_mode_force_stop_autotrade reason=mode_flip")
        autotrade_stop()
    for approval in list(autotrade_state.pending_approvals):
        autotrade_reject(approval.id)

    # Persist the new mode. ``set_mode`` writes ``data/trading_mode.json``
    # and updates the in-memory cache — subsequent Alpaca client
    # constructions will use the new credentials.
    set_mode(target)

    # Drop the old client so the next request rebuilds against the new
    # account. The WebSocket trade-updates stream re-reads credentials
    # on each reconnect, so its next backoff cycle picks up the flip.
    reset_shared_trading_client()

    # Account / positions caches are per-mode — nuking them prevents
    # the UI from briefly showing the PREVIOUS account's numbers after
    # the flip lands.
    _cache_invalidate()

    # Clear the in-memory autotrade day-state that's tied to the old
    # account's trade log. Monitored positions are already empty
    # (guaranteed by the preflight), so nothing to drain there.
    autotrade_state.trades_today.clear()
    autotrade_state.confirming.clear()
    autotrade_state.pending_approvals.clear()

    snap = mode_snapshot()
    logger.warning(
        "trading_mode_flipped from=%s to=%s user_invoked=true",
        current, target,
    )
    # Tell every connected browser to update the badge + reload the
    # wallet; the autotrade status snapshot goes on the same tick so
    # the "Autotrade: Idle" indicator sync'd in the same frame.
    try:
        from ..ws import emit_trading_mode_changed
        emit_trading_mode_changed(snap)
        from ..ws import emit_wallet_update
        emit_wallet_update()
    except Exception:
        pass

    return {
        "mode_snapshot":    snap,
        "autotrade_status": autotrade_status(),
    }


# ── Orders ────────────────────────────────────────────────────────────────────

@router.post("/orders")
def place_order(
    req: PlaceOrderRequest,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Submit a new order to Alpaca (paper or live depending on config)."""
    client = _get_client()
    _enforce_manual_position_cap(client, req.symbol, req.side, req.qty, req.limit_price,
                                 req.order_type)
    # Broker-boundary errors (InvalidOrderRequest / OrderRejected /
    # BrokerUnavailable / UncertainOutcome / PaperBoundaryViolation) are
    # translated to structured responses by the app-level handlers in main.py.
    order = client.submit_order(
        symbol=req.symbol,
        qty=req.qty,
        side=OrderSide(req.side),
        order_type=OrderType(req.order_type),
        time_in_force=TimeInForce(req.time_in_force),
        limit_price=req.limit_price,
        stop_price=req.stop_price,
        take_profit=req.take_profit_price,
        stop_loss=req.stop_loss_price,
    )
    _cache_invalidate("account", "positions")
    return _format_order(order)


@router.post("/quick-trade")
def quick_trade(
    req: QuickTradeRequest,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """One-click trade from the AI analysis page.

    If entry_price is set → limit order, else market order.
    If stop_loss_price and/or take_profit_price are set → bracket order.
    """
    client = _get_client()
    otype = OrderType.LIMIT if req.entry_price else OrderType.MARKET
    _enforce_manual_position_cap(client, req.symbol, req.side, req.qty, req.entry_price,
                                 otype.value)
    order = client.submit_order(
        symbol=req.symbol,
        qty=req.qty,
        side=OrderSide(req.side),
        order_type=otype,
        time_in_force=TimeInForce.DAY,
        limit_price=req.entry_price,
        take_profit=req.take_profit_price,
        stop_loss=req.stop_loss_price,
    )
    _cache_invalidate("account", "positions")
    return _format_order(order)


@router.get("/orders")
def list_orders(
    order_status: str = "open",
    _: object = Depends(require_operator),
) -> list[dict[str, Any]]:
    """List recent orders (open, closed, or all)."""
    client = _get_client()
    return [_format_order(o) for o in client.list_orders(status=order_status)]


@router.get("/orders/{order_id}")
def get_order(
    order_id: str,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    client = _get_client()
    return _format_order(client.get_order(order_id))


@router.delete("/orders/{order_id}")
def cancel_order(
    order_id: str,
    _: object = Depends(require_operator),
) -> dict[str, str]:
    client = _get_client()
    client.cancel_order(order_id)
    _cache_invalidate("account", "positions")
    return {"status": "cancelled", "order_id": order_id}


@router.delete("/orders")
def cancel_all_orders(
    _: object = Depends(require_operator),
) -> dict[str, str]:
    client = _get_client()
    client.cancel_all_orders()
    _cache_invalidate("account", "positions")
    return {"status": "all_orders_cancelled"}


# ── Positions ─────────────────────────────────────────────────────────────────

@router.get("/positions")
def list_positions(
    _: object = Depends(require_operator),
) -> list[dict[str, Any]]:
    """Return all open positions with P&L, enriched with AI trade context."""
    cached = _cache_get("positions")
    if cached is not None:
        return cached
    client = _get_client()
    positions = [_format_position(p) for p in client.list_positions()]

    # Flag positions opened today so the UI can block day-trade / GFV sells.
    try:
        todays_buys = client.get_todays_buy_symbols()
    except Exception:
        todays_buys = set()
    for pos in positions:
        pos["opened_today"] = (pos.get("symbol") or "").upper() in todays_buys

    # Enrich with autotrade context (target, stop, opened_at, etc.)
    from ..autotrade import state as _at_state
    monitored_map = {
        m.symbol: m for m in _at_state.monitored_positions if not m.closed
    }
    for pos in positions:
        m = monitored_map.get(pos["symbol"])
        if m:
            tl = m.trade_log
            pos["target"] = tl.target
            pos["stop_loss"] = tl.stop_loss
            pos["risk_reward"] = tl.risk_reward
            pos["confidence"] = tl.confidence
            pos["setup"] = tl.setup
            pos["reasoning"] = tl.reasoning
            pos["opened_at"] = m.opened_at
            pos["hold_seconds"] = None
            if m.opened_at:
                from datetime import datetime, timezone
                try:
                    opened = datetime.fromisoformat(m.opened_at)
                    pos["hold_seconds"] = int((datetime.now(timezone.utc) - opened).total_seconds())
                except Exception:
                    pass
    _cache_set("positions", positions)
    return positions


@router.get("/positions/{symbol}")
def get_position(
    symbol: str,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    client = _get_client()
    pos = client.get_position(symbol)
    if pos is None:
        raise HTTPException(status_code=404, detail=f"No open position in {symbol.upper()}")
    return _format_position(pos)


@router.delete("/positions/{symbol}")
def close_position(
    symbol: str,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Close a position. Uses market order during hours, extended-hours limit order otherwise."""
    from ..alpaca import AlpacaMarketDataClient, OrderSide, OrderType, TimeInForce
    client = _get_client()

    try:
        # Try normal market close first
        result = _format_order(client.close_position(symbol))
        _cache_invalidate("account", "positions")
        return result
    except Exception as exc:
        msg = str(exc)
        if "403" not in msg and "Forbidden" not in msg:
            raise

    # Market is closed — fall back to extended-hours limit order
    logger.info("close_position_extended_hours symbol=%s", symbol)

    # Get current position to know qty and side
    pos = client.get_position(symbol)
    if pos is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No open position in {symbol}",
        )

    qty = abs(float(pos.get("qty", 0)))
    if qty <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Position in {symbol} has zero quantity",
        )

    # Determine close side (sell to close long, buy to close short)
    is_long = float(pos.get("qty", 0)) > 0
    close_side = OrderSide.SELL if is_long else OrderSide.BUY

    # Get current price for the limit order.
    # Do NOT fall back to pos.current_price — that field can be hours old in
    # after-hours, which would let us submit a wildly mis-priced limit order.
    # Fail loudly and let the user retry instead.
    price = 0.0
    try:
        mkt_client = AlpacaMarketDataClient()
        snaps = mkt_client.fetch_snapshots([symbol])
        snap = snaps.get(symbol, {})
        trade = snap.get("latestTrade") or snap.get("latest_trade") or {}
        price = float(trade.get("p") or trade.get("price") or 0)
        if price <= 0:
            bar = snap.get("minuteBar") or snap.get("minute_bar") or {}
            price = float(bar.get("c", 0))
    except httpx.HTTPError as exc:
        logger.warning("close_position_ext_price_lookup_failed symbol=%s error=%s", symbol, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Could not fetch a fresh price for {symbol} "
                f"(market data request failed: {exc}). Please retry in a few seconds — "
                "we refuse to price an extended-hours limit order from stale data."
            ),
        ) from exc

    if price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"No live quote for {symbol} — extended-hours close needs a "
                "fresh price to set a safe limit. Try again shortly."
            ),
        )

    # Slight buffer to ensure fill: sell a bit below, buy a bit above
    limit_price = round(price * 0.998, 2) if is_long else round(price * 1.002, 2)

    try:
        order = client.submit_order(
            symbol=symbol,
            qty=qty,
            side=close_side,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            extended_hours=True,
        )
        logger.info("close_position_ext_order symbol=%s side=%s qty=%s limit=%.2f",
                     symbol, close_side.value, qty, limit_price)
        _cache_invalidate("account", "positions")
        return _format_order(order)
    except Exception as exc:
        logger.error("close_position_ext_failed symbol=%s error=%s", symbol, exc)
        is_paper = trading_mode.is_paper()
        if is_paper:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Paper trading doesn't support after-hours orders. {symbol} will carry overnight — close it after 9:30 AM ET.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Extended-hours close failed for {symbol}: {exc}",
        )


@router.delete("/positions")
def close_all_positions(
    _: object = Depends(require_operator),
) -> dict[str, str]:
    client = _get_client()
    client.close_all_positions()
    _cache_invalidate("account", "positions")
    return {"status": "all_positions_closed"}


# ── Auto-Trade ───────────────────────────────────────────────────────────

class AutoTradeConfigRequest(BaseModel):
    scan_interval_seconds: int | None = Field(default=None, ge=15, le=300)
    min_confidence: float | None = Field(default=None, ge=0.5, le=1.0)
    position_size_pct: float | None = Field(default=None, ge=0.001, le=0.10)
    max_open_trades: int | None = Field(default=None, ge=1, le=20)
    # v7.14.2 (2026-05-04): upper bound bumped 100 → 9999 so the runtime
    # setter can match the new 999 default. The default itself is set
    # in autotrade.py::AutoTradeState; this validator just bounds what
    # values the API will accept on a runtime override.
    max_trades_per_day: int | None = Field(default=None, ge=1, le=9999)
    confirmation_seconds: int | None = Field(default=None, ge=30, le=600)
    max_daily_loss: float | None = Field(default=None, ge=100, le=100_000)
    approval_timeout_seconds: int | None = Field(default=None, ge=30, le=600)
    # Trailing stop + time exit
    trailing_stop_pct: float | None = Field(default=None, ge=0.001, le=0.10)
    max_hold_minutes: int | None = Field(default=None, ge=5, le=480)
    eod_buffer_minutes: int | None = Field(default=None, ge=5, le=60)
    monitor_interval_seconds: int | None = Field(default=None, ge=5, le=60)
    enable_trailing_stop: bool | None = None
    enable_time_exit: bool | None = None
    # Trading universe override
    trading_universe: list[str] | None = None


@router.get("/autotrade/status")
def autotrade_get_status(
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return the current auto-trade state, config, and recent trade log."""
    return autotrade_status()


@router.post("/autotrade/start")
async def autotrade_start_endpoint(
    config: AutoTradeConfigRequest | None = None,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Start the AI auto-trader. Optionally update config before starting."""
    if config:
        if config.scan_interval_seconds is not None:
            autotrade_state.scan_interval_seconds = config.scan_interval_seconds
        if config.min_confidence is not None:
            autotrade_state.min_confidence = config.min_confidence
        if config.position_size_pct is not None:
            autotrade_state.position_size_pct = config.position_size_pct
        if config.max_open_trades is not None:
            autotrade_state.max_open_trades = config.max_open_trades
        if config.max_trades_per_day is not None:
            autotrade_state.max_trades_per_day = config.max_trades_per_day
        if config.confirmation_seconds is not None:
            autotrade_state.confirmation_seconds = config.confirmation_seconds
        if config.max_daily_loss is not None:
            autotrade_state.max_daily_loss = config.max_daily_loss
        if config.approval_timeout_seconds is not None:
            autotrade_state.approval_timeout_seconds = config.approval_timeout_seconds
        if config.trailing_stop_pct is not None:
            autotrade_state.trailing_stop_pct = config.trailing_stop_pct
        if config.max_hold_minutes is not None:
            autotrade_state.max_hold_minutes = config.max_hold_minutes
        if config.eod_buffer_minutes is not None:
            autotrade_state.eod_buffer_minutes = config.eod_buffer_minutes
        if config.monitor_interval_seconds is not None:
            autotrade_state.monitor_interval_seconds = config.monitor_interval_seconds
        if config.enable_trailing_stop is not None:
            autotrade_state.enable_trailing_stop = config.enable_trailing_stop
        if config.enable_time_exit is not None:
            autotrade_state.enable_time_exit = config.enable_time_exit
        if config.trading_universe is not None:
            set_trading_universe(config.trading_universe)
    autotrade_start(user_id=getattr(user, "id", None))
    return autotrade_status()


@router.post("/autotrade/stop")
async def autotrade_stop_endpoint(
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Stop the AI auto-trader."""
    autotrade_stop()
    return autotrade_status()


class AutonomousBody(BaseModel):
    enabled: bool


@router.post("/autotrade/autonomous")
async def autotrade_autonomous_endpoint(
    body: AutonomousBody,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Toggle autonomous (auto-approve) mode.

    v7.16.x — when enabled, matched setups that clear every gate are
    auto-approved + auto-executed instead of waiting for a manual
    approval click. Independent of the start/stop switch. Returns the
    full autotrade status so the dashboard can re-render the toggle.
    """
    return autotrade_set_autonomous(body.enabled)


class ApproveBody(BaseModel):
    qty: int | None = None  # Optional qty override from user


@router.post("/autotrade/approve/{approval_id}")
async def autotrade_approve_endpoint(
    approval_id: str,
    body: ApproveBody | None = None,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Approve a pending trade — execute it now. Optionally override qty."""
    qty_override = body.qty if body else None
    result = autotrade_approve(approval_id, qty_override=qty_override)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending approval with id '{approval_id}' — it may have expired.",
        )
    # Approve executes a real order — honor the cache invariant (see header
    # comment): every order/close mutation invalidates account+positions.
    _cache_invalidate("account", "positions")
    return {"trade": result, **autotrade_status()}


@router.post("/autotrade/reject/{approval_id}")
def autotrade_reject_endpoint(
    approval_id: str,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Reject a pending trade — do not execute."""
    result = autotrade_reject(approval_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending approval with id '{approval_id}' — it may have expired.",
        )
    return {"trade": result, **autotrade_status()}


@router.post("/autotrade/close/{symbol}")
async def autotrade_close_position(
    symbol: str,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Manually close a monitored autotrade position."""
    result = await autotrade_close_monitored(symbol)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No open monitored position for '{symbol.upper()}'.",
        )
    # Close mutates broker positions — honor the cache invariant.
    _cache_invalidate("account", "positions")
    return {"closed": result, **autotrade_status()}


# ── Trading Universe ────────────────────────────────────────────────────────


class UniverseRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=100)


@router.get("/autotrade/universe")
def get_universe(
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return the active trading universe and the .env default.

    v7.13 Phase 2: also surfaces ``applied_morning_universe`` —
    operator-action provenance for whichever apply event populated
    the current ``trading_universe`` (or None if running on default
    base). The dashboard reads ``restored_from_disk`` from this
    block to show "Restored from snapshot at restart" when the
    backend rehydrated state from a disk-persisted apply.
    """
    return {
        "active": get_active_universe(),
        "default": list(settings.training_universe),
        "is_custom": autotrade_state.trading_universe is not None,
        # v7.13 Phase 2 — apply-event metadata. None when no morning
        # universe has been applied this session (default-base run).
        "applied_morning_universe": autotrade_state.applied_universe_metadata,
    }


def _purge_study_insights_for_removed_symbols(
    user_id: int, prev_active: list[str], new_active: list[str]
) -> int:
    """Delete the current user's study insights for symbols dropped from the universe.

    Returns the number of rows deleted. Insights for symbols that remain in the
    universe are left alone so repetition counts keep building.
    """
    removed = {s.upper() for s in prev_active} - {s.upper() for s in new_active}
    if not removed:
        return 0
    db = SessionLocal()
    try:
        result = db.execute(
            delete(StudyInsight).where(
                StudyInsight.user_id == user_id,
                StudyInsight.symbol.in_(removed),
            )
        )
        db.commit()
        count = result.rowcount or 0
        if count:
            logger.info(
                "study_insights_purged user_id=%s symbols=%s rows=%d",
                user_id, sorted(removed), count,
            )
        return count
    finally:
        db.close()


@router.post("/autotrade/universe")
def update_universe(
    body: UniverseRequest,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Set a custom trading universe (overrides .env default).

    Also purges the caller's study insights for any symbol that was dropped —
    the Study panel should only show stocks currently in the universe.
    """
    prev_active = get_active_universe()
    set_trading_universe(body.symbols)
    new_active = get_active_universe()
    purged = _purge_study_insights_for_removed_symbols(user.id, prev_active, new_active)
    return {
        "active": new_active,
        "default": list(settings.training_universe),
        "is_custom": autotrade_state.trading_universe is not None,
        "study_insights_purged": purged,
    }


@router.delete("/autotrade/universe")
def reset_universe(
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Reset trading universe to .env default.

    Also purges the caller's study insights for any symbol the reset drops —
    normally the default is a superset, but if the user had manually added
    symbols on top of it, those get cleaned up here.
    """
    prev_active = get_active_universe()
    set_trading_universe(None)
    new_active = get_active_universe()
    purged = _purge_study_insights_for_removed_symbols(user.id, prev_active, new_active)
    return {
        "active": new_active,
        "default": list(settings.training_universe),
        "is_custom": False,
        "study_insights_purged": purged,
    }


# ── Analysis Cache ──────────────────────────────────────────────────────────


class CacheConfigRequest(BaseModel):
    enabled: bool | None = None
    ttl_seconds: float | None = Field(default=None, ge=10, le=600)
    max_price_drift_pct: float | None = Field(default=None, ge=0.1, le=5.0)


@router.get("/autotrade/cache")
def get_cache_stats(
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return AI analysis cache statistics."""
    return analysis_cache.stats()


@router.post("/autotrade/cache/clear")
def clear_cache(
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Clear the AI analysis cache."""
    analysis_cache.clear()
    return {"status": "cleared", **analysis_cache.stats()}


@router.post("/autotrade/cache/config")
def update_cache_config(
    config: CacheConfigRequest,
    _: object = Depends(require_operator),
) -> dict[str, Any]:
    """Update cache settings (TTL, price drift threshold, enable/disable)."""
    if config.enabled is not None:
        analysis_cache.enabled = config.enabled
    if config.ttl_seconds is not None:
        analysis_cache.ttl_seconds = config.ttl_seconds
    if config.max_price_drift_pct is not None:
        analysis_cache.max_price_drift_pct = config.max_price_drift_pct
    return analysis_cache.stats()


# ── Trade History & Performance ───────────────────────────────────────────────


@router.get("/history")
def get_history(
    days: int = 30,
    symbol: str | None = None,
    setup: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return persisted trade history with optional filters."""
    trades = get_trade_history(
        user.id, days=days, symbol=symbol, setup=setup, limit=limit, offset=offset,
    )
    return {"trades": trades, "count": len(trades)}


@router.get("/performance")
def get_performance(
    days: int = 30,
    symbol: str | None = None,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return aggregate performance stats over a time window."""
    return get_performance_stats(user.id, days=days, symbol=symbol)


@router.get("/export/csv")
def export_trades_csv(
    days: int = 365,
    symbol: str | None = None,
    user: object = Depends(require_operator),
):
    """Export trade history as a downloadable CSV file."""
    trades = get_trade_history(user.id, days=days, symbol=symbol, limit=10_000)

    columns = [
        "Date Opened", "Date Closed", "Symbol", "Direction", "Setup",
        "Confidence", "Qty", "Entry Price", "Exit Price",
        "P&L ($)", "P&L (%)", "R-Multiple",
        "Stop Loss", "Target", "Entry Zone", "Risk/Reward",
        "Exit Reason", "Hold Duration (min)", "Status",
    ]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)

    for t in trades:
        hold_min = round(t["hold_duration_seconds"] / 60, 1) if t.get("hold_duration_seconds") else ""
        writer.writerow([
            t.get("opened_at", ""),
            t.get("closed_at", ""),
            t.get("symbol", ""),
            t.get("direction", ""),
            t.get("setup", ""),
            t.get("confidence", ""),
            t.get("qty", ""),
            t.get("entry_price", ""),
            t.get("exit_price", ""),
            t.get("pnl", ""),
            f'{t["pnl_pct"]:.2f}' if t.get("pnl_pct") is not None else "",
            t.get("r_multiple", ""),
            t.get("stop_loss", ""),
            t.get("target", ""),
            t.get("entry_zone", ""),
            t.get("risk_reward", ""),
            t.get("exit_reason", ""),
            hold_min,
            t.get("status", ""),
        ])

    # Summary row
    total_pnl = sum(t["pnl"] for t in trades if t.get("pnl") is not None)
    total_trades = sum(1 for t in trades if t.get("pnl") is not None)
    wins = sum(1 for t in trades if t.get("pnl") is not None and t["pnl"] > 0)
    losses = total_trades - wins

    writer.writerow([])  # blank separator
    writer.writerow(["TOTAL", "", "", "", "", "", "", "", "",
                     round(total_pnl, 2), "", "", "", "", "", "", "", "", ""])
    writer.writerow(["Trades", total_trades, "Wins", wins, "Losses", losses,
                     "Win Rate", f"{(wins / total_trades * 100):.1f}%" if total_trades else "0%",
                     "", "", "", "", "", "", "", "", "", "", ""])

    buf.seek(0)
    filename = f"tradehack_trades_{days}d.csv"
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_order(o: dict) -> dict[str, Any]:
    return {
        "id":             o.get("id"),
        "symbol":         o.get("symbol"),
        "side":           o.get("side"),
        "qty":            o.get("qty"),
        "filled_qty":     o.get("filled_qty"),
        "type":           o.get("type"),
        "time_in_force":  o.get("time_in_force"),
        "status":         o.get("status"),
        "limit_price":    o.get("limit_price"),
        "stop_price":     o.get("stop_price"),
        "filled_avg_price": o.get("filled_avg_price"),
        "submitted_at":   o.get("submitted_at"),
        "filled_at":      o.get("filled_at"),
        "order_class":    o.get("order_class"),
    }


def _format_position(p: dict) -> dict[str, Any]:
    return {
        "symbol":           p.get("symbol"),
        "qty":              float(p.get("qty", 0)),
        "side":             p.get("side"),
        "avg_entry_price":  float(p.get("avg_entry_price", 0)),
        "current_price":    float(p.get("current_price", 0)),
        "market_value":     float(p.get("market_value", 0)),
        "cost_basis":       float(p.get("cost_basis", 0)),
        "unrealized_pl":    float(p.get("unrealized_pl", 0)),
        "unrealized_plpc":  float(p.get("unrealized_plpc", 0)),
        "change_today":     float(p.get("change_today", 0)),
    }


# ── AI Lessons ────────────────────────────────────────────────────────────────

# ── Simulator ────────────────────────────────────────────────────────────────

@router.get("/simulator/performance")
def get_sim_performance(
    days: int = 7,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return hypothetical performance if every AI recommendation had been followed."""
    return get_simulated_performance(user.id, days=days)


@router.get("/simulator/trades")
def get_sim_trades(
    days: int = 7,
    status: str | None = None,
    limit: int = 100,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return individual simulated trade records."""
    trades = get_simulated_trades(user.id, days=days, status=status, limit=limit)
    return {"trades": trades, "count": len(trades)}


# ── AI Lessons ────────────────────────────────────────────────────────────────

@router.get("/lessons")
def get_trade_lessons(
    limit: int = 20,
    symbol: str | None = None,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Return recent AI-generated lessons from losing trades."""
    lessons = get_lessons(user.id, limit=limit, symbol=symbol)
    return {"lessons": lessons, "count": len(lessons)}


def _lesson_to_dict(record: TradeLesson) -> dict[str, Any]:
    return {
        "id": record.id,
        "trade_id": record.trade_id,
        "symbol": record.symbol,
        "setup": record.setup,
        "direction": record.direction,
        "pnl": record.pnl,
        "lesson": record.lesson,
        "tags": record.tags,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@router.post("/lessons/generate/{trade_id}")
def generate_trade_lesson(
    trade_id: int,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Manually generate an AI lesson for a specific losing trade.

    Useful for trades that were closed before lesson-generation existed,
    or manual trades that weren't auto-reviewed.
    """
    db = SessionLocal()
    try:
        record = db.scalars(
            select(TradeRecord).where(
                TradeRecord.id == trade_id,
                TradeRecord.user_id == user.id,
            )
        ).first()
        if not record:
            raise HTTPException(status_code=404, detail="Trade not found")
        if record.pnl is None or record.pnl >= 0:
            raise HTTPException(
                status_code=400,
                detail="Lessons are only generated from closed losing trades",
            )

        # Already reviewed? Return the existing lesson rather than re-charging the API.
        existing = db.scalars(
            select(TradeLesson).where(TradeLesson.trade_id == trade_id).limit(1)
        ).first()
        if existing:
            return {"status": "exists", "lesson": _lesson_to_dict(existing)}

        trade_dict = {
            "id": record.id,
            "symbol": record.symbol,
            "direction": record.direction,
            "setup": record.setup,
            "confidence": record.confidence,
            "entry_price": record.entry_price,
            "exit_price": record.exit_price,
            "stop_loss": record.stop_loss,
            "target": record.target,
            "reasoning": record.reasoning,
            "analysis_context": record.analysis_context,
            "exit_reason": record.exit_reason,
            "highest_price_reached": record.highest_price_reached,
            "r_multiple": record.r_multiple,
            "hold_duration_seconds": record.hold_duration_seconds,
            "pnl": record.pnl,
            "pnl_pct": record.pnl_pct,
        }
    finally:
        db.close()

    # Call the LLM (blocking, can take a few seconds)
    result = generate_lesson(trade_dict)
    if not result:
        raise HTTPException(
            status_code=503,
            detail="Lesson generation failed — check server logs and ANTHROPIC_API_KEY",
        )

    lesson_id = save_lesson(
        user_id=user.id,
        trade_id=record.id,
        symbol=record.symbol,
        setup=record.setup,
        direction=record.direction,
        pnl=record.pnl,
        lesson=result["lesson"],
        tags=result.get("tags"),
    )
    if not lesson_id:
        raise HTTPException(status_code=500, detail="Failed to save lesson to database")

    db = SessionLocal()
    try:
        new_record = db.scalars(
            select(TradeLesson).where(TradeLesson.id == lesson_id).limit(1)
        ).first()
        return {"status": "generated", "lesson": _lesson_to_dict(new_record) if new_record else None}
    finally:
        db.close()


# ── After-Hours Study ────────────────────────────────────────────────────────

@router.post("/study/run")
def run_study(
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Run the after-hours AI study session across all trading universe symbols.

    Reviews the full day's price action, identifies missed setups, and saves
    insights that get injected into future analysis prompts.
    """
    result = run_study_session(user.id)
    return result


@router.post("/study/run/stream")
def run_study_stream(
    user: object = Depends(require_operator),
) -> StreamingResponse:
    """Streaming variant of /study/run — emits NDJSON progress events.

    One JSON object per line:
      {"event": "start", "total": N, "study_date": "..."}
      {"event": "progress", "index": i, "total": N, "symbol": "...", "status": "done|skipped|failed", "new": n, "merged": m}
      {"event": "done", ...}

    The client reads the body as a stream and updates the UI as each line arrives.
    """
    import json as _json
    import asyncio

    user_id = user.id

    async def _gen():
        # Run the blocking generator in a thread so we don't block the event loop.
        # We use a queue bridge: the worker thread puts events; the async side yields them.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        def _worker():
            try:
                for evt in iter_study_session(user_id):
                    asyncio.run_coroutine_threadsafe(queue.put(evt), loop).result()
            except Exception as exc:
                asyncio.run_coroutine_threadsafe(
                    queue.put({"event": "error", "error": str(exc)}),
                    loop,
                ).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(SENTINEL), loop).result()

        task = loop.run_in_executor(None, _worker)
        try:
            while True:
                evt = await queue.get()
                if evt is SENTINEL:
                    break
                yield _json.dumps(evt) + "\n"
        finally:
            await task

    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/study/insights")
def get_study_insights_endpoint(
    limit: int = 30,
    study_date: str | None = None,
    user: object = Depends(require_operator),
) -> dict[str, Any]:
    """Fetch recent after-hours study insights.

    Side effect: prunes any insights for symbols that are no longer in the
    active universe. This keeps the Study panel in sync when the user
    removed symbols before the auto-purge logic was added — one page
    refresh is enough to clean them up.
    """
    _prune_study_insights_outside_universe(user.id)
    insights = get_study_insights(user.id, limit=limit, study_date=study_date)
    return {"insights": insights, "count": len(insights)}


def _prune_study_insights_outside_universe(user_id: int) -> int:
    """Delete the user's study insights whose symbol is not in the current universe."""
    active = {s.upper() for s in get_active_universe()}
    if not active:
        return 0
    db = SessionLocal()
    try:
        result = db.execute(
            delete(StudyInsight).where(
                StudyInsight.user_id == user_id,
                StudyInsight.symbol.notin_(active),
            )
        )
        db.commit()
        count = result.rowcount or 0
        if count:
            logger.info(
                "study_insights_pruned_outside_universe user_id=%s rows=%d",
                user_id, count,
            )
        return count
    finally:
        db.close()


# ── Morning Readiness Guard (v7.16, 2026-05-07) ─────────────────────────────
#
# Operational telemetry: surfaces a multi-stage banner so the operator
# notices when autotrade is off / universe wasn't applied / backend
# started late, BEFORE NY_AM opens. Read endpoint is intentionally
# UNAUTHENTICATED so the frontend can render the banner before login
# (e.g., on a fresh page load when the user hasn't entered credentials
# yet). Audit-event POST is auth-required since it tags the user.


@router.get("/readiness")
async def get_readiness() -> dict:
    """Return the current ReadinessState as JSON. No auth — the
    frontend reads this before login is established so it can warn
    on a stale/blank dashboard. Cheap and idempotent (read-only)."""
    from dataclasses import asdict as _asdict
    from ..readiness import get_readiness_state
    return _asdict(get_readiness_state())


class _ReadinessEventRequest(BaseModel):
    """Frontend-emitted audit event. Sent when the user dismisses
    the banner, clicks the CTA, or the frontend records a notable
    severity transition. Backend appends to JSONL, no DB writes."""
    severity: str = Field(..., pattern="^(ok|info|warn|urgent|blocking)$")
    stage: str = Field(..., min_length=1, max_length=64)
    action: str = Field(..., pattern="^(user_dismissed|user_acted|frontend_alert)$")
    message: str = Field("", max_length=500)


@router.post("/readiness/event")
async def post_readiness_event(
    req: _ReadinessEventRequest,
    user: object = Depends(require_operator),
) -> dict:
    """Append a readiness event to today's JSONL audit. Used by the
    frontend to record dismissals + alert acknowledgements."""
    from ..readiness import log_readiness_event
    log_readiness_event(
        severity=req.severity,
        stage=req.stage,
        action=req.action,
        message=req.message,
        extra={"user_id": getattr(user, "id", None)},
    )
    return {"ok": True}
