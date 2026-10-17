from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status

from ..alpaca import AlpacaBarsRequest, AlpacaMarketDataClient, convert_alpaca_bars
from ..analyzer import analyze_chart, get_required_sector_etfs
from ..deps import require_operator
from ..ict import compute_ict_features
# Lessons/study imports removed 2026-04-24 — see analyze_chart docstring.
from ..schemas import (
    AnalysisResponse,
    CandleBar,
    ChartResponse,
    ICTZonesResponse,
    MarketPulseResponse,
    SymbolPulse,
)
from ..autotrade import get_active_universe
from ..settings import settings
from ..ws import emit_market_pulse

_SYMBOL_RE = re.compile(r"^[A-Z]{1,10}$")

logger = logging.getLogger("tradehack.market")
router = APIRouter(prefix="/market", tags=["market"])

# A move smaller than this (absolute %) is displayed as flat.
_FLAT_THRESHOLD = 0.05

# Wide lookback so every endpoint has data even on weekends / 3-day holidays.
# 120 h ≈ 5 calendar days.  Alpaca only returns bars during market +
# extended-hours windows, so the actual payload stays small (~1 k bars/symbol).
_LOOKBACK_HOURS = 120

# Pulse-specific lookback. The full 120h window in _LOOKBACK_HOURS is
# what /analyze and /scan-strategies need for ICT context (asian range,
# prior-day levels, EMA20 warmup). Pulse only needs enough history for
# a sparkline + relative-volume baseline — 6h covers premarket through
# close on the current trading day. Dropping pulse from 120h to 6h
# reduces the per-symbol payload from ~7800 bars to ~390 bars (20×
# smaller), which is the main reason Pulse felt slow vs Dynamic Stocks.
_PULSE_LOOKBACK_HOURS = 6

# Allowed intraday timeframes and their lookback windows.
# Wide enough to always show data even on weekends / holidays.
_TIMEFRAME_MAP: dict[str, tuple[str, int]] = {
    "1Min":  ("1Min",  120),   # ~5 calendar days — survives 3-day weekends
    "5Min":  ("5Min",  120),   # same wide window
    "15Min": ("15Min", 168),   # ~7 calendar days
}


def _compute_pulse(symbol: str, bars: list[dict]) -> SymbolPulse | None:
    """Convert a list of Alpaca bar dicts into a SymbolPulse summary."""
    if not bars:
        return None

    df = pd.DataFrame(bars).rename(columns={
        "t": "timestamp", "c": "close", "v": "volume",
    })
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    if len(df) < 2:
        return None

    session_open_price = float(df["close"].iloc[0])
    latest_price       = float(df["close"].iloc[-1])
    latest_time        = df["timestamp"].iloc[-1]
    latest_volume      = float(df["volume"].iloc[-1])
    avg_volume         = float(df["volume"].mean())

    change_pct = (
        (latest_price - session_open_price) / session_open_price * 100
        if session_open_price > 0 else 0.0
    )

    if change_pct > _FLAT_THRESHOLD:
        direction = "up"
    elif change_pct < -_FLAT_THRESHOLD:
        direction = "down"
    else:
        direction = "flat"

    rel_vol = round(latest_volume / avg_volume, 2) if avg_volume > 0 else 0.0
    bar_time = latest_time.isoformat() if hasattr(latest_time, "isoformat") else str(latest_time)

    # Build sparkline: downsample close prices to ~80 points for inline charts.
    closes = df["close"].tolist()
    if len(closes) > 80:
        step = len(closes) / 80
        sparkline = [round(float(closes[int(i * step)]), 2) for i in range(80)]
    else:
        sparkline = [round(float(c), 2) for c in closes]

    return SymbolPulse(
        symbol=symbol,
        price=round(latest_price, 2),
        change_pct=round(change_pct, 2),
        direction=direction,  # type: ignore[arg-type]
        relative_volume=rel_vol,
        bar_time=bar_time,
        sparkline=sparkline,
    )


# ── Pulse TTL cache (v7.13 Phase 2.3, 2026-05-01) ───────────────────────────
# The /market/pulse handler pulls 8h × 1-min bars for every symbol in the
# active universe (~33 symbols × ~390 bars = ~13k bars per call). That's a
# 3-6s round-trip to Alpaca every time the page loads. Most of the time
# the operator reloads within seconds of a previous load — caching the
# response for ~30s gives them an instant render with no scanner-behavior
# change (this is purely page-load speed; the cached object is the same
# data the live fetch would have returned).
#
# Cache key: ``(symbols_tuple)``. Universe changes (apply/reset) naturally
# bypass stale cache because the symbols tuple differs. TTL handles the
# wall-clock drift on its own.
#
# WebSocket emission ONLY fires on cache misses — re-broadcasting cached
# data would just be noise on the wire.
_PULSE_CACHE_TTL_SECONDS = 30.0
_pulse_cache: dict[tuple[str, ...], tuple[float, "MarketPulseResponse"]] = {}


def _pulse_cache_get(symbols: list[str]) -> "MarketPulseResponse | None":
    """Return the cached response if it's still within TTL, else None."""
    import time
    key = tuple(symbols)
    entry = _pulse_cache.get(key)
    if entry is None:
        return None
    ts, resp = entry
    if time.time() - ts > _PULSE_CACHE_TTL_SECONDS:
        # Stale — drop it so the next request triggers a fresh fetch
        # AND a fresh WebSocket emission.
        _pulse_cache.pop(key, None)
        return None
    return resp


def _pulse_cache_put(symbols: list[str], resp: "MarketPulseResponse") -> None:
    import time
    key = tuple(symbols)
    # Bound the cache size — there's only one universe at a time in
    # practice, but a quick burst of apply/reset cycles could leave
    # stale keys around. Cheap LRU-ish: drop the oldest entry when
    # we exceed a small bound.
    if len(_pulse_cache) >= 8:
        oldest_key = min(_pulse_cache, key=lambda k: _pulse_cache[k][0])
        _pulse_cache.pop(oldest_key, None)
    _pulse_cache[key] = (time.time(), resp)


