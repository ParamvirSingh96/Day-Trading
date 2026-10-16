from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import anthropic
import pandas as pd
from pydantic import BaseModel, Field, ValidationError, model_validator

from .settings import settings


# ── AI response schema ────────────────────────────────────────────────────────
# Every field the downstream code (autotrade, lessons, frontend) reads must be
# present here. When the model returns an unexpected shape, validation fails
# loudly instead of crashing later with a KeyError deep in the pipeline.

class _AIKeyLevels(BaseModel):
    support: Optional[float] = None
    resistance: Optional[float] = None
    vwap: Optional[float] = None

    class Config:
        extra = "allow"


class _AIRecommendation(BaseModel):
    direction: Literal["long", "short", "wait"]
    setup: str = ""
    entry_zone: Optional[str] = None
    stop_loss: Optional[str] = None
    target: Optional[str] = None
    risk_reward: Optional[str] = None

    # Structured probabilities (feature #10). The model must estimate the
    # probability distribution over three mutually-exclusive outcomes:
    #   - p_target:  P(target hit before stop, within the holding window)
    #   - p_stop:    P(stop hit before target, within the holding window)
    #   - p_neither: P(neither before the holding window closes)
    # They should sum to 1.0; the validator below normalizes and defends
    # against small drift (Claude sometimes rounds to 2 decimals).
    #
    # ``confidence`` is kept for backward compatibility but is now derived:
    # ``confidence = p_target`` (the actionable "probability of success").
    # Every downstream gate/multiplier in autotrade reads ``confidence``, so
    # making it the authoritative single number avoids a sweeping refactor
    # while letting the new calibration machinery consume the full distro.
    p_target:   Optional[float] = Field(default=None, ge=0.0, le=1.0)
    p_stop:     Optional[float] = Field(default=None, ge=0.0, le=1.0)
    p_neither:  Optional[float] = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    class Config:
        extra = "allow"

    @model_validator(mode="after")
    def _sync_probs_and_confidence(self) -> "_AIRecommendation":
        """Reconcile probabilities and ``confidence``.

        Three accepted shapes from the model:
          1. All three probs provided → normalize to sum=1, set
             ``confidence = p_target``.
          2. Only ``confidence`` provided → synthesize
             ``p_target=confidence``, ``p_stop=1-confidence``,
             ``p_neither=0``. This is the legacy-response fallback.
          3. No confidence-like field provided → defaults to 0, which the
             scan loop's gate already treats as "skip this trade".
        """
        probs = [self.p_target, self.p_stop, self.p_neither]
        provided = [p for p in probs if p is not None]

        if len(provided) == 3:
            total = sum(provided)
            # Tolerate Claude rounding "0.55 + 0.30 + 0.15 = 1.00" imperfectly.
            # Normalize to exact 1.0; but if the total is so far off (>0.15)
            # we treat it as a degenerate response and zero everything out —
            # the trade gets skipped rather than fired on bogus numbers.
            if abs(total - 1.0) > 0.15 or total <= 0.0:
                logger.warning(
                    "ai_probs_out_of_bounds target=%.3f stop=%.3f neither=%.3f total=%.3f — zeroing",
                    self.p_target or 0, self.p_stop or 0, self.p_neither or 0, total,
                )
                object.__setattr__(self, "p_target",  0.0)
                object.__setattr__(self, "p_stop",    0.0)
                object.__setattr__(self, "p_neither", 1.0)
                object.__setattr__(self, "confidence", 0.0)
                return self
            # Normalize softly — keep the ratios intact, force sum == 1.
            object.__setattr__(self, "p_target",  round((self.p_target  or 0.0) / total, 4))
            object.__setattr__(self, "p_stop",    round((self.p_stop    or 0.0) / total, 4))
            object.__setattr__(self, "p_neither", round((self.p_neither or 0.0) / total, 4))
            object.__setattr__(self, "confidence", self.p_target or 0.0)
            return self

        # Legacy path: only ``confidence`` — synthesize a 2-outcome distro.
        # ``p_neither = 0`` loses some calibration fidelity but matches the
        # old mental model, and keeps Brier scoring from breaking on old data.
        if self.confidence > 0:
            object.__setattr__(self, "p_target",  round(self.confidence, 4))
            object.__setattr__(self, "p_stop",    round(1.0 - self.confidence, 4))
            object.__setattr__(self, "p_neither", 0.0)
        return self


class AIAnalysisResponse(BaseModel):
    """Validated shape of the JSON the analysis model must return."""
    trend: Literal["bullish", "bearish", "sideways"]
    trend_strength: Literal["strong", "moderate", "weak"] = "moderate"
    pattern: str = ""
    key_levels: _AIKeyLevels = Field(default_factory=_AIKeyLevels)
    recommendation: _AIRecommendation
    reasoning: str = ""
    risks: str = ""

    class Config:
        # The model is free to include additional fields (sentiment, premarket,
        # etc.) — we only want to fail if *required* fields are missing or
        # malformed.
        extra = "allow"

logger = logging.getLogger("tradehack.analyzer")


# ── AI Analysis Cache ──────────────────────────────────────────────────────────
# Caches Claude analysis results per symbol to avoid duplicate API calls when
# the same stock is analyzed multiple times in quick succession (e.g. autotrade
# scan then user clicks Analyze on the same symbol from the Market Tools page).

@dataclass
class _CacheEntry:
    result: dict
    created: float          # time.monotonic() timestamp
    price_at_cache: float   # price when analysis was cached


@dataclass
class AnalysisCache:
    """Thread-safe LRU-ish cache for AI analysis results."""
    ttl_seconds: float = 120.0        # default 2 min
    max_price_drift_pct: float = 0.5  # invalidate if price moved >0.5%
    max_entries: int = 50
    enabled: bool = True

    _store: dict[str, _CacheEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _hits: int = 0
    _misses: int = 0

    def get(self, symbol: str, current_price: float | None = None) -> dict | None:
        """Return cached result if fresh enough, else None."""
        if not self.enabled:
            return None
        with self._lock:
            entry = self._store.get(symbol)
            if entry is None:
                self._misses += 1
                return None

            import time
            age = time.monotonic() - entry.created
            if age > self.ttl_seconds:
                del self._store[symbol]
                self._misses += 1
                logger.debug("cache_expired symbol=%s age=%.0fs", symbol, age)
                return None

            # Invalidate if price has drifted too far from when analysis was cached
            if current_price and entry.price_at_cache > 0:
                drift_pct = abs(current_price - entry.price_at_cache) / entry.price_at_cache * 100
                if drift_pct > self.max_price_drift_pct:
                    del self._store[symbol]
                    self._misses += 1
                    logger.debug("cache_price_drift symbol=%s drift=%.2f%%", symbol, drift_pct)
                    return None

            self._hits += 1
            logger.info(
                "cache_hit symbol=%s age=%.0fs hits=%d misses=%d",
                symbol, age, self._hits, self._misses,
            )
            return entry.result

    def put(self, symbol: str, result: dict, current_price: float):
        """Store an analysis result in the cache."""
        if not self.enabled:
            return
        import time
        with self._lock:
            # Evict oldest entries if at capacity
            if len(self._store) >= self.max_entries and symbol not in self._store:
                oldest_key = min(self._store, key=lambda k: self._store[k].created)
                del self._store[oldest_key]

            self._store[symbol] = _CacheEntry(
                result=result,
                created=time.monotonic(),
                price_at_cache=current_price,
            )
            logger.debug("cache_put symbol=%s price=%.2f entries=%d", symbol, current_price, len(self._store))

    def invalidate(self, symbol: str):
        """Remove a specific symbol from the cache."""
        with self._lock:
            self._store.pop(symbol, None)

    def clear(self):
        """Clear the entire cache."""
        with self._lock:
            self._store.clear()
            logger.info("cache_cleared")

    def stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "enabled":   self.enabled,
                "entries":   len(self._store),
                "hits":      self._hits,
                "misses":    self._misses,
                "hit_rate":  round(self._hits / total * 100, 1) if total > 0 else 0,
                "ttl_seconds":        self.ttl_seconds,
                "max_price_drift_pct": self.max_price_drift_pct,
                "symbols":   list(self._store.keys()),
            }


# Global cache instance
analysis_cache = AnalysisCache()

_ET = ZoneInfo("America/New_York")

_SYSTEM_PROMPT = (
    "You are an elite intraday trader and technical analyst with 20+ years trading US equities. "
    "You specialise in momentum, breakout, and VWAP mean-reversion setups on 1-minute charts, "
    "and you are fluent in ICT / smart-money structure (sweeps, MSS, FVGs, order blocks, "
    "premium/discount, OTE) when the chart presents them. "
    "You have been given a rich set of indicators — EMA, RSI, MACD, ATR, Bollinger Bands, VWAP, "
    "opening range, candlestick patterns, raw candle data, pre-market/after-hours data, bid-ask spread with order-flow imbalance, sector momentum with relative strength, recent news headlines with catalyst detection, daily chart context for higher-timeframe confluence, AND ICT smart-money structure when it fires. "
    "Before answering, work through the confluence of signals across BOTH timeframes: "
    "daily trend alignment, intraday trend, momentum, volume, volatility, key levels, and smart-money structure. "
    "\n\n"
    # Probability-first output contract. The downstream system tracks calibration
    # via Brier score + log loss, so every estimate you produce is graded against
    # the actual outcome. Well-calibrated 70% predictions matter more than vague
    # 'high-conviction' hand-waves.
    "**Output contract — structured probabilities.** For every trade, estimate "
    "the probability of three mutually exclusive outcomes within a 60-minute holding window:\n"
    "  • p_target  = P(target price hit before stop loss is hit)\n"
    "  • p_stop    = P(stop loss hit before target price is hit)\n"
    "  • p_neither = P(position closes at time limit or EOD without hitting either)\n"
    "These MUST be in [0.0, 1.0] and sum to exactly 1.0. Do not output a standalone 'confidence' field — "
    "the system derives it from p_target. If the setup is a 'wait' (no trade), return p_target=0, p_stop=0, p_neither=1.\n"
    "\n"
    "**Calibration discipline:**\n"
    "  • If you say p_target=0.80, it means you expect 80 of 100 such setups to hit target before stop. "
    "Your Brier score and log loss are tracked — being systematically over-confident (saying 80% when actual is 55%) "
    "will surface as miscalibration and erode the multiplier stack.\n"
    "  • p_target above 0.70 requires at least 3 independent confirming signals.\n"
    "  • p_target above 0.80 requires daily-trend alignment with the intraday setup AND bid-ask imbalance that supports the direction.\n"
    "  • If the intraday setup conflicts with the daily trend, cap p_target at 0.65.\n"
    "  • If the bid-ask imbalance opposes the setup direction, reduce p_target by at least 0.05.\n"
    "  • Sector momentum is critical: outperforming a rising sector is the strongest signal. If the entire sector is selling off, cap long p_target at 0.70 unless the stock shows exceptional relative strength.\n"
    "  • A clear pro-thesis news catalyst (earnings beat, FDA approval, upgrade) is the strongest possible confirmer — boost p_target.\n"
    "  • A contra-thesis catalyst (miss, downgrade, lawsuit) overrides bullish technicals — trade with the catalyst, not against it.\n"
    "  • p_neither should reflect genuine uncertainty. A low-conviction setup with unclear catalysts should lean on p_neither (e.g. 0.40) rather than splitting 0.50/0.50 between target and stop.\n"
    "\n"
    "Use previous day's high/low as key intraday support/resistance levels. "
    "Size stop losses using the ATR value provided (minimum 1× ATR). "
    "Never recommend a trade with a risk/reward below 1:1.5. "
    "Respond ONLY with valid JSON — no markdown fences, no extra text outside the JSON object."
)

_JSON_SCHEMA = """{
  "trend": "bullish|bearish|sideways",
  "trend_strength": "strong|moderate|weak",
  "pattern": "<concise pattern name, e.g. 'Bull flag on VWAP reclaim'>",
  "key_levels": {
    "support": <float>,
    "resistance": <float>,
    "vwap": <float>
  },
  "recommendation": {
    "direction": "long|short|wait",
    "setup": "<1-sentence setup description>",
    "entry_zone": "<price or tight range, e.g. '152.40 - 152.80'>",
    "stop_loss": "<price>",
    "target": "<price>",
    "risk_reward": "<e.g. 1:2.5>",
    "p_target":  <probability in [0.0, 1.0] — P(target hit before stop)>,
    "p_stop":    <probability in [0.0, 1.0] — P(stop hit before target)>,
    "p_neither": <probability in [0.0, 1.0] — P(neither hit within 60-min window)>
  },
  "reasoning": "<2-3 sentences explaining the confluence of signals AND why this probability split specifically (not a generic 'confidence is high')>",
  "risks": "<1-2 sentences on the main risks to this trade>"
}"""


