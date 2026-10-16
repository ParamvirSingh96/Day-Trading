"""
rules.py — Shared atomic predicates for both rule engines.
===========================================================
Both ``setups.py`` (historical DataFrame scanner used by the backtester /
simulator) and ``strategies.py`` (live snapshot pre-screen used by the
autotrader) evaluate things like "is the EMA stack bullish?" or "did
price break the opening range?" — but they reimplemented those checks
independently, which led to subtle drift.

This module owns the atomic predicates. Callers adapt their own data
shape (pandas Series, indicator dict, whatever) into scalar floats and
call the functions here. That keeps these predicates pure, trivially
testable, and guaranteed to agree across both engines.

Naming convention:
  • `is_*`        — pure boolean about a single instant (ema stack, vwap side)
  • `broke_*`     — instantaneous boundary crossing (OR break)
  • `crossed_*`   — two-bar transition (vwap reclaim/lose)
  • `meets_*`     — threshold comparison (relative volume)
"""

from __future__ import annotations


# ── EMA alignment ────────────────────────────────────────────────────────────

def is_bullish_ema_stack(ema9: float, ema20: float, ema50: float) -> bool:
    """Bullish trend stack: 9 > 20 > 50."""
    return ema9 > ema20 > ema50


def is_bearish_ema_stack(ema9: float, ema20: float, ema50: float) -> bool:
    """Bearish trend stack: 9 < 20 < 50."""
    return ema9 < ema20 < ema50


def ema9_above_ema20(ema9: float, ema20: float) -> bool:
    return ema9 > ema20


def ema9_below_ema20(ema9: float, ema20: float) -> bool:
    return ema9 < ema20


# ── VWAP position ────────────────────────────────────────────────────────────

def price_above_vwap(price: float, vwap: float) -> bool:
    return price > vwap


def price_below_vwap(price: float, vwap: float) -> bool:
    return price < vwap


def crossed_above_vwap(
    prev_close: float, prev_vwap: float,
    curr_close: float, curr_vwap: float,
) -> bool:
    """Two-bar VWAP reclaim: previously below, now above."""
    return prev_close < prev_vwap and curr_close > curr_vwap


def crossed_below_vwap(
    prev_close: float, prev_vwap: float,
    curr_close: float, curr_vwap: float,
) -> bool:
    """Two-bar VWAP loss: previously above, now below."""
    return prev_close > prev_vwap and curr_close < curr_vwap


# ── Opening range ────────────────────────────────────────────────────────────

def broke_above_opening_range(price: float, or_high: float) -> bool:
    return price > or_high


def broke_below_opening_range(price: float, or_low: float) -> bool:
    return price < or_low


# ── Volume ───────────────────────────────────────────────────────────────────

def meets_relative_volume(rv: float, min_rv: float) -> bool:
    """Relative volume is at or above the required threshold."""
    return rv >= min_rv