@router.get("/pulse", response_model=MarketPulseResponse)
async def get_market_pulse(
    _: object = Depends(require_operator),
) -> MarketPulseResponse:
    """Return a live price snapshot for every symbol in the training universe.

    Fetches the last few hours of 1-minute bars from Alpaca — enough
    for the sparkline + RVOL baseline. Returns 503 when Alpaca
    credentials are missing or invalid so the frontend can show a
    configuration prompt instead of a generic error.

    Performance (v7.13 Phase 2.3 + 2.4):
      * 30s in-memory TTL cache — repeat loads inside the same window
        return the cached response instantly. Cache key is the active
        symbols tuple so a universe change (apply/reset) bypasses it.
      * 6h lookback (was 120h) — 20× less data per symbol, which is
        the main reason cold loads were 3-6s. With the trim, cold
        loads are ~300-500ms.
      * Async handler — the Alpaca fetch runs in a worker thread via
        ``asyncio.to_thread`` so the FastAPI event loop stays
        responsive while a cold call is in flight.
    """
    symbols = get_active_universe()

    cached = _pulse_cache_get(symbols)
    if cached is not None:
        logger.debug("pulse_cache_hit symbols=%d", len(symbols))
        return cached

    try:
        client = AlpacaMarketDataClient()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    end   = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=_PULSE_LOOKBACK_HOURS)

    logger.info(
        "pulse_fetch symbols=%d window_hours=%d (cache miss)",
        len(symbols), _PULSE_LOOKBACK_HOURS,
    )

    try:
        # Run the blocking Alpaca call off the event loop. With the
        # trimmed lookback this is ~300-500ms, but `to_thread` keeps
        # other endpoints (status polling, autotrade UI) responsive
        # for the duration regardless.
        response = await asyncio.to_thread(
            client.fetch_stock_bars,
            AlpacaBarsRequest(symbols=symbols, timeframe="1Min", start=start, end=end),
        )
    except Exception as exc:
        logger.error("pulse_fetch_failed error=%s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch market data from Alpaca: {exc}",
        ) from exc
    finally:
        client.close()

    pulses: list[SymbolPulse] = []
    latest_bar_dt: datetime | None = None

    for symbol in symbols:
        raw_bars = response.get(symbol, [])
        pulse = _compute_pulse(symbol, raw_bars)
        if not pulse:
            # Always include the symbol — show a placeholder card
            logger.debug("pulse_no_data symbol=%s bars_returned=%d", symbol, len(raw_bars))
            pulse = SymbolPulse(
                symbol=symbol,
                price=0.0,
                change_pct=0.0,
                direction="flat",
                relative_volume=0.0,
                bar_time=end.isoformat(),
                sparkline=[],
                has_data=False,
            )
        pulses.append(pulse)
        try:
            bt = datetime.fromisoformat(pulse.bar_time)
            if latest_bar_dt is None or bt > latest_bar_dt:
                latest_bar_dt = bt
        except ValueError:
            pass

    # Market status based on actual US equity hours (ET).
    _et = ZoneInfo("America/New_York")
    now_et = datetime.now(_et)
    t = now_et.hour * 60 + now_et.minute  # minutes since midnight
    is_weekday = now_et.weekday() < 5

    if is_weekday and 570 <= t < 960:          # 9:30 AM – 4:00 PM
        market_open = True
        market_session = "open"
    elif is_weekday and 240 <= t < 570:        # 4:00 AM – 9:30 AM
        market_open = False
        market_session = "pre_market"
    elif is_weekday and 960 <= t < 1200:       # 4:00 PM – 8:00 PM
        market_open = False
        market_session = "after_hours"
    else:                                       # weekends + overnight
        market_open = False
        market_session = "closed"

    # Sort by price (cheapest first) so small-account users can scan
    # affordable names at a glance. No-data symbols are pushed to the end.
    pulses.sort(key=lambda p: (not p.has_data, p.price))

    no_data = [p.symbol for p in pulses if not p.has_data]
    if no_data:
        logger.warning("pulse_no_data_symbols symbols=%s", no_data)
    logger.info("pulse_complete total=%d with_data=%d no_data=%d session=%s",
                len(pulses), len(pulses) - len(no_data), len(no_data), market_session)

    result = MarketPulseResponse(
        as_of=end.isoformat(),
        market_open=market_open,
        market_session=market_session,
        pulses=pulses,
    )
    # Cache BEFORE the WebSocket emission so a concurrent request
    # arriving 1ms later sees the cached response (instead of duplicating
    # the Alpaca call).
    _pulse_cache_put(symbols, result)
    # Push to all WebSocket clients so they get fresh pulse data too.
    # Emission only happens on cache MISSES — cached responses skip this
    # path, avoiding the noise of re-broadcasting unchanged data.
    emit_market_pulse(result.model_dump())
    return result


# ── /market/chart TTL cache (2026-05-18, hot-fix) ────────────────────────────
#
# Why this exists: at ~09:33 ET on 2026-05-18 the dashboard fired 13 parallel
# /market/chart requests on every refresh (one per symbol on the "Today's
# Dynamic Stocks" strip). Each round-tripped to Alpaca for 1500–2900 1-min
# bars (multi-MB responses). Combined with the autotrade scan loop, the
# asyncio event loop got starved and /health stopped responding within 5s.
#
# Fix: cache the entire ChartResponse keyed on (symbol, timeframe) with a
# 60-second TTL. The dashboard's chart-polling cadence is ~30s; the autotrade
# scan loop's cadence is also ~25s. A 60s cache means a fresh fetch happens
# roughly once every two refreshes per symbol — load drops by ~50% in the
# steady state, and the "all 33 symbols at once" burst collapses to a single
# Alpaca call per cold cache key.
#
# This cache lives entirely in the HTTP endpoint — the autotrade scanner has
# its OWN bar-fetch path (``autotrade._fetch_bars``) and never goes through
# /market/chart. So a stale cache entry can never affect a trade decision.
# Frontend tolerates 60s staleness (the candles update once per minute anyway).
_CHART_CACHE_TTL_SECONDS = 60.0
_chart_cache: dict[tuple[str, str], tuple[float, "ChartResponse"]] = {}
_CHART_CACHE_MAX = 200  # 33 symbols × ~3 timeframes ≈ 100, doubled for safety

# ── /market/chart concurrent-fetch limiter (2026-05-19, second hot-fix) ──
#
# The 60s TTL cache above prevents repeated Alpaca calls for the same symbol,
# but doesn't help the COLD-START burst: when uvicorn restarts (cold cache)
# AND the dashboard refreshes (33 simultaneous chart requests), every cache
# miss spawns an Alpaca round-trip in a starlette thread-pool worker. 33
# concurrent sync HTTP calls + the autotrade scan loop = the asyncio event
# loop starves, /health stops responding, frontend banner fires.
#
# Fix: cap concurrent IN-FLIGHT Alpaca fetches from this endpoint to 5.
# Cache HITS don't acquire the semaphore (they return instantly), so the
# steady-state warm-cache case is unaffected. Only the cold burst — which
# is the actual problem — is serialized.
#
# Worst case: dashboard's first refresh after restart takes 7× longer
# (33 fetches / 5 workers ≈ 7 batches at ~300ms each = ~2s). That's an
# acceptable UX trade for not hanging uvicorn.
import threading as _threading
_CHART_FETCH_CONCURRENCY = 5
_chart_fetch_semaphore = _threading.BoundedSemaphore(_CHART_FETCH_CONCURRENCY)


