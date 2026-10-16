from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class AuthUser(BaseModel):
    name: str
    email: str


class AuthResponse(BaseModel):
    message: str
    user: AuthUser


class SymbolPulse(BaseModel):
    symbol: str
    price: float
    change_pct: float          # % change from session open bar to latest bar
    direction: Literal["up", "down", "flat"]
    relative_volume: float     # latest bar volume vs session average
    bar_time: str              # ISO timestamp of the latest bar
    sparkline: list[float] = Field(default_factory=list)  # recent close prices for inline chart
    has_data: bool = True      # False when no bars available (market closed / new symbol)


class MarketPulseResponse(BaseModel):
    as_of: str                 # ISO timestamp of when the snapshot was taken
    market_open: bool          # True during regular hours (9:30 AM – 4 PM ET)
    market_session: str = "closed"  # "open" | "pre_market" | "after_hours" | "closed"
    pulses: list[SymbolPulse]  # sorted by |change_pct| descending


class IndicatorSnapshot(BaseModel):
    ema9: float
    ema20: float
    rsi: float
    vwap: float
    current_price: float
    change_pct: float
    session_high: float
    session_low: float


class KeyLevels(BaseModel):
    support: float
    resistance: float
    vwap: float


class TradeRecommendation(BaseModel):
    direction: Literal["long", "short", "wait"]
    setup: str
    entry_zone: str
    stop_loss: str
    target: str
    risk_reward: str
    confidence: float = Field(ge=0.0, le=1.0)


class AnalysisResponse(BaseModel):
    symbol: str
    trend: Literal["bullish", "bearish", "sideways"]
    trend_strength: Literal["strong", "moderate", "weak"]
    pattern: str
    key_levels: KeyLevels
    recommendation: TradeRecommendation
    reasoning: str
    risks: str
    indicators: IndicatorSnapshot


class CandleBar(BaseModel):
    time: int      # Unix timestamp in seconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


class ChartResponse(BaseModel):
    symbol: str
    timeframe: str
    bars: list[CandleBar]


class ICTZonesResponse(BaseModel):
    """Wire shape for ``GET /market/ict/{symbol}``.

    Carries the chart bars *and* the full ICT geometry the frontend
    needs to draw overlays (FVG/OB boxes, liquidity lines, killzone
    band, sweep/MSS markers). Structured as a superset of
    :class:`ChartResponse`: a caller that only wants candles can just
    consume ``bars`` and ignore the rest; the overlay renderer reads
    ``zones`` + the top-level at-a-glance flags.

    The ``zones`` sub-dict is intentionally loose (``dict``) because
    its shape mirrors whatever ``ict.compute_ict_features`` dumps —
    keeping the schema lax here avoids a schema churn every time we
    add or rename a geometry key. Callers should treat it as opaque
    JSON and navigate by key name.
    """

    symbol: str
    timeframe: str
    bars: list[CandleBar]

    # ── At-a-glance indicator pills (populated when data is available) ──
    killzone:      str | None = None   # "ny_am" | "silver_bullet" | "ny_pm"
    pd_pct:        float | None = None
    pd_zone:       str | None = None   # "discount" | "equilibrium" | "premium"
    in_ote_long:   bool = False
    in_ote_short:  bool = False
    in_bull_zone:  bool = False
    in_bear_zone:  bool = False

    # ── Optional context dicts (None when detector couldn't resolve) ────
    dealing_range:     dict | None = None
    last_sweep:        dict | None = None
    last_mss:          dict | None = None
    nearest_bull_zone: dict | None = None
    nearest_bear_zone: dict | None = None

    # ── Full geometry for overlay rendering ─────────────────────────────
    # {fvgs, obs, swings, sweeps, mss_events, liquidity} — see
    # ``ict.compute_ict_features`` docs for the exact keys. Each list
    # item is capped so the payload stays browser-friendly.
    zones: dict = Field(default_factory=dict)
