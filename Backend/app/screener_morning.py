"""
Morning Universe Screener (v7.13 Phase 1, 2026-05-01)
=============================================================================
Builds a per-day list of candidate symbols at ~08:30 ET each trading
morning, before the regular session opens. The scanner runs the audited
v7.12.1 ICT predicates against this list, so the universe selection is
the dominant lever for "useful information per market day."

Two segments in the daily output:

  * ``base`` — the audited v7.12.1 23-symbol set. ALWAYS included,
    in audit order. Predicates have known per-(strategy × symbol)
    behavior on these names. Removing any of them would break the
    invariants in ``STABLE_SCANNER_CONTRACT.md``.

  * ``morning_dynamic`` — newly ranked names from a configurable
    candidate pool, scored on premarket gap %, news catalyst
    presence, sector ETF strength, and a premarket-volume floor.
    These are tagged so a 30-day audit can compare their per-trade
    edge against the base universe.

Why composition, not invention
------------------------------
The ICT scanner is the validated edge. The morning ranker doesn't try
to predict moves itself — it just surfaces names that have catalyst
energy AND respect the audited liquidity gates, so the predicates have
more chances to fire on names that meet their structural assumptions.

Composition order:
  1. Pull premarket bars (04:00–08:30 ET today) for every candidate
     in the pool, plus the relevant sector ETFs.
  2. Pull recent news (last ~24 hours) per candidate.
  3. Score each candidate via ``score_candidate`` (gap × news × sector
     × pm_volume gate).
  4. Apply liquidity gates (avg dollar volume floor).
  5. Pick top N by score.
  6. Concatenate base + dynamic, tag each with ``source``.

The ranker NEVER auto-applies — the result is just a JSON snapshot for
the operator to review and click through. The plan reserves
auto-apply for a 30-day-validated v2 (per ``cuddly-zooming-hoare.md``
v7.13 Phase 1 verification rules).
=============================================================================
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo

from .alpaca import (
    AlpacaBarsRequest,
    AlpacaMarketDataClient,
    convert_alpaca_bars,
)
from .analyzer import (
    compute_news_context,
    get_sector_etf,
)
from .settings import settings


logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


# ── Tunables ────────────────────────────────────────────────────────────────

# Minimum total premarket dollar volume to consider a candidate "liquid
# enough." A name with $100M+ trading by 08:30 has real participation;
# below ~$5M is mostly market-maker quotes. Below this floor we skip the
# candidate entirely (zero score).
DEFAULT_MIN_PM_DOLLAR_VOL = 5_000_000.0

# Second-tier liquidity gate: minimum 20-day avg DAILY volume.
# Catches names where today's premarket volume LOOKS adequate (e.g.
# squeeze on a thin-floater with no historical liquidity) but the
# stock can't be exited cleanly during regular hours. The friction
# audit (v7.10/v7.11) showed thin-tape names eat the post-friction
# edge — anything below ~5M shares/day average has unreliable fills
# at our position-size scale. ETFs are the natural exception (XLU /
# XLB / XLRE etc. trade ~3-4M/day but have institutional liquidity
# via creation/redemption); they're allowlisted via _LIQUIDITY_GATE_ETF_EXEMPT.
DEFAULT_MIN_AVG_DAILY_VOL = 5_000_000

# How many trading-day daily bars we want to compute the avg-daily-vol
# from. 20 is the standard for "20-day average volume" used across
# the analyzer. We fetch ~32 calendar days back to guarantee at least
# 20 trading days even with holidays.
_AVG_DAILY_VOL_LOOKBACK_DAYS = 20
_AVG_DAILY_VOL_CALENDAR_BUFFER = 32

# Sector / index ETFs that have institutional liquidity via creation/
# redemption arbitrage even when their on-screen daily volume is below
# the 5M floor. Without this allowlist, half the sector rotation plays
# would get filtered. (SPY / QQQ / IWM / XLK / XLF / XLV / XLE all
# trade well above 5M, but XLU / XLB / XLRE / XLC etc. don't always.)
_LIQUIDITY_GATE_ETF_EXEMPT = frozenset({
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLY", "XLI", "XLU", "XLP", "XLB",
    "XLE", "XLF", "XLV", "XLC", "XLRE",
})

# How many morning_dynamic candidates to surface beyond the base set.
# Plan says ~10-15 morning gappers / RVOL leaders / catalyst names is
# the sweet spot — enough variety, not so many that the autotrade loop
# gets diluted across illiquid setups.
DEFAULT_N_DYNAMIC = 10

# Maximum bar staleness when checking premarket activity. If the latest
# bar is older than this, the candidate's signal is too stale to trust
# (probably an Alpaca feed delay or a halted symbol).
_MAX_BAR_AGE_MINUTES = 30

# News lookback for catalyst detection. 24h covers overnight earnings,
# pre-market press releases, and prior-day late-day catalysts that
# carry overnight.
_NEWS_LOOKBACK_HOURS = 24
_NEWS_PER_SYMBOL_LIMIT = 5

# Premarket window. We expand the start to 04:00 ET (Alpaca's earliest
# extended-hours bar) and end at 09:30 ET (regular open). The scanner
# is intended to RUN at ~08:30 ET, but we still pull through 09:30 ET
# in case it runs later (e.g. operator manually triggers at 09:15).
_PM_START_ET = time(4, 0)
_PM_END_ET = time(9, 30)

# Score-component weights. Total max ≈ 2.5 (gap=1, news=0.5,
# sector=1). Keeping the gap component as the dominant signal because
# it has the most direct mechanical link to predicate firing — a stock
# gapping into a prior-day-low or sweeping the asian range is exactly
# what Multi-Day PD / Power-of-3 / Asian-Range-Breakout were audited
# on. News and sector are confirming context, not primary drivers.
_W_GAP = 1.0       # gap component peaks at 5% gap
_W_NEWS = 0.5      # binary boost when a catalyst keyword is present
_W_SECTOR = 1.0    # sector ETF gap component peaks at 2%

# Caps for normalization. Gap > 5% is rare enough on liquid large-caps
# (post-earnings, M&A) that we cap; same for sector ETFs (a 2% sector
# move is already a major day).
_GAP_CAP = 5.0
_SECTOR_GAP_CAP = 2.0

# Default candidate pool. Liquid US large-caps + sector ETFs, dedup'd
# against the v7.12.1 audited base. If you want to expand, override
# via ``MORNING_CANDIDATE_POOL`` in ``.env`` (comma-separated). Names
# below were chosen for: avg daily volume > 5M shares, bid-ask spread
# typically < $0.05, traded on a major US exchange. NO biotech, no
# penny stocks, no single-product-binary-event names.
DEFAULT_CANDIDATE_POOL: list[str] = [
    # Mega-cap tech (META not in v7.12.1 base — was excluded post-2025
    # due to extreme catalyst volatility; re-introducing as dynamic
    # candidate with the audit gate to catch it if it actually helps).
    "META", "AVGO", "NFLX", "CSCO", "ORCL",
    # Semis (NVDA/AMD/INTC/SOXL in base; rest are liquid large-caps)
    "AMAT", "LRCX", "KLAC", "MU", "QCOM", "TXN",
    # Software / SaaS
    "SHOP", "SNOW", "DDOG", "NET", "CRWD", "PANW", "ZS", "MDB",
    # Financials (JPM/MS/BAC/SOFI in base; these are large peers)
    "GS", "WFC", "C", "AXP", "BLK", "SCHW", "V", "MA",
    # Consumer / staples
    "COST", "WMT", "TGT", "MCD", "NKE", "SBUX", "DIS",
    "KO", "PEP", "PG", "LULU",
    # Healthcare / biotech-large (UNH/XLV in base)
    "LLY", "JNJ", "PFE", "ABBV", "MRK", "TMO", "ABT",
    # Energy (XLE in base)
    "XOM", "CVX", "COP", "OXY", "EOG",
    # Industrials / defense / transport
    "BA", "CAT", "GE", "HON", "LMT", "RTX", "NOC",
    # Communications
    "CMCSA", "T", "VZ", "EA",
    # High-vol momentum names (often catalyst-driven)
    "COIN", "RBLX", "ABNB", "UBER", "LYFT", "RIVN", "LCID", "MARA", "RIOT",
    # Sector / index ETFs (XLF/XLE/XLV in base)
    "XLK", "XLY", "XLI", "XLU", "XLP", "XLB", "XLC", "XLRE",
    "SPY", "QQQ", "IWM", "DIA",
]


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class MorningCandidate:
    """One symbol in the day's morning universe.

    ``source`` distinguishes the audited base from newly-ranked
    candidates and pure watchlist entries. The 30-day audit (per the
    plan) groups trades by ``source`` to test whether morning_dynamic
    names contributed usable edge before promoting any of them to the
    base. ``watchlist_only`` candidates pass the gates but are below
    the trading-universe top-N cap — informational, never traded.
    """
    symbol: str
    source: Literal["base", "morning_dynamic", "watchlist_only"]
    rank: int | None = None              # 1-indexed within scored set; None for base
    score: float | None = None           # Composite score; None for base (always-on)
    # When True, this candidate is included in the trading universe
    # at apply time. Mirrors the ``source`` enum (morning_dynamic →
    # True; watchlist_only → False; base → True implicitly). Surfaced
    # explicitly so the UI / API consumers don't have to reason about
    # the enum.
    in_trading_universe: bool = True
    # Component signals — None when the input wasn't available.
    gap_pct: float | None = None
    pm_dollar_vol: float | None = None
    avg_daily_volume: int | None = None  # 20-day trailing avg, in shares
    has_news_catalyst: bool = False
    catalyst_keywords: list[str] = field(default_factory=list)
    sector_etf: str | None = None
    sector_etf_gap_pct: float | None = None
    # Human-readable reason chips for the dashboard panel ("gap +4.2%",
    # "earnings", "XLK +1.1%", "pm $235M"). One per signal that
    # crossed its display threshold.
    reason_tags: list[str] = field(default_factory=list)


@dataclass
class ScreenerConfig:
    """Tunable parameters for one screener run.

    Captured in the JSON snapshot so future-you can tell what knobs
    produced today's universe — important for the 30-day audit, where
    we compare cells across different config eras.
    """
    n_dynamic: int = DEFAULT_N_DYNAMIC
    min_pm_dollar_vol: float = DEFAULT_MIN_PM_DOLLAR_VOL
    min_avg_daily_volume: int = DEFAULT_MIN_AVG_DAILY_VOL
    candidate_pool_size: int = 0  # filled in by build_morning_universe
    feed: str = "sip"


# ── Internal helpers ────────────────────────────────────────────────────────

def _utc_window_for_premarket(today_et: date) -> tuple[datetime, datetime]:
    """Return (utc_start, utc_end) covering the premarket window we want.

    Pulls 04:00 ET (Alpaca's earliest extended bar) through 09:30 ET
    of the requested ET date. Caller converts to UTC because Alpaca's
    bar API works in UTC.

    NOTE: Alpaca's 1Min ``end`` parameter is inclusive — a window
    ending at 09:30:00 ET will include the 09:30:00 bar (regular-
    session OPEN minute). That's why we layer ``_drop_rth_bars`` over
    every ``_premarket_metrics`` call. Don't try to "fix" this by
    setting end=09:29:59 — Alpaca rounds to the bar boundary and
    returns the same set; the runtime filter is the correct fix.
    """
    start_et = datetime.combine(today_et, _PM_START_ET, tzinfo=_ET)
    end_et = datetime.combine(today_et, _PM_END_ET, tzinfo=_ET)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def _premarket_cutoff_unix(today_et: date) -> int:
    """Unix-seconds timestamp for 09:30:00 ET on the requested date.

    Bars with ``time >= cutoff_unix`` are regular-session bars and
    must be dropped from premarket calculations. Returns an int (the
    bar timestamps in our converted format are int unix seconds, so
    int comparison is faster and avoids a tz-roundtrip per bar).
    """
    rth_open_et = datetime.combine(today_et, time(9, 30), tzinfo=_ET)
    return int(rth_open_et.astimezone(timezone.utc).timestamp())


def _drop_rth_bars(bars: list[dict], today_et: date) -> list[dict]:
    """Defensive filter — strip any bar at-or-after 09:30 ET today.

    Alpaca's 1Min bars end-parameter is inclusive, so a request for
    04:00→09:30 ET returns the 09:30:00 ET regular-session opening
    bar in the response. Including that bar in premarket-gap
    calculations conflates "where the premarket settled" with "where
    the stock opened," which can be a 1%+ swing on volatile names.
    Always run this filter before computing anything.

    Also protects against the operator running the scanner LATE (e.g.
    at 11:00 ET) — without the filter, an 11:00 invocation would pull
    1.5 hours of regular-session bars and treat them as premarket.
    """
    if not bars:
        return bars
    cutoff = _premarket_cutoff_unix(today_et)
    out: list[dict] = []
    for b in bars:
        ts = b.get("time")
        # Drop bars with no timestamp (malformed) AND any bar at-or-
        # after the 09:30 ET cutoff. The two checks are separate so a
        # missing-time bar isn't silently treated as "before cutoff."
        if ts is None:
            continue
        try:
            if int(ts) < cutoff:
                out.append(b)
        except (TypeError, ValueError):
            continue
    return out


def _utc_window_for_daily_history(today_et: date) -> tuple[datetime, datetime]:
    """Window for the trailing daily-bar history.

    Pulls ``_AVG_DAILY_VOL_CALENDAR_BUFFER`` calendar days back from
    today_et (~32 days, enough to guarantee 20 trading days even
    with holidays). The last bar in the response = prev close; the
    last 20 bars = the volume window we average over. Single fetch
    serves both needs.
    """
    start_et = datetime.combine(
        today_et - timedelta(days=_AVG_DAILY_VOL_CALENDAR_BUFFER),
        time(0, 0), tzinfo=_ET,
    )
    end_et = datetime.combine(today_et, time(0, 0), tzinfo=_ET)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def _resolve_prev_close(daily_bars: list[dict]) -> float | None:
    """The close of the most recent bar that's strictly BEFORE today."""
    if not daily_bars:
        return None
    # ``time`` is unix seconds (UTC). Use the LAST bar — Alpaca returns
    # them in chronological order. Caller already filters by date range
    # ending before today's open, so the last bar IS prev close.
    last = daily_bars[-1]
    val = last.get("close") if "close" in last else last.get("c")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_avg_daily_volume(daily_bars: list[dict]) -> int | None:
    """20-day trailing average daily volume in shares.

    Uses the LAST ``_AVG_DAILY_VOL_LOOKBACK_DAYS`` bars from the
    response (which is sorted chronologically). Returns None when we
    don't have enough bars to compute a meaningful average — better
    to fail open and let the caller decide than to gate a candidate
    on a 5-day average that's noisy by construction.
    """
    if not daily_bars or len(daily_bars) < 5:
        return None
    window = daily_bars[-_AVG_DAILY_VOL_LOOKBACK_DAYS:]
    vols: list[int] = []
    for b in window:
        v = b.get("volume") if "volume" in b else b.get("v")
        try:
            if v is not None:
                vols.append(int(v))
        except (TypeError, ValueError):
            continue
    if not vols:
        return None
    return int(sum(vols) / len(vols))


def _premarket_metrics(
    bars: list[dict], prev_close: float | None,
) -> tuple[float | None, float, float | None]:
    """Compute (gap_pct, pm_dollar_vol, last_premarket_price).

    ``gap_pct`` = (last_premarket_close − prev_close) / prev_close × 100.
    Uses the LAST premarket bar (closest to 09:30 ET) so the value
    reflects where the stock is sitting going into the open, not where
    it opened in extended hours overnight.

    ``pm_dollar_vol`` is the raw premarket-volume × VWAP-ish proxy
    (we approximate dollar volume per bar as ``volume * close``,
    summed). This is the liquidity gate input.

    Returns ``(None, 0.0, None)`` when bars are empty.
    """
    if not bars:
        return None, 0.0, None

    pm_dollar_vol = 0.0
    last_close: float | None = None
    for b in bars:
        close = b.get("close") if "close" in b else b.get("c")
        vol = b.get("volume") if "volume" in b else b.get("v")
        try:
            if close is not None and vol is not None:
                pm_dollar_vol += float(close) * float(vol)
                last_close = float(close)
        except (TypeError, ValueError):
            continue

    gap_pct: float | None = None
    if prev_close and last_close is not None and prev_close > 0:
        gap_pct = (last_close - prev_close) / prev_close * 100.0

    return gap_pct, pm_dollar_vol, last_close


def _has_recent_catalyst(news_articles: list[dict], symbol: str) -> tuple[bool, list[str]]:
    """Reuse ``compute_news_context`` to detect catalyst keywords.

    Returns (catalyst_present, list_of_keywords). The list lets the
    UI label WHICH catalyst drove the boost ("earnings", "FDA",
    "guidance"), not just that one was present.
    """
    if not news_articles:
        return False, []
    ctx = compute_news_context(news_articles, symbol)
    if not ctx:
        return False, []
    catalysts = ctx.get("catalysts") or []
    return bool(catalysts), list(catalysts)


# ── Scoring ────────────────────────────────────────────────────────────────

def score_candidate(
    *,
    gap_pct: float | None,
    has_news_catalyst: bool,
    sector_etf_gap_pct: float | None,
    pm_dollar_vol: float,
    min_pm_dollar_vol: float,
) -> tuple[float, list[str]]:
    """Composite score + reason tags for one candidate.

    Returns (score, reason_tags). Score ≈ 0..2.5 across components
    (max gap=1, news=0.5, sector=1). The liquidity gate is BINARY —
    candidates below ``min_pm_dollar_vol`` get score=0 and an empty
    reason list (won't surface). This keeps illiquid names out of
    the morning_dynamic segment regardless of how exciting the gap
    looks; thin tape kills the audited friction edge.

    Reason tags are short human-readable chips for the UI:
      "gap +4.2%", "earnings", "XLK +1.1%", "pm $vol $87M".
    Only generated when the underlying signal is meaningful enough
    to display (not the floor case, not <1% gaps, etc.).
    """
    if pm_dollar_vol < min_pm_dollar_vol:
        return 0.0, []

    score = 0.0
    tags: list[str] = []

    # Gap component (the dominant signal — direct mechanical link to
    # predicate firing on prior-day breaks / asian-range / multi-day-PD).
    if gap_pct is not None:
        gap_norm = min(abs(gap_pct), _GAP_CAP) / _GAP_CAP   # 0..1
        score += _W_GAP * gap_norm
        if abs(gap_pct) >= 1.0:
            tags.append(f"gap {gap_pct:+.1f}%")

    # News catalyst (binary boost — the keyword set in
    # _CATALYST_KEYWORDS is the audited set used by the live scanner).
    if has_news_catalyst:
        score += _W_NEWS
        # Caller adds the catalyst-keyword tag itself — it has the
        # full list and can pick the most informative keyword.

    # Sector strength (confirming context — a stock gapping with its
    # sector ETF gapping the same direction is more likely to follow
    # through than one fighting its sector).
    if sector_etf_gap_pct is not None:
        sec_norm = min(abs(sector_etf_gap_pct), _SECTOR_GAP_CAP) / _SECTOR_GAP_CAP
        score += _W_SECTOR * sec_norm
        if abs(sector_etf_gap_pct) >= 0.3:
            # Caller appends the ETF symbol — it knows which sector
            # this candidate maps to.
            tags.append(f"sector {sector_etf_gap_pct:+.2f}%")

    # Liquidity tag (informational — already gated above, but useful
    # to display so the operator can see at a glance which names had
    # heavy premarket participation).
    if pm_dollar_vol >= 50_000_000:
        tags.append(f"pm ${pm_dollar_vol/1_000_000:.0f}M")

    return score, tags


# ── Main entry point ────────────────────────────────────────────────────────

def build_morning_universe(
    today_et: date,
    *,
    base_symbols: Iterable[str],
    candidate_pool: Iterable[str] | None = None,
    config: ScreenerConfig | None = None,
    market_data_client: AlpacaMarketDataClient | None = None,
) -> tuple[list[MorningCandidate], ScreenerConfig]:
    """Build today's morning universe.

    Returns ``(candidates, config_used)`` where ``candidates`` is the
    base set in audit order, followed by the top-N morning_dynamic
    ranked candidates. ``config_used`` is the resolved config (with
    candidate_pool_size filled in, feed value captured, etc.) so the
    caller can serialize a complete provenance record.

    The ``today_et`` parameter is intentionally not defaulted — the
    caller (CLI script or scheduler) makes the date explicit, which
    keeps the function deterministic and testable.
    """
    cfg = config or ScreenerConfig()
    cfg.feed = settings.alpaca_data_feed or "sip"

    base_list = [s.upper() for s in base_symbols]
    # Dedupe candidate pool against base — no point ranking a name
    # that's already always-on. Preserves caller's order so the
    # default pool's "tech, financials, consumer, ..." grouping is
    # respected when there are score ties later.
    raw_pool = list(candidate_pool) if candidate_pool is not None else list(DEFAULT_CANDIDATE_POOL)
    seen: set[str] = set(base_list)
    pool: list[str] = []
    for s in raw_pool:
        u = s.upper().strip()
        if u and u not in seen:
            pool.append(u)
            seen.add(u)
    cfg.candidate_pool_size = len(pool)

    own_client = market_data_client is None
    client = market_data_client or AlpacaMarketDataClient()

    try:
        # ── Fetch premarket bars for every candidate + their sector ETFs ────
        # Sector ETFs: dedupe across the pool so a single XLK fetch
        # covers all the tech names.
        sector_etfs = sorted({get_sector_etf(s) for s in pool})
        all_symbols_to_fetch = list(set(pool) | set(sector_etfs))

        pm_utc_start, pm_utc_end = _utc_window_for_premarket(today_et)
        # Single daily-bar fetch serves both prev-close (last bar) and
        # 20-day-avg-volume (last 20 bars) — saves API calls.
        daily_utc_start, daily_utc_end = _utc_window_for_daily_history(today_et)

        # Premarket bars (1-min). Alpaca's bars endpoint takes one symbol
        # per request in our wrapper; loop sequentially. Failure on one
        # symbol shouldn't kill the whole run.
        pm_bars: dict[str, list[dict]] = {}
        for sym in all_symbols_to_fetch:
            try:
                resp = client.fetch_stock_bars(AlpacaBarsRequest(
                    symbols=[sym],
                    timeframe="1Min",
                    start=pm_utc_start,
                    end=pm_utc_end,
                    feed=cfg.feed,
                ))
                pm_bars[sym] = convert_alpaca_bars(resp.get(sym, []))
            except Exception as exc:
                logger.warning("morning_pm_fetch_failed symbol=%s err=%s", sym, exc)
                pm_bars[sym] = []

        # Trailing daily history — feeds BOTH prev_close (last bar) and
        # avg_daily_volume (last 20 bars). One fetch per symbol.
        prev_closes: dict[str, float | None] = {}
        avg_daily_vols: dict[str, int | None] = {}
        for sym in all_symbols_to_fetch:
            try:
                resp = client.fetch_stock_bars(AlpacaBarsRequest(
                    symbols=[sym],
                    timeframe="1Day",
                    start=daily_utc_start,
                    end=daily_utc_end,
                    feed=cfg.feed,
                ))
                daily_bars = convert_alpaca_bars(resp.get(sym, []))
                prev_closes[sym] = _resolve_prev_close(daily_bars)
                avg_daily_vols[sym] = _resolve_avg_daily_volume(daily_bars)
            except Exception as exc:
                logger.warning("morning_daily_fetch_failed symbol=%s err=%s", sym, exc)
                prev_closes[sym] = None
                avg_daily_vols[sym] = None

        # News (per pool symbol — sector ETFs don't get news lookups).
        news_lookback_start = (
            datetime.now(timezone.utc) - timedelta(hours=_NEWS_LOOKBACK_HOURS)
        )
        news_by_symbol: dict[str, list[dict]] = {}
        for sym in pool:
            try:
                # fetch_news takes a list, we ask one symbol at a time
                # so an error on one doesn't blank the rest.
                articles = client.fetch_news(
                    [sym], limit=_NEWS_PER_SYMBOL_LIMIT,
                    start=news_lookback_start,
                )
                news_by_symbol[sym] = articles or []
            except Exception as exc:
                logger.warning("morning_news_fetch_failed symbol=%s err=%s", sym, exc)
                news_by_symbol[sym] = []

        # ── Sector ETF gaps (used as confirming context) ────
        # _drop_rth_bars protects against Alpaca's inclusive end-time
        # behavior (09:30:00 ET regular-session bar gets returned in
        # a 04:00→09:30 ET request). See ``_drop_rth_bars`` docstring.
        sector_gaps: dict[str, float | None] = {}
        for etf in sector_etfs:
            gap_pct, _, _ = _premarket_metrics(
                _drop_rth_bars(pm_bars.get(etf, []), today_et),
                prev_closes.get(etf),
            )
            sector_gaps[etf] = gap_pct

        # ── Score every candidate (with avg-daily-volume gate) ────
        scored: list[MorningCandidate] = []
        for sym in pool:
            adv = avg_daily_vols.get(sym)
            # Tier-2 liquidity gate: skip names with thin AVERAGE
            # daily volume (regardless of today's PM activity). ETFs
            # in _LIQUIDITY_GATE_ETF_EXEMPT bypass this floor — their
            # institutional liquidity comes from create/redeem flow
            # and isn't visible in daily on-screen volume.
            if (
                sym not in _LIQUIDITY_GATE_ETF_EXEMPT
                and (adv is None or adv < cfg.min_avg_daily_volume)
            ):
                logger.debug(
                    "morning_skip_thin_adv symbol=%s adv=%s floor=%d",
                    sym, adv, cfg.min_avg_daily_volume,
                )
                continue

            gap_pct, pm_dollar_vol, _ = _premarket_metrics(
                _drop_rth_bars(pm_bars.get(sym, []), today_et),
                prev_closes.get(sym),
            )
            has_news, catalyst_kws = _has_recent_catalyst(
                news_by_symbol.get(sym, []), sym,
            )
            etf = get_sector_etf(sym)
            sector_gap = sector_gaps.get(etf)

            score, tags = score_candidate(
                gap_pct=gap_pct,
                has_news_catalyst=has_news,
                sector_etf_gap_pct=sector_gap,
                pm_dollar_vol=pm_dollar_vol,
                min_pm_dollar_vol=cfg.min_pm_dollar_vol,
            )
            # If we surfaced a sector tag, prefix it with the ETF
            # symbol so the UI shows which sector this is.
            tags = [
                f"{etf} {t.split(' ', 1)[1]}" if t.startswith("sector ") else t
                for t in tags
            ]
            # Add the catalyst keyword tag (caller knows the keywords).
            if has_news and catalyst_kws:
                # Pick the most informative single keyword for the
                # chip (operator sees the others by hovering).
                tags.insert(0, catalyst_kws[0])

            scored.append(MorningCandidate(
                symbol=sym,
                source="morning_dynamic",  # may downgrade to watchlist_only below
                score=round(score, 3),
                gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
                pm_dollar_vol=round(pm_dollar_vol, 2),
                avg_daily_volume=adv,
                has_news_catalyst=has_news,
                catalyst_keywords=catalyst_kws,
                sector_etf=etf,
                sector_etf_gap_pct=round(sector_gap, 2) if sector_gap is not None else None,
                reason_tags=tags,
            ))

        # Drop zero-scores (failed PM-dollar-vol gate inside score_candidate)
        # AND drop candidates whose signals all came in below the
        # "interesting" thresholds (gap < 1%, no news, sector < 0.3%,
        # pm_vol < $50M). Filtering on ``reason_tags`` is equivalent
        # to "at least one signal crossed its display threshold."
        scored = [
            c for c in scored
            if c.score and c.score > 0.0 and len(c.reason_tags) > 0
        ]
        scored.sort(key=lambda c: c.score or 0.0, reverse=True)

        # ── Watchlist vs trading universe split (review feedback #6) ──
        # ALL gated candidates land in the watchlist (informational
        # for the dashboard panel). Only the top N get source
        # ``morning_dynamic`` and ``in_trading_universe=True`` —
        # those are the ones the Apply button actually sends to the
        # autotrade loop. Below the cap = ``watchlist_only``,
        # surfaced in UI but never traded.
        for i, c in enumerate(scored, start=1):
            c.rank = i
            if i <= cfg.n_dynamic:
                c.source = "morning_dynamic"
                c.in_trading_universe = True
            else:
                c.source = "watchlist_only"
                c.in_trading_universe = False

        # ── Compose final list: base first (audit order), then watchlist ────
        out: list[MorningCandidate] = []
        for s in base_list:
            out.append(MorningCandidate(symbol=s, source="base"))
        out.extend(scored)
        return out, cfg
    finally:
        if own_client:
            client.close()


# ── Serialization ──────────────────────────────────────────────────────────

def candidates_to_dict(
    today_et: date,
    candidates: list[MorningCandidate],
    cfg: ScreenerConfig,
) -> dict[str, Any]:
    """JSON-safe snapshot of the day's universe + provenance.

    Schema (stable across runs — the 30-day audit script depends on
    these field names):

    {
      "date":               "2026-05-01",
      "generated_at":       "2026-05-01T08:30:14-04:00",
      "feed":               "sip",
      "base":               [ {symbol, source: "base"} ... 23 entries ],
      "morning_watchlist":  [
          # ALL gated candidates, ranked. Each has in_trading_universe
          # so the UI can style applied vs watchlist-only differently.
          {symbol, source: "morning_dynamic"|"watchlist_only", rank,
           in_trading_universe, score, gap_pct, ...},
          ...
      ],
      "trading_universe":   [ <23 base symbols> + <top-N from watchlist> ],
      "config":             { n_dynamic, min_pm_dollar_vol,
                              min_avg_daily_volume, candidate_pool_size,
                              feed }
    }

    ``trading_universe`` is denormalized for convenience — it's just
    the symbols that get sent to the existing
    ``POST /trading/autotrade/universe`` endpoint when the operator
    clicks Apply. The split between base / morning_watchlist (with
    ``source`` tags on each candidate) is the audit-query primitive.
    """
    base = [asdict(c) for c in candidates if c.source == "base"]
    watchlist = [
        asdict(c) for c in candidates
        if c.source in ("morning_dynamic", "watchlist_only")
    ]
    base_symbols = [c["symbol"] for c in base]
    dynamic_symbols = [
        c["symbol"] for c in watchlist if c.get("in_trading_universe")
    ]
    return {
        "date":             today_et.isoformat(),
        "generated_at":     datetime.now(_ET).isoformat(timespec="seconds"),
        "feed":             cfg.feed,
        "base":             base,
        "morning_watchlist": watchlist,
        "trading_universe": base_symbols + dynamic_symbols,
        "config":           asdict(cfg),
    }


def write_snapshot_json(
    today_et: date,
    candidates: list[MorningCandidate],
    cfg: ScreenerConfig,
    out_dir: Path,
) -> Path:
    """Write the snapshot to ``out_dir/morning_universe_<date>.json``.

    Uses indent=2 + sorted keys so the diff between consecutive days
    is human-readable (one symbol move = small diff, not a reordered
    blob).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"morning_universe_{today_et.isoformat()}.json"
    payload = candidates_to_dict(today_et, candidates, cfg)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False, default=str)
    applied = sum(
        1 for c in payload["morning_watchlist"] if c.get("in_trading_universe")
    )
    logger.info(
        "morning_snapshot_written path=%s base=%d watchlist=%d applied=%d",
        out_path,
        len(payload["base"]),
        len(payload["morning_watchlist"]),
        applied,
    )
    return out_path