def _chart_cache_get(symbol: str, timeframe: str) -> "ChartResponse | None":
    """Return the cached response if it's still within TTL, else None."""
    import time
    key = (symbol, timeframe)
    entry = _chart_cache.get(key)
    if entry is None:
        return None
    ts, resp = entry
    if time.time() - ts > _CHART_CACHE_TTL_SECONDS:
        # Stale → drop and force a fresh fetch.
        _chart_cache.pop(key, None)
        return None
    return resp


def _chart_cache_put(symbol: str, timeframe: str, resp: "ChartResponse") -> None:
    import time
    key = (symbol, timeframe)
    # Cheap LRU-ish bound: drop oldest when we exceed the cap.
    if len(_chart_cache) >= _CHART_CACHE_MAX:
        oldest_key = min(_chart_cache, key=lambda k: _chart_cache[k][0])
        _chart_cache.pop(oldest_key, None)
    _chart_cache[key] = (time.time(), resp)


@router.get("/chart/{symbol}", response_model=ChartResponse)
def get_chart(
    symbol: str,
    timeframe: str = "1Min",
    _: object = Depends(require_operator),
) -> ChartResponse:
    """Return OHLCV bars for a single symbol.

    Supports multiple intraday timeframes via the `timeframe` query param:
    - ``1Min``  — 1-minute bars, last 8 hours (default)
    - ``5Min``  — 5-minute bars, last 24 hours
    - ``15Min`` — 15-minute bars, last 48 hours

    Bars are returned with Unix-second timestamps for direct consumption by
    TradingView Lightweight Charts.

    2026-05-18: cached for 60s per (symbol, timeframe) — see _chart_cache.
    The autotrade scanner has its own bar fetch path and is unaffected.
    """
    symbol = symbol.upper()
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid symbol.")

    tf_entry = _TIMEFRAME_MAP.get(timeframe)
    if tf_entry is None:
        allowed = ", ".join(_TIMEFRAME_MAP.keys())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid timeframe '{timeframe}'. Allowed: {allowed}",
        )
    alpaca_tf, lookback_hours = tf_entry

    # ── TTL cache fast-path ─────────────────────────────────────────────
    # Note: the cache key uses ``alpaca_tf`` (the normalized form) so
    # "1Min" and "1min" share an entry. Cached responses are returned
    # before any Alpaca client / network work happens AND before the
    # concurrent-fetch semaphore is touched — so cache hits stay cheap
    # even under load.
    cached = _chart_cache_get(symbol, alpaca_tf)
    if cached is not None:
        logger.debug("chart_cache_hit symbol=%s timeframe=%s", symbol, alpaca_tf)
        return cached

    # Cold path: acquire the concurrent-fetch semaphore. Caps in-flight
    # Alpaca calls from this endpoint at _CHART_FETCH_CONCURRENCY. The
    # semaphore is on the THREAD pool side (starlette runs sync handlers
    # in threadpool workers), so blocking here parks the worker but
    # doesn't starve the asyncio event loop. Excess requests just queue.
    # Re-check the cache after acquiring — another thread may have
    # populated it while we were waiting.
    with _chart_fetch_semaphore:
        cached_after_wait = _chart_cache_get(symbol, alpaca_tf)
        if cached_after_wait is not None:
            logger.debug(
                "chart_cache_hit_after_wait symbol=%s timeframe=%s",
                symbol, alpaca_tf,
            )
            return cached_after_wait

        try:
            client = AlpacaMarketDataClient()
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

        end   = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(hours=lookback_hours)

        logger.info("chart_fetch symbol=%s timeframe=%s lookback_hours=%d", symbol, alpaca_tf, lookback_hours)
        try:
            response = client.fetch_stock_bars(
                AlpacaBarsRequest(symbols=[symbol], timeframe=alpaca_tf, start=start, end=end)
            )
        except Exception as exc:
            logger.error("chart_fetch_failed symbol=%s timeframe=%s error=%s", symbol, alpaca_tf, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to fetch chart data from Alpaca: {exc}",
            ) from exc
        finally:
            client.close()

    raw_bars = response.get(symbol, [])
    bars: list[CandleBar] = []
    for b in raw_bars:
        dt = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        bars.append(CandleBar(
            time=int(dt.timestamp()),
            open=round(float(b["o"]), 4),
            high=round(float(b["h"]), 4),
            low=round(float(b["l"]), 4),
            close=round(float(b["c"]), 4),
            volume=float(b["v"]),
        ))

    logger.info("chart_complete symbol=%s timeframe=%s bars=%d", symbol, alpaca_tf, len(bars))
    result = ChartResponse(symbol=symbol, timeframe=alpaca_tf, bars=bars)
    # Populate the cache so the next request within TTL is instant.
    _chart_cache_put(symbol, alpaca_tf, result)
    return result