# ── Technical indicator functions ─────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def _vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return (typical * df["volume"]).cumsum() / df["volume"].cumsum()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, prev_close = df["high"], df["low"], df["close"].shift()
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast    = close.ewm(span=fast,   adjust=False).mean()
    ema_slow    = close.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    sma    = close.rolling(period).mean()
    stddev = close.rolling(period).std()
    return sma + num_std * stddev, sma, sma - num_std * stddev


def _resample_bars(df: pd.DataFrame, freq_minutes: int) -> pd.DataFrame:
    """Aggregate 1-min OHLCV bars up to a higher timeframe.

    Used to compute 5-min and 15-min RSI / EMA so Sonnet sees a multi-
    timeframe alignment instead of just the noisy 1-min read. Pure
    resampling (no interpolation) — the aggregates are mathematically
    identical to pulling 5-min bars from the data provider.

    Returns a fresh frame with ``time`` (unix seconds) + OHLCV columns.
    Empty input → empty output (callers must bounds-check).
    """
    if df.empty:
        return df
    tmp = df.copy()
    tmp["_dt"] = pd.to_datetime(tmp["time"], unit="s", utc=True)
    tmp = tmp.set_index("_dt")
    agg = tmp.resample(f"{freq_minutes}min").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()
    if agg.empty:
        return agg.reset_index(drop=True)
    agg["time"] = (agg.index.view("int64") // 10**9).astype(int)
    return agg.reset_index(drop=True)


def _rsi_last(close: pd.Series, period: int = 14) -> float | None:
    """RSI(period) scalar for the final bar, or None if too few samples."""
    if len(close) < period + 1:
        return None
    val = float(_rsi(close, period).iloc[-1])
    return val if pd.notna(val) else None


def _rsi_zone(rsi: float | None) -> str:
    if rsi is None:
        return "—"
    return "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"


def _vwap_stddev(df: pd.DataFrame) -> float:
    """Volume-weighted stddev of typical-price around VWAP over the session.

    Canonical VWAP-band construction: σ² = Σ(v × (tp − vwap)²) / Σv.
    Returned as a single scalar (the final cumulative stddev) — used to
    draw the ±1σ / ±2σ bands in the prompt.
    """
    if df.empty or df["volume"].sum() == 0:
        return 0.0
    tp     = (df["high"] + df["low"] + df["close"]) / 3
    v      = df["volume"]
    vwap_s = (tp * v).cumsum() / v.cumsum()
    dev2   = v * (tp - vwap_s) ** 2
    var    = dev2.sum() / v.sum()
    return float(max(var, 0.0) ** 0.5)


def _vwap_band_position(price: float, vwap: float, stddev: float) -> str:
    """Human-readable bucket for Sonnet: which band the price sits in."""
    if stddev <= 0:
        return "inside VWAP (insufficient dispersion)"
    z = (price - vwap) / stddev
    if z >= 2.0:
        return f"above +2σ (extreme — very overextended long, {z:+.1f}σ)"
    if z >= 1.0:
        return f"between +1σ and +2σ (overextended long, {z:+.1f}σ)"
    if z <= -2.0:
        return f"below −2σ (extreme — very oversold vs VWAP, {z:+.1f}σ)"
    if z <= -1.0:
        return f"between −1σ and −2σ (overextended short, {z:+.1f}σ)"
    return f"within ±1σ of VWAP (neutral, {z:+.1f}σ)"


def _count_level_touches(df: pd.DataFrame, level: float | None, tol_pct: float = 0.03) -> int:
    """Count bars whose H/L range straddled ``level`` (within ±tol_pct%).

    ``3+ touches = liquidity stacking`` in day-trader parlance. The idea
    is that each failed breach leaves resting orders above/below the
    level, making the next attempt more explosive.

    Default tolerance is 0.03% (3bps) — tight enough that on a $100 stock
    the ±tolerance is ~3¢ and the signal stays clean. A $200 stock gets
    ~6¢ tolerance, still sub-ATR on 1-min bars.
    """
    if level is None or level <= 0 or df.empty:
        return 0
    tol = level * (tol_pct / 100.0)
    touched = ((df["high"] >= level - tol) & (df["low"] <= level + tol)).sum()
    return int(touched)


def _session_elapsed_fraction(df: pd.DataFrame) -> float:
    """Fraction of the 6.5-hour RTH session that has elapsed by the last bar.

    Pulled out of ``_rvol_vs_20d`` so we can still surface elapsed_pct in
    the prompt even when no daily-volume baseline is available for RVOL.
    """
    if df.empty:
        return 0.0
    try:
        last_ts = pd.to_datetime(int(df["time"].iloc[-1]), unit="s", utc=True).tz_convert(_ET)
    except Exception:
        return 0.0
    session_open = last_ts.replace(hour=9, minute=30, second=0, microsecond=0)
    if last_ts < session_open:
        return 0.0
    elapsed_min = min(390.0, (last_ts - session_open).total_seconds() / 60.0)
    return max(0.0, elapsed_min / 390.0)


def _rvol_vs_20d(df: pd.DataFrame, avg_daily_volume: float | None,
                  session_frac: float) -> float | None:
    """Session-cumulative RVOL vs a 20-day average, scaled by time-of-day.

    The ``session_frac`` argument is what fraction of the 6.5-hour RTH
    window has elapsed — computed separately by ``_session_elapsed_fraction``
    so it's always available to the prompt even when the RVOL baseline isn't.

    Intraday volume is not flat (U-shaped with open/close spikes), so this
    flat-elapsed approximation slightly overstates RVOL mid-session and
    understates it near the open/close. Good enough for a "2× vs 0.5×"
    conviction signal; not good enough for a quant volume model.
    """
    if avg_daily_volume is None or avg_daily_volume <= 0 or df.empty:
        return None
    if session_frac <= 0:
        return None
    expected = avg_daily_volume * session_frac
    if expected <= 0:
        return None
    session_volume = float(df["volume"].sum())
    return session_volume / expected


def _opening_range(df: pd.DataFrame) -> dict | None:
    """Return the 9:30–9:45 ET opening-range high/low if those bars exist."""
    try:
        times_et = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(_ET)
        mask     = times_et.apply(lambda t: t.hour == 9 and 30 <= t.minute < 45)
        or_bars  = df[mask.values]
        if len(or_bars) < 3:
            return None
        return {"high": float(or_bars["high"].max()), "low": float(or_bars["low"].min())}
    except Exception:
        return None


def _rth_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean mask for bars inside 09:30–16:00 ET (regular trading hours).

    The bar feed we pull (8-hour lookback in ``autotrade.py``) includes
    pre-market bars from ~04:00 ET. Those bars carry real liquidity-
    magnet signal (PMH / PML are classic overnight stop-run levels)
    but they POLLUTE any indicator computed as a cumulative session
    statistic — VWAP, session high/low, opening range, RVOL.

    The fix is to split the frame: indicators that represent "the RTH
    session so far" filter with this mask; ICT liquidity-level
    computation (in ``app/ict/__init__.py``) separately uses the
    pre-market bars to publish ``pmh`` / ``pml`` as independent
    reference points.

    Pre-2026-04-24 analyses had this backwards — a scan running at
    10:00 ET would report ``session_high`` as the 07:40 pre-market
    wick rather than the RTH's actual session high, which misled
    every Sonnet probability read and every liquidity-stack touch
    count. Always-use-full-frame was simpler; always-use-RTH is
    correct.

    Returns a pandas bool Series aligned with ``df.index``. Rows are
    ``True`` when the bar's ET time is within [09:30, 16:00).
    """
    if df.empty:
        return pd.Series([], dtype=bool)
    try:
        times_et = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(_ET)
        minutes  = times_et.dt.hour * 60 + times_et.dt.minute
        # 09:30 = 570, 16:00 = 960
        return (minutes >= 570) & (minutes < 960)
    except Exception:
        # Degraded path — treat everything as RTH rather than dropping
        # all bars and silently NaN'ing out the indicators.
        return pd.Series([True] * len(df), index=df.index)


# ── News / Catalyst context ──────────────────────────────────────────────────

# Keywords that indicate a potentially market-moving catalyst
_CATALYST_KEYWORDS = {
    "earnings", "revenue", "profit", "loss", "guidance", "forecast",
    "beat", "miss", "upgrade", "downgrade", "target", "rating",
    "fda", "approval", "trial", "patent", "lawsuit", "settlement",
    "merger", "acquisition", "buyout", "takeover", "deal",
    "dividend", "buyback", "split", "offering", "ipo",
    "sec", "investigation", "fraud", "recall", "warning",
    "layoff", "restructuring", "ceo", "cfo", "resign", "appoint",
    "contract", "partnership", "launch", "breakthrough",
}

_BULLISH_WORDS = {"beat", "upgrade", "approval", "breakthrough", "launch",
                  "record", "surge", "soar", "rally", "strong", "growth",
                  "partnership", "contract", "dividend", "buyback", "split"}
_BEARISH_WORDS = {"miss", "downgrade", "recall", "fraud", "warning",
                  "layoff", "resign", "investigation", "loss", "weak",
                  "decline", "plunge", "crash", "cut", "lawsuit"}


def compute_news_context(articles: list[dict], symbol: str) -> dict | None:
    """Analyse recent news articles for trading-relevant catalysts.

    Args:
        articles: List of Alpaca news article dicts.
        symbol:   The stock ticker being analysed.

    Returns a dict with headline summary, catalyst detection, and sentiment.
    """
    if not articles:
        return None

    symbol_upper = symbol.upper()

    # Filter to articles mentioning this specific symbol
    relevant = [
        a for a in articles
        if symbol_upper in [s.upper() for s in (a.get("symbols") or [])]
    ]
    # If no symbol-specific articles, use all provided (caller already filtered)
    if not relevant:
        relevant = articles

    if not relevant:
        return None

    # Analyse each article
    processed = []
    catalysts_found = []

    for art in relevant[:10]:  # Cap at 10 most recent
        headline = art.get("headline", "")
        summary  = art.get("summary", "")
        source   = art.get("source", "unknown")
        created  = art.get("created_at", "")
        text     = f"{headline} {summary}".lower()

        # Detect catalysts
        found = [kw for kw in _CATALYST_KEYWORDS if kw in text]

        # Simple sentiment from headline keywords
        bull_count = sum(1 for w in _BULLISH_WORDS if w in text)
        bear_count = sum(1 for w in _BEARISH_WORDS if w in text)

        if bull_count > bear_count:
            sentiment = "bullish"
        elif bear_count > bull_count:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        entry = {
            "headline":  headline,
            "source":    source,
            "time":      created,
            "sentiment": sentiment,
            "catalysts": found,
        }
        processed.append(entry)
        catalysts_found.extend(found)

    # Overall news sentiment
    sentiments = [p["sentiment"] for p in processed]
    bull = sentiments.count("bullish")
    bear = sentiments.count("bearish")
    if bull > bear:
        overall = "bullish"
    elif bear > bull:
        overall = "bearish"
    else:
        overall = "neutral / mixed"

    has_catalyst = len(catalysts_found) > 0
    unique_catalysts = sorted(set(catalysts_found))

    return {
        "article_count":    len(processed),
        "overall_sentiment": overall,
        "has_catalyst":     has_catalyst,
        "catalysts":        unique_catalysts,
        "articles":         processed,
    }


# ── Sector momentum context ──────────────────────────────────────────────────

# Map of popular stocks → sector ETF.  Covers the training universe and common
# day-trading names.  Unknown symbols fall back to SPY (broad market).
_SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK",
    "GOOGL": "XLK", "GOOG": "XLK", "INTC": "XLK", "AVGO": "XLK",
    "CRM": "XLK", "ADBE": "XLK", "ORCL": "XLK", "CSCO": "XLK",
    "QCOM": "XLK", "MU": "XLK", "AMAT": "XLK", "LRCX": "XLK",
    "KLAC": "XLK", "MRVL": "XLK", "SNPS": "XLK", "CDNS": "XLK",
    # Communication Services (META / NFLX live in XLC)
    "META": "XLC", "NFLX": "XLC", "DIS": "XLC", "CMCSA": "XLC",
    "T": "XLC", "VZ": "XLC", "TMUS": "XLC",
    # Consumer Discretionary
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "NKE": "XLY",
    "MCD": "XLY", "SBUX": "XLY", "LOW": "XLY", "TJX": "XLY",
    "BKNG": "XLY", "ABNB": "XLY", "RIVN": "XLY", "LCID": "XLY",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
    "WFC": "XLF", "C": "XLF", "BLK": "XLF", "SCHW": "XLF",
    "V": "XLF", "MA": "XLF", "AXP": "XLF",
    # Healthcare
    "UNH": "XLV", "JNJ": "XLV", "LLY": "XLV", "PFE": "XLV",
    "ABBV": "XLV", "MRK": "XLV", "TMO": "XLV", "ABT": "XLV",
    "BMY": "XLV", "MRNA": "XLV",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE",
    "OXY": "XLE", "MPC": "XLE", "VLO": "XLE", "PSX": "XLE",
    # Industrials
    "CAT": "XLI", "BA": "XLI", "HON": "XLI", "UPS": "XLI",
    "GE": "XLI", "RTX": "XLI", "DE": "XLI", "LMT": "XLI",
    # Consumer Staples
    "PG": "XLP", "KO": "XLP", "PEP": "XLP", "COST": "XLP",
    "WMT": "XLP", "PM": "XLP", "MO": "XLP",
    # Real Estate
    "AMT": "XLRE", "PLD": "XLRE", "CCI": "XLRE", "O": "XLRE",
    # Utilities
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "D": "XLU",
    # Materials
    "LIN": "XLB", "APD": "XLB", "FCX": "XLB", "NEM": "XLB",
    # Broad market / index ETFs — map to themselves
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "DIA": "DIA",
}

_ETF_NAMES: dict[str, str] = {
    "XLK": "Technology", "XLC": "Communication Services",
    "XLY": "Consumer Discretionary", "XLF": "Financials",
    "XLV": "Healthcare", "XLE": "Energy", "XLI": "Industrials",
    "XLP": "Consumer Staples", "XLRE": "Real Estate",
    "XLU": "Utilities", "XLB": "Materials",
    "SPY": "S&P 500", "QQQ": "Nasdaq 100",
    "IWM": "Russell 2000", "DIA": "Dow Jones",
}


def get_sector_etf(symbol: str) -> str:
    """Return the sector ETF ticker for a given stock. Falls back to SPY."""
    return _SECTOR_MAP.get(symbol.upper(), "SPY")


def get_required_sector_etfs(symbols: list[str]) -> list[str]:
    """Return the unique set of sector ETFs needed for a list of symbols."""
    etfs = {get_sector_etf(s) for s in symbols}
    # Always include SPY for broad-market context
    etfs.add("SPY")
    return list(etfs)


def compute_sector_context(
    symbol: str,
    symbol_change_pct: float,
    sector_snapshots: dict[str, dict],
) -> dict | None:
    """Compare a stock's intraday move to its sector and the broad market.

    Args:
        symbol:            The stock ticker.
        symbol_change_pct: The stock's intraday % change (already computed).
        sector_snapshots:  {etf_ticker: alpaca_snapshot_dict} for sector ETFs.
    """
    sector_etf = get_sector_etf(symbol)
    sector_name = _ETF_NAMES.get(sector_etf, sector_etf)

    # If the symbol IS an ETF, skip (no point comparing SPY to SPY)
    if symbol.upper() in _ETF_NAMES:
        return None

    # Extract sector ETF change from its snapshot
    sector_change = _snapshot_change_pct(sector_snapshots.get(sector_etf))
    spy_change    = _snapshot_change_pct(sector_snapshots.get("SPY"))

    if sector_change is None:
        return None

    stock_vs_sector = symbol_change_pct - sector_change
    stock_vs_spy    = symbol_change_pct - (spy_change or 0)

    # Relative strength classification
    if stock_vs_sector > 1.5:
        relative = "significantly outperforming sector"
    elif stock_vs_sector > 0.5:
        relative = "outperforming sector"
    elif stock_vs_sector > -0.5:
        relative = "in line with sector"
    elif stock_vs_sector > -1.5:
        relative = "underperforming sector"
    else:
        relative = "significantly underperforming sector"

    # Sector trend
    if sector_change > 0.5:
        sector_trend = "bullish"
    elif sector_change > 0.1:
        sector_trend = "mildly bullish"
    elif sector_change > -0.1:
        sector_trend = "flat"
    elif sector_change > -0.5:
        sector_trend = "mildly bearish"
    else:
        sector_trend = "bearish"

    # Broad market trend
    spy_trend = None
    if spy_change is not None:
        if spy_change > 0.5:
            spy_trend = "bullish"
        elif spy_change > 0.1:
            spy_trend = "mildly bullish"
        elif spy_change > -0.1:
            spy_trend = "flat"
        elif spy_change > -0.5:
            spy_trend = "mildly bearish"
        else:
            spy_trend = "bearish"

    # Is the stock leading or lagging?
    if symbol_change_pct > 0 and sector_change > 0 and stock_vs_sector > 0.3:
        leadership = "sector leader — stock is pulling the sector up"
    elif symbol_change_pct < 0 and sector_change < 0 and stock_vs_sector < -0.3:
        leadership = "sector laggard — stock is falling harder than peers"
    elif symbol_change_pct > 0 and sector_change < -0.1:
        leadership = "bucking the sector — stock rising while sector falls (watch for reversal)"
    elif symbol_change_pct < 0 and sector_change > 0.1:
        leadership = "diverging from sector — stock falling while sector rises (relative weakness)"
    else:
        leadership = "moving with sector — no notable divergence"

    return {
        "sector_etf":       sector_etf,
        "sector_name":      sector_name,
        "sector_change_pct": round(sector_change, 2),
        "sector_trend":     sector_trend,
        "spy_change_pct":   round(spy_change, 2) if spy_change is not None else None,
        "spy_trend":        spy_trend,
        "stock_vs_sector":  round(stock_vs_sector, 2),
        "stock_vs_spy":     round(stock_vs_spy, 2) if spy_change is not None else None,
        "relative_strength": relative,
        "leadership":       leadership,
    }


def _snapshot_change_pct(snapshot: dict | None) -> float | None:
    """Extract intraday % change from an Alpaca snapshot."""
    if not snapshot:
        return None

    daily_bar = snapshot.get("dailyBar") or snapshot.get("daily_bar")
    prev_daily = snapshot.get("prevDailyBar") or snapshot.get("prev_daily_bar")

    if daily_bar and prev_daily:
        current = float(daily_bar.get("c", 0))
        prev_close = float(prev_daily.get("c", 0))
        if prev_close > 0:
            return (current - prev_close) / prev_close * 100

    # Fallback: use minuteBar open vs close
    minute_bar = snapshot.get("minuteBar") or snapshot.get("minute_bar")
    if daily_bar:
        o = float(daily_bar.get("o", 0))
        c = float(daily_bar.get("c", 0))
        if o > 0:
            return (c - o) / o * 100

    return None


# ── Bid-Ask / Quote context ──────────────────────────────────────────────────

def compute_quote_context(snapshot: dict | None, current_price: float = 0) -> dict | None:
    """Analyse the latest NBBO quote (bid/ask) from an Alpaca snapshot.

    Provides:
      - Spread (absolute and as % of price)
      - Bid/ask sizes and imbalance ratio
      - Buyer/seller pressure interpretation
    """
    if not snapshot:
        return None

    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote")
    if not quote:
        return None

    bid       = float(quote.get("bp") or quote.get("bid_price") or 0)
    ask       = float(quote.get("ap") or quote.get("ask_price") or 0)
    bid_size  = int(quote.get("bs") or quote.get("bid_size") or 0)
    ask_size  = int(quote.get("as") or quote.get("ask_size") or 0)

    if bid <= 0 or ask <= 0:
        return None

    spread     = ask - bid
    mid        = (bid + ask) / 2
    spread_pct = spread / mid * 100 if mid else 0
    price_ref  = current_price or mid

    # Spread classification
    if spread_pct < 0.02:
        spread_label = "extremely tight (high liquidity, institutional interest)"
    elif spread_pct < 0.05:
        spread_label = "tight (good liquidity)"
    elif spread_pct < 0.15:
        spread_label = "moderate (normal for mid-cap)"
    elif spread_pct < 0.50:
        spread_label = "wide (low liquidity — use caution with market orders)"
    else:
        spread_label = "very wide (illiquid — avoid market orders, use limits)"

    # Bid/ask size imbalance — shows real buyer/seller pressure
    total_size = bid_size + ask_size
    if total_size > 0:
        bid_pct = bid_size / total_size * 100
        ask_pct = ask_size / total_size * 100
    else:
        bid_pct = ask_pct = 50.0

    imbalance_ratio = bid_size / ask_size if ask_size > 0 else 99.0

    if imbalance_ratio > 2.0:
        pressure = "strong buying pressure (bid size >> ask size)"
    elif imbalance_ratio > 1.3:
        pressure = "moderate buying pressure (more buyers than sellers)"
    elif imbalance_ratio < 0.5:
        pressure = "strong selling pressure (ask size >> bid size)"
    elif imbalance_ratio < 0.77:
        pressure = "moderate selling pressure (more sellers than buyers)"
    else:
        pressure = "balanced (no clear directional pressure)"

    # Where is price relative to the spread?
    if price_ref >= ask:
        price_position = "at or above ask (buyers aggressive — bullish)"
    elif price_ref <= bid:
        price_position = "at or below bid (sellers aggressive — bearish)"
    elif price_ref > mid:
        price_position = "above midpoint (leaning toward buyers)"
    else:
        price_position = "below midpoint (leaning toward sellers)"

    return {
        "bid":              round(bid, 2),
        "ask":              round(ask, 2),
        "spread":           round(spread, 4),
        "spread_pct":       round(spread_pct, 4),
        "spread_label":     spread_label,
        "mid":              round(mid, 2),
        "bid_size":         bid_size,
        "ask_size":         ask_size,
        "bid_pct":          round(bid_pct, 1),
        "ask_pct":          round(ask_pct, 1),
        "imbalance_ratio":  round(imbalance_ratio, 2),
        "pressure":         pressure,
        "price_position":   price_position,
    }


# ── Pre-market / After-hours context ─────────────────────────────────────────

def compute_extended_hours(bars: list[dict], prev_close: float | None = None) -> dict | None:
    """Analyse pre-market and after-hours price action.

    Uses the intraday bars to identify activity outside regular hours
    (pre-market: 04:00–09:30 ET, after-hours: 16:00–20:00 ET).
    Also computes the overnight gap from the previous day's close.
    """
    if not bars:
        return None

    df = pd.DataFrame(bars).sort_values("time").reset_index(drop=True)

    try:
        times_et = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(_ET)
    except Exception:
        return None

    # Pre-market: 04:00 – 09:29 ET
    pm_mask = times_et.apply(lambda t: (t.hour >= 4 and t.hour < 9) or (t.hour == 9 and t.minute < 30))
    pm_bars = df[pm_mask.values]

    # After-hours (previous session): 16:00 – 19:59 ET
    ah_mask = times_et.apply(lambda t: t.hour >= 16 and t.hour < 20)
    ah_bars = df[ah_mask.values]

    # Regular hours: 09:30 – 15:59 ET
    rh_mask = times_et.apply(
        lambda t: (t.hour > 9 or (t.hour == 9 and t.minute >= 30)) and t.hour < 16
    )
    rh_bars = df[rh_mask.values]
    regular_open = float(rh_bars.iloc[0]["open"]) if len(rh_bars) > 0 else None

    result: dict = {}

    # Pre-market analysis
    if len(pm_bars) >= 2:
        pm_open  = float(pm_bars.iloc[0]["open"])
        pm_high  = float(pm_bars["high"].max())
        pm_low   = float(pm_bars["low"].min())
        pm_close = float(pm_bars.iloc[-1]["close"])
        pm_vol   = int(pm_bars["volume"].sum())
        pm_change = (pm_close - pm_open) / pm_open * 100 if pm_open else 0.0

        result["premarket"] = {
            "available": True,
            "bars":     len(pm_bars),
            "open":     round(pm_open, 2),
            "high":     round(pm_high, 2),
            "low":      round(pm_low, 2),
            "close":    round(pm_close, 2),
            "volume":   pm_vol,
            "change_pct": round(pm_change, 2),
            "direction": "bullish" if pm_change > 0.1 else "bearish" if pm_change < -0.1 else "flat",
        }
    else:
        result["premarket"] = {"available": False}

    # After-hours analysis (from previous session, if present)
    if len(ah_bars) >= 2:
        ah_open  = float(ah_bars.iloc[0]["open"])
        ah_high  = float(ah_bars["high"].max())
        ah_low   = float(ah_bars["low"].min())
        ah_close = float(ah_bars.iloc[-1]["close"])
        ah_vol   = int(ah_bars["volume"].sum())
        ah_change = (ah_close - ah_open) / ah_open * 100 if ah_open else 0.0

        result["afterhours"] = {
            "available": True,
            "bars":     len(ah_bars),
            "open":     round(ah_open, 2),
            "high":     round(ah_high, 2),
            "low":      round(ah_low, 2),
            "close":    round(ah_close, 2),
            "volume":   ah_vol,
            "change_pct": round(ah_change, 2),
            "direction": "bullish" if ah_change > 0.1 else "bearish" if ah_change < -0.1 else "flat",
        }
    else:
        result["afterhours"] = {"available": False}

    # Overnight gap: previous close → today's regular session open
    if prev_close and regular_open:
        gap = regular_open - prev_close
        gap_pct = gap / prev_close * 100
        if gap_pct > 0.3:
            gap_type = "gap UP"
        elif gap_pct < -0.3:
            gap_type = "gap DOWN"
        else:
            gap_type = "flat open (no significant gap)"
        result["gap"] = {
            "prev_close":   round(prev_close, 2),
            "regular_open": round(regular_open, 2),
            "gap_amount":   round(gap, 2),
            "gap_pct":      round(gap_pct, 2),
            "type":         gap_type,
        }
    else:
        result["gap"] = None

    # Sentiment summary
    pm_dir = result["premarket"].get("direction", "unknown")
    ah_dir = result["afterhours"].get("direction", "unknown")
    gap_type_str = result["gap"]["type"] if result["gap"] else "unknown"

    if pm_dir == "bullish" and "UP" in gap_type_str:
        result["sentiment"] = "strongly bullish (gap up + bullish pre-market)"
    elif pm_dir == "bearish" and "DOWN" in gap_type_str:
        result["sentiment"] = "strongly bearish (gap down + bearish pre-market)"
    elif pm_dir == "bullish":
        result["sentiment"] = "mildly bullish (bullish pre-market)"
    elif pm_dir == "bearish":
        result["sentiment"] = "mildly bearish (bearish pre-market)"
    elif "UP" in gap_type_str:
        result["sentiment"] = "mildly bullish (gap up)"
    elif "DOWN" in gap_type_str:
        result["sentiment"] = "mildly bearish (gap down)"
    else:
        result["sentiment"] = "neutral / no clear overnight bias"

    return result


# ── Daily (higher-timeframe) context ─────────────────────────────────────────

def compute_daily_context(daily_bars: list[dict]) -> dict | None:
    """Compute daily-chart indicators for higher-timeframe confluence.

    Expects a list of daily bar dicts with keys: time, open, high, low, close, volume.
    Returns None if there aren't enough bars (need at least 50 for EMA50).
    """
    if len(daily_bars) < 50:
        return None

    df    = pd.DataFrame(daily_bars).sort_values("time").reset_index(drop=True)
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    ema9  = float(_ema(close, 9).iloc[-1])
    ema20 = float(_ema(close, 20).iloc[-1])
    ema50 = float(_ema(close, 50).iloc[-1])
    rsi   = float(_rsi(close, 14).iloc[-1])
    atr   = float(_atr(df, 14).iloc[-1])
    macd_l, sig_l, hist_l = _macd(close)
    macd  = float(macd_l.iloc[-1])
    sig   = float(sig_l.iloc[-1])
    hist  = float(hist_l.iloc[-1])

    current = float(close.iloc[-1])

    # Daily support / resistance from the last 20 days
    tail_20   = df.tail(20)
    daily_high = float(tail_20["high"].max())
    daily_low  = float(tail_20["low"].min())

    # Previous day's levels (important for intraday traders)
    prev = df.iloc[-2]
    prev_high  = float(prev["high"])
    prev_low   = float(prev["low"])
    prev_close = float(prev["close"])

    # Daily trend determination
    if current > ema9 > ema20 > ema50:
        trend = "strong uptrend"
    elif current > ema20 > ema50:
        trend = "uptrend"
    elif current < ema9 < ema20 < ema50:
        trend = "strong downtrend"
    elif current < ema20 < ema50:
        trend = "downtrend"
    elif current > ema50:
        trend = "above 50-EMA but choppy"
    else:
        trend = "below 50-EMA — bearish bias"

    # EMA alignment
    if ema9 > ema20 > ema50:
        ema_alignment = "fully bullish (9 > 20 > 50)"
    elif ema9 < ema20 < ema50:
        ema_alignment = "fully bearish (9 < 20 < 50)"
    else:
        ema_alignment = "mixed / transitioning"

    return {
        "trend":          trend,
        "ema9":           round(ema9, 2),
        "ema20":          round(ema20, 2),
        "ema50":          round(ema50, 2),
        "ema_alignment":  ema_alignment,
        "rsi":            round(rsi, 1),
        "rsi_zone":       "overbought (>70)" if rsi > 70 else "oversold (<30)" if rsi < 30 else "neutral",
        "atr":            round(atr, 2),
        "macd":           round(macd, 4),
        "macd_signal":    round(sig, 4),
        "macd_histogram": round(hist, 4),
        "macd_side":      "bullish" if macd > sig else "bearish",
        "current":        round(current, 2),
        "20d_high":       round(daily_high, 2),
        "20d_low":        round(daily_low, 2),
        "prev_high":      round(prev_high, 2),
        "prev_low":       round(prev_low, 2),
        "prev_close":     round(prev_close, 2),
        "pct_from_20d_high": round((daily_high - current) / current * 100, 2),
        "pct_from_20d_low":  round((current - daily_low) / current * 100, 2),
    }


# ── Main compute function ─────────────────────────────────────────────────────

def compute_indicators(
    bars: list[dict],
    *,
    prev_close: float | None = None,
    prev_high: float | None = None,
    prev_low: float | None = None,
    avg_daily_volume: float | None = None,
) -> dict:
    """Compute intraday indicators from a list of bars.

    Optional daily-context kwargs let strategies reason about gap behaviour
    and prior-day levels without needing a separate daily-bars call at
    match time. When provided they appear in the returned dict as
    ``prev_close`` / ``prev_high`` / ``prev_low`` / ``gap_pct``. When
    absent those keys are ``None`` and predicates that need them should
    bail out via ``ind.get(...)``.

    ``avg_daily_volume`` (the mean of the last ~20 daily bars' total volume)
    unlocks the RVOL reading. When absent, ``rvol_vs_20d`` is None and the
    prompt prints "— (no daily history)".

    Added 2026-04-24 to boost Sonnet's confidence signal:
      * Multi-timeframe RSI (1/5/15-min) — three confirming reads from one feature.
      * Multi-timeframe EMA (1/5/15-min) — catches trend alignment across TFs.
      * VWAP ±1σ/±2σ bands — statistical mean-reversion triggers.
      * Key-level touch counts (PDH/PDL/session H/L) — liquidity stacking.
      * RVOL vs 20-day average — the classic day-trader conviction metric.
    """
    df    = pd.DataFrame(bars).sort_values("time").reset_index(drop=True)
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # ── RTH (regular trading hours) slice ─────────────────────────────────────
    # The bar feed includes pre-market bars (starting ~04:00 ET on an 8-hour
    # lookback). Those bars carry real liquidity-magnet signal but they
    # pollute any indicator that represents "this session" — VWAP, session
    # open/high/low, RVOL. Split the frame here so session statistics are
    # RTH-only while running-window indicators (EMA/RSI/ATR/MACD/BB) still
    # see the full history they need for a stable signal. When the scanner
    # fires before 09:30 ET (pre-open scans), there are no RTH bars yet —
    # fall back to the full frame so indicators don't null-out.
    rth_mask = _rth_mask(df)
    rth_df = df[rth_mask].reset_index(drop=True) if bool(rth_mask.any()) else df

    # Series
    ema9_s,  ema20_s  = _ema(close, 9),  _ema(close, 20)
    rsi_s             = _rsi(close, 14)
    vwap_s            = _vwap(rth_df)   # RTH-anchored VWAP
    atr_s             = _atr(df, 14)
    macd_s, sig_s, hist_s = _macd(close)
    bb_up_s, bb_mid_s, bb_lo_s = _bollinger(close, 20)

    # Scalars
    current = float(close.iloc[-1])
    # Session stats are RTH-only. On a pre-open scan rth_df == df, so the
    # values match the pre-2026-04-24 behavior for that edge case.
    s_open  = float(rth_df["open"].iloc[0])
    s_high  = float(rth_df["high"].max())
    s_low   = float(rth_df["low"].min())

    ema9    = float(ema9_s.iloc[-1])
    ema20   = float(ema20_s.iloc[-1])
    rsi     = float(rsi_s.iloc[-1])
    vwap    = float(vwap_s.iloc[-1])
    atr     = float(atr_s.iloc[-1])
    macd    = float(macd_s.iloc[-1])
    sig     = float(sig_s.iloc[-1])
    hist    = float(hist_s.iloc[-1])
    bb_up   = float(bb_up_s.iloc[-1])
    bb_mid  = float(bb_mid_s.iloc[-1])
    bb_lo   = float(bb_lo_s.iloc[-1])

    # Bollinger %B
    bb_range = bb_up - bb_lo
    bb_pct_b = (current - bb_lo) / bb_range if bb_range > 0 else 0.5

    # ── Multi-timeframe resamples ────────────────────────────────────────────
    # 5-min RSI(14)  needs ≥ 14 × 5  =  70 1-min bars before it's stable.
    # 15-min RSI(14) needs ≥ 14 × 15 = 210 1-min bars. Extended-hours sessions
    # exceed this easily; early-morning RTH may not — we degrade to None.
    df_5m  = _resample_bars(df, 5)
    df_15m = _resample_bars(df, 15)

    rsi_5m  = _rsi_last(df_5m["close"],  14) if len(df_5m)  >= 15 else None
    rsi_15m = _rsi_last(df_15m["close"], 14) if len(df_15m) >= 15 else None

    # 5-min / 15-min EMA9/20 — short-TF-noise-cutter
    def _ema_tf(d: pd.DataFrame, period: int) -> float | None:
        if len(d) < period:
            return None
        return float(_ema(d["close"], period).iloc[-1])

    ema9_5m,   ema20_5m   = _ema_tf(df_5m,  9),  _ema_tf(df_5m,  20)
    ema9_15m,  ema20_15m  = _ema_tf(df_15m, 9),  _ema_tf(df_15m, 20)

    # RSI alignment — how many of {1m, 5m, 15m} agree on direction?
    rsi_vals = [v for v in (rsi, rsi_5m, rsi_15m) if v is not None]
    if len(rsi_vals) >= 2:
        bullish = sum(1 for v in rsi_vals if v > 50)
        bearish = sum(1 for v in rsi_vals if v < 50)
        if bullish == len(rsi_vals):
            rsi_alignment = f"all {len(rsi_vals)} timeframes bullish (RSI > 50)"
        elif bearish == len(rsi_vals):
            rsi_alignment = f"all {len(rsi_vals)} timeframes bearish (RSI < 50)"
        else:
            rsi_alignment = f"mixed ({bullish} bullish / {bearish} bearish across 1m/5m/15m)"
    else:
        rsi_alignment = "insufficient higher-TF history"

    # EMA alignment across timeframes — asks the stronger question:
    # is price above BOTH EMAs on ALL available timeframes?
    def _tf_stance(price: float, e9: float | None, e20: float | None) -> str | None:
        if e9 is None or e20 is None:
            return None
        if price > e9 > e20:
            return "bullish"
        if price < e9 < e20:
            return "bearish"
        return "mixed"

    stances = [
        _tf_stance(current, ema9,     ema20),
        _tf_stance(current, ema9_5m,  ema20_5m),
        _tf_stance(current, ema9_15m, ema20_15m),
    ]
    valid_stances = [s for s in stances if s is not None]
    if valid_stances and all(s == "bullish" for s in valid_stances):
        ema_alignment_mtf = f"fully bullish across all {len(valid_stances)} timeframes (price > EMA9 > EMA20)"
    elif valid_stances and all(s == "bearish" for s in valid_stances):
        ema_alignment_mtf = f"fully bearish across all {len(valid_stances)} timeframes (price < EMA9 < EMA20)"
    elif valid_stances:
        tf_names = ["1m", "5m", "15m"]
        parts = [f"{tf_names[i]} {stances[i]}" for i in range(len(stances)) if stances[i] is not None]
        ema_alignment_mtf = "mixed — " + ", ".join(parts)
    else:
        ema_alignment_mtf = "insufficient higher-TF history"

    # VWAP standard-deviation bands — computed on the RTH slice so the bands
    # describe today's regular-hours dispersion, not a stretched range that
    # includes a 05:30 ET pre-market wick.
    vwap_std = _vwap_stddev(rth_df)
    vwap_band_pos = _vwap_band_position(current, vwap, vwap_std)

    # Key-level touch counts (liquidity-stacking signal). Counted on RTH
    # bars so "3+ touches" genuinely means "tested during the session" —
    # pre-market wicks of PDH/PDL are already reflected in the separate
    # PMH/PML liquidity levels surfaced by the ICT layer.
    touches_prev_high   = _count_level_touches(rth_df, prev_high)  if prev_high  is not None else 0
    touches_prev_low    = _count_level_touches(rth_df, prev_low)   if prev_low   is not None else 0
    touches_sess_high   = _count_level_touches(rth_df, s_high)
    touches_sess_low    = _count_level_touches(rth_df, s_low)

    # RVOL vs 20-day average (None when no daily baseline supplied).
    # session_frac is computed separately so elapsed_pct shows regardless.
    # RVOL denominator is the 20-day avg DAILY volume, so the numerator
    # needs to match: RTH session cumulative only, not RTH+pre-market.
    session_frac = _session_elapsed_fraction(df)
    rvol         = _rvol_vs_20d(rth_df, avg_daily_volume, session_frac)

    # Price structure (last 20 bars)
    tail = df.tail(min(20, len(df)))
    mid  = len(tail) // 2
    if tail["high"].iloc[mid:].max() > tail["high"].iloc[:mid].max() and \
       tail["low"].iloc[mid:].min()  > tail["low"].iloc[:mid].min():
        structure = "Higher highs and higher lows (uptrend)"
    elif tail["high"].iloc[mid:].max() < tail["high"].iloc[:mid].max() and \
         tail["low"].iloc[mid:].min()  < tail["low"].iloc[:mid].min():
        structure = "Lower highs and lower lows (downtrend)"
    else:
        structure = "Mixed — consolidation / no clear direction"

    # Volume. Recent vs session average — both on RTH bars so the ratio
    # isn't artificially inflated by pre-market bars dragging down the
    # denominator. On a pre-open scan rth_df == df, unchanged behavior.
    avg_all    = float(rth_df["volume"].mean())
    avg_recent = float(rth_df["volume"].tail(5).mean())
    vol_ratio  = avg_recent / avg_all if avg_all > 0 else 1.0
    vol_label  = "elevated" if vol_ratio > 1.5 else "declining" if vol_ratio < 0.7 else "normal"

    # ATR as % of price
    atr_pct = atr / current * 100 if current else 0.0

    # Daily-context derivatives (gap_pct measures the overnight move at the
    # session open — the basis for Gap-and-Go continuation). Kept in a
    # dedicated block so the return dict below stays a flat projection.
    gap_pct: float | None = None
    if prev_close is not None and prev_close > 0 and s_open:
        gap_pct = round((s_open - prev_close) / prev_close * 100, 2)

    return {
        # Price
        "current_price":  round(current, 2),
        "session_open":   round(s_open,  2),
        "session_high":   round(s_high,  2),
        "session_low":    round(s_low,   2),
        "change_pct":     round((current - s_open) / s_open * 100, 2) if s_open else 0.0,
        "pct_from_high":  round((s_high - current) / current * 100, 2) if current else 0.0,
        "pct_from_low":   round((current - s_low)  / current * 100, 2) if current else 0.0,
        # Daily context (None when caller didn't supply prev-day bars)
        "prev_close":     round(prev_close, 2) if prev_close is not None else None,
        "prev_high":      round(prev_high,  2) if prev_high  is not None else None,
        "prev_low":       round(prev_low,   2) if prev_low   is not None else None,
        "gap_pct":        gap_pct,
        # EMAs
        "ema9":           round(ema9,  2),
        "ema20":          round(ema20, 2),
        "price_vs_ema9":  "above" if current > ema9  else "below",
        "price_vs_ema20": "above" if current > ema20 else "below",
        "ema_crossover":  "EMA9 above EMA20 (bullish bias)" if ema9 > ema20
                          else "EMA9 below EMA20 (bearish bias)",
        # RSI
        "rsi":      round(rsi, 1),
        "rsi_zone": "overbought (>70)" if rsi > 70 else "oversold (<30)" if rsi < 30 else "neutral",
        # Multi-timeframe RSI (None when not enough higher-TF history yet)
        "rsi_5m":   round(rsi_5m,  1) if rsi_5m  is not None else None,
        "rsi_15m":  round(rsi_15m, 1) if rsi_15m is not None else None,
        "rsi_5m_zone":  _rsi_zone(rsi_5m),
        "rsi_15m_zone": _rsi_zone(rsi_15m),
        "rsi_alignment": rsi_alignment,
        # Multi-timeframe EMA (None when not enough higher-TF history yet)
        "ema9_5m":   round(ema9_5m,   2) if ema9_5m   is not None else None,
        "ema20_5m":  round(ema20_5m,  2) if ema20_5m  is not None else None,
        "ema9_15m":  round(ema9_15m,  2) if ema9_15m  is not None else None,
        "ema20_15m": round(ema20_15m, 2) if ema20_15m is not None else None,
        "ema_alignment_mtf": ema_alignment_mtf,
        # VWAP
        "vwap":          round(vwap, 2),
        "price_vs_vwap": "above" if current > vwap else "below",
        "pct_from_vwap": round((current - vwap) / vwap * 100, 2) if vwap else 0.0,
        # VWAP statistical bands (volume-weighted stddev)
        "vwap_stddev":    round(vwap_std, 3),
        "vwap_plus_1s":   round(vwap + vwap_std,     2),
        "vwap_plus_2s":   round(vwap + 2 * vwap_std, 2),
        "vwap_minus_1s":  round(vwap - vwap_std,     2),
        "vwap_minus_2s":  round(vwap - 2 * vwap_std, 2),
        "vwap_band_position": vwap_band_pos,
        # Key-level touch counts (3+ = liquidity stacking)
        "touches_prev_high":    touches_prev_high,
        "touches_prev_low":     touches_prev_low,
        "touches_session_high": touches_sess_high,
        "touches_session_low":  touches_sess_low,
        # RVOL vs 20-day average (None when caller didn't supply avg_daily_volume)
        "rvol_vs_20d":        round(rvol, 2) if rvol is not None else None,
        "session_elapsed_pct": round(session_frac * 100, 1),
        # ATR
        "atr":     round(atr, 3),
        "atr_pct": round(atr_pct, 2),
        # MACD — kept for scanner.py, study.py, and the daily-context section.
        # The live prompt no longer prints these (per 2026-04-24 cleanup) but
        # downstream consumers still read them, so we keep computing them.
        "macd":           round(macd, 4),
        "macd_signal":    round(sig,  4),
        "macd_histogram": round(hist, 4),
        "macd_side":      "bullish (MACD > signal)" if macd > sig else "bearish (MACD < signal)",
        # Bollinger Bands — bb_pct_b is consumed by strategies.py; the rest are
        # kept for scanner.py and frontend snapshots. The verbose bb_position
        # English string was dropped 2026-04-24 (unused by strategies).
        "bb_upper":   round(bb_up,  2),
        "bb_mid":     round(bb_mid, 2),
        "bb_lower":   round(bb_lo,  2),
        "bb_pct_b":   round(bb_pct_b, 2),
        # Structure
        "structure":      structure,
        # Volume
        "vol_trend": vol_label,
        "vol_ratio": round(vol_ratio, 2),
        # Opening range
        "opening_range": _opening_range(df),
    }


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_news_section(news: dict | None) -> str:
    """Build the NEWS / CATALYST block for the prompt."""
    if not news:
        return (
            "\nNEWS & CATALYSTS:\n"
            "  No recent news available. Analyse on technicals only.\n"
        )

    lines = [f"\nNEWS & CATALYSTS ({news['article_count']} recent articles):"]
    lines.append(f"  Overall news sentiment: {news['overall_sentiment']}")

    if news["has_catalyst"]:
        lines.append(f"  Catalysts detected: {', '.join(news['catalysts'])}")
    else:
        lines.append("  No major catalysts detected (routine news flow)")

    # Show top headlines
    lines.append("")
    for art in news["articles"][:5]:
        time_str = ""
        if art["time"]:
            try:
                from datetime import datetime as _dt
                t = _dt.fromisoformat(art["time"].replace("Z", "+00:00"))
                time_str = t.strftime("%I:%M %p")
            except Exception:
                time_str = ""
        sentiment_icon = {"bullish": "+", "bearish": "-", "neutral": "~"}.get(art["sentiment"], "~")
        source = art["source"][:15] if art["source"] else ""
        lines.append(f"  [{sentiment_icon}] {art['headline'][:120]}")
        if time_str or source:
            lines.append(f"      ({source} {time_str})")

    lines.append("")
    lines.append("  IMPORTANT: News context affects trade confidence:")
    lines.append("  - A clear positive catalyst (earnings beat, upgrade, FDA approval) is a strong confirming signal for longs.")
    lines.append("  - A clear negative catalyst (miss, downgrade, lawsuit) is a strong confirming signal for shorts.")
    lines.append("  - Catalyst-driven moves tend to have follow-through — trade WITH the catalyst, not against it.")
    lines.append("  - If there is a major catalyst, it overrides some technical signals.")
    lines.append("  - No catalyst = the move is purely technical — rely on chart signals only.")
    lines.append("")

    return "\n".join(lines)


def _build_sector_section(s: dict | None) -> str:
    """Build the SECTOR MOMENTUM block for the prompt."""
    if not s:
        return (
            "\nSECTOR MOMENTUM:\n"
            "  Not available. Analyse without sector context.\n"
        )

    lines = ["\nSECTOR MOMENTUM (is the whole sector moving, or just this stock?):"]
    lines.append(f"  Sector        : {s['sector_name']} ({s['sector_etf']})")

    sign = "+" if s["sector_change_pct"] >= 0 else ""
    lines.append(f"  Sector today  : {s['sector_trend']} ({sign}{s['sector_change_pct']:.2f}%)")

    if s["spy_change_pct"] is not None:
        spy_sign = "+" if s["spy_change_pct"] >= 0 else ""
        lines.append(f"  Broad market  : {s['spy_trend']} (SPY {spy_sign}{s['spy_change_pct']:.2f}%)")

    vs_sign = "+" if s["stock_vs_sector"] >= 0 else ""
    lines.append(f"  Stock vs sector: {vs_sign}{s['stock_vs_sector']:.2f}%  →  {s['relative_strength']}")

    if s["stock_vs_spy"] is not None:
        vs_spy_sign = "+" if s["stock_vs_spy"] >= 0 else ""
        lines.append(f"  Stock vs SPY   : {vs_spy_sign}{s['stock_vs_spy']:.2f}%")

    lines.append(f"  Leadership    : {s['leadership']}")
    lines.append("")
    lines.append("  IMPORTANT: Sector context is a key confluence signal:")
    lines.append("  - A long setup is STRONGER when the sector and broad market are also bullish.")
    lines.append("  - A stock outperforming a rising sector = highest-confidence long setup.")
    lines.append("  - A stock bucking a falling sector can work, but it is RISKIER — reduce confidence.")
    lines.append("  - If the ENTIRE sector is selling off, even a strong chart setup is suspect.")
    lines.append("")

    return "\n".join(lines)


def _build_quote_section(q: dict | None) -> str:
    """Build the BID-ASK / LEVEL 2 block for the prompt."""
    if not q:
        return (
            "\nBID-ASK SPREAD (Level 2):\n"
            "  Not available. Analyse without order-flow data.\n"
        )
    return f"""
BID-ASK SPREAD (Level 2 — real buyer/seller pressure):
  Bid  : ${q['bid']}  ×  {q['bid_size']:,} shares ({q['bid_pct']:.1f}% of book)
  Ask  : ${q['ask']}  ×  {q['ask_size']:,} shares ({q['ask_pct']:.1f}% of book)
  Spread        : ${q['spread']:.4f} ({q['spread_pct']:.4f}%)  →  {q['spread_label']}
  Midpoint      : ${q['mid']}
  Imbalance     : {q['imbalance_ratio']:.2f}x  →  {q['pressure']}
  Price position: {q['price_position']}

  IMPORTANT: Bid-ask data reveals what the price action alone cannot:
  - Strong buying pressure (bid >> ask) confirms bullish setups.
  - Strong selling pressure (ask >> bid) confirms bearish setups.
  - A wide spread means low liquidity — prefer limit orders over market orders.
  - If the imbalance CONFLICTS with the chart setup, reduce confidence.
"""


def _build_extended_hours_section(ext: dict | None) -> str:
    """Build the PRE-MARKET / AFTER-HOURS block for the prompt."""
    if not ext:
        return (
            "\nPRE-MARKET & AFTER-HOURS:\n"
            "  Not available. Analyse using regular-hours data only.\n"
        )

    lines = ["\nPRE-MARKET & AFTER-HOURS (overnight sentiment):"]
    lines.append(f"  Overnight sentiment: {ext['sentiment']}")

    # Gap
    gap = ext.get("gap")
    if gap:
        sign = "+" if gap["gap_pct"] >= 0 else ""
        lines.append(
            f"  Overnight gap    : {gap['type']} — "
            f"prev close ${gap['prev_close']} → open ${gap['regular_open']} "
            f"({sign}{gap['gap_pct']:.2f}%, ${gap['gap_amount']:+.2f})"
        )

    # Pre-market
    pm = ext.get("premarket", {})
    if pm.get("available"):
        sign = "+" if pm["change_pct"] >= 0 else ""
        lines.append(
            f"  Pre-market (4am–9:30am ET): {pm['direction']} "
            f"({sign}{pm['change_pct']:.2f}%) — "
            f"H ${pm['high']}  L ${pm['low']}  Vol {pm['volume']:,}  "
            f"({pm['bars']} bars)"
        )
    else:
        lines.append("  Pre-market: No data available (may need SIP feed)")

    # After-hours
    ah = ext.get("afterhours", {})
    if ah.get("available"):
        sign = "+" if ah["change_pct"] >= 0 else ""
        lines.append(
            f"  After-hours (prev session): {ah['direction']} "
            f"({sign}{ah['change_pct']:.2f}%) — "
            f"H ${ah['high']}  L ${ah['low']}  Vol {ah['volume']:,}"
        )
    else:
        lines.append("  After-hours: No data available")

    lines.append("")
    lines.append("  IMPORTANT: Gap direction and pre-market trend are leading indicators.")
    lines.append("  - A gap up with strong pre-market volume confirms bullish bias.")
    lines.append("  - A gap down that holds (doesn't fill) confirms bearish pressure.")
    lines.append("  - A gap that fills quickly suggests the opposite direction may win.")
    lines.append("  - Pre-market highs/lows are key levels that often act as support/resistance.")
    lines.append("")

    return "\n".join(lines)


def _build_daily_section(daily: dict | None) -> str:
    """Build the DAILY CHART CONTEXT block for the prompt."""
    if not daily:
        return (
            "\nDAILY CHART CONTEXT:\n"
            "  Not available (fewer than 50 daily bars). Analyse using intraday data only.\n"
        )
    return f"""
DAILY CHART CONTEXT (higher timeframe — critical for confluence):
  Daily trend    : {daily['trend']}
  EMA alignment  : {daily['ema_alignment']}
    EMA(9)  = ${daily['ema9']}
    EMA(20) = ${daily['ema20']}
    EMA(50) = ${daily['ema50']}
  RSI(14)        : {daily['rsi']} ({daily['rsi_zone']})
  MACD           : {daily['macd']:+.4f} (signal {daily['macd_signal']:+.4f}, histogram {daily['macd_histogram']:+.4f}, {daily['macd_side']})
  Daily ATR(14)  : ${daily['atr']}
  20-day high    : ${daily['20d_high']}  ({daily['pct_from_20d_high']:.2f}% above current)
  20-day low     : ${daily['20d_low']}   ({daily['pct_from_20d_low']:.2f}% below current)
  Previous day   : High ${daily['prev_high']}  Low ${daily['prev_low']}  Close ${daily['prev_close']}

  IMPORTANT: A high-confidence intraday trade should ALIGN with the daily trend.
  - Long setups are stronger when the daily trend is bullish and price is above daily EMA20.
  - Short setups are stronger when the daily trend is bearish and price is below daily EMA20.
  - If the intraday setup CONFLICTS with the daily trend, reduce confidence significantly.
  - Previous day's high/low are key intraday support/resistance levels.
"""


def _build_ict_section(ind: dict) -> str:
    """Render the ICT (smart-money) context block for the Opus prompt.

    Reads the ``ict_*`` fields that ``compute_ict_features`` merged into
    ``ind``. Returns an empty string when the detector output is empty
    (too few bars, all fields None) so the prompt doesn't burn tokens
    on a heading with nothing under it.

    The glossary lines at the top of the block are intentional: ICT
    terminology (MSS, FVG, OB, sweep, OTE) isn't in the base system
    prompt, so self-documenting the section keeps the rest of the
    system prompt lean for non-ICT analyses.
    """
    kz          = ind.get("ict_killzone")
    pd_zone     = ind.get("ict_pd_zone")
    pd_pct      = ind.get("ict_pd_pct")
    dealing     = ind.get("ict_dealing_range")
    last_sweep  = ind.get("ict_last_sweep")
    last_mss    = ind.get("ict_last_mss")
    near_bull   = ind.get("ict_nearest_bull_zone")
    near_bear   = ind.get("ict_nearest_bear_zone")
    in_bull     = ind.get("ict_in_bull_zone")
    in_bear     = ind.get("ict_in_bear_zone")
    in_ote_long  = ind.get("ict_in_ote_long")
    in_ote_short = ind.get("ict_in_ote_short")

    if not any([kz, pd_zone, last_sweep, last_mss, near_bull, near_bear, dealing]):
        return ""

    kz_labels = {
        "ny_am":         "NY AM (09:30–11:00 ET — morning follow-through window)",
        "silver_bullet": "Silver Bullet (10:00–11:00 ET — ICT's highest-conviction window)",
        "ny_pm":         "NY PM (13:30–16:00 ET — afternoon follow-through window)",
    }

    lines = [
        "",
        "INNER-CIRCLE-TRADER (ICT / SMART-MONEY) CONTEXT:",
        "  Glossary — Sweep: wick past a level then close back (stop-run);",
        "    MSS (Market Structure Shift): first close through an opposing swing pivot;",
        "    FVG (Fair Value Gap): 3-candle imbalance, acts as demand/supply magnet on retest;",
        "    OB (Order Block): last opposing candle before a displacement leg;",
        "    Discount/Premium: lower/upper half of the current dealing range;",
        "    OTE (Optimal Trade Entry): 62–79% retrace into discount (long) or premium (short).",
        "",
    ]

    lines.append(
        f"  Killzone     : {kz_labels.get(kz, 'Outside ICT windows — lower follow-through expected')}"
    )

    if dealing:
        lines.append(f"  Dealing range: ${dealing['low']:.2f} – ${dealing['high']:.2f}")

    # NOTE (2026-04-24): The RTH-anchoring fix made session_high/low and the
    # dealing range numerically tighter (was polluted by pre-market wicks),
    # and we TEMPORARILY added a "Pre-market: H/L" line + "(RTH-anchored)"
    # parenthetical to explain the change to Sonnet. The 75-trade lift
    # compare showed WORSE Sonnet calibration post-change — specifically
    # 20/75 direction flips and calibration going backwards (Sonnet got
    # less confident on winners, more confident on losers). Path-2
    # hypothesis: the prompt-facing additions are what disorient Sonnet,
    # not the shifted values themselves. Reverted to the original prompt
    # wording here so we can isolate whether the data-layer fix alone is
    # enough, or whether Sonnet needs the extra PMH/PML context back. If
    # v3 recovers baseline, the data fix is a keeper; if not, we widen
    # the sample or revert the compute-layer change too.
    # The PMH/PML keys are still published in ict_zones.liquidity for
    # frontend overlays and future prompt work — just not surfaced to
    # Sonnet's prompt for this iteration.

    if pd_zone:
        pct_str = f" ({int(pd_pct * 100)}%)" if pd_pct is not None else ""
        ote_note = ""
        if in_ote_long:
            ote_note = " — IN OTE-LONG (62–79% discount retrace, classic long entry)"
        elif in_ote_short:
            ote_note = " — IN OTE-SHORT (62–79% premium retrace, classic short entry)"
        lines.append(f"  PD position  : {pd_zone.upper()}{pct_str}{ote_note}")

    if last_sweep:
        age = last_sweep.get("bars_ago", 0)
        age_s = "this bar" if age == 0 else f"{age} bar(s) ago"
        arrow = "BULLISH (implies upside leg)" if last_sweep["direction"] == "bullish" else "BEARISH (implies downside leg)"
        lines.append(
            f"  Last sweep   : {arrow} — wicked {last_sweep['level_name']} "
            f"@ ${last_sweep['level_price']:.2f}, {age_s}"
        )

    if last_mss:
        age = last_mss.get("bars_ago", 0)
        age_s = "this bar" if age == 0 else f"{age} bar(s) ago"
        direction = "BULLISH MSS" if last_mss["direction"] == "bullish" else "BEARISH MSS"
        broken = last_mss.get("broken_swing_price")
        broken_s = f" (broke ${broken:.2f})" if broken else ""
        lines.append(f"  Last MSS     : {direction}{broken_s}, {age_s}")

    if in_bull and near_bull:
        age = near_bull.get("bars_ago", 0)
        kind = near_bull.get("kind", "zone").upper()
        lines.append(
            f"  Bull zone    : TOUCHING bull {kind} "
            f"[${near_bull['low']:.2f}–${near_bull['high']:.2f}] "
            f"(formed {age} bars ago) — potential long retest entry"
        )
    elif near_bull:
        lines.append(
            f"  Nearest bull zone: [${near_bull['low']:.2f}–${near_bull['high']:.2f}] "
            f"({near_bull.get('kind', '').upper()}) — above/below price, not touching"
        )

    if in_bear and near_bear:
        age = near_bear.get("bars_ago", 0)
        kind = near_bear.get("kind", "zone").upper()
        lines.append(
            f"  Bear zone    : TOUCHING bear {kind} "
            f"[${near_bear['low']:.2f}–${near_bear['high']:.2f}] "
            f"(formed {age} bars ago) — potential short retest entry"
        )
    elif near_bear:
        lines.append(
            f"  Nearest bear zone: [${near_bear['low']:.2f}–${near_bear['high']:.2f}] "
            f"({near_bear.get('kind', '').upper()}) — above/below price, not touching"
        )

    lines.append("")
    lines.append(
        "  Weight this as SMART-MONEY STRUCTURE. The canonical ICT long is: "
        "bullish sweep → bullish MSS → retest into a bull FVG/OB in OTE/discount. "
        "Mirror for shorts. ICT signals STACK with the traditional indicators — "
        "don't double-count, but treat alignment as strong confluence. If ICT "
        "contradicts the matched strategies (e.g. bull sweep + MSS but matched "
        "strategies are all short), call the conflict out in 'reasoning' and "
        "reflect it in the probability split."
    )

    return "\n".join(lines) + "\n"


# ── Multi-timeframe / VWAP-band / level-touch / RVOL section builders ─────────
# Added 2026-04-24 to give Sonnet the "3 confirming signals" confluence needed
# to clear the 0.70 confidence gate more often. Each helper handles its own
# None fallbacks so the prompt never prints "None"-soup if higher-TF history
# or daily bars are missing — it prints a short "— insufficient …" note.

def _build_mtf_rsi_lines(ind: dict) -> str:
    def _fmt(v, z):
        return f"{v} ({z})" if v is not None else "— (insufficient history)"
    return (
        f"  RSI(14) 1-min  = {_fmt(ind.get('rsi'),     ind.get('rsi_zone',     '—'))}\n"
        f"  RSI(14) 5-min  = {_fmt(ind.get('rsi_5m'),  ind.get('rsi_5m_zone',  '—'))}\n"
        f"  RSI(14) 15-min = {_fmt(ind.get('rsi_15m'), ind.get('rsi_15m_zone', '—'))}\n"
        f"  Alignment      : {ind.get('rsi_alignment', '—')}"
    )


def _build_mtf_ema_lines(ind: dict) -> str:
    def _pair(e9, e20):
        if e9 is None or e20 is None:
            return "— (insufficient history)"
        bias = "EMA9 > EMA20 (bullish)" if e9 > e20 else "EMA9 < EMA20 (bearish)"
        return f"EMA9 ${e9}  |  EMA20 ${e20}  |  {bias}"
    return (
        f"  1-min  : {_pair(ind.get('ema9'),     ind.get('ema20'))}\n"
        f"  5-min  : {_pair(ind.get('ema9_5m'),  ind.get('ema20_5m'))}\n"
        f"  15-min : {_pair(ind.get('ema9_15m'), ind.get('ema20_15m'))}\n"
        f"  MTF alignment: {ind.get('ema_alignment_mtf', '—')}"
    )


def _build_vwap_bands_lines(ind: dict) -> str:
    return (
        f"  VWAP         = ${ind.get('vwap', 0.0)}  — price is {ind.get('price_vs_vwap', '—')} "
        f"({ind.get('pct_from_vwap', 0.0):+.2f}% away)\n"
        f"  +2σ band     = ${ind.get('vwap_plus_2s',  0.0)}   ← extreme overextended long\n"
        f"  +1σ band     = ${ind.get('vwap_plus_1s',  0.0)}\n"
        f"  −1σ band     = ${ind.get('vwap_minus_1s', 0.0)}\n"
        f"  −2σ band     = ${ind.get('vwap_minus_2s', 0.0)}   ← extreme overextended short\n"
        f"  Position     : {ind.get('vwap_band_position', '—')}"
    )


def _build_level_touches_lines(ind: dict) -> str:
    """Key-level touch counts — 3+ touches = liquidity stacking."""
    def _line(label: str, level, touches: int) -> str:
        if level is None:
            return f"  {label:<16} — (not available)"
        flag = "  ← LIQUIDITY STACK" if touches >= 3 else ""
        plural = "time" if touches == 1 else "times"
        return f"  {label:<16} ${level}  — tested {touches} {plural} this session{flag}"
    return "\n".join([
        _line("Prev Day High",  ind.get("prev_high"),    ind.get("touches_prev_high",    0)),
        _line("Prev Day Low",   ind.get("prev_low"),     ind.get("touches_prev_low",     0)),
        _line("Session High",   ind.get("session_high"), ind.get("touches_session_high", 0)),
        _line("Session Low",    ind.get("session_low"),  ind.get("touches_session_low",  0)),
    ])


def _build_rvol_line(ind: dict) -> str:
    """One-line RVOL reading — the classic day-trader conviction metric."""
    rvol = ind.get("rvol_vs_20d")
    elapsed = ind.get("session_elapsed_pct", 0.0)
    if rvol is None:
        return "  RVOL (vs 20d avg): — (no daily baseline available)"
    # Qualitative label the way a trader would read it
    if rvol >= 3.0:
        label = "EXTREME conviction (3×+)"
    elif rvol >= 2.0:
        label = "STRONG conviction (2-3×)"
    elif rvol >= 1.5:
        label = "elevated (1.5-2×)"
    elif rvol >= 0.8:
        label = "in-line with normal"
    else:
        label = "LOW — lacking participation"
    return f"  RVOL (vs 20d avg): {rvol}× at {elapsed:.0f}% of session elapsed — {label}"


def _build_user_prompt(symbol: str, ind: dict, matched_strategies: list | None = None, daily_context: dict | None = None, extended_hours: dict | None = None, quote_context: dict | None = None, sector_context: dict | None = None, news_context: dict | None = None, context_matches: list | None = None) -> str:
    """Build the user prompt for Claude.

    Trimmed 2026-04-24 based on the Tier-2 lift study: dropped MACD (4 fields,
    lagging/redundant with EMA9/20 on 1-min bars), Bollinger Bands %B +
    position (redundant with RSI + VWAP), single-bar candle_pattern + streak
    (noise at 1-min), the 20-candle OHLCV table (~1.2k tokens, redundant with
    the structure summary), price_vs_ema{9,20} English strings (Sonnet can
    read it from the numeric EMA + price), and lessons_text/study_text
    (token drain — to return via retrieval only).

    Expanded 2026-04-24 with five confluence-boosting feature groups:
      * Multi-timeframe EMA (1m / 5m / 15m) with MTF alignment string.
      * Multi-timeframe RSI (1m / 5m / 15m) with alignment string.
      * VWAP ±1σ / ±2σ statistical bands + band-position label.
      * Key-level touch counts (PDH / PDL / session H/L).
      * RVOL vs 20-day average (session-cumulative, time-of-day-scaled).
    All five have clean None-fallbacks so the prompt never breaks on
    short sessions or missing daily data.
    """
    sign = "+" if ind["change_pct"] >= 0 else ""

    # Opening range section (optional)
    or_section = ""
    if ind["opening_range"]:
        orh = ind["opening_range"]["high"]
        orl = ind["opening_range"]["low"]
        cur = ind["current_price"]
        if cur > orh:
            or_pos = f"ABOVE OR high (breakout +{(cur - orh) / cur * 100:.2f}%)"
        elif cur < orl:
            or_pos = f"BELOW OR low  (breakdown -{(orl - cur) / cur * 100:.2f}%)"
        else:
            or_pos = "INSIDE opening range (still consolidating)"
        or_section = (
            f"\nOPENING RANGE (9:30–9:45 ET):\n"
            f"  OR High = ${orh:.2f}  |  OR Low = ${orl:.2f}\n"
            f"  Current price is {or_pos}\n"
        )

    return f"""Analyze this intraday 1-minute chart for {symbol}:

PRICE ACTION:
  Current       : ${ind['current_price']} ({sign}{ind['change_pct']}% vs session open ${ind['session_open']})
  Session high  : ${ind['session_high']}  ({ind['pct_from_high']:.2f}% above current)
  Session low   : ${ind['session_low']}   ({ind['pct_from_low']:.2f}% below current)
{_build_extended_hours_section(extended_hours)}
{_build_quote_section(quote_context)}
{_build_sector_section(sector_context)}
{_build_news_section(news_context)}
TREND (multi-timeframe EMAs):
{_build_mtf_ema_lines(ind)}

MOMENTUM (multi-timeframe RSI):
{_build_mtf_rsi_lines(ind)}

VOLATILITY:
  ATR(14)  = ${ind['atr']} ({ind['atr_pct']}% of price) ← use for stop sizing

VWAP & STATISTICAL BANDS:
{_build_vwap_bands_lines(ind)}
{or_section}
{_build_daily_section(daily_context)}
VOLUME:
  Last-5-bar : {ind['vol_trend']} ({ind['vol_ratio']}× session avg over last 5 bars)
{_build_rvol_line(ind)}

KEY LEVELS & TOUCH COUNTS (3+ tests = liquidity stacking):
{_build_level_touches_lines(ind)}

PRICE STRUCTURE (last 20 bars):
  {ind['structure']}

Set the stop loss at least 1× ATR (${ind['atr']}) from entry.
{_build_ict_section(ind)}{_context_setup_section(context_matches)}{_strategy_section(matched_strategies)}
Respond with ONLY valid JSON matching this schema exactly:
{_JSON_SCHEMA}"""


def _context_setup_section(context_matches: list | None) -> str:
    """Build context-only setup notes for suspended/research predicates."""
    if not context_matches:
        return ""
    lines = [
        "",
        "CONTEXT-ONLY STRUCTURAL SETUPS (informational; not auto-trade triggers):",
    ]
    for setup in context_matches:
        direction = str(getattr(setup, "direction", "")).upper()
        description = str(getattr(setup, "description", "")).strip()
        lines.append(f"  - {setup.name} ({direction}) — {description}")
    lines.append(
        "  Treat these as confluence notes only. The actionable decision must still "
        "come from price action, risk/reward, and any live PLAYBOOK match below."
    )
    return "\n".join(lines) + "\n"


# v7.11 — tier-aware prompt framing for matched strategies. The
# tier comes from the audit-locked ``Strategy.tier`` field (see
# ``app/strategies.py`` v7.11 docstring). Sonnet uses these labels
# to weight the matched setup against the live market context: an
# A-grade match should drive higher confidence on a green analysis
# than a B-grade match with the same structural fit.
_TIER_LABELS = {
    "A": (
        "A-grade — historically survives realistic 0.10R-per-trade "
        "friction (positive post-friction AvgR on the 18-month replay). "
        "High-confidence setup; weight your analysis accordingly."
    ),
    "B": (
        "B-grade — raw historical edge exists but is friction-fragile "
        "(post-friction AvgR is small or slightly negative). Requires "
        "extra confluence (volume, regime, MFE > MAE) beyond the listed "
        "entry rules before recommending an entry."
    ),
    "context": (
        "Context-only — historically weak after friction; treat as a "
        "confluence note rather than a primary trigger. The structural "
        "pattern is real but the empirical edge is too thin to act on "
        "alone."
    ),
    "suppress": "(Suppressed setup — this should not normally appear here.)",
}


def _strategy_section(matched: list | None) -> str:
    """Build the strategy context block for the prompt.

    v7.11: each strategy's prompt_block is followed by a one-line
    tier label so Sonnet can distinguish A-grade primary setups from
    B-grade friction-fragile ones from context-only fillers.
    """
    if not matched:
        return (
            "\nSTRATEGY SCANNER: No pre-defined strategies matched the current setup.\n"
            "Analyse freely based on all indicators above.\n"
        )
    rendered_blocks = []
    for s in matched:
        block = s.prompt_block()
        tier_label = _TIER_LABELS.get(getattr(s, "tier", "B"), "")
        if tier_label:
            block += f"  Quality tier: {tier_label}\n"
        rendered_blocks.append(block)
    blocks = "\n".join(rendered_blocks)
    return (
        f"\n{'='*60}\n"
        f"STRATEGY SCANNER: {len(matched)} strategy/strategies matched!\n"
        f"Evaluate the matching strategies below and determine which (if any) "
        f"provides the best risk/reward setup right now. You may also reject "
        f"them if the signals are weak or conflicting. The Quality tier line "
        f"under each strategy is from the v7.10.5 execution audit — A-grade "
        f"setups have historically generated positive expectancy after realistic "
        f"trading friction; B-grade and context entries are weaker after friction.\n\n"
        f"{blocks}\n"
        f"{'='*60}\n"
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def analyze_chart(
    symbol: str,
    bars: list[dict],
    daily_bars: list[dict] | None = None,
    extended_bars: list[dict] | None = None,
    snapshot: dict | None = None,
    sector_snapshots: dict[str, dict] | None = None,
    news_articles: list[dict] | None = None,
) -> dict:
    """Compute indicators, call Claude with full context, return analysis.

    Args:
        symbol:            Ticker symbol.
        bars:              1-minute intraday bars.
        daily_bars:        Optional daily bars (≥50 days) for higher-timeframe context.
        extended_bars:     Optional 1-min bars including pre-market/after-hours for
                           overnight sentiment analysis.
        snapshot:          Optional Alpaca snapshot with latest NBBO quote (bid/ask/sizes).
        sector_snapshots:  Optional {etf: snapshot} dict for sector ETFs + SPY.
        news_articles:     Optional list of Alpaca news article dicts.

    Note: ``lessons_text`` and ``study_text`` were dropped 2026-04-24 — they
    were populated on every call with generic text that added ~500-1000 tokens
    without measurably changing Sonnet's confidence distribution. Retrieval-
    based recall (only fetch lessons from setups matching the current
    strategy/direction/killzone) is a TODO.
    """
    if not settings.anthropic_api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY is not set. Add it to Backend/.env to enable AI analysis."
        )

    # Pull daily context FIRST so we can thread prev-day levels + the 20-day
    # avg volume into compute_indicators (needed for gap_pct + RVOL).
    daily_ctx = compute_daily_context(daily_bars) if daily_bars else None
    if daily_ctx:
        logger.info("daily_context symbol=%s trend=%s ema_align=%s", symbol, daily_ctx["trend"], daily_ctx["ema_alignment"])

    # 20-day avg volume for the RVOL reading. Uses the most recent 20 daily
    # bars (or whatever's available — a shorter window still gives Sonnet a
    # usable "relative to recent history" signal).
    avg_daily_vol: float | None = None
    if daily_bars:
        try:
            vols = [float(b.get("volume") or b.get("v") or 0) for b in daily_bars[-20:]]
            vols = [v for v in vols if v > 0]
            if vols:
                avg_daily_vol = sum(vols) / len(vols)
        except Exception:
            avg_daily_vol = None

    ind = compute_indicators(
        bars,
        prev_close=daily_ctx["prev_close"] if daily_ctx else None,
        prev_high=daily_ctx.get("prev_high")  if daily_ctx else None,
        prev_low=daily_ctx.get("prev_low")    if daily_ctx else None,
        avg_daily_volume=avg_daily_vol,
    )

    # ── Check cache before calling Claude ──
    cached = analysis_cache.get(symbol, current_price=ind["current_price"])
    if cached is not None:
        return cached

    # Compute extended-hours context (pre-market / after-hours / gap)
    prev_close = daily_ctx["prev_close"] if daily_ctx else None
    ext_ctx = compute_extended_hours(extended_bars or bars, prev_close=prev_close)
    if ext_ctx:
        logger.info("extended_hours symbol=%s sentiment=%s", symbol, ext_ctx.get("sentiment"))

    # Compute bid-ask / quote context
    quote_ctx = compute_quote_context(snapshot, current_price=ind["current_price"])
    if quote_ctx:
        logger.info("quote_context symbol=%s spread=%.4f%% pressure=%s", symbol, quote_ctx["spread_pct"], quote_ctx["pressure"])

    # Compute sector momentum context
    sector_ctx = None
    if sector_snapshots:
        sector_ctx = compute_sector_context(symbol, ind["change_pct"], sector_snapshots)
        if sector_ctx:
            logger.info("sector_context symbol=%s sector=%s relative=%s", symbol, sector_ctx["sector_name"], sector_ctx["relative_strength"])

    # Compute news / catalyst context
    news_ctx = compute_news_context(news_articles, symbol) if news_articles else None
    if news_ctx:
        logger.info("news_context symbol=%s articles=%d sentiment=%s catalyst=%s",
                     symbol, news_ctx["article_count"], news_ctx["overall_sentiment"], news_ctx["has_catalyst"])

    # ── ICT (smart-money) feature merge ──────────────────────────────────────
    # Runs BEFORE the strategy screen so ICT predicates (Sweep+MSS, Silver
    # Bullet) can match — they read the ``ict_*`` flags that compute_ict_features
    # stamps onto ``ind``. The prev-day levels are seeded from daily_ctx when
    # available so sweep detection has real liquidity magnets; without them
    # the feature builder still returns session-level features (session H/L)
    # so the ICT section degrades gracefully rather than disappearing.
    #
    # Isolated in try/except for the same reason the scanner isolates it:
    # a detector bug must not take down the whole analyze path.
    try:
        from .ict import compute_ict_features
        ict_seed = None
        if daily_ctx:
            ict_seed = {
                "prev_close": daily_ctx.get("prev_close"),
                "prev_high":  daily_ctx.get("prev_high"),
                "prev_low":   daily_ctx.get("prev_low"),
            }
        ind.update(compute_ict_features(bars, ict_seed))
    except Exception:
        logger.exception("analyze_ict_features_failed symbol=%s", symbol)

    # Run strategy pre-screen (Tier 1 — instant, no AI)
    from .strategies import context_screen, screen
    ind["symbol"] = symbol
    matched = screen(ind)
    context_matches = context_screen(ind)
    logger.info(
        "strategy_screen symbol=%s matched=%s context=%s ict_kz=%s ict_pd=%s",
        symbol, [s.name for s in matched] or "none",
        [s.name for s in context_matches] or "none",
        ind.get("ict_killzone"), ind.get("ict_pd_zone"),
    )

    prompt = _build_user_prompt(symbol, ind, matched, daily_context=daily_ctx, extended_hours=ext_ctx, quote_context=quote_ctx, sector_context=sector_ctx, news_context=news_ctx, context_matches=context_matches)

    logger.info("analyze_start symbol=%s bars=%d strategies=%d", symbol, len(bars), len(matched))

    # ── Primary: Claude Sonnet 4 with extended thinking ──────────────────────
    # Extended thinking lets the model reason step-by-step through signal
    # confluence before producing the final JSON — big quality boost.
    _RETRIES = 3
    _RETRYABLE = (429, 529, 500, 502, 503)
    raw: str | None = None
    used_model = "claude-sonnet-4-20250514"

    claude_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    for _attempt in range(1, _RETRIES + 1):
        try:
            message = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=16000,
                thinking={"type": "enabled", "budget_tokens": 10000},
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            # Thinking blocks come first — grab the text block with the JSON
            raw = next(
                (block.text for block in message.content if block.type == "text"),
                "",
            ).strip()
            break
        except anthropic.APIStatusError as exc:
            # Only retry rate-limits (429/529) — server errors (500/502/503)
            # won't resolve in seconds, fall through to GPT-4o immediately.
            _worth_retrying = exc.status_code in (429, 529) and _attempt < _RETRIES
            if _worth_retrying:
                wait = _attempt * 5  # 5s, 10s
                logger.warning(
                    "analyze_retry symbol=%s model=sonnet-4 attempt=%d/%d status=%d wait=%ds",
                    symbol, _attempt, _RETRIES, exc.status_code, wait,
                )
                import time; time.sleep(wait)
            elif settings.openai_api_key:
                # Exhausted retries OR non-retryable (400 usage limit, etc.)
                logger.warning(
                    "analyze_fallback symbol=%s to=gpt-4o status=%d error=%s",
                    symbol, exc.status_code, str(exc)[:200],
                )
                break  # fall through to GPT-4o
            else:
                raise

    # ── Fallback: GPT-4o when Claude is overloaded ───────────────────────────
    if raw is None and settings.openai_api_key:
        import openai as _openai
        oai_client = _openai.OpenAI(api_key=settings.openai_api_key)
        used_model = "gpt-4o"

        for _attempt in range(1, _RETRIES + 1):
            try:
                oai_resp = oai_client.chat.completions.create(
                    model="gpt-4o",
                    max_tokens=8192,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                )
                raw = (oai_resp.choices[0].message.content or "").strip()
                logger.info("analyze_used_fallback symbol=%s model=gpt-4o", symbol)
                break
            except Exception as exc:
                if _attempt < _RETRIES:
                    logger.warning(
                        "analyze_retry symbol=%s model=gpt-4o attempt=%d/%d error=%s",
                        symbol, _attempt, _RETRIES, exc,
                    )
                    import time; time.sleep(_attempt * 3)
                else:
                    raise

    if raw is None:
        raise ValueError(f"AI analysis failed for {symbol} — all models exhausted")

    # Strip accidental markdown fences
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    # raw_decode ignores any trailing text after the JSON object
    decoder = json.JSONDecoder()
    try:
        result, _ = decoder.raw_decode(raw)
    except json.JSONDecodeError as exc:
        logger.error("analyze_json_parse_failed symbol=%s raw=%s", symbol, raw[:500])
        raise ValueError(
            f"AI returned malformed JSON for {symbol}. "
            f"Parse error: {exc}. Raw response starts with: {raw[:200]!r}"
        ) from exc

    # Schema-validate the response so a missing field (e.g. trend_strength)
    # fails here with a clear error instead of a KeyError several frames down.
    try:
        parsed = AIAnalysisResponse.model_validate(result)
    except ValidationError as exc:
        logger.error(
            "analyze_schema_invalid symbol=%s errors=%s raw=%s",
            symbol, exc.errors(), raw[:500],
        )
        err_summary = "; ".join(
            ".".join(str(p) for p in e["loc"]) + ": " + e["msg"]
            for e in exc.errors()
        )
        raise ValueError(
            f"AI response for {symbol} is missing required fields: {err_summary}"
        ) from exc

    # Propagate the validator's post-init work (feature #10) back into the
    # raw result dict:
    #   • probabilities normalized to sum = 1 (tolerating ±0.15 drift)
    #   • synthesized p_target/p_stop/p_neither when Claude only returned
    #     the legacy ``confidence`` scalar
    #   • ``confidence = p_target`` so the scan-loop gate stays consistent
    # Without this step the scan loop would read the pre-validation values
    # and Brier scoring would see un-normalized probabilities.
    result["recommendation"] = parsed.recommendation.model_dump()

    # Attach indicator snapshot for the frontend
    result["indicators"] = {
        "ema9":          ind["ema9"],
        "ema20":         ind["ema20"],
        "rsi":           ind["rsi"],
        "vwap":          ind["vwap"],
        "atr":           ind["atr"],
        "macd":          ind["macd"],
        "macd_signal":   ind["macd_signal"],
        "bb_upper":      ind["bb_upper"],
        "bb_lower":      ind["bb_lower"],
        "current_price": ind["current_price"],
        "change_pct":    ind["change_pct"],
        "session_high":  ind["session_high"],
        "session_low":   ind["session_low"],
    }

    # ── Attach which strategies matched (for frontend display) ──
    # v7.11: each entry now includes tier / score / reasons /
    # post_friction_avg_r decoration from the Trade Quality Score
    # layer in app/trade_quality.py. Pure metadata — does not gate
    # autotrade (gating change is deferred to v7.12+); the frontend
    # uses this to render tier badges + hover tooltips.
    from .trade_quality import score as _score_match

    rth_min = ind.get("ict_rth_minute")
    rth_hour_et = (9 + (rth_min + 30) // 60) if rth_min is not None else None

    def _decorate(s) -> dict:
        q = _score_match(
            strategy_name=s.name,
            symbol=symbol,
            rth_hour_et=rth_hour_et,
            ind=ind,
            base_tier=s.tier,
        )
        return {
            "name": s.name,
            "direction": s.direction,
            "tier": q["tier"],
            "score": q["score"],
            "reasons": q["reasons"],
            "post_friction_avg_r": q["post_friction_avg_r"],
        }

    result["matched_strategies"] = [_decorate(s) for s in matched]

    # v7.11: surface CONTEXT_PLAYBOOK matches separately so the
    # frontend can render them as confluence-only entries (different
    # visual treatment) without polluting the auto-tradeable
    # matched_strategies list. The autotrade path consumes the scanner's
    # hit dict (which only includes screen() output), so this addition
    # is analyzer-response-only and cannot influence autotrade firing.
    result["context_strategies"] = [_decorate(s) for s in context_matches]

    # v7.12: forming setups (sweep/MSS partial-state) for the
    # frontend's "Watching" panel. Computed from PLAYBOOK only —
    # CONTEXT_PLAYBOOK predicates are context-only by definition and
    # don't have _partial_check wired. Skip strategies that already
    # fully matched to avoid double-rendering. Skip "fully_formed"
    # forming states that didn't reach matched_strategies (the
    # symbol_blacklist gate likely fired) — the user can't act on a
    # blacklisted setup.
    from .strategies import PLAYBOOK as _PLAYBOOK
    matched_names = {s.name for s in matched}
    forming_setups: list[dict] = []
    for s in _PLAYBOOK:
        if s._partial_check is None or s.name in matched_names:
            continue
        state = s.partial_match(ind)
        if state is None or state.get("stage") == "fully_formed":
            continue
        forming_setups.append({
            "name": s.name,
            "direction": s.direction,
            "tier": "forming",
            "stage": state.get("stage"),
            "missing": state.get("missing", []),
            "structural_notes": state.get("structural_notes", []),
        })
    result["forming_setups"] = forming_setups

    # Attach the full analysis prompt so lessons can do deep introspection
    result["_analysis_prompt"] = prompt

    logger.info(
        "analyze_done symbol=%s trend=%s direction=%s confidence=%s strategies=%s",
        symbol, result.get("trend"),
        result.get("recommendation", {}).get("direction"),
        result.get("recommendation", {}).get("confidence"),
        [s.name for s in matched] or "none",
    )

    # ── Store in cache ──
    analysis_cache.put(symbol, result, ind["current_price"])

    return result