@router.get("/ict/{symbol}", response_model=ICTZonesResponse)
def get_ict_zones(
    symbol: str,
    timeframe: str = "1Min",
    _: object = Depends(require_operator),
) -> ICTZonesResponse:
    """Return chart bars + full ICT zone geometry for overlay rendering.

    This is the single call the frontend makes to draw an ICT-aware
    chart: it receives the candles *and* the FVGs / OBs / swings /
    sweeps / MSS events / liquidity levels, plus the at-a-glance
    flags (killzone, premium/discount zone, OTE booleans) that power
    the indicator pills above the chart.

    Mechanics:
      1. Fetch intraday bars (same 120 h lookback as ``/chart``).
      2. Fetch daily bars and derive ``prev_close / prev_high / prev_low``
         so sweep detection has real liquidity magnets to test against.
         Daily fetch failures are non-fatal — we fall back to just the
         intraday-derived ``session_high / session_low`` levels.
      3. Delegate zone math to ``ict.compute_ict_features`` — the same
         function the live scanner uses, so the frontend sees exactly
         what the Strategy predicates saw.

    Zones reference BAR INDICES (``created_idx`` etc.) into the returned
    ``bars`` array. The frontend translates index → timestamp by
    looking up ``bars[idx].time`` — no timestamp duplication in the
    payload.
    """
    symbol = symbol.upper()
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid symbol.")

    tf_entry = _TIMEFRAME_MAP.get(timeframe)
    if tf_entry is None:
        allowed = ", ".join(_TIMEFRAME_MAP.keys())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid timeframe '{timeframe}'. Allowed: {allowed}",
        )
    alpaca_tf, lookback_hours = tf_entry

    try:
        client = AlpacaMarketDataClient()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    end           = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    intraday_start = end - timedelta(hours=lookback_hours)
    daily_start    = end - timedelta(days=7)  # last week is more than enough for prev-day levels

    # ── Fetch intraday + daily in parallel to keep the endpoint snappy.
    # The daily fetch is the one that can be slow / flaky; pooling
    # means a transient daily failure doesn't gate the intraday bars.
    def _fetch_intraday() -> list[dict]:
        resp = client.fetch_stock_bars(
            AlpacaBarsRequest(symbols=[symbol], timeframe=alpaca_tf,
                              start=intraday_start, end=end)
        )
        return resp.get(symbol, [])

    def _fetch_daily_ctx() -> dict:
        """Derive ``{prev_close, prev_high, prev_low}`` for today in ET.

        Returns an empty dict on any failure so the ICT features can
        still be computed from intraday-only levels (session H/L).
        """
        try:
            resp = client.fetch_stock_bars(
                AlpacaBarsRequest(symbols=[symbol], timeframe="1Day",
                                  start=daily_start, end=end)
            )
        except Exception as exc:
            logger.warning("ict_daily_fetch_failed symbol=%s error=%s", symbol, exc)
            return {}
        raw = resp.get(symbol, [])
        if len(raw) < 2:
            return {}

        # Prefer the most recent daily bar whose ET date is strictly
        # before today — during market hours Alpaca returns today's
        # still-forming bar at the tail, which would misreport "prev".
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        prev = None
        for b in reversed(raw):
            # raw bars from Alpaca use ISO "t" field; parse once.
            dt_utc = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            if dt_utc.astimezone(ZoneInfo("America/New_York")).date() < today_et:
                prev = b
                break
        if prev is None:
            return {}
        return {
            "prev_close": float(prev["c"]),
            "prev_high":  float(prev["h"]),
            "prev_low":   float(prev["l"]),
        }

    logger.info("ict_fetch symbol=%s timeframe=%s lookback_hours=%d", symbol, alpaca_tf, lookback_hours)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_intraday = pool.submit(_fetch_intraday)
        fut_daily    = pool.submit(_fetch_daily_ctx)

    try:
        raw_bars = fut_intraday.result()
    except Exception as exc:
        logger.error("ict_intraday_fetch_failed symbol=%s error=%s", symbol, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch intraday bars from Alpaca: {exc}",
        ) from exc

    try:
        daily_ctx = fut_daily.result()
    except Exception as exc:
        # Already logged inside _fetch_daily_ctx; re-assert empty so
        # the ICT pipeline proceeds without prev-day levels.
        logger.warning("ict_daily_ctx_unexpected_error symbol=%s error=%s", symbol, exc)
        daily_ctx = {}

    # Convert raw Alpaca bars to the scanner/ict shape (unix-second
    # timestamps in ``time``). ``convert_alpaca_bars`` is the canonical
    # translator shared with the autotrade path, so anything the ICT
    # detectors do here is byte-identical to what the scanner sees.
    bars_dicts = convert_alpaca_bars(raw_bars)

    # Build the TradingView-friendly ``CandleBar`` list for the wire.
    bars: list[CandleBar] = [
        CandleBar(
            time=int(b["time"]),
            open=round(float(b["open"]), 4),
            high=round(float(b["high"]), 4),
            low=round(float(b["low"]), 4),
            close=round(float(b["close"]), 4),
            volume=float(b["volume"]),
        )
        for b in bars_dicts
    ]

    # Compute ICT features — gracefully returns a "no-op" shape when
    # we have fewer than 30 bars (same contract the scanner relies on).
    try:
        features = compute_ict_features(bars_dicts, daily_ctx or None)
    except Exception as exc:
        logger.exception("ict_features_compute_failed symbol=%s", symbol)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ICT feature computation failed: {exc}",
        ) from exc

    logger.info(
        "ict_complete symbol=%s timeframe=%s bars=%d killzone=%s pd=%s fvgs=%d obs=%d sweeps=%d",
        symbol, alpaca_tf, len(bars),
        features.get("ict_killzone"),
        features.get("ict_pd_zone"),
        len((features.get("ict_zones") or {}).get("fvgs", [])),
        len((features.get("ict_zones") or {}).get("obs", [])),
        len((features.get("ict_zones") or {}).get("sweeps", [])),
    )

    return ICTZonesResponse(
        symbol=symbol,
        timeframe=alpaca_tf,
        bars=bars,
        killzone=features.get("ict_killzone"),
        pd_pct=features.get("ict_pd_pct"),
        pd_zone=features.get("ict_pd_zone"),
        in_ote_long=bool(features.get("ict_in_ote_long")),
        in_ote_short=bool(features.get("ict_in_ote_short")),
        in_bull_zone=bool(features.get("ict_in_bull_zone")),
        in_bear_zone=bool(features.get("ict_in_bear_zone")),
        dealing_range=features.get("ict_dealing_range"),
        last_sweep=features.get("ict_last_sweep"),
        last_mss=features.get("ict_last_mss"),
        nearest_bull_zone=features.get("ict_nearest_bull_zone"),
        nearest_bear_zone=features.get("ict_nearest_bear_zone"),
        zones=features.get("ict_zones") or {},
    )


@router.get("/analyze/{symbol}", response_model=AnalysisResponse)
def get_analysis(
    symbol: str,
    user: object = Depends(require_operator),
) -> AnalysisResponse:
    """Fetch intraday bars, compute technical indicators, and return an AI trade analysis.

    Calls the Claude API with a structured prompt containing EMAs, RSI, VWAP, volume
    trend, price structure, and the last 10 raw candles.  Returns a structured
    AnalysisResponse with trend, key levels, a trade recommendation, and reasoning.
    """
    symbol = symbol.upper()
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid symbol.")

    try:
        client = AlpacaMarketDataClient()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    end   = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=_LOOKBACK_HOURS)

    logger.info("analyze_fetch symbol=%s", symbol)
    try:
        response = client.fetch_stock_bars(
            AlpacaBarsRequest(symbols=[symbol], timeframe="1Min", start=start, end=end)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch market data: {exc}",
        ) from exc

    raw_bars = response.get(symbol, [])
    if len(raw_bars) < 20:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Not enough intraday data for {symbol} — market may be closed.",
        )

    bars = convert_alpaca_bars(raw_bars)

    # ── Fetch all supplementary data in parallel ──────────────────────────
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
    now_et = datetime.now(_ET)
    ext_start_et = (now_et - timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)
    ext_start = ext_start_et.astimezone(timezone.utc)
    daily_start = end - timedelta(days=90)
    sector_etfs = get_required_sector_etfs([symbol])
    all_snapshot_symbols = list(set([symbol] + sector_etfs))

    def _fetch_daily():
        resp = client.fetch_stock_bars(
            AlpacaBarsRequest(symbols=[symbol], timeframe="1Day", start=daily_start, end=end)
        )
        raw = resp.get(symbol, [])
        return convert_alpaca_bars(raw) if len(raw) >= 50 else None

    def _fetch_extended():
        resp = client.fetch_stock_bars(
            AlpacaBarsRequest(symbols=[symbol], timeframe="1Min", start=ext_start, end=end, feed="sip")
        )
        raw = resp.get(symbol, [])
        return convert_alpaca_bars(raw) if raw else None

    def _fetch_snapshots():
        return client.fetch_snapshots(all_snapshot_symbols)

    def _fetch_news():
        return client.fetch_news(symbols=[symbol], limit=10)

    daily_bars = None
    extended_bars = None
    snapshot = None
    sector_snapshots = None
    news_articles = None

    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_daily = pool.submit(_fetch_daily)
        fut_ext   = pool.submit(_fetch_extended)
        fut_snap  = pool.submit(_fetch_snapshots)
        fut_news  = pool.submit(_fetch_news)

    try:
        daily_bars = fut_daily.result()
        if daily_bars:
            logger.info("daily_bars_fetched symbol=%s count=%d", symbol, len(daily_bars))
    except Exception as exc:
        logger.warning("daily_bars_fetch_failed symbol=%s error=%s", symbol, exc)

    try:
        extended_bars = fut_ext.result()
        if extended_bars:
            logger.info("extended_bars_fetched symbol=%s count=%d", symbol, len(extended_bars))
    except Exception as exc:
        logger.warning("extended_bars_fetch_failed symbol=%s error=%s", symbol, exc)

    try:
        all_snapshots = fut_snap.result()
        snapshot = all_snapshots.get(symbol)
        sector_snapshots = {etf: all_snapshots[etf] for etf in sector_etfs if etf in all_snapshots}
        logger.info("snapshots_fetched symbol=%s sectors=%s", symbol, list(sector_snapshots.keys()))
    except Exception as exc:
        logger.warning("snapshots_fetch_failed symbol=%s error=%s", symbol, exc)

    try:
        news_articles = fut_news.result()
        if news_articles:
            logger.info("news_fetched symbol=%s articles=%d", symbol, len(news_articles))
    except Exception as exc:
        logger.warning("news_fetch_failed symbol=%s error=%s", symbol, exc)

    # NOTE: lessons_text + study_text were removed 2026-04-24 after the Tier-2
    # lift study found no measurable confidence lift from the ~500-1000 tokens
    # they added. Retrieval-based recall (only pull lessons where strategy +
    # direction + killzone match the current setup) is the planned replacement.

    try:
        result = analyze_chart(symbol, bars, daily_bars=daily_bars, extended_bars=extended_bars, snapshot=snapshot, sector_snapshots=sector_snapshots, news_articles=news_articles)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("analyze_failed symbol=%s error=%s", symbol, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Analysis failed: {exc}",
        ) from exc

    return AnalysisResponse(symbol=symbol, **result)


@router.get("/scan-strategies")
def scan_strategies(
    _: object = Depends(require_operator),
):
    """Scan all training-universe symbols for strategy matches (Tier 1 — no AI).

    Returns only symbols where at least one playbook strategy's conditions
    are met.  The frontend can then send the user to the full AI analysis
    for any matching symbol.  Runs in ~2–3 seconds total.
    """
    from ..scanner import scan_ict_symbols

    symbols = get_active_universe()
    try:
        client = AlpacaMarketDataClient()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    end   = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=_LOOKBACK_HOURS)

    logger.info("strategy_scan_fetch symbols=%d", len(symbols))
    try:
        response = client.fetch_stock_bars(
            AlpacaBarsRequest(symbols=symbols, timeframe="1Min", start=start, end=end)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch market data: {exc}",
        ) from exc

    # Convert Alpaca bars to the format compute_indicators expects
    bars_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        raw = response.get(sym, [])
        if len(raw) < 30:
            continue
        bars_by_symbol[sym] = convert_alpaca_bars(raw)

    hits = scan_ict_symbols(bars_by_symbol)
    logger.info("strategy_scan_complete hits=%d", len(hits))
    return {"scanned": len(bars_by_symbol), "hits": hits}


# ── Morning Universe Scanner (v7.13 Phase 2, 2026-05-01) ────────────────────
#
# The morning universe scanner runs once per trading day (~08:30 ET) and
# writes ``Backend/data/morning_universe_<date>.json`` — base + ranked
# dynamic candidates with reason chips. These two endpoints expose that
# snapshot to the dashboard:
#
#   GET  /market/morning-universe         → today's screener output
#   POST /market/morning-universe/apply   → snapshot-first apply pipeline
#
# Apply is MANUAL ONLY. There is no scheduler / auto-apply path; every
# trading-universe mutation requires a deliberate operator click that
# carries a session cookie (``require_current_user``). The plan reserves
# auto-apply for a 30-day-validated v2.

import json as _json
from datetime import date as _date, datetime as _datetime
from pathlib import Path as _Path
from typing import Any as _Any

from pydantic import BaseModel as _BaseModel, Field as _Field

from .. import autotrade as _autotrade_mod
from ..applied_universe import (
    ApplyError,
    apply_morning_universe,
)


_MORNING_DATA_DIR = _Path(__file__).resolve().parent.parent.parent / "data"
# Apply errors map to HTTP status codes. The 500 case captures the
# atomic-write failure path: snapshot couldn't be persisted, so
# runtime state was NEVER mutated — the operator should retry.
_APPLY_ERROR_HTTP = {
    "not_found":              status.HTTP_404_NOT_FOUND,
    "stale_date":             status.HTTP_400_BAD_REQUEST,
    "stale_age":              status.HTTP_400_BAD_REQUEST,
    "empty":                  status.HTTP_400_BAD_REQUEST,
    "snapshot_write_failed":  status.HTTP_500_INTERNAL_SERVER_ERROR,
}


class ApplyMorningUniverseRequest(_BaseModel):
    """Body for POST /market/morning-universe/apply.

    All fields optional; default behavior applies today's screener
    output if it exists and is fresh. ``snapshot_path`` lets the
    operator point at a specific file (e.g. one re-run with a custom
    pool). ``max_age_hours`` overrides the default freshness window
    when an operator deliberately wants to apply an older file.
    """
    snapshot_path: str | None = _Field(
        default=None,
        description=(
            "Override the screener-output path. Default: "
            "Backend/data/morning_universe_<today>.json"
        ),
    )
    max_age_hours: float | None = _Field(
        default=None,
        description=(
            "Reject the apply if the screener generated_at is older "
            "than this. Default: 12.0 hours."
        ),
        gt=0,
        le=48,
    )


def _read_morning_snapshot(today: _date) -> dict[str, _Any]:
    """Load today's screener JSON or raise 404. Helper for GET endpoint."""
    path = _MORNING_DATA_DIR / f"morning_universe_{today.isoformat()}.json"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No morning universe snapshot for {today.isoformat()}. "
                "Run `venv/bin/python -m scripts.build_morning_universe` "
                "from Backend/ to generate one."
            ),
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not read morning snapshot: {exc}",
        )


class BuildMorningUniverseRequest(_BaseModel):
    """Body for POST /market/morning-universe/build.

    All fields optional; default re-runs the screener with the same
    knobs the CLI uses (today's ET date, 82-symbol curated pool,
    n_dynamic=10, current SIP feed). The request shape mirrors the
    CLI flags so a future "advanced" UI can expose them, but the
    dashboard build button always sends ``{}``.
    """
    n_dynamic: int | None = _Field(
        default=None, ge=1, le=50,
        description="Override DEFAULT_N_DYNAMIC (10). Caps at 50 — anything bigger probably means a misconfiguration.",
    )
    min_pm_dollar_vol: float | None = _Field(
        default=None, ge=0,
        description="Override DEFAULT_MIN_PM_DOLLAR_VOL ($5M). Lower = more candidates surface but more noise.",
    )
    candidate_pool: list[str] | None = _Field(
        default=None, max_length=500,
        description="Override DEFAULT_CANDIDATE_POOL. None = curated 82-symbol pool.",
    )


from datetime import date as _date


@router.post("/morning-universe/build")
async def build_morning_universe_route(
    body: BuildMorningUniverseRequest,
    _: object = Depends(require_operator),
) -> dict[str, _Any]:
    """Run the morning-universe screener server-side. Returns when done.

    Wall time: ~2 minutes for the curated 82-symbol pool (Alpaca rate
    limits dominate). The handler runs ``build_morning_universe()`` in
    a worker thread via ``asyncio.to_thread`` so the FastAPI event
    loop stays responsive — other endpoints (status polling,
    autotrade) keep responding while this is in flight.

    On success the screener writes ``Backend/data/morning_universe_<today>.json``
    and returns the same payload the GET endpoint would surface.
    The dashboard button calls this then refetches the panel.

    Failure modes:
      * Alpaca credentials missing/rejected → 503 (bubbled up from
        ``AlpacaMarketDataClient.__init__``).
      * Network / timeout during screener run → 502.
      * Any other unexpected error → 500.

    Manual-only path (no scheduler). Each call is a deliberate
    operator click → an audit-friendly invocation. The runbook
    deliberately keeps auto-run off for the first 30 days.
    """
    # Late imports to mirror the apply route's pattern + avoid pulling
    # the screener into the module-level import graph (it pulls in
    # the entire analyzer / Alpaca client; not needed for routes that
    # don't touch it).
    from ..screener_morning import (
        DEFAULT_CANDIDATE_POOL as _MORNING_DEFAULT_POOL,
        DEFAULT_MIN_PM_DOLLAR_VOL as _MORNING_DEFAULT_MIN_PM,
        DEFAULT_N_DYNAMIC as _MORNING_DEFAULT_N_DYNAMIC,
        ScreenerConfig,
        build_morning_universe,
        write_snapshot_json,
    )
    from ..alpaca import AlpacaMarketDataClient

    today_et = _datetime.now(ZoneInfo("America/New_York")).date()
    base_symbols = list(settings.training_universe)
    candidate_pool = (
        [s.strip().upper() for s in body.candidate_pool if s and s.strip()]
        if body.candidate_pool
        else list(_MORNING_DEFAULT_POOL)
    )
    cfg = ScreenerConfig(
        n_dynamic=body.n_dynamic or _MORNING_DEFAULT_N_DYNAMIC,
        min_pm_dollar_vol=body.min_pm_dollar_vol or _MORNING_DEFAULT_MIN_PM,
    )

    # Construct the Alpaca client up-front so credential errors surface
    # as 503 BEFORE the long fetch starts. Otherwise the operator
    # would wait 30s for the first failed bar fetch to bubble up.
    try:
        client = AlpacaMarketDataClient()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Alpaca credentials missing or rejected: {exc}",
        )

    def _run_in_thread() -> dict[str, _Any]:
        """The blocking screener run, isolated to a worker thread.

        Returns the snapshot path (str) so the outer async handler
        can build the response without re-reading the file.
        """
        try:
            candidates, cfg_used = build_morning_universe(
                today_et,
                base_symbols=base_symbols,
                candidate_pool=candidate_pool,
                config=cfg,
                market_data_client=client,
            )
            path = write_snapshot_json(today_et, candidates, cfg_used, _MORNING_DATA_DIR)
            n_base = sum(1 for c in candidates if c.source == "base")
            n_dyn = sum(1 for c in candidates if c.source == "morning_dynamic")
            n_wl = sum(1 for c in candidates if c.source == "watchlist_only")
            return {
                "snapshot_path": str(path),
                "n_base":         n_base,
                "n_dynamic":      n_dyn,
                "n_watchlist":    n_wl,
                "feed":           cfg_used.feed,
            }
        finally:
            client.close()

    try:
        result = await asyncio.to_thread(_run_in_thread)
    except Exception as exc:
        logger.exception("morning_build_failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Morning universe build failed: {exc}",
        )

    logger.info(
        "morning_universe_built path=%s n_base=%d n_dynamic=%d n_watchlist=%d",
        result["snapshot_path"], result["n_base"], result["n_dynamic"], result["n_watchlist"],
    )
    return {
        "ok":             True,
        "snapshot_path":  result["snapshot_path"],
        "n_base":         result["n_base"],
        "n_dynamic":      result["n_dynamic"],
        "n_watchlist":    result["n_watchlist"],
        "feed":           result["feed"],
        "built_at":       _datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds"),
    }


@router.get("/morning-universe")
def get_morning_universe(
    _: object = Depends(require_operator),
) -> dict[str, _Any]:
    """Return today's morning-universe snapshot + apply state.

    Response shape::

      {
        "snapshot":         {... full screener output ...} | null,
        "snapshot_path":    "data/morning_universe_2026-05-01.json" | null,
        "snapshot_age_seconds": 3712 | null,
        "applied":          {...applied_universe_metadata...} | null,
        "active_universe":  ["AAPL","NVDA",...]  // CURRENT trading universe
      }

    The dashboard renders ``snapshot.morning_watchlist`` as the panel,
    uses ``snapshot.generated_at`` + ``snapshot_age_seconds`` for the
    "Data as of" header (review feedback #4), and shows the
    ``applied`` block (with ``restored_from_disk`` flag) so the
    operator can see the apply-event provenance at a glance.
    """
    today = _datetime.now(ZoneInfo("America/New_York")).date()
    path = _MORNING_DATA_DIR / f"morning_universe_{today.isoformat()}.json"
    snapshot: dict[str, _Any] | None = None
    snapshot_age_seconds: float | None = None
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                snapshot = _json.load(f)
            generated_at_raw = snapshot.get("generated_at") if snapshot else None
            if generated_at_raw:
                gen_at = _datetime.fromisoformat(generated_at_raw)
                if gen_at.tzinfo is None:
                    gen_at = gen_at.replace(tzinfo=ZoneInfo("America/New_York"))
                snapshot_age_seconds = (
                    _datetime.now(ZoneInfo("America/New_York")) - gen_at
                ).total_seconds()
        except Exception as exc:
            logger.warning("morning_universe_read_failed err=%s", exc)
            # Fall through with snapshot=None — UI shows "no snapshot."

    return {
        "snapshot":             snapshot,
        "snapshot_path":        str(path) if snapshot else None,
        "snapshot_age_seconds": snapshot_age_seconds,
        "applied":              _autotrade_mod.state.applied_universe_metadata,
        "active_universe":      _autotrade_mod.get_active_universe(),
    }


@router.post("/morning-universe/apply")
def apply_morning_universe_route(
    body: ApplyMorningUniverseRequest,
    user: object = Depends(require_operator),
) -> dict[str, _Any]:
    """Apply today's morning universe to the autotrade loop.

    Pipeline (review feedback #1 — atomic provenance):

      1. Load + validate the screener JSON (date == today,
         generated_at within ``max_age_hours``).
      2. Write ``applied_morning_universe_<date>_<time>.json``
         (audit-trail file). If THIS fails, runtime state is
         NEVER mutated.
      3. Mutate ``state.trading_universe`` and
         ``state.morning_universe_source_map``.
      4. Publish ``state.applied_universe_metadata`` so the
         status endpoint shows "Applied at ... from snapshot ..."
         to the operator.
      5. Return the apply receipt (review feedback #2 — UI gets
         the exact ``applied_snapshot_path``).

    Errors are mapped from ApplyError.code → HTTP status:
      * not_found             → 404
      * stale_date / stale_age / empty → 400
      * snapshot_write_failed → 500 (state untouched)

    Manual-only — no auto-apply scheduler. Caller must hold a
    valid session cookie (``require_current_user``).
    """
    snapshot_path = (
        _Path(body.snapshot_path) if body.snapshot_path else None
    )
    # Caller-supplied apply metadata so the audit file captures who
    # clicked Apply (the user-id from the session). Stays loosely
    # typed because the metadata field is intentionally open-ended.
    extra_metadata: dict[str, _Any] = {
        "applied_via": "POST /market/morning-universe/apply",
    }
    user_id = getattr(user, "id", None)
    if user_id is not None:
        extra_metadata["user_id"] = user_id

    def _set_metadata(md: dict) -> None:
        _autotrade_mod.state.applied_universe_metadata = md

    try:
        result = apply_morning_universe(
            set_universe_fn=_autotrade_mod.set_trading_universe,
            set_metadata_fn=_set_metadata,
            screener_snapshot_path=snapshot_path,
            max_age_hours=(
                body.max_age_hours if body.max_age_hours is not None else 12.0
            ),
            extra_metadata=extra_metadata,
        )
    except ApplyError as exc:
        http_status = _APPLY_ERROR_HTTP.get(
            exc.code, status.HTTP_400_BAD_REQUEST,
        )
        logger.warning(
            "apply_morning_universe_rejected code=%s msg=%s", exc.code, exc,
        )
        raise HTTPException(
            status_code=http_status,
            detail={"code": exc.code, "message": str(exc)},
        )

    return {
        "ok":                    True,
        "applied_snapshot_path": result["applied_snapshot_path"],
        "applied_at":            result["applied_at"],
        "trading_universe":      result["trading_universe"],
        "n_base":                result["n_base"],
        "n_dynamic":             result["n_dynamic"],
        "source_snapshot_path":  result["source_snapshot_path"],
    }


# ── Setup Watch (v7.15a, 2026-05-04) ───────────────────────────────────────
#
# Read-only diagnostic endpoint. For every symbol in the active
# trading universe, compute "why no setup" diagnostic codes from a
# fresh Alpaca bar fetch. Surfaces in the dashboard as the "Setup
# Watch" panel — operator can see at a glance which symbols are
# structurally near a match, which gapped past their levels, etc.
#
# **Strict separation from the predicate path:**
#   * This endpoint NEVER feeds the scanner.
#   * Diagnostic codes NEVER produce SimulatedTrade or TradeRecord rows.
#   * Computation is a pure function on the same `ind` dict
#     compute_indicators produces — no new thresholds, no new
#     features, no modification of any `_check` predicate.
#
# Caching: 30s TTL like /market/pulse. Dashboard polls this every
# ~30s; cached calls skip the bar fetch entirely.

import time as _time

_SETUP_WATCH_CACHE_TTL_SECONDS = 30.0
_SETUP_WATCH_LOOKBACK_HOURS = 4   # 4h is enough for VWAP / RSI / ATR /
                                   # ICT features (sweep, MSS within last
                                   # ~30 1-min bars). Cheap per call.
_setup_watch_cache: dict[str, _Any] = {"ts": 0.0, "payload": None}


@router.get("/setup-watch")
async def get_setup_watch(
    _: object = Depends(require_operator),
) -> dict[str, _Any]:
    """Per-symbol structural diagnostics for the active trading universe.

    Response shape::

      {
        "scanned_at":            ISO timestamp,
        "active_universe_size":  int,
        "diagnostics": [
          {
            "symbol": "QCOM",
            "codes": ["level_skipped_by_gap", "trend_day_candidate"],
            "summary": "Gap skipped prior-day extreme (gap +5.7%)",
            "gap_pct": 5.7, "distance_from_vwap_atr": 0.4,
            "vol_ratio": 1.6, "last_sweep_bars_ago": null,
            "last_mss_bars_ago": null, "in_killzone": true
          }, ...
        ],
        "summary_counts": {
          "setup_forming":          0,
          "swept_no_mss":           1,
          "asian_range_intact":     5,
          ...
        }
      }

    The dashboard reads ``diagnostics`` to render per-symbol rows
    (sorted by ``codes[0]`` priority — setup_forming first), and
    ``summary_counts`` for the aggregate header line ("0 forming,
    1 swept, 5 still in asian range...").
    """
    # ── Cache hit? ──
    now = _time.time()
    if (
        _setup_watch_cache["payload"] is not None
        and now - _setup_watch_cache["ts"] < _SETUP_WATCH_CACHE_TTL_SECONDS
    ):
        return _setup_watch_cache["payload"]

    # Late imports — same pattern as the morning-universe endpoints
    # (avoid pulling the analyzer / scanner dependency graph into
    # routes that don't use it).
    from ..analyzer import compute_indicators
    from ..ict import compute_ict_features
    from ..diagnostics_morning import diagnose_symbol
    from dataclasses import asdict
    from datetime import datetime, timedelta, timezone

    symbols = _autotrade_mod.get_active_universe()
    if not symbols:
        payload = {
            "scanned_at":           _datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds"),
            "active_universe_size": 0,
            "diagnostics":          [],
            "summary_counts":       {},
        }
        _setup_watch_cache.update({"ts": now, "payload": payload})
        return payload

    # ── Fetch bars (1Min × _SETUP_WATCH_LOOKBACK_HOURS) ──
    try:
        client = AlpacaMarketDataClient()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Alpaca credentials missing or rejected: {exc}",
        )

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=_SETUP_WATCH_LOOKBACK_HOURS)

    def _fetch_intraday() -> dict[str, list[dict]]:
        """Single batched intraday call. Returns symbol → bars."""
        try:
            resp = client.fetch_stock_bars(
                AlpacaBarsRequest(symbols=symbols, timeframe="1Min",
                                  start=start, end=end),
            )
            return {sym: convert_alpaca_bars(resp.get(sym, [])) for sym in symbols}
        except Exception as exc:
            logger.warning("setup_watch_intraday_fetch_failed err=%s", exc)
            return {sym: [] for sym in symbols}

    def _fetch_daily_ctx() -> dict[str, dict]:
        """Per-symbol daily context (prev_close/high/low). Single
        batched call over a 7-day window — picks up the most recent
        non-empty bar as 'previous day'."""
        try:
            d_end = end
            d_start = end - timedelta(days=10)
            resp = client.fetch_stock_bars(
                AlpacaBarsRequest(symbols=symbols, timeframe="1Day",
                                  start=d_start, end=d_end),
            )
            out: dict[str, dict] = {}
            for sym in symbols:
                bars = convert_alpaca_bars(resp.get(sym, []))
                if not bars:
                    out[sym] = {}
                    continue
                # Last non-empty bar = prev close
                last = bars[-1]
                out[sym] = {
                    "prev_close": float(last.get("close") or 0) or None,
                    "prev_high":  float(last.get("high") or 0) or None,
                    "prev_low":   float(last.get("low") or 0) or None,
                }
            return out
        except Exception as exc:
            logger.warning("setup_watch_daily_fetch_failed err=%s", exc)
            return {sym: {} for sym in symbols}

    # Run both fetches off the event loop so other endpoints stay
    # responsive during the 1-2s Alpaca round-trip.
    intraday = await asyncio.to_thread(_fetch_intraday)
    daily_ctx = await asyncio.to_thread(_fetch_daily_ctx)
    client.close()

    # ── Per-symbol diagnostic ──
    diagnostics_out: list[dict[str, _Any]] = []
    summary_counts: dict[str, int] = {}

    for sym in symbols:
        bars = intraday.get(sym, [])
        ctx = daily_ctx.get(sym, {})
        diag = None

        if len(bars) >= 15:
            try:
                ind = compute_indicators(
                    bars,
                    prev_close=ctx.get("prev_close"),
                    prev_high=ctx.get("prev_high"),
                    prev_low=ctx.get("prev_low"),
                )
                # ICT feature layer (sweep / MSS / killzone). Failure
                # here just produces a thinner ind — diagnostic still
                # works, just with fewer codes available.
                try:
                    ind.update(compute_ict_features(bars, ctx))
                except Exception:
                    logger.debug("setup_watch_ict_failed symbol=%s", sym)
                ind["symbol"] = sym
                diag = diagnose_symbol(sym, ind)
            except Exception as exc:
                logger.warning(
                    "setup_watch_diag_failed symbol=%s err=%s", sym, exc,
                )
                diag = None
        # Fall through with a minimal "no data" diagnostic so the UI
        # always shows the symbol (just without codes).
        if diag is None:
            from ..diagnostics_morning import SymbolDiagnostic
            diag = SymbolDiagnostic(symbol=sym)

        d_dict = asdict(diag)
        diagnostics_out.append(d_dict)
        for code in diag.codes:
            summary_counts[code] = summary_counts.get(code, 0) + 1

    # Sort by priority of the head code (matches diagnose_symbol's
    # internal sort: setup_forming first, etc.). Symbols with empty
    # codes sink to the bottom.
    from ..diagnostics_morning import _CODE_PRIORITY
    diagnostics_out.sort(key=lambda d: (
        _CODE_PRIORITY.get((d["codes"] or [""])[0], 999),
        d["symbol"],
    ))

    payload = {
        "scanned_at":            _datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds"),
        "active_universe_size":  len(symbols),
        "diagnostics":           diagnostics_out,
        "summary_counts":        summary_counts,
    }
    _setup_watch_cache.update({"ts": now, "payload": payload})
    return payload


# ── In-process scheduler endpoints (v7.15.3, 2026-05-06) ────────────────────
#
# Read-only inspection + manual-trigger endpoints for the morning-
# universe scheduler that lives in app/scheduler.py. Surfaces the
# next fire time, last-run state, total fires, and failure count so
# the operator can verify the schedule is healthy without tailing logs.

@router.get("/scheduler/morning-build")
def get_morning_build_scheduler_status(
    _: object = Depends(require_operator),
) -> dict[str, _Any]:
    """Return the in-process scheduler's current state.

    Response shape (all timestamps ISO 8601 with ET offset)::

      {
        "enabled":              true,
        "next_fire_at":         "2026-05-07T08:30:00-04:00",
        "last_fire_started_at": "2026-05-06T08:30:00-04:00",
        "last_fire_finished_at":"2026-05-06T08:32:14-04:00",
        "last_fire_status":     "success" | "failed" | "skipped_holiday" | null,
        "last_fire_error":      null | "<error message>",
        "last_fire_summary":    {n_base, n_dynamic, n_watchlist, snapshot_path, feed} | null,
        "fires_total":          int,
        "fires_failed":         int
      }
    """
    from .. import scheduler as _scheduler
    return _scheduler.get_status()


@router.post("/scheduler/morning-build/fire-now")
async def fire_morning_build_now(
    _: object = Depends(require_operator),
) -> dict[str, _Any]:
    """Manually trigger an immediate morning-universe build.
    Bypasses the 08:30 ET time gate. Used for smoke testing.

    Updates the same ``last_fire_*`` fields the scheduled fire does,
    so a manual trigger is indistinguishable from a scheduled one in
    the status payload (except by the timestamp).

    Returns the post-fire status snapshot.
    """
    from .. import scheduler as _scheduler
    await _scheduler.fire_now()
    return {"ok": True, "status": _scheduler.get_status()}
