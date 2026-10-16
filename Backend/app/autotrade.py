"""
AI Auto-Trader — background loop that scans, analyzes, and trades.
=============================================================================
Tier 1:   Fast Python strategy pre-screen across all symbols (~2-3s)
Tier 1.5: Haiku 4.5 setup screener — yes/no gate on each Tier-1 match
          before spending on the expensive Opus call (feature #11).
Tier 2:   Claude Opus deep analysis with extended thinking on survivors
Tier 3: Immediate user approval — notify user and wait for approve/reject.
        Approvals expire after _APPROVAL_TIMEOUT_SECONDS and are removed;
        the next scan will re-issue them if the setup still qualifies.
Tier 4: Execute trade only after user approves (with slippage + sector checks)
Tier 5: Position monitor — trailing stop + time-based exit management

The loop is managed via start/stop and exposes status for the frontend.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import time
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .alpaca import (
    AlpacaBarsRequest,
    AlpacaMarketDataClient,
    AlpacaTradingClient,
    OrderSide,
    OrderType,
    TimeInForce,
    convert_alpaca_bars,
    get_shared_trading_client,
)
from .analyzer import (
    analyze_chart,
    analysis_cache,
    get_required_sector_etfs,
    get_sector_etf,
    _ETF_NAMES,
    compute_sector_context,
    _snapshot_change_pct,
)
from .calibration import get_multiplier as _get_calibration_multiplier
from .regime import (
    RegimeSnapshot,
    SUPPRESSION_THRESHOLD as _REGIME_SUPPRESS_THRESHOLD,
    classify as _classify_regime,
    get_weight as _get_regime_weight,
    is_regime_vetoed as _is_regime_vetoed,
    snapshot_dict as _regime_snapshot_dict,
)
from .rel_strength import (
    RS_DISABLED_STRATEGIES,
    get_multiplier as _rs_multiplier,
)
from .hour_weights import (
    get_multiplier as _hour_multiplier,
)
from .news_classifier import (
    NewsVerdict,
    classify_news as _classify_news,
    news_multiplier as _news_multiplier,
    verdict_dict as _news_verdict_dict,
)
from .haiku_screener import (
    ScreenerVerdict,
    screen_setup as _haiku_screen_setup,
)
from .scanner import scan_ict_symbols
from .scanner_experimental import scan_experimental_symbols
from .settings import settings
from . import trading_mode
from .lessons import check_and_generate_lesson
# NOTE: get_recent_lessons_for_prompt / get_recent_insights_for_prompt were
# removed from the Tier-2 prompt on 2026-04-24 (lift study showed no signal
# gain for the ~500-1000 tokens they added). Still imported by routers/market.py
# for the manual /analyze endpoint — remove here to avoid dead imports.
from .simulator import (
    log_simulated_trade,
    check_simulated_exits,
    get_open_simulated_symbols,
    mark_simulated_approval,
)
from .trade_history import save_trade_opened, update_trade_closed
from . import brier
from .ws import emit_autotrade_status, emit_trade_event, emit_monitor_event, emit_wallet_update

logger = logging.getLogger("tradehack.autotrade")

_LOOKBACK_HOURS = 8
_CONFIRMATION_SECONDS = 120  # 2 minutes
_APPROVAL_TIMEOUT_SECONDS = 120  # expire + remove after this; next scan can re-issue
_ET = ZoneInfo("America/New_York")

# v7.14a — friend-test-cohort dossier output dir. The exporter writes
# one JSON per closed trade here when ``settings.export_trades=True``;
# the central operator box has the flag off so this dir stays empty.
# Resolved relative to the Backend/ root via the same pattern other
# data paths use (e.g., ``app/applied_universe.py::_DEFAULT_OUT_DIR``).
from pathlib import Path as _Path
_TRADE_EXPORT_DIR = _Path(__file__).resolve().parent.parent / "data" / "trade_exports"


# ── Scan-cycle hang diagnostic (2026-05-20) ──────────────────────────────────
#
# Four post-match hangs (MSFT 05-18, SOXL+ORCL 05-19 AM, JPM 05-19 close) all
# froze uvicorn the instant a Tier-1 match needed processing — and /health
# stopped responding too, which means the asyncio EVENT LOOP itself is being
# blocked by a synchronous call somewhere in the post-match pipeline.
# ``asyncio.wait_for`` can't catch that (its timeout callback is scheduled on
# the very loop that's blocked). ``faulthandler`` can: it runs on a separate
# C-level timer thread, so ``dump_traceback_later`` fires and writes every
# thread's stack EVEN WHEN the event loop is fully frozen.
#
# The scan loop arms ``dump_traceback_later`` before each ``_scan_and_trade``
# and cancels it after. A cycle that exceeds the budget triggers a full
# thread-stack dump to ``~/.tradehack/hang_dumps/<date>.txt`` — the dump
# pinpoints the exact frozen line. Pure diagnostic: no behavior change, the
# process is NOT killed (``exit=False``).
_HANG_DUMP_DIR = _Path.home() / ".tradehack" / "hang_dumps"
# A normal cycle is ~3s; the slowest legitimate cycle (cold caches, many
# candidates) is well under a minute. 120s is comfortably above any healthy
# cycle and well below "the operator notices uvicorn is dead".
_SCAN_CYCLE_HANG_BUDGET_SECONDS = 120
_hang_dump_file = None  # lazily-opened append-mode handle, kept open process-life


def _hang_dump_target():
    """Return the open hang-dump file handle (lazily created). faulthandler
    needs a real file object that stays open at dump time."""
    global _hang_dump_file
    if _hang_dump_file is None:
        _HANG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        path = _HANG_DUMP_DIR / f"scan_hang_{datetime.now(_ET):%Y-%m-%d}.txt"
        _hang_dump_file = open(path, "a", encoding="utf-8")
    return _hang_dump_file


def _build_context_safe(
    hit: dict | None,
    regime_meta: dict | None,
    news_meta: dict | None,
    applied_universe: dict | None,
) -> str | None:
    """Best-effort wrapper around ``trade_exporter.build_context_at_firing``.

    The builder is bounded + JSON-serialized; passing it junk shouldn't
    raise, but as a v7.14a safety per refinement #3 (telemetry only)
    we wrap any unexpected failure in a try/except so the open path
    NEVER breaks because of a context-capture bug. None on failure or
    when every input is empty — TradeRecord.context_at_firing stays
    NULL, the per-trade dossier later carries a partial dossier.
    """
    try:
        from . import trade_exporter as _te
        return _te.build_context_at_firing(
            indicators_snapshot=(hit or {}).get("indicators_snapshot"),
            regime_snapshot=regime_meta,
            news_verdict=news_meta,
            applied_universe=applied_universe,
        )
    except Exception as exc:
        logger.warning("context_at_firing_build_failed err=%s", exc)
        return None

# Statuses that count as "actually executed" against the daily trade limit
_EXECUTED_STATUSES = {"submitted", "filled", "accepted", "new",
                      "partially_filled", "pending_new"}
# Terminal post-execution statuses (2026-07-06 P1 fix). A trade that ran its
# full lifecycle must KEEP counting against the per-day caps: every close set
# status="closed" (within <=60 min via trail/max-hold/EOD), silently dropping
# the trade from the "daily" tally — so the autonomous cap re-armed all day
# and an unattended run could fire far beyond AUTONOMOUS_MAX_TRADES_PER_DAY.
# All three statuses imply the entry DID execute; never-executed trades
# ("failed", "rejected") remain uncounted.
_TERMINAL_EXECUTED_STATUSES = {"closed", "close_failed", "close_unconfirmed"}
_DAILY_COUNTED_STATUSES = _EXECUTED_STATUSES | _TERMINAL_EXECUTED_STATUSES

# Daily-context cache for Tier-1 strategy pre-screen. Prior-day levels
# (prev_close / prev_high / prev_low) are constant within a single
# trading day, so we fetch the daily-bars batch once per ET date and
# reuse it across every scan cycle. Gap-and-Go and Prior-Day-Break
# strategies self-skip when this map is empty, so a failed refresh
# degrades gracefully instead of taking the whole scan down.
_daily_ctx_cache: dict[str, dict] = {}
_daily_ctx_cache_date: date | None = None

# Regime cache (feature #8). SPY's daily bars change only once per
# session — classify once per ET date and reuse across every scan.
# On failure we leave the cache empty and get_weight() returns 1.0
# for every strategy, which is the correct degraded behavior: regime
# detection turns into a no-op instead of silently nuking the playbook.
_regime_snapshot: RegimeSnapshot | None = None
_regime_cache_date: date | None = None

# News-classifier cache (feature #9). Keyed by (symbol, articles_hash) so
# repeat scans against the same news batch reuse the same Claude verdict
# — typical scan interval is 20–150s, news drifts much slower. Entries
# are evicted lazily on TTL expiry and capped at a loose bound so the
# dict doesn't grow unboundedly across a trading day.
_news_cache: dict[tuple[str, str], tuple[float, NewsVerdict]] = {}
_NEWS_CACHE_TTL_SECONDS = 900.0     # 15 minutes
_NEWS_CACHE_MAX_ENTRIES = 200

# Brier calibration cache (feature #10 — feedback loop). A
# ``CalibrationSummary`` computed from the last 30 days of graded trades,
# re-scored once per ET day. The scan loop reads this on every candidate
# to derive a per-decile multiplier from the reliability diagram. On a
# refresh failure (or pre-start), the cache stays None and the multiplier
# lookup degrades to 1.0 via ``brier.calibration_multiplier``'s no-summary
# branch — same pattern as regime + news when data is missing.
_calibration_summary: brier.CalibrationSummary | None = None
_calibration_cache_date: date | None = None


@dataclass
class TradeLog:
    """Record of an auto-executed trade."""
    time: str
    symbol: str
    side: str
    qty: int
    order_type: str
    confidence: float                     # FINAL confidence used for the gate + sizing (post-calibration)
    setup: str
    status: str
    order_id: str | None = None
    error: str | None = None
    db_record_id: int | None = None   # TradeRecord.id for persistence
    # Confirmation tracking
    prediction_price: float | None = None
    confirmation_price: float | None = None
    price_move_pct: float | None = None
    # Approval tracking
    entry_zone: str | None = None
    stop_loss: str | None = None
    target: str | None = None
    risk_reward: str | None = None
    reasoning: str | None = None
    analysis_context: str | None = None
    # Post-execution monitoring (Tier 6)
    exit_price: float | None = None
    exit_reason: str | None = None        # trailing_stop | max_hold_time | eod_exit | manual
    trailing_stop_final: float | None = None
    highest_price_reached: float | None = None
    hold_duration_seconds: int | None = None
    # Simulator tracking
    sim_trade_id: int | None = None       # SimulatedTrade.id for linking
    # Per-(strategy, symbol) calibration (feature #6) — surfaced in approval UI
    matched_strategies: str | None = None        # comma-separated PLAYBOOK names from Tier-1
    raw_confidence: float | None = None           # Claude's unadjusted confidence
    calibration_multiplier: float | None = None   # multiplier applied to raw_confidence
    calibration_reason: str | None = None         # "cell" | "strategy_fallback" | "no_data"
    calibration_n: int | None = None              # sample size backing the multiplier
    calibration_detail: dict | None = None        # full per-strategy breakdown for UI
    # Market regime (#8) — surfaced in approval UI so the user can see e.g.
    # "Mean Reversion dampened 30% in trending_up regime" next to the adj confidence.
    regime: str | None = None                     # trending_up | trending_down | choppy | unknown
    regime_label: str | None = None               # human-readable summary (e.g. "Trending Up · vol 14%")
    regime_multiplier: float | None = None        # mean weight applied to raw_confidence for this trade
    regime_detail: dict | None = None             # per-strategy breakdown (weight + suppressed flag)
    # News catalyst classifier (#9) — "is this a thesis-breaking catalyst?" from
    # the Haiku classifier. Directionalized: bullish news on a short is a
    # contradicting catalyst (damp), bullish news on a long is confirming (boost).
    news_stance: str | None = None                # confirms | contradicts | mixed | none
    news_impact: str | None = None                # bullish | bearish | mixed | none
    news_severity: str | None = None              # low | medium | high
    news_label: str | None = None                 # short phrase for the UI
    news_multiplier: float | None = None          # the multiplier applied to raw_confidence
    news_detail: dict | None = None               # per-article breakdown (headline + reason)
    # Structured probabilities (#10) — Claude's P(target hit before stop),
    # P(stop hit before target), P(neither within holding window). Persisted
    # at open so Brier score + log-loss can be computed after the trade
    # closes. outcome_class is filled in by brier.classify_outcome when the
    # monitor marks the trade closed. Nullable because legacy responses
    # that only emit ``confidence`` keep p_neither = 0 (handled by the
    # analyzer's Pydantic validator).
    p_target: float | None = None
    p_stop: float | None = None
    p_neither: float | None = None
    outcome_class: str | None = None              # "target_hit" | "stop_hit" | "neither"
    # Brier-calibration feedback loop (#10) — multiplier applied to
    # raw_confidence based on how Claude's predicted p_target bucket has
    # empirically played out over the last 30 days. 1.0 means "no
    # adjustment" (low sample, missing bucket, or well-calibrated).
    brier_multiplier: float | None = None
    brier_detail: dict | None = None              # {reason, bucket, predicted, empirical, n, raw_ratio}
    # Haiku pre-screen (#11) — Tier-1.5 yes/no gate before Opus fires.
    # ``haiku_screen_valid`` is always True on approvals that reach the
    # user (rejects never become approvals). ``haiku_screen_reason`` is
    # Haiku's one-line rationale, surfaced on the approval card as an
    # evidence chip so the user sees WHY Haiku let this through. None
    # when the screener was unreachable (fail-open — trade flowed to
    # Opus anyway).
    haiku_screen_valid: bool | None = None
    haiku_screen_reason: str | None = None
    # Relative Strength / Relative Weakness (#12) — soft multiplier that
    # composes with regime / calibration / news / brier. Boosts a trade
    # when the stock is genuinely leading (long) or lagging (short) vs
    # SPY, dampens when it's fighting the tape. None when no sector
    # snapshot was available (degraded-no-op path, multiplier stored
    # as 1.0 separately on ``rs_multiplier``). Surfaced on the approval
    # card as an "📈 Leader · +1.2% vs SPY · ×1.20" chip.
    rs_state: str | None = None                   # strong_leader | leader | ... | unknown
    rs_label: str | None = None                   # short human-readable ("Leader · +1.2% vs SPY")
    rs_stock_vs_spy: float | None = None          # stock's % excess return vs SPY since open
    rs_spy_change_pct: float | None = None        # SPY's own change (disambiguates alpha from beta)
    rs_multiplier: float | None = None
    rs_detail: dict | None = None                 # full meta dict for tooltip
    # Hour-of-day multiplier (#12.5) — per-strategy weight for the ET
    # hour the trade fires in. Only strategies with evidence-backed
    # hour edges populate cells; everything else passes through at 1.0
    # (see ``hour_weights.py``). Surfaced on the approval card so the
    # user sees e.g. "11 ET · ×1.15 (Gap Fill Short hour-11 edge)".
    hour_et: int | None = None                    # 0–23, ET hour at firing time
    hour_multiplier: float | None = None          # mean per-strategy hour weight applied
    hour_detail: dict | None = None               # full meta dict (per-strategy breakdown)
    # Universe source tag (v7.13 Phase 1.5, 2026-05-01) — was this
    # symbol part of the always-on audited base, or did it come in via
    # the morning universe scanner? Drives the 30-day audit query
    # ("did dynamic symbols help or hurt vs the audited base?"). Set
    # at TradeLog construction time via ``get_universe_source(symbol)``.
    # Possible values:
    #   "base"            — in TRAINING_UNIVERSE (audited 23)
    #   "morning_dynamic" — applied via the morning scanner today
    #   "unknown"         — symbol present in autotrade universe but
    #                       not tagged (e.g. legacy override, manual
    #                       add). Treat as data-quality issue at audit.
    universe_source: str | None = None
    # v7.15 (2026-05-18) — which scanner produced the hit that became
    # this TradeLog. Mirrors ``TradeRecord.scanner_id``; needed in-
    # memory so the daily-cap path can count "today's experimental
    # trades" separately from "today's ICT trades" without a DB
    # round-trip every scan. Default "ict" so any legacy path that
    # constructs a TradeLog without setting it lands in the ICT
    # bucket (matches v7.14's column default).
    scanner_id: str = "ict"
    # 2026-05-28 audit-bug fix — scan-time context dossier captured at
    # TradeLog construction (when ``hit``, ``regime_meta``, ``news_meta``
    # are all in scope inside ``_scan_and_trade``). Previously the
    # builder was called from ``_execute_trade``, where those variables
    # no longer exist — that raised ``NameError: name 'hit' is not
    # defined``, the outer try/except caught it, and the trade landed
    # on Alpaca without a TradeRecord row (today's ADBE trade). Storing
    # the JSON string on TradeLog lets it flow trade_log → PendingApproval
    # → _execute_trade → save_trade_opened without re-scoping ``hit``.
    context_at_firing: str | None = None


@dataclass
class PendingApproval:
    """A trade waiting for user approval. Expires + is removed after the timeout;
    if the setup still qualifies, the next scan re-issues a fresh approval."""
    id: str
    trade_log: TradeLog
    rec: dict
    direction: str
    created: str
    expires: str
    # Position sizing breakdown for frontend display
    sizing: dict | None = None


@dataclass
class MonitoredPosition:
    """A position opened by the auto-trader, actively monitored for exit."""
    symbol: str
    direction: str                      # "long" or "short"
    entry_price: float
    qty: int
    order_id: str | None
    opened_at: str                      # ISO timestamp
    trade_log: TradeLog                 # reference back to the trade log entry

    # Trailing stop state
    initial_stop: float | None = None   # AI's original stop loss
    trailing_stop: float | None = None  # current trailing stop price (ratchets)
    highest_price: float | None = None  # high-water mark since entry (longs)
    lowest_price: float | None = None   # low-water mark since entry (shorts)

    # Exit tracking
    current_price: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None      # trailing_stop | max_hold_time | eod_exit | external
    exited_at: str | None = None
    closing: bool = False               # close request in flight; prevents duplicate submissions
    close_submitted: bool = False       # broker ACCEPTED a close — never resubmit; reconcile read-only
    close_submitted_at: str | None = None
    close_order_id: str | None = None   # close ORDER id from Alpaca — exact-fill lookup (telemetry)
    pending_close_reason: str | None = None   # reason/price captured at submission, used when a
    pending_close_price: float | None = None  # later reconcile cycle confirms the fill
    closed: bool = False


@dataclass
class AutoTradeState:
    """Mutable singleton holding the auto-trader's runtime state."""
    running: bool = False
    last_scan: str | None = None
    symbols_scanned: int = 0
    strategies_matched: int = 0
    trades_today: list[TradeLog] = field(default_factory=list)
    scan_count: int = 0
    # v7.16.x (2026-05-21): per-ET-day scan counter. ``scan_count`` above
    # is cumulative for the uvicorn PROCESS lifetime — it only resets on a
    # uvicorn restart or the trade-gated reset in ``start()``, so the
    # dashboard badge legitimately reads e.g. 366 mid-morning after a
    # multi-day-uptime process. ``scan_count_today`` rolls over at the ET
    # date boundary so the UI can show a "today only" figure next to the
    # cumulative one. ``scan_count_today_et_date`` is the ET date string
    # the counter currently belongs to; when a scan sees a different ET
    # date, the counter resets.
    scan_count_today: int = 0
    scan_count_today_et_date: str | None = None
    last_error: str | None = None
    last_scan_latest_bar_et: str | None = None
    last_scan_hit_count: int = 0
    last_scan_matched_symbol_count: int = 0
    last_scan_matched_setup_count: int = 0
    last_scan_forming_symbol_count: int = 0
    last_scan_forming_setup_count: int = 0
    last_scan_matched_symbols: list[str] = field(default_factory=list)
    last_scan_forming_symbols: list[str] = field(default_factory=list)
    confirming: list[dict] = field(default_factory=list)
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    monitored_positions: list[MonitoredPosition] = field(default_factory=list)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _monitor_task: asyncio.Task | None = field(default=None, repr=False)
    circuit_breaker_tripped: bool = False
    active_user_id: int | None = None  # set on start for trade persistence

    # ── Haiku gate observability (#11) ──
    # Per-cycle counters reset at the top of every scan. Surfaced in
    # ``get_status()`` so the dashboard can plot ``haiku_none_rate`` and
    # ``haiku_reject_rate``. Operator targets:
    #   * none_rate > 5%   → Haiku is unreachable / parse-failing too
    #     often. The fail-open pass-through is masking an infra issue.
    #   * reject_rate < 50% → the gate isn't earning its keep; either the
    #     prompt is too lenient or Tier-1 is already filtering well.
    haiku_calls: int = 0          # attempts this cycle (where we actually called Haiku)
    haiku_none_count: int = 0     # verdict came back None (timeout/parse/no-key)
    haiku_reject_count: int = 0   # verdict came back valid=False

    # ── Config (can be changed at runtime) ──
    scan_interval_seconds: int = 60
    # Gate derived empirically from the Tier-2 lift study
    # (scripts/tier2_lift_sample.py + tier2_lift_report.py, 290 trades):
    #   conf ≥0.65 → 30 takes, 23% win, -0.495 mean_R  (the old 0.65 bar was bad)
    #   conf ≥0.70 →  4 takes, 75% win, +0.905 mean_R  ← real signal lives here
    #   conf ≥0.80 →  0 takes                          (Sonnet tops out at 0.72)
    # Raise tighter only once we have >30 trades in the ≥0.70 bucket to confirm.
    min_confidence: float = 0.70
    max_open_trades: int = 3
    # v7.14.2 (2026-05-04): bumped 10 → 999 to align live with the
    # 6m / 18m backtests (which never applied a per-day cap). The
    # v7.12.1 audited baseline of 126 trades / 125 days = ~1/day
    # average; the busiest single day in the 6m run was well under
    # 10, but the 10-cap was a vestigial early-development guardrail
    # from when Layer 2 (Sonnet) could rapid-fire approvals. With
    # Layer 2 disabled and Layer 1 firing only on rare structural
    # setups, the cap was mostly never biting — except on busy days
    # (earnings clusters, big news), where it artificially silenced
    # legitimate audit-aligned signals. 999 is "effectively
    # unlimited" while keeping the safety-hatch field available; a
    # runaway scanner with a code bug stops at 999 instead of going
    # truly unbounded. To re-cap at runtime, pass
    # {"max_trades_per_day": <n>} in the POST /trading/autotrade/start body.
    max_trades_per_day: int = 999
    confirmation_seconds: int = _CONFIRMATION_SECONDS
    max_daily_loss: float = 10000.00
    approval_timeout_seconds: int = _APPROVAL_TIMEOUT_SECONDS
    position_size_pct: float = 0.05   # 5% of portfolio per trade (RISK budget)
    # v7.16.x (2026-06-15): hard cap on each position's NOTIONAL as a fraction
    # of equity, so one entry can't consume all buying power and starve later
    # concurrent matches. With max_open_trades=3, 30% each leaves ~10% headroom.
    # Pure capital allocation — the strategy's R-edge is size-independent, so
    # this scales dollar P&L, not the validated edge. Only ever reduces qty.
    max_position_equity_pct: float = 0.30
    # Trailing stop + time exit config
    trailing_stop_pct: float = 0.005       # 0.5% trailing distance
    max_hold_minutes: int = 60             # close after 60 min
    eod_buffer_minutes: int = 15           # close 15 min before 4 PM ET
    monitor_interval_seconds: int = 10     # price check frequency
    enable_trailing_stop: bool = True
    enable_time_exit: bool = True
    max_per_sector: int = 2           # max positions in the same sector
    # Trading universe override (None = use settings.training_universe)
    trading_universe: list[str] | None = None

    # ── Layer 2 AI gate switch (v7.12.1, 2026-05-01) ──
    # When False, the autotrade loop trades Layer-1 (predicate scanner)
    # matches alone — no Haiku pre-screen, no Sonnet analyze_chart.
    # The Haiku NEWS classifier still runs (independent of the screen
    # gate, feeds news_multiplier downstream). Stops/targets are
    # synthesized via _build_predicate_only_result() with a fixed
    # 0.7%-stop / 1.5R-target rule. Flip ``ENABLE_AI_GATE=true`` in
    # ``.env`` to restore Sonnet + Haiku screening (default seeded
    # from settings.enable_ai_gate at module import time).
    enable_ai_gate: bool = field(default_factory=lambda: settings.enable_ai_gate)

    # ── Autonomous mode (v7.16.x, 2026-05-21) ──
    # When True, a matched setup that clears every gate is auto-approved
    # (→ auto-executed → auto-managed by the monitor loop) instead of
    # creating an approval card that waits for a manual dashboard click.
    # Runtime-toggleable via ``set_autonomous_mode()`` / the dashboard
    # "Autonomous" switch — no restart needed. Seeded at import time
    # from ``settings.autonomous_trading_enabled`` (the .env boot
    # default, normally False). Bounded by
    # ``settings.autonomous_max_trades_per_day`` so an unattended run
    # can't fire unlimited trades.
    autonomous_mode: bool = field(
        default_factory=lambda: settings.autonomous_trading_enabled
    )

    # ── Morning universe source map (v7.13 Phase 1.5, 2026-05-01) ──
    # When the operator clicks "Apply today's universe" from the
    # dashboard, the morning scanner snapshot ships a {symbol: source}
    # map that gets stored here. The autotrade loop reads it to stamp
    # the source tag on every TradeLog. None until first apply (the
    # default-base case where every symbol is implicitly ``base``).
    # Cleared when the universe is reset to default.
    morning_universe_source_map: dict[str, str] | None = None

    # ── Apply-event metadata (v7.13 Phase 2, 2026-05-01) ──
    # Operator-action provenance for the currently-applied morning
    # universe. Populated by ``apply_morning_universe`` after both
    # the apply snapshot (file) and the runtime state mutation
    # succeed; cleared when the universe is reset to default.
    # ``restored_from_disk=True`` distinguishes "operator clicked
    # Apply this session" from "lifespan restored last apply on
    # startup" — surfaced in the status endpoint so the dashboard
    # can show "Restored at restart, originally applied at 10:15"
    # rather than letting the operator wonder why the universe is
    # larger than base. None when running on the default base.
    # Schema:
    #   {
    #     "applied_at":            ISO timestamp (ET)
    #     "applied_snapshot_path": absolute path to the apply file
    #     "source_snapshot_path":  pointer to the screener output
    #                              that produced this apply
    #     "restored_from_disk":    bool
    #     "trading_universe_size": int
    #     "n_dynamic":             count of morning_dynamic symbols
    #   }
    applied_universe_metadata: dict | None = None


# Global singleton
state = AutoTradeState()

_approval_counter = 0


def get_active_universe() -> list[str]:
    """Return the active trading universe (runtime override or settings default)."""
    return state.trading_universe if state.trading_universe else list(settings.training_universe)


def set_trading_universe(
    symbols: list[str] | None,
    source_map: dict[str, str] | None = None,
):
    """Update the runtime trading universe. None resets to the .env default.

    ``source_map`` (v7.13 Phase 1.5) optionally tags each symbol with
    its provenance — currently ``"base"`` or ``"morning_dynamic"``,
    set by the morning scanner's Apply path. None on a regular
    universe override means "no source info" — the autotrade loop
    falls back to inferring source from settings.training_universe
    membership in ``get_universe_source``.

    When ``symbols`` is None (reset to default), the source map is
    cleared too — keeping it would lie about provenance for symbols
    that returned to the audited base.
    """
    if symbols is not None:
        # Normalize: uppercase, strip, deduplicate, keep order
        seen: set[str] = set()
        clean: list[str] = []
        for s in symbols:
            s = s.strip().upper()
            if s and s not in seen:
                seen.add(s)
                clean.append(s)
        state.trading_universe = clean if clean else None
    else:
        state.trading_universe = None

    # Source map: only retain if the caller supplied one AND we
    # accepted a non-empty universe. Reset-to-default clears it.
    if source_map and state.trading_universe is not None:
        # Normalize keys to uppercase to match the universe symbols.
        state.morning_universe_source_map = {
            k.strip().upper(): v for k, v in source_map.items() if k
        }
    else:
        state.morning_universe_source_map = None
    # Apply-event metadata is operator-provenance; it's set by the
    # apply orchestrator AFTER this function returns. We only clear
    # it here on reset-to-default so a follow-up universe change
    # doesn't carry stale provenance from a prior apply.
    if state.trading_universe is None:
        state.applied_universe_metadata = None
    logger.info(
        "trading_universe_updated symbols=%s source_map=%s",
        state.trading_universe or "default",
        "set" if state.morning_universe_source_map else "cleared",
    )


def get_universe_source(symbol: str) -> str:
    """Return the provenance tag for a symbol in the active universe.

    Resolution order:
      1. If ``state.morning_universe_source_map`` has the symbol, use
         that tag (set by the dashboard "Apply" path).
      2. Else if the symbol is in ``settings.training_universe``,
         it's part of the audited base (always-on).
      3. Else return ``"unknown"`` — symbol present in the active
         universe but never tagged. Treat as a data-quality issue at
         audit time (suggests a manual override outside the morning
         scanner path).

    Returns the value to stamp on ``TradeLog.universe_source``.
    """
    sym = symbol.strip().upper()
    if state.morning_universe_source_map and sym in state.morning_universe_source_map:
        return state.morning_universe_source_map[sym]
    if sym in {s.upper() for s in settings.training_universe}:
        return "base"
    return "unknown"


def _push_status():
    """Push a full status snapshot to all WebSocket clients."""
    try:
        emit_autotrade_status(get_status())
    except Exception:
        pass  # never let WS failures break the autotrade loop


def _next_approval_id() -> str:
    global _approval_counter
    _approval_counter += 1
    return f"approval-{_approval_counter}"


def _executed_trade_count() -> int:
    """Count trades that executed today — including ones since closed."""
    return sum(1 for t in state.trades_today if t.status in _DAILY_COUNTED_STATUSES)


def _executed_trade_count_by_scanner(scanner_id: str) -> int:
    """v7.15: count today's executed trades produced by ``scanner_id``.

    Used by the experimental daily-cap guard so the cap on
    ``scanner_id="experimental_v1"`` is independent of the ICT
    daily cap. ICT's count is unchanged (it still uses
    ``_executed_trade_count`` for the global ceiling)."""
    return sum(
        1 for t in state.trades_today
        if t.status in _DAILY_COUNTED_STATUSES and t.scanner_id == scanner_id
    )


def _holding_symbol(symbol: str) -> bool:
    """True if the in-process monitor currently holds an open position in
    ``symbol``. The monitor list is the free, always-available source of
    truth for autotrade positions — entry dedup consults it FIRST so the
    check can never fail open on a broker-API error (2026-07-06 P1 fix)."""
    sym = symbol.upper()
    return any(
        (not m.closed) and m.symbol.upper() == sym
        for m in state.monitored_positions
    )


def _latest_bar_et(bars_by_symbol: dict[str, list[dict]]) -> str | None:
    latest_ts = max(
        (
            float(bars[-1]["time"])
            for bars in bars_by_symbol.values()
            if bars and bars[-1].get("time") is not None
        ),
        default=None,
    )
    if latest_ts is None:
        return None
    return datetime.fromtimestamp(latest_ts, tz=_ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def _join_symbols(symbols: list[str]) -> str:
    return ",".join(symbols) if symbols else "-"


def _update_scan_observability(hits: list[dict], bars_by_symbol: dict[str, list[dict]]) -> None:
    matched_symbols = [
        h["symbol"] for h in hits if h.get("matched_strategies")
    ]
    forming_symbols = [
        h["symbol"]
        for h in hits
        if h.get("forming_setups") and not h.get("matched_strategies")
    ]

    state.last_scan_hit_count = len(hits)
    state.last_scan_matched_symbols = matched_symbols
    state.last_scan_forming_symbols = forming_symbols
    state.last_scan_matched_symbol_count = len(matched_symbols)
    state.last_scan_forming_symbol_count = len(forming_symbols)
    state.last_scan_matched_setup_count = sum(
        len(h.get("matched_strategies") or []) for h in hits
    )
    state.last_scan_forming_setup_count = sum(
        len(h.get("forming_setups") or []) for h in hits
    )
    state.last_scan_latest_bar_et = _latest_bar_et(bars_by_symbol)


def get_status() -> dict:
    """Return a JSON-safe snapshot of the auto-trader state."""
    _expire_pending_approvals()

    return {
        "running":             state.running,
        "last_scan":           state.last_scan,
        "symbols_scanned":     state.symbols_scanned,
        "strategies_matched":  state.strategies_matched,
        "scan_observability": {
            "latest_bar_et":        state.last_scan_latest_bar_et,
            "hit_count":            state.last_scan_hit_count,
            "matched_symbol_count": state.last_scan_matched_symbol_count,
            "matched_setup_count":  state.last_scan_matched_setup_count,
            "forming_symbol_count": state.last_scan_forming_symbol_count,
            "forming_setup_count":  state.last_scan_forming_setup_count,
            "matched_symbols":      state.last_scan_matched_symbols,
            "forming_symbols":      state.last_scan_forming_symbols,
        },
        "trades_today":        [_log_dict(t) for t in state.trades_today[-20:]],
        "scan_count":          state.scan_count,
        # v7.16.x — per-ET-day count for the "Today's Scans" tile. The
        # cumulative ``scan_count`` above is process-lifetime.
        "scan_count_today":    state.scan_count_today,
        # v7.16.x — autonomous (auto-approve) mode on/off, for the
        # dashboard "Autonomous" toggle.
        "autonomous_mode":     state.autonomous_mode,
        "autonomous_max_trades_per_day": settings.autonomous_max_trades_per_day,
        "last_error":          state.last_error,
        "circuit_breaker_tripped": state.circuit_breaker_tripped,
        "confirming":          [
            {"symbol": c["symbol"], "direction": c["direction"],
             "confidence": c["confidence"], "started": c["started"]}
            for c in state.confirming
        ],
        "pending_approvals":   [_approval_dict(a) for a in state.pending_approvals],
        "monitored_positions": [_monitored_dict(m) for m in state.monitored_positions if not m.closed],
        "closed_positions":    [_monitored_dict(m) for m in state.monitored_positions if m.closed][-10:],
        "analysis_cache":      analysis_cache.stats(),
        # Market regime (#8) — None until the first scan fetches SPY bars.
        # Dashboard renders "Trending Up · SPY +1.2% vs 20-SMA · vol 14%".
        "regime":              _regime_snapshot_dict(_regime_snapshot) if _regime_snapshot else None,
        # News classifier cache (#9) — cheap observability; the actual
        # verdicts live per-trade on TradeLog.news_* fields.
        "news_cache":          {"entries": len(_news_cache),
                                "ttl_seconds": _NEWS_CACHE_TTL_SECONDS},
        # Haiku gate observability (#11). Rates are fractions in [0, 1]
        # and reset to 0/0/0 at the top of every scan cycle. The UI
        # alerts when ``none_rate > 0.05`` (infra) or
        # ``reject_rate < 0.50`` (gate is too lenient / Tier-1 is too
        # strict). ``calls == 0`` (cold cycle) → both rates are None.
        "haiku_gate":          _haiku_gate_status_block(),
        # Calibration stats (#10) — rolling Brier / log-loss over the last
        # 30 days of GRADED trades. Returns a zero-trade summary when the
        # user hasn't closed enough scored trades yet (min_trades=5). None
        # if no active user (pre-start).
        "calibration":         _calibration_status_block(),
        "config": {
            "scan_interval_seconds":    state.scan_interval_seconds,
            "min_confidence":           state.min_confidence,
            "position_size_pct":        state.position_size_pct,
            "max_position_equity_pct":  state.max_position_equity_pct,
            "max_open_trades":          state.max_open_trades,
            "max_trades_per_day":       state.max_trades_per_day,
            "confirmation_seconds":     state.confirmation_seconds,
            "max_daily_loss":           state.max_daily_loss,
            "approval_timeout_seconds": state.approval_timeout_seconds,
            "trailing_stop_pct":        state.trailing_stop_pct,
            "max_hold_minutes":         state.max_hold_minutes,
            "eod_buffer_minutes":       state.eod_buffer_minutes,
            "monitor_interval_seconds": state.monitor_interval_seconds,
            "enable_trailing_stop":     state.enable_trailing_stop,
            "enable_time_exit":         state.enable_time_exit,
            "max_per_sector":           state.max_per_sector,
            "trading_universe":         get_active_universe(),
            # v7.12.1 (2026-05-01): Layer-2 AI gate switch. False =
            # predicate-only mode (no Haiku/Sonnet calls). Surfaced so
            # the dashboard can display a clear "AI gate OFF" banner.
            "enable_ai_gate":           state.enable_ai_gate,
        },
    }


def set_autonomous_mode(enabled: bool) -> dict:
    """Toggle autonomous (auto-approve) mode at runtime.

    v7.16.x (2026-05-21). When ON, matched setups that clear every gate
    are auto-approved + auto-executed instead of waiting for a manual
    dashboard click. Independent of the autotrade running/stopped
    switch — you can pre-arm autonomous before starting the scanner.
    Returns the full status dict so the caller can echo it to the UI.
    """
    state.autonomous_mode = bool(enabled)
    logger.info("autonomous_mode_set enabled=%s", state.autonomous_mode)
    emit_trade_event("autonomous_mode", {"enabled": state.autonomous_mode})
    _push_status()
    return get_status()


def _log_dict(t: TradeLog) -> dict:
    return {
        "time": t.time, "symbol": t.symbol, "side": t.side,
        "qty": t.qty, "order_type": t.order_type, "confidence": t.confidence,
        "setup": t.setup, "status": t.status, "order_id": t.order_id,
        "error": t.error,
        "prediction_price": t.prediction_price,
        "confirmation_price": t.confirmation_price,
        "price_move_pct": t.price_move_pct,
        "entry_zone": t.entry_zone,
        "stop_loss": t.stop_loss,
        "target": t.target,
        "risk_reward": t.risk_reward,
        "reasoning": t.reasoning,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "trailing_stop_final": t.trailing_stop_final,
        "highest_price_reached": t.highest_price_reached,
        "hold_duration_seconds": t.hold_duration_seconds,
        # Calibration (#6)
        "raw_confidence":         t.raw_confidence,
        "calibration_multiplier": t.calibration_multiplier,
        "calibration_reason":     t.calibration_reason,
        "calibration_n":          t.calibration_n,
        "matched_strategies":     t.matched_strategies,
        # Regime (#8)
        "regime":             t.regime,
        "regime_label":       t.regime_label,
        "regime_multiplier":  t.regime_multiplier,
        # News (#9)
        "news_stance":        t.news_stance,
        "news_impact":        t.news_impact,
        "news_severity":      t.news_severity,
        "news_label":         t.news_label,
        "news_multiplier":    t.news_multiplier,
        # Structured probabilities (#10) — lets the UI draw the full
        # distribution (e.g. "T 72% · S 18% · — 10%") alongside the
        # graded outcome once the trade closes.
        "p_target":       t.p_target,
        "p_stop":         t.p_stop,
        "p_neither":      t.p_neither,
        "outcome_class":  t.outcome_class,
        # Brier feedback-loop multiplier (#10) — the factor the calibration
        # feedback applied to raw_confidence.
        "brier_multiplier": t.brier_multiplier,
        # Haiku pre-screen (#11) — which Tier-1.5 verdict let this trade
        # through (or None when the screener didn't run, i.e. fail-open).
        "haiku_screen_valid":  t.haiku_screen_valid,
        "haiku_screen_reason": t.haiku_screen_reason,
        # Relative Strength / Relative Weakness (#12) — alpha-vs-beta
        # multiplier. ``rs_state`` = one of the state labels from
        # rel_strength.py; ``rs_multiplier`` is the factor that landed
        # in the composition stack.
        "rs_state":             t.rs_state,
        "rs_label":             t.rs_label,
        "rs_stock_vs_spy":      t.rs_stock_vs_spy,
        "rs_spy_change_pct":    t.rs_spy_change_pct,
        "rs_multiplier":        t.rs_multiplier,
        # Hour-of-day multiplier (#12.5) — evidence-backed per-strategy
        # hour weight (e.g. Gap Fill Short hour-11 → ×1.15).
        "hour_et":              t.hour_et,
        "hour_multiplier":      t.hour_multiplier,
        # Universe provenance (v7.13 Phase 1.5) — "base" / "morning_dynamic"
        # / "unknown". The 30-day audit groups trade outcomes by this
        # field to validate whether the morning scanner adds edge.
        "universe_source":      t.universe_source,
    }


def _approval_dict(a: PendingApproval) -> dict:
    d = {
        "id":          a.id,
        "symbol":      a.trade_log.symbol,
        "side":        a.trade_log.side,
        "direction":   a.direction,
        "qty":         a.trade_log.qty,
        "confidence":  a.trade_log.confidence,
        "setup":       a.trade_log.setup,
        "entry_zone":  a.trade_log.entry_zone,
        "stop_loss":   a.trade_log.stop_loss,
        "target":      a.trade_log.target,
        "risk_reward": a.trade_log.risk_reward,
        "reasoning":   a.trade_log.reasoning,
        "prediction_price":   a.trade_log.prediction_price,
        "confirmation_price": a.trade_log.confirmation_price,
        "price_move_pct":     a.trade_log.price_move_pct,
        "created":     a.created,
        "expires":     a.expires,
        "sizing":      a.sizing,
        # Calibration (#6) — lets the UI explain why the final confidence differs
        # from Claude's raw score (e.g. "Raw 72% → 81% (SOFI +1.12× on OB, n=34)").
        "raw_confidence":         a.trade_log.raw_confidence,
        "calibration_multiplier": a.trade_log.calibration_multiplier,
        "calibration_reason":     a.trade_log.calibration_reason,
        "calibration_n":          a.trade_log.calibration_n,
        "calibration_detail":     a.trade_log.calibration_detail,
        "matched_strategies":     a.trade_log.matched_strategies,
        # Regime (#8) — shows which strategies got damped/boosted by SPY regime
        # (e.g. "Mean Reversion × 0.30 — SPY Trending Up, vol 14%").
        "regime":             a.trade_log.regime,
        "regime_label":       a.trade_log.regime_label,
        "regime_multiplier":  a.trade_log.regime_multiplier,
        "regime_detail":      a.trade_log.regime_detail,
        # News (#9) — explains the catalyst damp/boost, with per-article
        # headlines + reasons so the user can see exactly which article
        # moved the confidence and why ("Bearish catalyst: FDA rejection").
        "news_stance":        a.trade_log.news_stance,
        "news_impact":        a.trade_log.news_impact,
        "news_severity":      a.trade_log.news_severity,
        "news_label":         a.trade_log.news_label,
        "news_multiplier":    a.trade_log.news_multiplier,
        "news_detail":        a.trade_log.news_detail,
        # Structured probabilities (#10). Surfaced on the approval card so
        # the user sees Claude's actual distribution — not just a scalar
        # — e.g. "P(target) 72% · P(stop) 18% · P(neither) 10%".
        "p_target":           a.trade_log.p_target,
        "p_stop":             a.trade_log.p_stop,
        "p_neither":          a.trade_log.p_neither,
        # Brier feedback loop (#10) — explains why confidence moved from
        # the raw × cal × regime × news product. detail has the bucket,
        # predicted_mean, empirical_hit_rate, n, and raw_ratio, so the
        # UI can render "0.8-0.9 bucket: predicted 0.85, hit 0.60, n=24
        # → 0.71×".
        "brier_multiplier":   a.trade_log.brier_multiplier,
        "brier_detail":       a.trade_log.brier_detail,
        # Haiku pre-screen (#11) — two-stage AI gate. ``haiku_screen_valid``
        # is True when Haiku escalated the setup to Opus, None when the
        # screener didn't run (no API key, timeout, parse failure — all
        # fail-open). False rejects never become approvals, so you'll
        # never see one of those here. ``haiku_screen_reason`` is the
        # one-sentence rationale Haiku returned — shown in the "Haiku ✓"
        # chip tooltip on the approval card.
        "haiku_screen_valid":  a.trade_log.haiku_screen_valid,
        "haiku_screen_reason": a.trade_log.haiku_screen_reason,
        # Relative Strength / Relative Weakness (#12) — soft multiplier
        # that scores the trade against SPY to separate alpha from beta.
        # ``rs_detail`` carries the full meta dict so the RS chip can
        # render a tooltip like "Leader · +1.2% vs SPY · ×1.20
        # (genuine leadership — SPY flat)".
        "rs_state":            a.trade_log.rs_state,
        "rs_label":            a.trade_log.rs_label,
        "rs_stock_vs_spy":     a.trade_log.rs_stock_vs_spy,
        "rs_spy_change_pct":   a.trade_log.rs_spy_change_pct,
        "rs_multiplier":       a.trade_log.rs_multiplier,
        "rs_detail":           a.trade_log.rs_detail,
        # Hour-of-day multiplier (#12.5) — surfaced on the approval card so
        # the user can see "11 ET · ×1.15 (Gap Fill Short edge)" with a
        # per-strategy breakdown in the tooltip. detail is the full meta
        # dict mirroring regime_detail's shape.
        "hour_et":             a.trade_log.hour_et,
        "hour_multiplier":     a.trade_log.hour_multiplier,
        "hour_detail":         a.trade_log.hour_detail,
    }
    return d


def _monitored_dict(m: MonitoredPosition) -> dict:
    now = datetime.now(timezone.utc)
    opened = datetime.fromisoformat(m.opened_at)
    hold_seconds = int((now - opened).total_seconds()) if not m.closed else None
    if m.closed and m.exited_at:
        exited = datetime.fromisoformat(m.exited_at)
        hold_seconds = int((exited - opened).total_seconds())

    tl = m.trade_log
    return {
        "symbol":         m.symbol,
        "direction":      m.direction,
        "entry_price":    round(m.entry_price, 2),
        "qty":            m.qty,
        "opened_at":      m.opened_at,
        "current_price":  round(m.current_price, 2) if m.current_price else None,
        "trailing_stop":  round(m.trailing_stop, 2) if m.trailing_stop else None,
        "initial_stop":   round(m.initial_stop, 2) if m.initial_stop else None,
        "highest_price":  round(m.highest_price, 2) if m.highest_price else None,
        "lowest_price":   round(m.lowest_price, 2) if m.lowest_price else None,
        "trail_pct":      state.trailing_stop_pct,
        "max_hold_minutes": state.max_hold_minutes,
        "hold_seconds":   hold_seconds,
        "exit_price":     round(m.exit_price, 2) if m.exit_price else None,
        "exit_reason":    m.exit_reason,
        "exited_at":      m.exited_at,
        "closed":         m.closed,
        # Extra trade log context for expanded detail view
        "confidence":     tl.confidence,
        "setup":          tl.setup,
        "target":         tl.target,
        "stop_loss":      tl.stop_loss,
        "risk_reward":    tl.risk_reward,
        "reasoning":      tl.reasoning,
        "entry_zone":     tl.entry_zone,
    }


def _haiku_gate_status_block() -> dict:
    """Per-cycle Haiku gate observability for get_status().

    Returns the raw counters plus two rates:

      * ``none_rate``  = haiku_none_count / haiku_calls
        Fraction of calls where the verdict came back ``None`` —
        timeouts, parse failures, or missing API key. The pipeline
        fails open in those cases so the trade still flows to Opus,
        but a sustained ``none_rate > 0.05`` means real infra rot is
        being papered over.
      * ``reject_rate`` = haiku_reject_count / haiku_calls
        Fraction of calls where Haiku returned ``valid=False``.
        ``reject_rate < 0.50`` means the gate isn't earning its keep —
        either the prompt is too lenient or Tier-1 is already strict
        enough that almost everything Haiku sees is real.

    Both rates are ``None`` when ``calls == 0`` (cold cycle / before the
    first scan finishes) so the dashboard can render a neutral
    "—" instead of a misleading 0%.
    """
    calls   = state.haiku_calls
    none_n  = state.haiku_none_count
    rej_n   = state.haiku_reject_count
    if calls > 0:
        none_rate   = round(none_n / calls, 3)
        reject_rate = round(rej_n / calls, 3)
    else:
        none_rate   = None
        reject_rate = None
    return {
        "calls":         calls,
        "none_count":    none_n,
        "reject_count":  rej_n,
        "none_rate":     none_rate,
        "reject_rate":   reject_rate,
        "infra_alert":   (none_rate is not None and none_rate > 0.05),
        "leniency_alert": (
            reject_rate is not None and calls >= 4 and reject_rate < 0.50
        ),
    }


def _calibration_status_block() -> dict | None:
    """Rolling Brier / log-loss snapshot for get_status().

    Prefers the module-level ``_calibration_summary`` cache (refreshed once
    per ET day by the scan loop) so repeated status polls don't hammer the
    DB. Falls back to a live DB read when no cache exists yet (e.g. the
    dashboard loads before the first scan has run).

    Swallows DB errors with a warning — calibration telemetry should never
    be able to take the status endpoint down. Returns None when no user
    is active (pre-start, or the autotrader is running in anonymous/test
    mode without persistence).
    """
    if not state.active_user_id:
        return None
    if _calibration_summary is not None:
        return brier.summary_dict(_calibration_summary)
    try:
        summary = brier.calibration_summary_from_db(
            state.active_user_id, days=30, min_trades=5,
        )
        return brier.summary_dict(summary)
    except Exception as exc:
        logger.warning("calibration_status_failed error=%s", exc)
        return None


def _refresh_calibration_summary_if_needed() -> None:
    """Refresh the Brier calibration snapshot once per ET day.

    Closed trades accumulate over days, not seconds, so re-scoring the
    full 30-day window on every scan is pure waste. Matches the
    ET-date caching pattern used by the regime snapshot and daily-context
    cache. On failure we keep the stale snapshot (same "keep yesterday's
    read rather than nuke the stack" guarantee the regime cache makes);
    if we've never succeeded, the cache stays None and
    ``brier.calibration_multiplier`` degrades to a 1.0 no-op.
    """
    global _calibration_summary, _calibration_cache_date
    if not state.active_user_id:
        return
    today_et = datetime.now(_ET).date()
    if _calibration_cache_date == today_et and _calibration_summary is not None:
        return
    try:
        snap = brier.calibration_summary_from_db(
            state.active_user_id, days=30, min_trades=5,
        )
    except Exception as exc:
        logger.warning("calibration_summary_refresh_failed error=%s", exc)
        return

    _calibration_summary = snap
    _calibration_cache_date = today_et
    logger.info(
        "calibration_summary_refreshed date=%s n=%d brier=%s log_loss=%s buckets=%d",
        today_et.isoformat(), snap.count,
        snap.mean_brier, snap.mean_log_loss,
        len(snap.reliability),
    )


def _expire_pending_approvals():
    """Remove approvals past their timeout. The next scan will re-issue a fresh
    one if the setup still qualifies — no need to keep stale state around."""
    now = datetime.now(timezone.utc)
    keep: list[PendingApproval] = []
    for a in state.pending_approvals:
        expires = datetime.fromisoformat(a.expires)
        if now < expires:
            keep.append(a)
            continue
        # Expired — mark the simulated trade as 'expired' (so the sim P&L
        # distinguishes "user ignored" from "user rejected"), emit an event
        # so any UI listening can drop the card, then let it fall out of
        # the list.
        a.trade_log.status = "expired"
        logger.info("autotrade_approval_expired symbol=%s", a.trade_log.symbol)
        emit_trade_event("approval_expired", {"symbol": a.trade_log.symbol, "id": a.id})
        # v7.16.x (2026-05-22) — observability: record the expiry so the
        # daily summary surfaces "QCOM blocked: approval_expired" instead
        # of falsely reporting no matched signals were processed. The
        # approval only existed because the match cleared every gate — it
        # just wasn't approved (manual mode) before the timeout. Wrapped
        # in try/except: a telemetry hiccup must never break the expiry
        # sweep (which itself must never break the scan loop).
        try:
            from . import matched_outcomes as _matched_outcomes
            _exp_csv = a.trade_log.matched_strategies or ""
            _exp_strats = [
                {"name": _n.strip(), "direction": a.direction}
                for _n in _exp_csv.split(",") if _n.strip()
            ]
            _waited = (now - datetime.fromisoformat(a.created)).total_seconds()
            _matched_outcomes.record(
                symbol=a.trade_log.symbol,
                matched_strategies=_exp_strats,
                outcome="blocked",
                reason="approval_expired",
                detail=(
                    f"approval card expired unapproved after {_waited:.0f}s "
                    f"(setup={a.trade_log.setup!r}, "
                    f"confidence={a.trade_log.confidence:.2f}) — manual "
                    f"approval needed; enable Autonomous mode to auto-approve"
                ),
            )
        except Exception:
            logger.debug("matched_outcome_expiry_record_failed", exc_info=True)
        if a.trade_log.sim_trade_id:
            try:
                mark_simulated_approval(a.trade_log.sim_trade_id, "expired")
            except Exception as exc:
                logger.debug("sim_mark_expired_failed id=%s: %s", a.trade_log.sim_trade_id, exc)
    state.pending_approvals = keep


def _is_market_hours() -> bool:
    """Check if US equity markets are likely open (weekday, 4 AM – 8 PM ET)."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    return 4 <= now.hour < 20


def _dynamic_scan_interval() -> int:
    """Return scan interval in seconds based on time of day (ET).

    - Opening rush  (9:30–10:00 AM):  20s — highest quality setups
    - Morning momentum (10:00–11:30):  40s — still active
    - Midday chop   (11:30 AM–2:00 PM): 150s — most false signals
    - Afternoon setup (2:00–3:30 PM):  40s — trend resumptions
    - Closing rush  (3:30–4:00 PM):   20s — end-of-day moves
    - Extended hours:                  120s — lower priority
    """
    now = datetime.now(_ET)
    t = now.hour * 60 + now.minute  # minutes since midnight

    if 570 <= t < 600:    # 9:30–10:00
        return 20
    elif 600 <= t < 690:  # 10:00–11:30
        return 40
    elif 690 <= t < 840:  # 11:30–14:00
        return 150
    elif 840 <= t < 930:  # 14:00–15:30
        return 40
    elif 930 <= t < 960:  # 15:30–16:00
        return 20
    else:
        return 120        # pre-market / after-hours


# ── Layer-2-disabled fallback (v7.12.1, 2026-05-01) ─────────────────────────

# Risk geometry for predicate-only trades. With Layer 2 (Sonnet)
# disabled, we don't have structural FVG/OB anchored stops — fall back
# to a simple fixed-percentage rule that historically clears the
# 0.10R friction baseline on the v7.12.1 universe (the audit window
# averaged ATR ≈ 0.6-1.0%, so a 0.7% stop sits in the wick-tolerant
# zone without giving back too much R when the stop hits).
_PREDICATE_ONLY_STOP_PCT = 0.007    # 0.7% adverse move = ≈ 1R
_PREDICATE_ONLY_RR = 1.5            # 1.5R target
# Synthetic confidence stamped on predicate-only trades. Picked above
# the 0.70 ``min_confidence`` floor so the gate at the bottom of
# _scan_and_trade naturally accepts these (and so the multiplier stack
# still has headroom to dampen weak setups). Distinguishable from real
# Sonnet outputs (which top out around 0.72 per the lift study).
_PREDICATE_ONLY_RAW_CONFIDENCE = 0.85


def _build_predicate_only_result(
    hit: dict,
    bars: list[dict],
) -> dict | None:
    """Build a synthetic analyzer result from a Layer-1 predicate match.

    Used when ``state.enable_ai_gate`` is False — the autotrade loop
    skips the Sonnet analyze_chart call entirely (saves API cost) and
    trades the predicate scanner alone. Direction comes from the first
    matched strategy (the scanner pre-orders by hit-count). Stop and
    target are computed via the fixed
    ``_PREDICATE_ONLY_STOP_PCT`` / ``_PREDICATE_ONLY_RR`` rules above.

    The returned dict is shape-compatible with ``analyze_chart``'s
    output so downstream multiplier composition + trade-log fields
    work unchanged. ``p_target / p_stop / p_neither`` are deliberately
    None so Brier scoring (#10) skips this trade — we don't want
    synthetic probabilities corrupting the calibration table.

    Returns None if direction can't be determined or bars are missing
    — caller treats that the same as a Sonnet rejection (skip).
    """
    matched = hit.get("matched_strategies") or []
    if not matched:
        return None
    primary = matched[0]
    direction = primary.get("direction")
    if direction not in ("long", "short"):
        return None
    if not bars:
        return None
    # Latest close = synthetic entry price. Using the same
    # ``_latest_price`` helper the live path uses below would be
    # nicer, but it's defined further down the file and we don't
    # want a forward reference here — direct extraction matches
    # what _latest_price does anyway.
    last = bars[-1]
    last_close = last.get("c") if "c" in last else last.get("close")
    if last_close is None:
        return None
    try:
        entry = float(last_close)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None

    stop_pct = _PREDICATE_ONLY_STOP_PCT
    rr = _PREDICATE_ONLY_RR
    if direction == "long":
        stop = round(entry * (1 - stop_pct), 2)
        target = round(entry + rr * (entry - stop), 2)
    else:  # short
        stop = round(entry * (1 + stop_pct), 2)
        target = round(entry - rr * (stop - entry), 2)

    setup = primary.get("name", "Layer-1 predicate match")
    matched_names = ", ".join(
        m.get("name", "") for m in matched if m.get("name")
    )

    return {
        "recommendation": {
            "direction": direction,
            "confidence": _PREDICATE_ONLY_RAW_CONFIDENCE,
            "setup": setup,
            "entry_zone": f"{entry:.2f}",
            "stop_loss": f"{stop:.2f}",
            "target": f"{target:.2f}",
            "risk_reward": f"{rr:.1f}",
            # Probability fields stay None so Brier (#10) skips
            # synthetic trades — preserves calibration integrity.
            "p_target": None,
            "p_stop": None,
            "p_neither": None,
        },
        "reasoning": (
            f"Layer-1 predicate match: {matched_names or setup}. "
            "Layer-2 (Sonnet AI gate) disabled via ENABLE_AI_GATE=false. "
            f"Stop = {stop_pct * 100:.1f}% adverse move (~1R), "
            f"target = {rr:.1f}R. Trade fires on the predicate alone."
        ),
        "_analysis_prompt": (
            "[Sonnet analyze_chart skipped — ENABLE_AI_GATE=false. "
            "See app/autotrade.py::_build_predicate_only_result for "
            "the synthetic recommendation logic.]"
        ),
    }


# ── Position sizing ──────────────────────────────────────────────────────────

async def _compute_position_qty(
    symbol_price: float,
    stop_loss_price: float | None = None,
    confidence: float = 0.80,
    scanner_id: str = "ict",
) -> dict:
    """Risk-based position sizing with confidence scaling.

    1. Base risk per trade = position_size_pct of portfolio (e.g. 5% = $5,000 on $100K)
    2. Confidence multiplier: 0.80 = 1.0x, 0.85 = 1.15x, 0.90+ = 1.5x
    3. v7.15: experimental risk multiplier — when ``scanner_id="experimental_v1"``,
       multiply the risk budget by ``settings.experimental_risk_multiplier``
       (default 0.25 → quarter-risk per experimental trade). ICT trades
       pass through at the existing multiplier (1.0 effectively).
    4. If stop loss is known, qty = risk_budget / distance_to_stop
       Otherwise, fall back to risk_budget / price

    Returns a dict with qty and a full breakdown for the frontend.
    """
    if symbol_price <= 0:
        return {"qty": 1, "portfolio_value": 0, "risk_budget": 0,
                "confidence_multiplier": 1.0, "method": "fallback",
                "stop_distance": None, "total_cost": symbol_price}

    buying_power = 0.0
    cash = 0.0
    non_marginable_bp = 0.0
    unsettled_today = 0.0
    try:
        trade_client = get_shared_trading_client()
        acct = await asyncio.to_thread(trade_client.get_account)
        portfolio_value = float(acct.get("portfolio_value", 0)) or float(acct.get("equity", 0))
        buying_power = float(acct.get("buying_power", 0))
        cash = float(acct.get("cash", 0))
        # non_marginable_buying_power = settled cash you can spend right now
        # without risking a Good Faith Violation (GFV). On a cash account,
        # same-day proceeds from a sale are NOT settled until T+1, so cash/BP
        # can be higher than what's actually safe to redeploy today.
        non_marginable_bp = float(acct.get("non_marginable_buying_power", 0))
        # Alpaca sometimes reports non_marginable_bp = 0 even when cash is
        # positive, on fresh cash accounts. Compute unsettled proceeds from
        # today's fills directly so we can back it out of cash ourselves.
        if not trading_mode.is_paper():
            unsettled_today = await asyncio.to_thread(trade_client.get_todays_sell_proceeds)
    except Exception as exc:
        # Fail safe: tiny budget so a bad fallback can't auto-size a huge trade
        logger.warning("position_sizing_fallback error=%s — using $5k safe default", exc)
        portfolio_value = 5_000
        buying_power = 5_000
        cash = 5_000
        non_marginable_bp = 5_000

    # Spendable funds: never use margin — cap at the smaller of cash and BP.
    # Alpaca reports BP as 4× equity on day-trading accounts, which would let
    # us size a $300K position on a $100K account. Cash is the real limit.
    # On a live cash account we further cap by settled cash only, computed two
    # ways (use the stricter of the two): (a) non_marginable_buying_power as
    # reported by Alpaca, (b) cash minus today's sell proceeds that haven't
    # settled yet. Whichever is lower wins, so GFVs are impossible even when
    # Alpaca's field lags.
    spendable = min(cash, buying_power) if cash > 0 else buying_power
    if not trading_mode.is_paper():
        live_caps = [spendable]
        if non_marginable_bp > 0:
            live_caps.append(non_marginable_bp)
        live_caps.append(max(0.0, cash - unsettled_today))
        spendable = min(live_caps)

    # Confidence multiplier: scale up for high-conviction trades
    if confidence >= 0.90:
        conf_mult = 1.5
    elif confidence >= 0.85:
        conf_mult = 1.15 + (confidence - 0.85) * 7  # 1.15 → 1.5 linearly
    else:
        conf_mult = 1.0

    base_risk = portfolio_value * state.position_size_pct
    risk_budget = base_risk * conf_mult

    # v7.15: experimental-scanner risk multiplier. Applied AFTER the
    # confidence multiplier so the two stack predictably:
    #     experimental @ 0.85 conf, 0.25× multiplier
    #     → base_risk × 1.15 × 0.25 = 0.2875× the base ICT trade.
    # The multiplier is read from settings at call time (not snapshot)
    # so an operator can dial it via .env + restart without code change.
    exp_mult = 1.0
    if scanner_id == "experimental_v1":
        try:
            exp_mult = float(settings.experimental_risk_multiplier or 1.0)
        except (TypeError, ValueError):
            exp_mult = 1.0
        risk_budget = risk_budget * exp_mult

    # Risk-based: size by stop distance if we know the stop
    stop_distance = None
    method = "portfolio_pct"
    if stop_loss_price and stop_loss_price > 0:
        stop_distance = abs(symbol_price - stop_loss_price)
        if stop_distance > 0:
            qty = math.floor(risk_budget / stop_distance)
            method = "risk_based"
        else:
            qty = math.floor(risk_budget / symbol_price)
    else:
        qty = math.floor(risk_budget / symbol_price)

    # Per-position NOTIONAL cap (2026-06-15): ≤ max_position_equity_pct of
    # equity, so one entry can't eat all the buying power and block later
    # concurrent matches. Only ever REDUCES qty — a risk-limited small position
    # is untouched. Size-only; does not change which trades fire or their R.
    max_qty_by_equity = math.floor(portfolio_value * state.max_position_equity_pct / symbol_price)

    # Cap at spendable funds — never let total cost exceed actual cash available
    if spendable > 0:
        max_qty_by_funds = math.floor(spendable * 0.95 / symbol_price)  # 95% safety margin
        qty = max(1, min(qty, max_qty_by_funds, max_qty_by_equity, 10_000))
    else:
        # On live: settled cash is 0 (probably because today's sells haven't
        # settled). Signal "don't trade" by returning qty=0 — _execute_trade
        # will refuse to submit and emit a GFV-aware status.
        if not trading_mode.is_paper():
            qty = 0
        else:
            qty = max(1, min(qty, max_qty_by_equity, 10_000))

    total_cost = round(qty * symbol_price, 2)

    logger.info(
        "position_sizing scanner=%s method=%s portfolio=$%.0f cash=$%.0f buying_power=$%.0f non_marg_bp=$%.0f unsettled=$%.0f spendable=$%.0f base_risk=$%.0f conf=%.2f mult=%.2fx exp_mult=%.2fx budget=$%.0f stop_dist=%s qty=%d total=$%.0f",
        scanner_id, method, portfolio_value, cash, buying_power, non_marginable_bp, unsettled_today, spendable, base_risk, confidence, conf_mult, exp_mult,
        risk_budget, f"${stop_distance:.2f}" if stop_distance else "N/A",
        qty, total_cost,
    )

    # Max shares the account can actually afford (for frontend qty clamping).
    # Uses spendable (= min(cash, BP)) so typing/+- can't push the user into margin.
    max_affordable = math.floor(spendable / symbol_price) if symbol_price > 0 else 0

    return {
        "qty": qty,
        "portfolio_value": round(portfolio_value, 2),
        "buying_power": round(buying_power, 2),
        "cash": round(cash, 2),
        "non_marginable_buying_power": round(non_marginable_bp, 2),
        "unsettled_today": round(unsettled_today, 2),
        "spendable": round(spendable, 2),
        "max_affordable": max_affordable,
        "risk_budget": round(risk_budget, 2),
        "base_risk": round(base_risk, 2),
        "confidence_multiplier": round(conf_mult, 2),
        # v7.15: experimental risk multiplier + producing scanner.
        # Frontend / approval UI can surface "Risk: 0.25× (experimental)"
        # using these fields without re-deriving from .env.
        "scanner_id": scanner_id,
        "experimental_multiplier": round(exp_mult, 3),
        "method": method,
        "stop_distance": round(stop_distance, 2) if stop_distance else None,
        "total_cost": total_cost,
        "price": round(symbol_price, 2),
    }


# ── Duplicate position protection ────────────────────────────────────────────

async def _get_blocked_symbols() -> set[str]:
    """Return symbols that should NOT get new orders.

    Includes: open positions, pending/open orders, confirming symbols,
    and symbols with pending approvals.
    """
    trade_client = get_shared_trading_client()
    blocked: set[str] = set()

    # Open positions
    try:
        positions = await asyncio.to_thread(trade_client.list_positions)
        blocked.update(p["symbol"] for p in positions)
    except Exception:
        logger.exception("autotrade_positions_check_failed")

    # Open/pending orders (not yet filled)
    try:
        open_orders = await asyncio.to_thread(trade_client.list_orders, "open")
        blocked.update(o["symbol"] for o in open_orders if o.get("symbol"))
    except Exception:
        logger.warning("autotrade_open_orders_check_failed")

    # Symbols currently in confirmation watch
    blocked.update(c["symbol"] for c in state.confirming)

    # Symbols with pending approvals
    blocked.update(a.trade_log.symbol for a in state.pending_approvals)

    return blocked


# ── Approval actions (called from API endpoints) ────────────────────────────

def approve_trade(approval_id: str, qty_override: int | None = None) -> dict | None:
    """User approves a pending trade — execute it now.

    Args:
        qty_override: If set, use this qty instead of the AI-computed one.
    """
    _expire_pending_approvals()

    target = None
    for a in state.pending_approvals:
        if a.id == approval_id:
            target = a
            break

    if not target:
        return None

    state.pending_approvals = [a for a in state.pending_approvals if a.id != approval_id]

    trade_log = target.trade_log

    # Apply user's qty override if provided
    if qty_override is not None and qty_override > 0:
        logger.info("autotrade_qty_override symbol=%s ai_qty=%d user_qty=%d",
                     trade_log.symbol, trade_log.qty, qty_override)
        trade_log.qty = qty_override

    trade_log.status = "approved"
    rec = target.rec
    direction = target.direction

    logger.info("autotrade_approved symbol=%s by_user", trade_log.symbol)
    emit_trade_event("approved", {"symbol": trade_log.symbol, "side": trade_log.side})
    _push_status()

    # Mark simulated trade as approved
    if trade_log.sim_trade_id:
        try:
            mark_simulated_approval(trade_log.sim_trade_id, "yes")
        except Exception as exc:
            logger.debug("sim_mark_approved_failed id=%s: %s", trade_log.sim_trade_id, exc)

    asyncio.get_running_loop().create_task(
        _execute_trade(trade_log, rec, direction)
    )

    return _log_dict(trade_log)


async def close_monitored(symbol: str) -> dict | None:
    """User manually closes a monitored position."""
    target = None
    for m in state.monitored_positions:
        if m.symbol.upper() == symbol.upper() and not m.closed:
            target = m
            break

    if target is None:
        return None

    price = await _fetch_symbol_price(symbol)
    if price is None:
        price = target.current_price or target.entry_price

    logger.info("manual_close_requested symbol=%s price=%.2f", symbol, price)
    await _close_monitored_position(target, "manual_close", price)
    return _monitored_dict(target)


def reject_trade(approval_id: str) -> dict | None:
    """User rejects a pending trade."""
    target = None
    for a in state.pending_approvals:
        if a.id == approval_id:
            target = a
            break

    if not target:
        return None

    state.pending_approvals = [a for a in state.pending_approvals if a.id != approval_id]
    target.trade_log.status = "rejected"
    target.trade_log.error = "Rejected by user"
    logger.info("autotrade_rejected symbol=%s by_user", target.trade_log.symbol)
    emit_trade_event("rejected", {"symbol": target.trade_log.symbol})
    _push_status()

    # Mark simulated trade as rejected
    if target.trade_log.sim_trade_id:
        try:
            mark_simulated_approval(target.trade_log.sim_trade_id, "no")
        except Exception as exc:
            logger.debug("sim_mark_rejected_failed id=%s: %s", target.trade_log.sim_trade_id, exc)

    return _log_dict(target.trade_log)


# ── Tier 6: Position Monitor — trailing stop + time-based exit ──────────────

async def _fetch_monitored_prices(symbols: list[str]) -> dict[str, float]:
    """Batch-fetch current prices for all monitored positions in one API call."""
    if not symbols:
        return {}
    client = AlpacaMarketDataClient()
    try:
        snaps = await asyncio.to_thread(client.fetch_snapshots, symbols)
    except Exception as exc:
        logger.warning("monitor_price_fetch_failed error=%s", exc)
        return {}

    prices: dict[str, float] = {}
    for sym in symbols:
        snap = snaps.get(sym)
        if not snap:
            continue
        trade = snap.get("latestTrade") or snap.get("latest_trade")
        if trade:
            p = float(trade.get("p") or trade.get("price") or 0)
            if p > 0:
                prices[sym] = p
                continue
        minute_bar = snap.get("minuteBar") or snap.get("minute_bar")
        if minute_bar:
            p = float(minute_bar.get("c", 0))
            if p > 0:
                prices[sym] = p
    return prices


# Close-confirmation poll: Alpaca's close endpoint confirms ACCEPTANCE, not the
# final fill. After an accepted close we poll get_position until the broker
# reports flat (~10s total). Module-level so the lifecycle regression can
# shrink them; read at call time, not bound at def.
_CLOSE_CONFIRM_POLL_INTERVAL_S = 1.0
_CLOSE_CONFIRM_POLL_ATTEMPTS = 10


def _best_effort_hold_seconds(pos: MonitoredPosition) -> int:
    """Hold duration is METADATA — a malformed opened_at must never affect
    the close path (a deterministic parse error inside the broker-critical
    block would re-mark close_failed every retry cycle, forever)."""
    try:
        opened = datetime.fromisoformat(pos.opened_at)
        return int((datetime.now(timezone.utc) - opened).total_seconds())
    except Exception as exc:
        logger.warning(
            "monitor_hold_parse_failed symbol=%s opened_at=%r error=%s",
            pos.symbol, pos.opened_at, exc,
        )
        return 0


def _validated_fill(order: dict | None, expected_qty: int) -> float | None:
    """Return an order's fill price ONLY if it is unambiguously the complete
    fill for ``expected_qty`` shares (Option A, 2026-06-12): the order must be
    fully ``filled`` and its ``filled_qty`` must reconcile with the position's
    qty. A partial/canceled order, or an aggregate close of repeated same-symbol
    entries (broker qty ≠ this record's qty), returns None → the caller persists
    NULL and the metric falls back to the monitor price. Never guesses."""
    if not order or order.get("status") != "filled":
        return None
    try:
        fq = float(order.get("filled_qty") or 0)        # NO truncation — 10.5 must NOT match 10
        fp = order.get("filled_avg_price")
        if fp is None or abs(fq - float(expected_qty)) > 1e-9:
            return None
        return float(fp)
    except (ValueError, TypeError):
        return None


async def _close_monitored_position(pos: MonitoredPosition, reason: str, price: float):
    """Close a monitored position: submit once, confirm flat, then finalize.

    Three failure boundaries, never conflated:
      1. Broker REJECTS/errors the submission → close_failed; the position
         stays open and retryable on the next monitor cycle.
      2. Broker ACCEPTS but the fill is unconfirmed after the poll window →
         close_unconfirmed + loud manual-action alert; the position stays
         monitored and is reconciled READ-ONLY (never a second close order)
         until Alpaca reports flat.
      3. Post-confirmation persistence/telemetry failures → logged; never
         relabel an accepted-and-confirmed broker close.
    """
    if pos.closed or pos.closing:
        return
    if pos.close_submitted:
        # A close was already accepted — re-entry must never resubmit.
        await _reconcile_unconfirmed_close(pos)
        return

    tl = pos.trade_log
    pos.closing = True
    try:
        trade_client = get_shared_trading_client()
        try:
            existing = await asyncio.to_thread(trade_client.get_position, pos.symbol)
            if existing is None:
                logger.info("monitor_position_already_closed symbol=%s reason=%s", pos.symbol, reason)
                reason = "external"
            else:
                # Submit the close via the PROVEN mechanism (DELETE
                # /v2/positions) — unchanged. Capture the close-order id
                # IN-PROCESS for the exit-fill lookup at finalize; it is NOT
                # persisted before broker confirmation (durable crash recovery
                # is Option B, a separate project). poll-until-flat +
                # read-only reconcile (below) are the proven close-confirmation
                # layer and are untouched.
                close_resp = await asyncio.to_thread(trade_client.close_position, pos.symbol)
                if isinstance(close_resp, dict):
                    pos.close_order_id = close_resp.get("id")
                pos.close_submitted = True
                pos.close_submitted_at = datetime.now(timezone.utc).isoformat()
                pos.pending_close_reason = reason
                pos.pending_close_price = price
                logger.info(
                    "monitor_close_accepted symbol=%s reason=%s price=%.2f entry=%.2f hold=%ds",
                    pos.symbol, reason, price, pos.entry_price, _best_effort_hold_seconds(pos),
                )
        except Exception as exc:
            # Boundary 1: genuine broker/API failure — stays open + retryable.
            logger.error("monitor_close_failed symbol=%s reason=%s error=%s", pos.symbol, reason, exc)
            tl.error = f"Auto-close failed ({reason}): {exc}"
            tl.status = "close_failed"
            emit_monitor_event("close_failed", {"symbol": pos.symbol, "reason": reason, "error": str(exc)})
            _push_status()
            return

        if existing is None:
            # Broker already reports flat — nothing to confirm.
            await _finalize_closed_position(pos, reason, price)
            return

        # Boundary 2: acceptance ≠ fill. Poll until the broker reports flat.
        for attempt in range(_CLOSE_CONFIRM_POLL_ATTEMPTS):
            await asyncio.sleep(_CLOSE_CONFIRM_POLL_INTERVAL_S)
            try:
                still_open = await asyncio.to_thread(trade_client.get_position, pos.symbol)
            except Exception as exc:
                logger.warning(
                    "monitor_close_confirm_poll_error symbol=%s attempt=%d error=%s",
                    pos.symbol, attempt + 1, exc,
                )
                continue
            if still_open is None:
                await _finalize_closed_position(pos, reason, price)
                return

        # Still open after the window: DO NOT mark closed, DO NOT resubmit.
        window_s = int(_CLOSE_CONFIRM_POLL_ATTEMPTS * _CLOSE_CONFIRM_POLL_INTERVAL_S)
        tl.status = "close_unconfirmed"
        tl.error = (
            f"Close accepted but fill unconfirmed after ~{window_s}s ({reason}) — "
            "manual attention; reconciling read-only"
        )
        logger.error(
            "monitor_close_unconfirmed symbol=%s reason=%s submitted_at=%s — "
            "MANUAL ATTENTION REQUIRED: no further close orders will be sent; "
            "monitor reconciles read-only until Alpaca reports flat",
            pos.symbol, reason, pos.close_submitted_at,
        )
        emit_monitor_event("close_unconfirmed", {
            "symbol": pos.symbol, "reason": reason,
            "submitted_at": pos.close_submitted_at,
            "error": "close accepted but fill unconfirmed — manual attention required",
        })
        _push_status()
    finally:
        pos.closing = False


async def _reconcile_unconfirmed_close(pos: MonitoredPosition):
    """Read-only reconciliation of an accepted-but-unconfirmed close.

    Runs every monitor cycle while ``pos.close_submitted and not pos.closed``.
    Never submits an order: it only asks Alpaca whether the position is flat
    yet, and finalizes with the original pending reason/price when it is.
    """
    if pos.closed or pos.closing:
        return
    pos.closing = True
    try:
        trade_client = get_shared_trading_client()
        try:
            still_open = await asyncio.to_thread(trade_client.get_position, pos.symbol)
        except Exception as exc:
            logger.warning("monitor_close_reconcile_poll_error symbol=%s error=%s", pos.symbol, exc)
            return
        if still_open is not None:
            logger.error(
                "monitor_close_unconfirmed_still_open symbol=%s submitted_at=%s — "
                "read-only reconciliation continues; manual attention required",
                pos.symbol, pos.close_submitted_at,
            )
            return
        reason = pos.pending_close_reason or pos.exit_reason or "unconfirmed_close"
        price = pos.pending_close_price if pos.pending_close_price is not None else pos.entry_price
        logger.info("monitor_close_reconciled symbol=%s reason=%s", pos.symbol, reason)
        await _finalize_closed_position(pos, reason, price)
    finally:
        pos.closing = False


async def _finalize_closed_position(pos: MonitoredPosition, reason: str, price: float):
    """Mark closed + run post-close persistence/telemetry.

    Reached ONLY after Alpaca confirms the position is flat (or reported no
    position to begin with). From here on, failures are persistence/telemetry
    failures and must never relabel the confirmed broker close.
    """
    tl = pos.trade_log
    pos.closed = True
    pos.exit_reason = reason
    pos.exit_price = price
    pos.exited_at = datetime.now(timezone.utc).isoformat()
    tl.exit_price = price
    tl.exit_reason = reason
    tl.trailing_stop_final = pos.trailing_stop
    tl.highest_price_reached = pos.highest_price if pos.direction == "long" else pos.lowest_price
    tl.hold_duration_seconds = _best_effort_hold_seconds(pos)
    tl.status = "closed"
    tl.error = None
    logger.info(
        "monitor_position_closed symbol=%s reason=%s price=%.2f entry=%.2f hold=%ds",
        pos.symbol, reason, price, pos.entry_price, tl.hold_duration_seconds,
    )

    try:
        # ── Outcome classification (#10) — label the trade as target_hit /
        # stop_hit / neither so we can score Brier + log-loss later. We feed
        # the favorable/unfavorable extremes (not the exit price), because
        # Claude's P(target before stop) is about whether the LEVEL was
        # touched, not whether we captured it cleanly. A trade that hit
        # target first then reversed into a trailing-stop exit is correctly
        # labeled "target_hit" — Claude predicted that successfully. Any
        # failure here leaves outcome_class as None and Brier just skips
        # this row.
        outcome_class: str | None = None
        try:
            # tl.target / tl.stop_loss are AI-formatted strings (e.g.
            # "$52.50" / "52.50–52.60") — brier.classify_outcome expects
            # parsed floats. _parse_price extracts the first numeric token.
            outcome_class = brier.classify_outcome(
                direction=pos.direction,
                target=_parse_price(tl.target),
                stop=_parse_price(tl.stop_loss),
                highest_price=pos.highest_price,
                lowest_price=pos.lowest_price,
                exit_reason=reason,
                exit_price=price,
            )
            tl.outcome_class = outcome_class
        except Exception as exc:
            logger.warning(
                "outcome_classify_failed symbol=%s reason=%s error=%s",
                pos.symbol, reason, exc,
            )

        # Order-ID-linked broker fills (telemetry-only — a lookup failure must
        # never affect the close path; fields stay None and metrics fall back
        # to monitor prices on a CONSISTENT basis). Gated: inert unless enabled.
        # Safe to call here: finalize runs only after Alpaca confirmed flat, so
        # the close order has its final filled_avg_price.
        entry_fill = exit_fill = None
        if settings.fill_persist_enabled:
            _fill_client = None
            try:
                _fill_client = get_shared_trading_client()
            except Exception as exc:
                logger.warning("fill_client_unavailable symbol=%s error=%s", pos.symbol, exc)
            # Split tries (an entry-order lookup failure must not erase the exit
            # fill, and vice versa) + strict validation (P1 #3): only a fully
            # filled, qty-reconciled order is trusted; anything else → NULL +
            # loud log → metric falls back to the monitor price.
            try:
                if _fill_client and pos.order_id:
                    _o = await asyncio.to_thread(_fill_client.get_order, pos.order_id)
                    entry_fill = _validated_fill(_o, pos.qty)
                    if entry_fill is None and _o:
                        logger.warning(
                            "entry_fill_unresolved symbol=%s status=%s filled_qty=%s expected=%s — NULL",
                            pos.symbol, _o.get("status"), _o.get("filled_qty"), pos.qty)
            except Exception as exc:
                logger.warning("entry_fill_lookup_failed symbol=%s error=%s", pos.symbol, exc)
            try:
                if _fill_client and pos.close_order_id:
                    _o = await asyncio.to_thread(_fill_client.get_order, pos.close_order_id)
                    exit_fill = _validated_fill(_o, pos.qty)
                    if exit_fill is None and _o:
                        logger.warning(
                            "exit_fill_unresolved symbol=%s status=%s filled_qty=%s expected=%s — NULL "
                            "(aggregate close of repeated same-symbol entries reads as unresolved)",
                            pos.symbol, _o.get("status"), _o.get("filled_qty"), pos.qty)
            except Exception as exc:
                logger.warning("exit_fill_lookup_failed symbol=%s error=%s", pos.symbol, exc)

        # Update DB record with exit data
        updated = update_trade_closed(
            order_id=pos.order_id,
            record_id=tl.db_record_id,
            status=tl.status,
            exit_price=price,
            exit_reason=reason,
            trailing_stop_final=pos.trailing_stop,
            highest_price_reached=pos.highest_price if pos.direction == "long" else pos.lowest_price,
            hold_duration_seconds=tl.hold_duration_seconds,
            outcome_class=outcome_class,
            entry_fill_price=entry_fill,
            exit_fill_price=exit_fill,
            exit_order_id=(pos.close_order_id if settings.fill_persist_enabled else None),
        )
        if not updated:
            logger.error(
                "monitor_close_persist_failed symbol=%s order_id=%s record_id=%s",
                pos.symbol, pos.order_id, tl.db_record_id,
            )
        emit_monitor_event("position_closed", {
            "symbol": pos.symbol, "reason": reason,
            "entry_price": pos.entry_price, "exit_price": price,
            "hold_seconds": tl.hold_duration_seconds,
        })
        emit_wallet_update()
        _push_status()

        # Generate AI lesson from losing trades (runs in background)
        if tl.db_record_id:
            try:
                # Close-time P&L / R fields live on the persisted TradeRecord,
                # not on the in-memory TradeLog. Re-fetch after
                # update_trade_closed() so lesson generation sees the final,
                # auditable values and cannot fail on nonexistent tl.* fields.
                from .db import SessionLocal as _LessonSession
                from .models import TradeRecord as _LessonTradeRecord
                _lesson_db = _LessonSession()
                try:
                    lesson_record = _lesson_db.get(_LessonTradeRecord, tl.db_record_id)
                finally:
                    _lesson_db.close()
                if lesson_record is None:
                    raise LookupError(f"TradeRecord {tl.db_record_id} not found")
                trade_dict = {
                    "id": lesson_record.id, "symbol": lesson_record.symbol,
                    "direction": lesson_record.direction or pos.direction,
                    "setup": lesson_record.setup,
                    "confidence": lesson_record.confidence,
                    "entry_price": lesson_record.entry_price,
                    "exit_price": lesson_record.exit_price,
                    "stop_loss": lesson_record.stop_loss,
                    "target": lesson_record.target,
                    "reasoning": lesson_record.reasoning,
                    "exit_reason": lesson_record.exit_reason,
                    "r_multiple": lesson_record.r_multiple,
                    "hold_duration_seconds": lesson_record.hold_duration_seconds,
                    "pnl": lesson_record.pnl, "pnl_pct": lesson_record.pnl_pct,
                }
                await asyncio.to_thread(check_and_generate_lesson, state.active_user_id or 1, trade_dict)
            except Exception as exc:
                logger.warning("lesson_generation_error symbol=%s: %s", pos.symbol, exc)

        # ── Per-trade dossier export (v7.14a, 2026-05-03) ─────────────
        # Telemetry only — runs AFTER update_trade_closed() returned + the
        # DB row is fully persisted. Wrapped in try/except per the v7.14
        # plan refinement #3: a serializer bug or disk-full error here
        # MUST NOT delay or fail the close path. The export is a no-op
        # on the central operator box (settings.export_trades=False).
        # On friend instances (settings.export_trades=True), the closed
        # TradeRecord is re-fetched from the DB so the dossier sees the
        # exit fields the update_trade_closed call just wrote (the
        # in-memory ``tl`` predates them).
        if settings.export_trades and tl.db_record_id:
            try:
                from . import trade_exporter as _trade_exporter
                from .db import SessionLocal as _ExportSession
                from .models import TradeRecord as _TR
                _exp_db = _ExportSession()
                try:
                    record = _exp_db.get(_TR, tl.db_record_id)
                finally:
                    _exp_db.close()
                if record is not None:
                    _trade_exporter.export_closed_trade(
                        trade_record=record,
                        instance_id=settings.friend_instance_id,
                        out_dir=_TRADE_EXPORT_DIR,
                    )
            except Exception as exc:
                # Telemetry-only: log loudly, never propagate.
                logger.warning(
                    "trade_export_failed symbol=%s db_id=%s error=%s",
                    pos.symbol, tl.db_record_id, exc,
                )
    except Exception as exc:
        logger.exception(
            "monitor_post_close_failed symbol=%s reason=%s error=%s",
            pos.symbol, reason, exc,
        )


def _update_trailing_stop(pos: MonitoredPosition, current_price: float):
    """Update the trailing stop based on current price movement."""
    trail_pct = state.trailing_stop_pct

    if pos.direction == "long":
        # Update high-water mark
        if pos.highest_price is None or current_price > pos.highest_price:
            pos.highest_price = current_price
            new_stop = current_price * (1 - trail_pct)
            # Only raise the stop, never lower it
            if pos.trailing_stop is None or new_stop > pos.trailing_stop:
                pos.trailing_stop = new_stop
                logger.debug(
                    "monitor_trail_raised symbol=%s stop=%.2f high=%.2f",
                    pos.symbol, pos.trailing_stop, pos.highest_price,
                )
    else:  # short
        # Update low-water mark
        if pos.lowest_price is None or current_price < pos.lowest_price:
            pos.lowest_price = current_price
            new_stop = current_price * (1 + trail_pct)
            # Only lower the stop, never raise it
            if pos.trailing_stop is None or new_stop < pos.trailing_stop:
                pos.trailing_stop = new_stop
                logger.debug(
                    "monitor_trail_lowered symbol=%s stop=%.2f low=%.2f",
                    pos.symbol, pos.trailing_stop, pos.lowest_price,
                )


def _check_trailing_stop_hit(pos: MonitoredPosition, current_price: float) -> bool:
    """Return True if the trailing stop has been breached."""
    if pos.trailing_stop is None:
        return False
    if pos.direction == "long":
        return current_price <= pos.trailing_stop
    else:  # short
        return current_price >= pos.trailing_stop


def _check_time_exit(pos: MonitoredPosition) -> str | None:
    """Return exit reason if a time-based exit is triggered, else None."""
    now_utc = datetime.now(timezone.utc)
    now_et = datetime.now(_ET)

    # 1. Max hold time
    opened = datetime.fromisoformat(pos.opened_at)
    elapsed_minutes = (now_utc - opened).total_seconds() / 60
    if elapsed_minutes >= state.max_hold_minutes:
        return "max_hold_time"

    # 2. End-of-day exit (weekdays only)
    if now_et.weekday() < 5:
        market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        cutoff = market_close - timedelta(minutes=state.eod_buffer_minutes)
        if now_et >= cutoff:
            return "eod_exit"

    return None


async def _run_monitor_loop():
    """Position monitor loop — checks trailing stops and time exits."""
    logger.info(
        "monitor_started trail=%.1f%% max_hold=%dm eod_buffer=%dm interval=%ds",
        state.trailing_stop_pct * 100, state.max_hold_minutes,
        state.eod_buffer_minutes, state.monitor_interval_seconds,
    )
    while state.running:
        try:
            active = [m for m in state.monitored_positions if not m.closed]
            prices: dict[str, float] = {}
            if active:
                symbols = list({m.symbol for m in active})
                prices = await _fetch_monitored_prices(symbols)

                for pos in active:
                    # Accepted-but-unconfirmed close: reconcile read-only every
                    # cycle until Alpaca reports flat. Never re-evaluate exits
                    # or send further orders for this position.
                    if pos.close_submitted:
                        await _reconcile_unconfirmed_close(pos)
                        continue

                    current_price = prices.get(pos.symbol)
                    if current_price is None:
                        continue

                    pos.current_price = current_price

                    # 1. Time-based exit (highest priority)
                    if state.enable_time_exit:
                        time_reason = _check_time_exit(pos)
                        if time_reason:
                            logger.info(
                                "monitor_time_exit symbol=%s reason=%s price=%.2f",
                                pos.symbol, time_reason, current_price,
                            )
                            await _close_monitored_position(pos, time_reason, current_price)
                            continue

                    # 2. Trailing stop
                    if state.enable_trailing_stop:
                        _update_trailing_stop(pos, current_price)
                        if _check_trailing_stop_hit(pos, current_price):
                            logger.info(
                                "monitor_trailing_stop_hit symbol=%s price=%.2f stop=%.2f high=%.2f",
                                pos.symbol, current_price, pos.trailing_stop,
                                pos.highest_price or pos.lowest_price or 0,
                            )
                            await _close_monitored_position(pos, "trailing_stop", current_price)
                            continue

            # Push updated prices/stops to clients
            if active:
                _push_status()

            # ── Simulator: check open simulated trades against live prices ──
            try:
                sim_symbols = await asyncio.to_thread(get_open_simulated_symbols, state.active_user_id)
                if sim_symbols:
                    # Reuse prices from real position monitoring if available
                    all_sim_prices = dict(prices)
                    missing = [s for s in sim_symbols if s not in all_sim_prices]
                    if missing:
                        sim_prices = await _fetch_monitored_prices(missing)
                        all_sim_prices.update(sim_prices)
                    if all_sim_prices:
                        await asyncio.to_thread(
                            check_simulated_exits,
                            all_sim_prices,
                            state.max_hold_minutes,
                        )
            except Exception as exc:
                logger.warning("sim_monitor_error: %s", exc)

        except Exception as exc:
            logger.exception("monitor_loop_error error=%s", exc)

        for _ in range(state.monitor_interval_seconds):
            if not state.running:
                break
            await asyncio.sleep(1)

    logger.info("monitor_stopped")


# ── Core loop ────────────────────────────────────────────────────────────────

async def _run_loop():
    """Main auto-trade loop — runs until state.running is set to False."""
    logger.info("autotrade_started confirmation=%ds max_daily_loss=$%.0f position_size=%.1f%%",
                state.confirmation_seconds,
                state.max_daily_loss, state.position_size_pct * 100)
    while state.running:
        try:
            # Watchdog: restart monitor task if it died unexpectedly
            if (state.enable_trailing_stop or state.enable_time_exit) and (
                state._monitor_task is None or state._monitor_task.done()
            ):
                logger.warning("monitor_watchdog restarting position monitor")
                state._monitor_task = asyncio.get_running_loop().create_task(_run_monitor_loop())

            if _is_market_hours():
                # 2026-05-20 hang diagnostic: arm a faulthandler stack-dump
                # timer around the scan cycle. If _scan_and_trade hangs past
                # the budget (the 4-for-4 post-match freeze), faulthandler
                # dumps every thread's stack to ~/.tradehack/hang_dumps/ —
                # this works even when the event loop is fully blocked,
                # which asyncio.wait_for cannot. Armed/cancelled per cycle.
                _armed = False
                try:
                    _df = _hang_dump_target()
                    _df.write(
                        f"\n=== scan cycle armed {datetime.now(_ET).isoformat()} "
                        f"(scan={state.scan_count + 1}) — dump fires if this "
                        f"cycle exceeds {_SCAN_CYCLE_HANG_BUDGET_SECONDS}s ===\n"
                    )
                    _df.flush()
                    faulthandler.dump_traceback_later(
                        _SCAN_CYCLE_HANG_BUDGET_SECONDS,
                        repeat=False, file=_df, exit=False,
                    )
                    _armed = True
                except Exception as exc:
                    logger.warning("hang_dump_arm_failed err=%s", exc)
                try:
                    await _scan_and_trade()
                finally:
                    if _armed:
                        try:
                            faulthandler.cancel_dump_traceback_later()
                        except Exception:
                            pass
            else:
                logger.debug("autotrade_skip_off_hours")
        except Exception as exc:
            state.last_error = str(exc)
            logger.exception("autotrade_loop_error")

        interval = _dynamic_scan_interval()
        state.scan_interval_seconds = interval  # expose current interval in status
        for _ in range(interval):
            if not state.running:
                break
            await asyncio.sleep(1)
    logger.info("autotrade_stopped")


async def _check_daily_loss() -> bool:
    """Check if the daily P&L has hit the circuit breaker limit."""
    try:
        trade_client = get_shared_trading_client()
        acct = await asyncio.to_thread(trade_client.get_account)

        equity = float(acct.get("equity", 0))
        last_equity = float(acct.get("last_equity", 0))

        if last_equity > 0:
            daily_pl = equity - last_equity
        else:
            daily_pl = float(acct.get("unrealized_pl", 0))

        if daily_pl <= -state.max_daily_loss:
            logger.warning(
                "CIRCUIT_BREAKER daily_pl=$%.2f limit=-$%.0f — STOPPING",
                daily_pl, state.max_daily_loss,
            )
            state.circuit_breaker_tripped = True
            state.last_error = (
                f"Daily loss limit hit (${abs(daily_pl):,.2f} loss). "
                f"Auto-trading paused to protect your account."
            )
            emit_trade_event("circuit_breaker", {"daily_pl": round(daily_pl, 2), "limit": state.max_daily_loss})
            _push_status()
            return True
        return False
    except Exception as exc:
        logger.error("daily_loss_check_failed error=%s", exc)
        return False


async def _current_daily_pl() -> float | None:
    """Return today's realized+unrealized P&L, or None if unavailable.

    Used by the pre-submit projection — a separate helper so _check_daily_loss
    keeps its side effects (tripping the breaker, pushing status) in one place.
    """
    try:
        trade_client = get_shared_trading_client()
        acct = await asyncio.to_thread(trade_client.get_account)
        equity = float(acct.get("equity", 0))
        last_equity = float(acct.get("last_equity", 0))
        if last_equity > 0:
            return equity - last_equity
        return float(acct.get("unrealized_pl", 0))
    except Exception as exc:
        logger.warning("daily_pl_lookup_failed error=%s", exc)
        return None


async def _would_breach_daily_loss(trade_log: "TradeLog", direction: str) -> tuple[bool, str]:
    """Reject a trade if its worst-case loss + current daily P&L would exceed the cap.

    Worst case = qty × |entry - stop_loss| (per-share risk × share count). When
    the stop is unknown we fall back to 2% of notional so we don't rubber-stamp
    un-sized trades.

    This closes the race where _check_daily_loss runs once per scan but trades
    keep filling — a trade approved at 10:30 that wipes us out at 10:31 would
    previously slip through because the breaker hadn't re-checked.
    """
    current_pl = await _current_daily_pl()
    if current_pl is None:
        return (False, "")  # fail-open: don't block trades when we can't read P&L

    entry = trade_log.prediction_price or 0.0
    stop_raw = trade_log.stop_loss
    qty = max(1, int(trade_log.qty or 1))

    per_share_risk: float | None = None
    if stop_raw:
        try:
            # stop_loss is stored as a string ("152.40") or range; take the first float
            import re
            match = re.search(r"-?\d+(?:\.\d+)?", str(stop_raw))
            if match and entry > 0:
                stop = float(match.group(0))
                per_share_risk = abs(entry - stop)
        except (ValueError, TypeError):
            pass

    if per_share_risk is None or per_share_risk <= 0:
        # No stop set — assume 2% notional as a conservative worst case.
        per_share_risk = entry * 0.02 if entry > 0 else 0.0

    worst_case_loss = per_share_risk * qty
    projected_pl = current_pl - worst_case_loss

    if projected_pl <= -state.max_daily_loss:
        msg = (
            f"Projected daily loss would hit ${abs(projected_pl):,.2f} "
            f"(current ${current_pl:+,.2f}, worst-case ${-worst_case_loss:,.2f}) — "
            f"exceeds ${state.max_daily_loss:,.0f} limit."
        )
        logger.warning("pretrade_breaker_block symbol=%s %s", trade_log.symbol, msg)
        return (True, msg)

    return (False, "")


async def _fetch_bars(
    symbols: list[str] | None = None,
    mkt_client: AlpacaMarketDataClient | None = None,
) -> dict[str, list[dict]]:
    symbols = symbols or get_active_universe()
    client = mkt_client or AlpacaMarketDataClient()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=_LOOKBACK_HOURS)

    response = await asyncio.to_thread(
        client.fetch_stock_bars,
        AlpacaBarsRequest(symbols=symbols, timeframe="1Min", start=start, end=end),
    )

    bars_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        raw = response.get(sym, [])
        if len(raw) < 30:
            if raw:
                logger.debug("autotrade_skip_low_bars symbol=%s bars=%d", sym, len(raw))
            continue
        bars_by_symbol[sym] = convert_alpaca_bars(raw)
    return bars_by_symbol


async def _fetch_daily_bars(
    symbols: list[str],
    mkt_client: AlpacaMarketDataClient | None = None,
) -> dict[str, list[dict]]:
    client = mkt_client or AlpacaMarketDataClient()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=90)

    response = await asyncio.to_thread(
        client.fetch_stock_bars,
        AlpacaBarsRequest(symbols=symbols, timeframe="1Day", start=start, end=end),
    )

    daily_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        raw = response.get(sym, [])
        if len(raw) < 50:
            continue
        daily_by_symbol[sym] = convert_alpaca_bars(raw)
    return daily_by_symbol


async def _get_daily_ctx_map(
    symbols: list[str],
    mkt_client: AlpacaMarketDataClient | None = None,
) -> dict[str, dict]:
    """Return {symbol: {prev_close, prev_high, prev_low, avg_daily_volume,
    20d_high, 20d_low}} for Tier-1.

    Cached per ET date. The prev-day levels don't change during a session,
    so fetching daily bars every scan cycle would be pure waste. On the
    first call of a new ET date the cache is cleared and repopulated;
    subsequent calls within the same day are pure dict lookups.

    Symbols whose daily history is missing or too short silently drop
    from the returned map — the Tier-1 predicates that need daily
    context (Gap-and-Go, Prior Day Break) self-skip on ind.get(...)
    returning None.
    """
    global _daily_ctx_cache, _daily_ctx_cache_date
    today_et = datetime.now(_ET).date()

    stale = _daily_ctx_cache_date != today_et
    missing = [s for s in symbols if s not in _daily_ctx_cache]

    if stale:
        _daily_ctx_cache.clear()
        _daily_ctx_cache_date = today_et
        missing = list(symbols)

    if not missing:
        return {s: _daily_ctx_cache[s] for s in symbols if s in _daily_ctx_cache}

    try:
        daily_by_symbol = await _fetch_daily_bars(missing, mkt_client=mkt_client)
    except Exception:
        logger.warning("autotrade_daily_ctx_fetch_failed symbols=%d", len(missing))
        return {s: _daily_ctx_cache[s] for s in symbols if s in _daily_ctx_cache}

    for sym, bars in daily_by_symbol.items():
        if len(bars) < 2:
            continue
        # During market hours Alpaca's daily feed typically includes
        # today's still-forming bar at the tail. We want the most recent
        # bar whose ET date is strictly earlier than today.
        sorted_bars = sorted(bars, key=lambda x: x["time"])
        prev_bar = None
        for b in reversed(sorted_bars):
            bar_et_date = datetime.fromtimestamp(b["time"], tz=_ET).date()
            if bar_et_date < today_et:
                prev_bar = b
                break
        if prev_bar is None:
            continue
        # 20-day window ending at (and including) the prev_bar. Used for
        # both the RVOL volume baseline and the multi-day H/L band. Missing/
        # zero volume bars drop out of the mean so a symbol that just started
        # trading doesn't skew it down.
        closed_bars = [b for b in sorted_bars if b["time"] <= prev_bar["time"]]
        window20 = closed_bars[-20:]
        vols = [float(b.get("volume") or 0) for b in window20]
        vols = [v for v in vols if v > 0]
        avg_dv = (sum(vols) / len(vols)) if vols else None
        # 2026-06-04 PARITY FIX: 20-day high/low band. compute_ict_features
        # reads these from the daily-ctx SEED to populate ict_multi_day_pd_zone;
        # live previously omitted them, so that field was permanently None and
        # the Multi-Day PD predicates reproduced 0% of their backtest matches
        # (verified: +12.06R net dropped over the 6-month baseline). Same
        # 20-bar window as the backtester's _build_prev_day_lookup
        # (_MULTI_DAY_LOOKBACK=20), so live now matches the validated path.
        _highs = [float(b["high"]) for b in window20]
        _lows  = [float(b["low"])  for b in window20]
        _daily_ctx_cache[sym] = {
            "prev_close":       float(prev_bar["close"]),
            "prev_high":        float(prev_bar["high"]),
            "prev_low":         float(prev_bar["low"]),
            "avg_daily_volume": avg_dv,
            "20d_high":         (max(_highs) if _highs else None),
            "20d_low":          (min(_lows)  if _lows  else None),
        }

    logger.info(
        "autotrade_daily_ctx_refreshed date=%s cached=%d",
        today_et.isoformat(), len(_daily_ctx_cache),
    )
    return {s: _daily_ctx_cache[s] for s in symbols if s in _daily_ctx_cache}


async def _get_current_regime(
    mkt_client: AlpacaMarketDataClient | None = None,
) -> RegimeSnapshot | None:
    """Return today's market regime, caching once per ET date.

    Fetches SPY's daily bars via the existing ``_fetch_daily_bars``
    helper (same 90-day window), classifies them, and stashes the
    result. Returns ``None`` only when the feed fails — callers treat
    that as "regime unknown, no weighting" via ``_regime_multiplier``.
    """
    global _regime_snapshot, _regime_cache_date
    today_et = datetime.now(_ET).date()

    if _regime_snapshot is not None and _regime_cache_date == today_et:
        return _regime_snapshot

    try:
        daily = await _fetch_daily_bars(["SPY"], mkt_client=mkt_client)
    except Exception:
        logger.warning("autotrade_regime_fetch_failed", exc_info=True)
        return _regime_snapshot  # keep yesterday's read rather than nuking

    spy_bars = daily.get("SPY") or []
    if not spy_bars:
        logger.warning("autotrade_regime_no_spy_bars")
        return _regime_snapshot

    snap = await asyncio.to_thread(_classify_regime, spy_bars)
    _regime_snapshot = snap
    _regime_cache_date = today_et
    logger.info(
        "autotrade_regime_refreshed date=%s regime=%s vol=%.2f%% crosses=%d",
        today_et.isoformat(), snap.regime, snap.realized_vol_20d * 100,
        snap.crosses_last_5d,
    )
    return snap


def _regime_multiplier(
    snap: RegimeSnapshot | None,
    aligned_strategies: list[dict],
) -> tuple[float, dict]:
    """Mean regime weight across the aligned Tier-1 matches.

    Returns ``(multiplier, metadata)``. When the regime is unknown
    (insufficient SPY history or the feed hiccupped) we return 1.0 so
    detection silently no-ops rather than suppressing the whole book.

    The metadata dict is surfaced to the approval card so the user can
    see exactly which strategy got dampened and why.
    """
    if snap is None or snap.regime == "unknown" or not aligned_strategies:
        return 1.0, {
            "regime": snap.regime if snap else "unknown",
            "label": snap.label if snap else "Regime unknown (no SPY bars yet)",
            "multiplier": 1.0,
            "per_strategy": [],
        }

    per_strategy: list[dict] = []
    for m in aligned_strategies:
        name = m.get("name") or ""
        if not name:
            continue
        w = _get_regime_weight(name, snap.regime)
        per_strategy.append({
            "strategy": name,
            "weight": round(w, 3),
            "suppressed": w < _REGIME_SUPPRESS_THRESHOLD,
        })

    if not per_strategy:
        mult = 1.0
    else:
        mult = sum(p["weight"] for p in per_strategy) / len(per_strategy)

    return mult, {
        "regime":       snap.regime,
        "label":        snap.label,
        "multiplier":   round(mult, 3),
        "per_strategy": per_strategy,
    }


# ── News classifier cache (#9) ───────────────────────────────────────────────

def _cached_news_verdict(symbol: str, articles_hash: str) -> NewsVerdict | None:
    """Return a cached verdict for ``(symbol, articles_hash)`` if fresh."""
    key = (symbol.upper(), articles_hash)
    entry = _news_cache.get(key)
    if not entry:
        return None
    when, verdict = entry
    if (time.time() - when) > _NEWS_CACHE_TTL_SECONDS:
        _news_cache.pop(key, None)
        return None
    return verdict


def _store_news_verdict(symbol: str, verdict: NewsVerdict) -> None:
    key = (symbol.upper(), verdict.articles_hash)
    _news_cache[key] = (time.time(), verdict)
    # Loose eviction — drop the oldest 20 entries if we crossed the ceiling.
    # News cache is per-(symbol, articles_hash), so it grows by ~1 per scan
    # cycle per active symbol; 200 entries = comfortable headroom.
    if len(_news_cache) > _NEWS_CACHE_MAX_ENTRIES:
        stale = sorted(_news_cache.items(), key=lambda kv: kv[1][0])[:20]
        for k, _ in stale:
            _news_cache.pop(k, None)


async def _classify_news_cached(
    symbol: str,
    articles: list[dict] | None,
) -> NewsVerdict | None:
    """Run the news classifier with per-(symbol, articles_hash) caching.

    Returns ``None`` on classifier failure or when no API key is configured —
    the downstream ``_news_multiplier`` call silently no-ops to 1.0 in that
    case, matching the #8 regime degraded-path pattern.

    2026-05-19 hot-fix: wrapped in ``asyncio.wait_for(15s)``. Three uvicorn
    hangs today (MSFT @ 09:33-05-18, SOXL @ 09:34, ORCL @ 09:51) all
    coincided with the autotrade pipeline processing a matched hit. The
    most likely root cause was an Anthropic Haiku call stuck without a
    timeout — when the API stalls, the threadpool worker is held forever,
    and with 6 concurrent candidates × news_classify + dashboard chart
    fetches + scan loop iterations, the threadpool exhausts and every
    sync handler including /health starves. A 15s ceiling means a slow
    Haiku call degrades to ``no news verdict`` (the news_multiplier
    silently no-ops to 1.0, same as the API-key-missing path) instead
    of bricking the autotrade loop.
    """
    # Hash the live article list up front so cache lookups see the same key
    # the classifier would compute. Done here (not inside classify_news) so
    # that repeat scans over identical news skip the Claude call entirely.
    from .news_classifier import _hash_articles  # internal — same module boundary
    articles_hash = _hash_articles(articles or [])

    cached = _cached_news_verdict(symbol, articles_hash)
    if cached is not None:
        return cached

    try:
        verdict = await asyncio.wait_for(
            asyncio.to_thread(_classify_news, symbol, articles),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "autotrade_news_classify_timeout symbol=%s — degrading to no verdict",
            symbol,
        )
        return None
    if verdict is not None:
        _store_news_verdict(symbol, verdict)
    return verdict


async def _fetch_extended_bars(
    symbols: list[str],
    mkt_client: AlpacaMarketDataClient | None = None,
) -> dict[str, list[dict]]:
    client = mkt_client or AlpacaMarketDataClient()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now_et = datetime.now(_ET)
    ext_start_et = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    if now_et.hour < 4:
        ext_start_et -= timedelta(days=1)
    start = ext_start_et.astimezone(timezone.utc)

    response = await asyncio.to_thread(
        client.fetch_stock_bars,
        AlpacaBarsRequest(symbols=symbols, timeframe="1Min", start=start, end=end, feed="sip"),
    )

    ext_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        raw = response.get(sym, [])
        if not raw:
            continue
        ext_by_symbol[sym] = convert_alpaca_bars(raw)
    return ext_by_symbol


async def _fetch_symbol_price(
    symbol: str,
    mkt_client: AlpacaMarketDataClient | None = None,
) -> float | None:
    client = mkt_client or AlpacaMarketDataClient()
    try:
        snaps = await asyncio.to_thread(client.fetch_snapshots, [symbol])
        snap = snaps.get(symbol)
        if not snap:
            return None
        trade = snap.get("latestTrade") or snap.get("latest_trade")
        if trade:
            return float(trade.get("p") or trade.get("price") or 0) or None
        minute_bar = snap.get("minuteBar") or snap.get("minute_bar")
        if minute_bar:
            return float(minute_bar.get("c", 0)) or None
    except Exception:
        pass
    return None


def _latest_price(bars: list[dict]) -> float | None:
    if not bars:
        return None
    return float(bars[-1]["close"])


# ── Entry risk-control guards (2026-06-15 XLF runaway-re-entry incident) ─────
# Two pure-RESTRICTIVE guards — they can only block entries, never add them, so
# they cannot manufacture edge. Both mirror behavior the backtest already had.
_SAME_SYMBOL_COOLDOWN_S = 15 * 60   # mirror backtest COOLDOWN_BARS=15 (1-min bars)
_recent_entry_ts: dict[str, float] = {}   # symbol -> last entry-submit epoch (s)


def _entry_blocked_eod(now_et: datetime, eod_buffer_minutes: int) -> bool:
    """No NEW entries within ``eod_buffer_minutes`` of the 16:00 ET close.
    The monitor force-closes in that window, so a new entry only churns
    (open → instant eod_exit) — a factor in the XLF loop. Weekdays only;
    monitoring/closing is a separate loop and is unaffected."""
    if now_et.weekday() >= 5:
        return False
    cutoff = now_et.replace(hour=16, minute=0, second=0, microsecond=0) \
        - timedelta(minutes=eod_buffer_minutes)
    return now_et >= cutoff


def _symbol_on_cooldown(symbol: str, now_ts: float) -> bool:
    """True if an entry was submitted for ``symbol`` within the cooldown
    window — reserves the symbol the instant we submit AND prevents the
    cross-cycle re-entry that fired XLF four times in 75s."""
    ts = _recent_entry_ts.get(symbol)
    return ts is not None and (now_ts - ts) < _SAME_SYMBOL_COOLDOWN_S


async def _scan_and_trade():
    """One full scan cycle: fetch → screen → analyze → confirm → request approval."""
    if state.circuit_breaker_tripped:
        return

    if await _check_daily_loss():
        return

    if _executed_trade_count() >= state.max_trades_per_day:
        logger.info("autotrade_daily_limit_reached trades=%d", _executed_trade_count())
        return

    mkt_client = AlpacaMarketDataClient()

    try:
        bars_by_symbol = await _fetch_bars(mkt_client=mkt_client)
    except ValueError as exc:
        state.last_error = str(exc)
        return

    state.symbols_scanned = len(bars_by_symbol)
    # Reset Haiku gate counters at the start of every scan so the rates
    # surfaced in ``get_status()`` reflect the *current* cycle, not a
    # stale running total. Same pattern as ``symbols_scanned`` /
    # ``strategies_matched``.
    state.haiku_calls = 0
    state.haiku_none_count = 0
    state.haiku_reject_count = 0

    # ── Daily context: prev-day levels for Gap-and-Go / Prior-Day-Break ──
    # Cache hit on every call after the first of each ET date. Failure
    # degrades gracefully — the 8 original strategies don't need this.
    try:
        daily_ctx = await _get_daily_ctx_map(
            list(bars_by_symbol.keys()), mkt_client=mkt_client,
        )
    except Exception:
        logger.warning("autotrade_daily_ctx_unexpected_error", exc_info=True)
        daily_ctx = {}

    # ── Market regime (feature #8) ── classified once per ET date
    # from SPY daily bars. Used downstream to weight Claude's confidence
    # by strategy×regime fit. Failure degrades to no-op (all weights=1.0).
    regime_snap = await _get_current_regime(mkt_client=mkt_client)

    # ── Brier calibration snapshot (feature #10 — feedback loop) ──
    # Re-scored from the last 30 days of GRADED trades once per ET day.
    # Used by brier.calibration_multiplier below to damp/boost confidence
    # by how well Claude's predicted p_target bucket has empirically
    # played out. Pre-start (no active user) or refresh failure → cache
    # stays None → multiplier is a 1.0 no-op.
    _refresh_calibration_summary_if_needed()

    # ── Tier 1: Fast strategy pre-screen ──
    # ICT scanner first — its output drives the v7.12.1 audit baseline
    # and is the SOURCE OF TRUTH for the "Watching" / forming panel.
    hits = await asyncio.to_thread(scan_ict_symbols, bars_by_symbol, daily_ctx)

    # v7.15 (2026-05-18): experimental scanner runs on the SAME fetched
    # bars (no second Alpaca call) when EXPERIMENTAL_SCANNER_ENABLED=true.
    # Wrapped in ``asyncio.wait_for`` so a slow predicate, deadlock, or
    # infinite loop in the experimental playbook can NEVER eat ICT's
    # cycle budget. On timeout we log + skip experimental for this cycle;
    # ICT continues uninterrupted.
    exp_hits: list[dict] = []
    exp_timeout = False
    if settings.experimental_scanner_enabled:
        try:
            exp_hits = await asyncio.wait_for(
                asyncio.to_thread(
                    scan_experimental_symbols, bars_by_symbol, daily_ctx,
                ),
                timeout=settings.experimental_scan_timeout_seconds,
            )
        except asyncio.TimeoutError:
            exp_timeout = True
            exp_hits = []
            logger.warning(
                "experimental_scan_timeout budget=%.1fs scan=%d — ICT path continues normally",
                settings.experimental_scan_timeout_seconds, state.scan_count + 1,
            )
        except Exception:
            # Belt-and-suspenders: even if the to_thread wrapper or the
            # scanner itself raises, ICT continues. The experimental
            # scanner is best-effort — its job is to fire trades when
            # safe, not to compromise the ICT lane when unsafe.
            logger.exception(
                "experimental_scan_error scan=%d — ICT path continues normally",
                state.scan_count + 1,
            )
            exp_hits = []

    # Dupe resolution — ICT wins on overlap. Drop any experimental hit
    # whose symbol ALSO appears in this cycle's ICT hits. The audit
    # contract is: every TradeRecord row has one and only one
    # ``scanner_id``; if the same symbol matched both scanners, the
    # ICT side gets the trade and the experimental side logs a drop.
    ict_symbols = {h["symbol"] for h in hits}
    dropped_dupes: list[str] = []
    if exp_hits:
        kept_exp: list[dict] = []
        for eh in exp_hits:
            if eh["symbol"] in ict_symbols:
                dropped_dupes.append(eh["symbol"])
                logger.info(
                    "experimental_hit_dropped reason=duplicate_with_ict symbol=%s",
                    eh["symbol"],
                )
                continue
            kept_exp.append(eh)
        exp_hits = kept_exp

    # v7.15: append experimental telemetry row (even on no-op cycles
    # so the daily aggregation has visibility). Non-blocking — write
    # failure logs WARNING and returns.
    from . import scan_stats_experimental as _scan_stats_exp
    _scan_stats_exp.append_scan(
        scan_n=state.scan_count + 1,        # state.scan_count is incremented below; +1 keeps the row's scan_n aligned with the ICT row
        symbols=len(bars_by_symbol),
        matched_symbols=len(exp_hits),
        matched_setups=sum(len(h.get("matched_strategies") or []) for h in exp_hits),
        matched=[h["symbol"] for h in exp_hits],
        dropped_dupe=dropped_dupes,
        latest_bar_et=None,                  # filled in below where state.last_scan_latest_bar_et is computed
        timeout_skipped=exp_timeout,
    )

    # Merge into the main hits list. Downstream code (AI gate,
    # predicate-only path, sizing, order submission, save_trade_opened)
    # now sees both scanners' hits in one pass. Each carries its own
    # ``scanner_id``, so the audit lane stays clean.
    hits = list(hits) + list(exp_hits)

    _update_scan_observability(hits, bars_by_symbol)
    state.strategies_matched = sum(len(h["matched_strategies"]) for h in hits)
    state.last_scan = datetime.now(timezone.utc).isoformat()
    state.scan_count += 1
    # v7.16.x: per-ET-day counter. Roll over when the ET date changes —
    # independent of the cumulative ``scan_count`` above (which survives
    # day boundaries within a single uvicorn process). Drives the
    # "Today's Scans" dashboard tile.
    _scan_et_date = datetime.now(_ET).date().isoformat()
    if state.scan_count_today_et_date != _scan_et_date:
        state.scan_count_today_et_date = _scan_et_date
        state.scan_count_today = 0
    state.scan_count_today += 1
    state.last_error = None

    logger.info(
        "autotrade_scan scan=%d symbols=%d hits=%d matched_symbols=%d "
        "matched_setups=%d forming_symbols=%d forming_setups=%d "
        "latest_bar_et=%s matched=%s forming=%s regime=%s",
        state.scan_count, state.symbols_scanned, len(hits),
        state.last_scan_matched_symbol_count,
        state.last_scan_matched_setup_count,
        state.last_scan_forming_symbol_count,
        state.last_scan_forming_setup_count,
        state.last_scan_latest_bar_et or "-",
        _join_symbols(state.last_scan_matched_symbols),
        _join_symbols(state.last_scan_forming_symbols),
        regime_snap.regime if regime_snap else "unknown",
    )

    # v7.16 (2026-05-13): persist scan stats to a daily JSONL so
    # "did I miss any trades on day X?" is a one-grep answer instead
    # of a terminal-scrollback hunt. Non-blocking — a write failure
    # inside scan_stats logs a warning but never breaks the scan loop.
    from . import scan_stats
    scan_stats.append_scan(
        scan_n=state.scan_count,
        symbols=state.symbols_scanned,
        matched_symbols=state.last_scan_matched_symbol_count,
        matched_setups=state.last_scan_matched_setup_count,
        forming_symbols=state.last_scan_forming_symbol_count,
        forming_setups=state.last_scan_forming_setup_count,
        matched=list(state.last_scan_matched_symbols),
        forming=list(state.last_scan_forming_symbols),
        latest_bar_et=state.last_scan_latest_bar_et,
    )
    _push_status()

    if not hits:
        return

    # ── Guard 2 (2026-06-15): no new entries inside the EOD close window ──
    if _entry_blocked_eod(datetime.now(_ET), state.eod_buffer_minutes):
        logger.info("autotrade_no_new_entries_eod — within %dm of close; entries blocked "
                    "(monitoring/closing continues)", state.eod_buffer_minutes)
        return

    # ── Duplicate position protection — get ALL blocked symbols ──
    blocked = await _get_blocked_symbols()

    slots_available = state.max_open_trades - len([
        s for s in blocked
        # Only count actual positions toward the slot limit, not pending approvals
        # (positions are the ones from _get_blocked_symbols that came from list_positions)
    ])
    # Re-check with a simpler count: positions + active confirming
    try:
        trade_client = get_shared_trading_client()
        positions = await asyncio.to_thread(trade_client.list_positions)
        position_count = len(positions)
    except Exception:
        position_count = 0

    slots_available = state.max_open_trades - position_count
    if slots_available <= 0:
        logger.info("autotrade_max_positions_reached open=%d", position_count)
        return

    # ── Guard 1 (2026-06-15): same-symbol cooldown — exclude symbols with a
    # recent entry submit so the scanner can't re-fire the same setup every
    # cycle (the XLF×4 cause). Mirrors the backtest's per-strategy cooldown.
    _now_ts = datetime.now(timezone.utc).timestamp()
    cooled = [h["symbol"] for h in hits if _symbol_on_cooldown(h["symbol"], _now_ts)]
    if cooled:
        logger.info("autotrade_cooldown_skip symbols=%s", _join_symbols(sorted(set(cooled))))

    _MAX_AI_PER_SCAN = 6  # cap AI calls per cycle to avoid API overload
    candidates = [
        h for h in hits
        if h["symbol"] not in blocked and not _symbol_on_cooldown(h["symbol"], _now_ts)
    ][:min(slots_available, _MAX_AI_PER_SCAN)]
    if not candidates:
        return

    candidate_symbols = [h["symbol"] for h in candidates]

    # ── Fetch additional data only for candidates ──
    daily_by_symbol: dict[str, list[dict]] = {}
    try:
        daily_by_symbol = await _fetch_daily_bars(candidate_symbols, mkt_client=mkt_client)
    except Exception:
        logger.warning("autotrade_daily_fetch_failed")

    ext_by_symbol: dict[str, list[dict]] = {}
    try:
        ext_by_symbol = await _fetch_extended_bars(candidate_symbols, mkt_client=mkt_client)
    except Exception:
        logger.warning("autotrade_extended_fetch_failed")

    sector_etfs = get_required_sector_etfs(candidate_symbols)
    all_snapshot_symbols = list(set(candidate_symbols + sector_etfs))
    snapshots: dict[str, dict] = {}
    try:
        snapshots = await asyncio.to_thread(mkt_client.fetch_snapshots, all_snapshot_symbols)
    except Exception:
        logger.warning("autotrade_snapshots_fetch_failed")

    sector_snapshots = {etf: snapshots[etf] for etf in sector_etfs if etf in snapshots}

    # ── Tier 2: AI analysis on candidates (runs in parallel) ──
    # Note: lessons_text + per-symbol study_text were removed 2026-04-24 after
    # the Tier-2 lift study showed they were adding ~500-1000 tokens per call
    # without moving Sonnet's confidence distribution. Retrieval-based recall
    # (fetch lessons only when strategy/direction/killzone matches the current
    # setup) is the planned replacement.

    async def _analyze_one(
        hit: dict,
    ) -> tuple[dict, list[dict], dict | None, NewsVerdict | None, ScreenerVerdict | None]:
        """Haiku screen → Opus analyze for one candidate.

        Pipeline:
          1. Haiku 4.5 gate (#11) — if it says the Tier-1 match is a false
             alarm, short-circuit BEFORE fetching news or firing Opus.
             This is where the 80%+ cost savings come from — skip the
             expensive path entirely for rejected candidates.
          2. If Haiku says valid (or fails open), fetch news + run Opus
             analyze_chart + news classifier in parallel (unchanged from
             the pre-#11 flow).

        Returns ``(hit, bars, result, news_verdict, haiku_verdict)``. Any of
        the last three can be ``None`` — processing loop handles that.
        """
        sym = hit["symbol"]
        sym_bars = bars_by_symbol.get(sym, [])
        if not sym_bars:
            return hit, [], None, None, None

        daily_bars = daily_by_symbol.get(sym)
        extended_bars = ext_by_symbol.get(sym)
        sym_snapshot = snapshots.get(sym)

        # ── Tier 1.5: Haiku pre-screen (#11) ──
        # Run BEFORE news fetch / Opus call so a reject short-circuits the
        # whole expensive pipeline. Uses the direction inferred by Tier-1
        # from the matched strategies (if the matches are a mix of long and
        # short, pick the dominant direction; tie → skip the screen and let
        # Opus figure it out).
        matched = hit.get("matched_strategies") or []
        longs  = sum(1 for m in matched if m.get("direction") == "long")
        shorts = sum(1 for m in matched if m.get("direction") == "short")
        if longs > shorts:
            tier1_direction = "long"
        elif shorts > longs:
            tier1_direction = "short"
        else:
            tier1_direction = ""

        haiku_verdict: ScreenerVerdict | None = None
        if tier1_direction and matched and state.enable_ai_gate:
            # Per-cycle observability counter — incremented for every
            # *attempted* call (not for skips). Reset at the top of the
            # scan in ``_run_scan_cycle``.
            #
            # v7.12.1 (2026-05-01): the ``state.enable_ai_gate`` guard
            # short-circuits the entire Haiku screen when Layer 2 is
            # disabled — no API call, no veto. The downstream Sonnet
            # path is similarly skipped below.
            state.haiku_calls += 1
            try:
                latest = sym_bars[-1] if sym_bars else {}
                current_price = float(
                    latest.get("c") or latest.get("close") or 0
                )

                # ── Structural levers for Haiku (#11 follow-up) ──
                # The four extra fields are cheap reads from data we
                # already have in scope (Tier-1 indicator snapshot,
                # cached regime snapshot, sector-ETF snapshots). Giving
                # Haiku these lets it reject on conviction-class
                # grounds (low vol_ratio, off-killzone, against-regime,
                # weak RS) instead of guessing from raw bars.
                snap = hit.get("indicators_snapshot") or {}
                vol_ratio_val = snap.get("vol_ratio")
                ict_killzone_val = snap.get("ict_killzone")
                regime_val = regime_snap.regime if regime_snap is not None else None
                rs_zscore_val: float | None = None
                try:
                    sym_chg = _snapshot_change_pct(sym_snapshot)
                    spy_chg = _snapshot_change_pct(sector_snapshots.get("SPY"))
                    if sym_chg is not None and spy_chg is not None:
                        rs_zscore_val = round(sym_chg - spy_chg, 2)
                except Exception:
                    rs_zscore_val = None

                haiku_verdict = await asyncio.to_thread(
                    _haiku_screen_setup,
                    sym,
                    price=current_price,
                    direction=tier1_direction,
                    matched_strategies=matched,
                    bars=sym_bars,
                    daily_bars=daily_bars,
                    vol_ratio=vol_ratio_val,
                    ict_killzone=ict_killzone_val,
                    regime=regime_val,
                    rs_zscore=rs_zscore_val,
                )
            except Exception as exc:
                # Fail-open: Haiku is infrastructure, not a correctness
                # layer. A transient error should not drop a real signal.
                logger.warning("haiku_screen_call_failed symbol=%s err=%s", sym, exc)
                haiku_verdict = None

            # Tally verdicts for the per-cycle observability rates the
            # dashboard renders. ``None`` means we failed open (timeout /
            # parse / no key); ``valid=False`` means Haiku rejected.
            # ``valid=True`` doesn't increment either — pass-throughs are
            # the unspoken denominator.
            if haiku_verdict is None:
                state.haiku_none_count += 1
            elif not haiku_verdict.valid:
                state.haiku_reject_count += 1

        # Explicit reject → short-circuit. None (fail-open) and True
        # (pass) both proceed to Opus.
        if haiku_verdict is not None and not haiku_verdict.valid:
            logger.info(
                "autotrade_haiku_reject symbol=%s direction=%s reason=%r",
                sym, tier1_direction, haiku_verdict.reason[:120],
            )
            return hit, sym_bars, None, None, haiku_verdict

        sym_news = None
        try:
            sym_news = await asyncio.to_thread(mkt_client.fetch_news, [sym], 10)
        except Exception:
            logger.warning("autotrade_news_fetch_failed symbol=%s", sym)

        if state.enable_ai_gate:
            # Run analyze_chart + news classifier in parallel — both are Claude
            # calls, both depend only on the fetched bars/news. The classifier
            # is a Haiku call (~1-2s) so it usually finishes while Sonnet is
            # still thinking. No extra wall-time on the scan loop.
            analyze_coro = asyncio.to_thread(
                analyze_chart, sym, sym_bars, daily_bars, extended_bars,
                sym_snapshot, sector_snapshots, sym_news,
            )
            news_coro = _classify_news_cached(sym, sym_news)

            try:
                result, news_verdict = await asyncio.gather(
                    analyze_coro, news_coro, return_exceptions=False,
                )
            except Exception as exc:
                logger.error("autotrade_analysis_failed symbol=%s error=%s", sym, exc)
                return hit, sym_bars, None, None, haiku_verdict
        else:
            # v7.12.1 (2026-05-01): Layer 2 disabled — skip the Sonnet
            # analyze_chart call entirely (saves the API spend) and
            # synthesize the recommendation from the Layer-1 predicate
            # match. The Haiku NEWS classifier still runs because it
            # feeds the news_multiplier downstream — it's a cheap call
            # (~1-2s, ~$0.001/setup) and gives the trade a fair shot at
            # being damped/boosted by genuine catalyst flow.
            try:
                news_verdict = await _classify_news_cached(sym, sym_news)
            except Exception as exc:
                logger.warning(
                    "autotrade_news_classify_failed symbol=%s err=%s", sym, exc,
                )
                news_verdict = None
            result = _build_predicate_only_result(hit, sym_bars)
            if result is None:
                # Predicate didn't yield a usable direction (mixed
                # long/short matches, missing bars, etc.) — treat the
                # same as a Sonnet None: skip without firing.
                logger.info(
                    "autotrade_predicate_only_skip symbol=%s reason=no_result",
                    sym,
                )
                # v7.16+ (2026-05-14): record the rejection with enough
                # detail to diagnose later. Mirror the None-return
                # conditions in _build_predicate_only_result so the
                # daily summary can say WHY (direction_undeterminable,
                # no_bars, no_strategies).
                _matched_list = hit.get("matched_strategies") or []
                if _matched_list:
                    if _matched_list[0].get("direction") not in ("long", "short"):
                        _po_detail = (
                            f"matched_strategies[0].direction="
                            f"{_matched_list[0].get('direction')!r} "
                            f"(name={_matched_list[0].get('name')!r}); "
                            f"predicate-only path requires explicit long/short"
                        )
                    elif not sym_bars:
                        _po_detail = "no bars available for synthetic entry price"
                    else:
                        _po_detail = (
                            "predicate-only builder returned None for unknown reason"
                        )
                    from . import matched_outcomes as _matched_outcomes
                    _matched_outcomes.record(
                        symbol=sym,
                        matched_strategies=_matched_list,
                        outcome="blocked",
                        reason="no_result",
                        detail=_po_detail,
                        scan_n=state.scan_count,
                    )
                return hit, sym_bars, None, news_verdict, haiku_verdict

        # ── RS context (#12) ── attach the (stock − SPY) excess-return
        # snapshot to the result so the composition block below can
        # compute the Relative Strength multiplier without re-reading
        # sector_snapshots. This duplicates one call to
        # compute_sector_context() that the analyzer already made
        # internally — acceptable because it's a pure-function arithmetic
        # helper and keeps the RS plumbing isolated from the analyzer's
        # prompt-building surface.
        if result is not None:
            try:
                sym_change_pct = _snapshot_change_pct(sym_snapshot)
                if sym_change_pct is not None:
                    rs_ctx = compute_sector_context(
                        sym, sym_change_pct, sector_snapshots,
                    )
                    if rs_ctx:
                        result["_rs_context"] = rs_ctx
            except Exception as exc:
                # Purely additive signal — a failure here must not lose the trade.
                logger.warning("autotrade_rs_context_failed symbol=%s err=%s", sym, exc)

        return hit, sym_bars, result, news_verdict, haiku_verdict

    # Run every candidate analysis concurrently — ~3x speedup on the slow AI step
    analyses = await asyncio.gather(*[_analyze_one(hit) for hit in candidates])

    # Process results sequentially (ordered state mutation: trade logs, approvals)
    for hit, bars, result, news_verdict, haiku_verdict in analyses:
        if not state.running:
            break
        if _executed_trade_count() >= state.max_trades_per_day:
            break
        if result is None or not bars:
            continue

        symbol = hit["symbol"]
        rec = result.get("recommendation", {})
        direction = rec.get("direction", "wait")
        raw_confidence = float(rec.get("confidence", 0) or 0)
        setup = rec.get("setup", "")

        # Structured probabilities (#10) — the analyzer's Pydantic validator
        # guarantees either all three are filled or all three are None
        # (for truly malformed responses). Coerce to float safely; if they're
        # missing, we persist None and Brier scoring will skip this trade.
        def _opt_float(x):
            if x is None:
                return None
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        p_target  = _opt_float(rec.get("p_target"))
        p_stop    = _opt_float(rec.get("p_stop"))
        p_neither = _opt_float(rec.get("p_neither"))

        # ── Per-(strategy, symbol) calibration (#6) ──
        # Pull the Tier-1 strategy names that matched for THIS symbol + direction,
        # look each one up in the calibration table, and average their multipliers.
        # Direction filter: a Long-side strategy on this symbol has no bearing on
        # a trade Claude is taking short, even if the scanner fired both.
        matched = hit.get("matched_strategies") or []
        if direction in ("long", "short"):
            aligned = [m for m in matched if m.get("direction") == direction]
        else:
            aligned = []

        # ── Hard regime veto (2026-04-25) ──
        # Long trades during a confirmed downtrend (and short trades
        # during a confirmed uptrend) are structurally adverse. The
        # post-mortem on the low Tier-1 win rate showed the existing
        # soft regime multiplier wasn't strong enough to drag those
        # trades below the firing threshold. Reject outright before
        # composing any of the per-strategy multipliers — saves the
        # downstream work and keeps the rejection visible in the log.
        # Choppy + unknown regimes are NOT vetoed (see regime.py).
        if regime_snap is not None and _is_regime_vetoed(direction, regime_snap.regime):
            logger.info(
                "autotrade_regime_veto symbol=%s direction=%s regime=%s "
                "raw_conf=%.2f setup=%r",
                symbol, direction, regime_snap.regime, raw_confidence, setup,
            )
            # v7.16+ (2026-05-14): observability — record why this match
            # was blocked so the daily summary email surfaces it.
            from . import matched_outcomes as _matched_outcomes
            _matched_outcomes.record(
                symbol=symbol,
                matched_strategies=matched,
                outcome="blocked",
                reason="regime_veto",
                detail=(
                    f"direction={direction} regime={regime_snap.regime} "
                    f"setup={setup!r}"
                ),
                scan_n=state.scan_count,
                extra={
                    "raw_confidence": raw_confidence,
                    "regime":         regime_snap.regime,
                    "direction":      direction,
                },
            )
            continue

        per_strategy_cal: list[dict] = []
        for m in aligned:
            strat_name = m.get("name")
            if not strat_name:
                continue
            mult, meta = _get_calibration_multiplier(
                strat_name, symbol, raw_confidence=raw_confidence,
            )
            per_strategy_cal.append(meta)

        if per_strategy_cal:
            mults = [c["multiplier"] for c in per_strategy_cal]
            avg_mult = sum(mults) / len(mults)
            # Pick the "best" reason among cell > strategy_fallback > no_data so
            # the UI reflects the strongest signal backing the multiplier.
            reason_rank = {"cell": 2, "strategy_fallback": 1, "no_data": 0}
            best_meta = max(per_strategy_cal, key=lambda c: reason_rank.get(c.get("reason"), -1))
            cal_reason = best_meta.get("reason")
            cal_n = best_meta.get("n") or best_meta.get("strategy_n") or 0
        else:
            avg_mult = 1.0
            cal_reason = "no_data"
            cal_n = 0

        # ── Regime multiplier (#8) ── compose with the per-(strategy, symbol)
        # multiplier from #6. Trend-following setups get damped in chop,
        # mean-reversion setups get damped in trends. Unknown regime → 1.0.
        regime_mult, regime_meta = _regime_multiplier(regime_snap, aligned)

        # ── News catalyst multiplier (#9) ── compose with #6 + #8. Claude
        # classifies each recent article ("would this flip my thesis?");
        # bullish catalyst + long trade → confirming boost (1.05–1.10×),
        # bearish catalyst + long trade → contradicting damp (0.50×), and
        # HIGH-severity contradictions drop to 0.20× (veto-tier — the trade
        # stops firing because the product falls below the 0.80 gate).
        # Unavailable classifier → no-op 1.0.
        news_mult, news_meta = _news_multiplier(news_verdict, direction)

        # ── Brier calibration multiplier (#10 — feedback loop) ── compose
        # with #6 + #8 + #9. Closes the "Claude said 80% but only 60% hit"
        # loop: look up the p_target decile in the rolling 30-day
        # reliability diagram and apply (empirical / predicted), clamped
        # to [0.5, 1.2]. Low-N bucket, missing snapshot, or missing
        # probability → no-op 1.0, matching the degraded-no-op discipline
        # used by regime + news. We key on p_target (not confidence) so
        # the lookup uses the raw probability Claude emitted; both are
        # equal post-Pydantic-validator but p_target is the authoritative
        # name for the thing being calibrated.
        brier_mult, brier_meta = brier.calibration_multiplier(
            p_target, _calibration_summary,
        )

        # ── Relative Strength multiplier (#12) ── compose with the rest
        # of the stack. Boosts trades on stocks genuinely leading (long)
        # or lagging (short) the tape, dampens trades that are fighting
        # their own relative-strength signal. Degrades to 1.0 when no
        # sector snapshot is available (e.g. trading SPY itself, or the
        # feed missed). Clamped to [0.70, 1.20] inside rel_strength.py.
        #
        # Per-strategy bypass (v5, 2026-04-21): the v5 post-hoc showed RS
        # filtering actively *harms* the Opening Range Retest family (the
        # retest setup already selects extended leaders — RS ends up
        # double-counting the signal). When every aligned Tier-1 match is
        # in ``RS_DISABLED_STRATEGIES``, force the multiplier to 1.0 and
        # attach a meta flag so the approval card / logs surface why.
        # Mixed-match trades (e.g. ORR Long + Gap-and-Go Long) keep RS
        # engaged — the evidence is specific to ORR-only trades.
        #
        # Per-strategy RS profile (v5.1, 2026-04-21): each aligned match
        # classifies the tape through its own ``STRATEGY_RS_PROFILES``
        # entry — so mean-reversion setups (Gap Fill Short) flip the
        # leader/lagger interpretation, and strategies with evidence-
        # backed neutral drag (GFS 0.70, ORB Short 0.80) damp neutral
        # more aggressively than the baseline. Averaged across aligned
        # names the same way regime is, with the first match's snapshot
        # standing in as the representative label/state for the UI chip.
        rs_ctx = result.get("_rs_context") or {}
        aligned_names = [m.get("name") for m in aligned if m.get("name")]
        rs_disabled = bool(aligned_names) and all(
            n in RS_DISABLED_STRATEGIES for n in aligned_names
        )
        if rs_disabled:
            rs_mult = 1.0
            rs_meta = {
                "state":           "bypassed",
                "direction":       direction,
                "stock_vs_spy":    rs_ctx.get("stock_vs_spy"),
                "spy_change_pct":  rs_ctx.get("spy_change_pct"),
                "multiplier":      1.0,
                "label":           "RS bypassed",
                "reason": (
                    "RS multiplier bypassed — every aligned Tier-1 match "
                    f"({', '.join(aligned_names)}) is on the RS-disabled "
                    "list (v5 post-hoc showed RS filtering hurts these "
                    "setups)."
                ),
            }
        elif aligned_names:
            per_rs: list[dict] = []
            rs_meta = None
            for name in aligned_names:
                mult_i, meta_i = _rs_multiplier(
                    rs_ctx.get("stock_vs_spy"),
                    rs_ctx.get("spy_change_pct"),
                    direction,
                    regime=regime_snap.regime if regime_snap else None,
                    strategy_name=name,
                )
                per_rs.append({
                    "strategy":   name,
                    "multiplier": round(mult_i, 3),
                    "state":      meta_i.get("state"),
                    "label":      meta_i.get("label"),
                })
                if rs_meta is None:
                    rs_meta = dict(meta_i)  # representative snapshot for UI chip
            rs_mult = sum(p["multiplier"] for p in per_rs) / len(per_rs)
            rs_meta["multiplier"]   = round(rs_mult, 3)
            rs_meta["per_strategy"] = per_rs
        else:
            # No aligned matches — fall back to profile-less classify so the
            # degraded-no-op path still surfaces stock_vs_spy / label.
            rs_mult, rs_meta = _rs_multiplier(
                rs_ctx.get("stock_vs_spy"),
                rs_ctx.get("spy_change_pct"),
                direction,
                regime=regime_snap.regime if regime_snap else None,
            )

        # ── Hour-of-day multiplier (#12.5) ── evidence-backed per-strategy
        # hour weight. Currently only Gap Fill Reversal (Short) has
        # populated cells (11 ET → 1.15, 12 ET → 0.85); every other
        # strategy + hour passes through at 1.0 (see ``hour_weights.py``).
        # The Hour × RS cross-tab for Gap Fill Short (``data/
        # rs_hour_crosstab_gfs.txt``) confirmed the 11 ET edge is
        # INDEPENDENT of the RS leader filter (observed/expected = 1.01),
        # so this composes cleanly with ``rs_mult`` rather than double-
        # counting the same trades.
        hour_et_now = datetime.now(_ET).hour
        hour_mult, hour_meta = _hour_multiplier(aligned, hour_et_now)

        # Final = raw × calibration (#6) × regime (#8) × RS (#12) × hour (#12.5)
        # × news (#9) × brier (#10), clamped to [0, 1]. All six stack
        # multiplicatively: a setup that's well-calibrated per-strategy,
        # in-regime, actually leading its peers, firing in a proven
        # hour window, has a confirming catalyst, AND sits in a
        # historically-accurate p_target bucket gets the full stack of
        # boosts. A poorly-calibrated + out-of-regime + lagging-vs-SPY +
        # wrong-hour + thesis-breaking-news + over-confident-bucket
        # setup is six-way dampened into oblivion.
        confidence = max(
            0.0,
            min(1.0, raw_confidence * avg_mult * regime_mult * rs_mult
                     * hour_mult * news_mult * brier_mult),
        )
        matched_names_csv = ",".join(
            m.get("name", "") for m in aligned if m.get("name")
        ) or None

        logger.info(
            "autotrade_analysis symbol=%s direction=%s raw=%.2f adj=%.2f "
            "cal_mult=%.2f regime_mult=%.2f rs_mult=%.2f hour_mult=%.2f "
            "news_mult=%.2f brier_mult=%.2f "
            "regime=%s rs_state=%s hour_et=%02d news_stance=%s news_impact=%s "
            "brier_reason=%s brier_bucket=%s brier_n=%s "
            "cal_reason=%s n=%d",
            symbol, direction, raw_confidence, confidence,
            avg_mult, regime_mult, rs_mult, hour_mult, news_mult, brier_mult,
            regime_meta.get("regime"), rs_meta.get("state"), hour_et_now,
            news_meta.get("stance"), news_meta.get("impact"),
            brier_meta.get("reason"), brier_meta.get("bucket"), brier_meta.get("n"),
            cal_reason, cal_n,
        )

        if direction == "wait" or confidence < state.min_confidence:
            # v7.16.x (2026-05-22) — observability: record WHY a matched
            # signal didn't trade, so the daily summary stops falsely
            # claiming "no matched signals processed". Two sub-reasons
            # at this gate: a "wait" direction (AI path) or an adjusted
            # confidence below the floor (the COIN case on 2026-05-21,
            # raw 0.85 dampened to 0.55 by regime/RS/news multipliers).
            from . import matched_outcomes as _matched_outcomes
            if direction == "wait":
                _gate_reason = "direction_wait"
                _gate_detail = f"setup={setup!r} — analyzer returned direction=wait"
            else:
                _gate_reason = "low_confidence"
                _gate_detail = (
                    f"adjusted confidence {confidence:.2f} < floor "
                    f"{state.min_confidence:.2f} (raw {raw_confidence:.2f}, "
                    f"setup={setup!r})"
                )
            _extra: dict[str, Any] = {
                "raw_confidence":      raw_confidence,
                "adjusted_confidence": round(confidence, 4),
                "min_confidence":      state.min_confidence,
            }
            # For low-confidence blocks, persist the full multiplier breakdown
            # so "why was 0.85 cut to 0.59?" is answerable from the JSONL
            # after the fact (stdout logs aren't retained). Each factor is
            # already computed above for the `autotrade_analysis` log line;
            # we just record it. (cal_mult is the `avg_mult` calibration term.)
            if _gate_reason == "low_confidence":
                _extra.update({
                    "cal_mult":     round(avg_mult, 4),
                    "regime_mult":  round(regime_mult, 4),
                    "rs_mult":      round(rs_mult, 4),
                    "hour_mult":    round(hour_mult, 4),
                    "news_mult":    round(news_mult, 4),
                    "brier_mult":   round(brier_mult, 4),
                    "regime":       regime_meta.get("regime"),
                    "rs_state":     rs_meta.get("state"),
                    "news_stance":  news_meta.get("stance"),
                    "brier_reason": brier_meta.get("reason"),
                })
            _matched_outcomes.record(
                symbol=symbol,
                matched_strategies=matched,
                outcome="blocked",
                reason=_gate_reason,
                detail=_gate_detail,
                scan_n=state.scan_count,
                extra=_extra,
            )
            continue

        # ── Get current price for the trade ──
        prediction_price = _latest_price(bars)
        if prediction_price is None:
            continue

        # v7.15: surface the producing scanner's id. Default "ict" to
        # match the v7.14 column default for any hit that somehow
        # reaches this point without one (legacy code path, bug, etc.).
        hit_scanner_id = (hit.get("scanner_id") or "ict")

        # v7.15: experimental daily cap (independent of state.max_trades_per_day,
        # which is the global cap). Skip the experimental hit with an
        # observable log entry; ICT hits are unaffected.
        if hit_scanner_id == "experimental_v1":
            exp_today = _executed_trade_count_by_scanner("experimental_v1")
            if exp_today >= settings.experimental_max_trades_per_day:
                logger.info(
                    "experimental_skip reason=daily_cap_reached symbol=%s exp_today=%d cap=%d",
                    symbol, exp_today, settings.experimental_max_trades_per_day,
                )
                continue

        # v7.15: belt-and-suspenders — refuse to size an order whose
        # hit is missing scanner_id when require_scanner_id is on.
        # Catches a future refactor bug that could strip the field.
        if settings.experimental_require_scanner_id and not hit.get("scanner_id"):
            logger.error(
                "autotrade_submit_blocked reason=missing_scanner_id symbol=%s setup=%r",
                symbol, setup,
            )
            continue

        # Compute position size (risk-based + adjusted-confidence scaling +
        # v7.15 experimental risk multiplier when scanner_id is experimental).
        stop_price = _parse_price(rec.get("stop_loss"))
        sizing = await _compute_position_qty(
            prediction_price, stop_price, confidence, scanner_id=hit_scanner_id,
        )
        qty = sizing["qty"]

        trade_log = TradeLog(
            time=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            side="buy" if direction == "long" else "sell",
            qty=qty,
            order_type="bracket" if (rec.get("stop_loss") and rec.get("target")) else "market",
            confidence=confidence,
            setup=setup,
            status="pending_approval",
            prediction_price=prediction_price,
            entry_zone=rec.get("entry_zone"),
            stop_loss=rec.get("stop_loss"),
            target=rec.get("target"),
            risk_reward=rec.get("risk_reward"),
            reasoning=result.get("reasoning", ""),
            analysis_context=result.get("_analysis_prompt", ""),
            matched_strategies=matched_names_csv,
            raw_confidence=raw_confidence,
            calibration_multiplier=round(avg_mult, 3),
            calibration_reason=cal_reason,
            calibration_n=cal_n,
            calibration_detail={"per_strategy": per_strategy_cal} if per_strategy_cal else None,
            regime=regime_meta.get("regime"),
            regime_label=regime_meta.get("label"),
            regime_multiplier=round(regime_mult, 3),
            regime_detail=regime_meta if regime_meta.get("per_strategy") else None,
            news_stance=news_meta.get("stance"),
            news_impact=news_meta.get("impact"),
            news_severity=news_meta.get("severity"),
            news_label=news_meta.get("label"),
            news_multiplier=round(news_mult, 3),
            news_detail=news_meta if news_meta.get("articles") else None,
            # Structured probabilities (#10) — captured from the analyzer's
            # normalized recommendation dict. outcome_class stays None until
            # the monitor classifies the exit.
            p_target=p_target,
            p_stop=p_stop,
            p_neither=p_neither,
            # Brier-calibration feedback loop (#10) — multiplier derived
            # from the p_target decile's historical hit rate, plus the
            # full reliability-bucket breakdown so the approval UI can
            # explain "Claude's 0.8-0.9 bucket hits at 60%, n=24 → 0.71×".
            brier_multiplier=round(brier_mult, 3),
            brier_detail=brier_meta,
            # Haiku pre-screen (#11) — every trade_log that reaches this
            # point either passed the Haiku gate or the gate was unreachable
            # (fail-open). haiku_screen_valid is True on a pass, None when
            # the screener didn't run; False rejects never reach this code.
            haiku_screen_valid=(haiku_verdict.valid if haiku_verdict else None),
            haiku_screen_reason=(haiku_verdict.reason if haiku_verdict else None),
            # Relative Strength / Relative Weakness (#12) — soft multiplier
            # surfaced on the approval card. ``rs_detail`` is the full
            # meta dict (state, multiplier, label, reason) so the UI
            # tooltip can explain the classification.
            rs_state=rs_meta.get("state"),
            rs_label=rs_meta.get("label"),
            rs_stock_vs_spy=rs_meta.get("stock_vs_spy"),
            rs_spy_change_pct=rs_meta.get("spy_change_pct"),
            rs_multiplier=round(rs_mult, 3),
            rs_detail=rs_meta,
            # Hour-of-day multiplier (#12.5) — one axis, one value per
            # (strategy, hour), composed multiplicatively with the rest of
            # the stack. hour_detail carries the per-strategy breakdown
            # only when at least one match contributed a non-neutral cell.
            hour_et=hour_et_now,
            hour_multiplier=round(hour_mult, 3),
            hour_detail=hour_meta if hour_meta.get("per_strategy") else None,
            # Universe provenance tag (v7.13 Phase 1.5) — drives the
            # 30-day "did dynamic symbols help?" audit.
            universe_source=get_universe_source(symbol),
            # v7.15 (2026-05-18) — producing scanner. Threaded all the
            # way through to ``save_trade_opened`` below so the
            # ``TradeRecord.scanner_id`` column matches the in-memory
            # TradeLog. The daily-cap counter
            # (``_executed_trade_count_by_scanner``) reads this field.
            scanner_id=hit_scanner_id,
            # 2026-05-28 audit-bug fix — capture context_at_firing HERE,
            # where ``hit`` / ``regime_meta`` / ``news_meta`` are still
            # in scope. The save at trade-open time (in _execute_trade)
            # just reads ``trade_log.context_at_firing`` — no re-scoping
            # the scan-time locals across a coroutine boundary.
            context_at_firing=_build_context_safe(
                hit, regime_meta, news_meta, state.applied_universe_metadata,
            ),
        )
        state.trades_today.append(trade_log)

        # ── Simulator: log every AI recommendation for hypothetical tracking ──
        if state.active_user_id:
            try:
                sim_id = await asyncio.to_thread(
                    log_simulated_trade,
                    state.active_user_id,
                    symbol=symbol,
                    direction=direction,
                    setup=setup,
                    confidence=confidence,
                    entry_price=prediction_price,
                    stop_loss=_parse_price(rec.get("stop_loss")),
                    target=_parse_price(rec.get("target")),
                    entry_zone=rec.get("entry_zone"),
                    risk_reward=rec.get("risk_reward"),
                    reasoning=result.get("reasoning", ""),
                    qty=qty,
                )
                trade_log.sim_trade_id = sim_id
            except Exception as exc:
                logger.warning("sim_trade_log_error symbol=%s: %s", symbol, exc)

        # ── Immediate approval request (no confirmation delay) ──
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=state.approval_timeout_seconds)

        approval = PendingApproval(
            id=_next_approval_id(),
            trade_log=trade_log,
            rec=rec,
            direction=direction,
            created=now.isoformat(),
            expires=expires.isoformat(),
            sizing=sizing,
        )
        state.pending_approvals.append(approval)

        logger.info(
            "autotrade_approval_ready symbol=%s direction=%s confidence=%.2f price=%.2f qty=%d",
            symbol, direction, confidence, prediction_price, qty,
        )
        emit_trade_event("approval_requested", {
            "symbol": symbol, "direction": direction,
            "confidence": trade_log.confidence, "approval_id": approval.id,
        })
        _push_status()

        # ── v7.16.x (2026-05-21): autonomous mode — auto-approve ──
        # When autonomous mode is on, immediately approve the trade we
        # just created instead of leaving an approval card to expire.
        # Every gate (confidence floor, regime veto, dup-position,
        # max-open, daily-loss circuit breaker, sizing, paper-only)
        # has ALREADY been cleared above — the approval would not exist
        # otherwise. Auto-approve only removes the human click; it does
        # not bypass any safety check.
        #
        # Bounded by ``autonomous_max_trades_per_day``: count trades
        # already fired today (``_EXECUTED_STATUSES`` covers submitted/
        # filled/etc.; "approved" catches a trade auto-approved earlier
        # THIS cycle whose _execute_trade task hasn't run yet). At the
        # cap, the approval card is left in place for optional manual
        # review — only the auto-approve is skipped.
        if state.autonomous_mode:
            # 2026-07-06 P1 fix: count terminal statuses too — closed trades
            # must not re-arm the daily cap (see _DAILY_COUNTED_STATUSES).
            _fired_today = sum(
                1 for t in state.trades_today
                if t.status in _DAILY_COUNTED_STATUSES or t.status == "approved"
            )
            _cap = settings.autonomous_max_trades_per_day
            if _fired_today >= _cap:
                logger.info(
                    "autonomous_skip reason=daily_cap_reached symbol=%s "
                    "fired_today=%d cap=%d — approval card left for manual review",
                    symbol, _fired_today, _cap,
                )
            else:
                logger.info(
                    "autonomous_auto_approve symbol=%s confidence=%.2f qty=%d "
                    "fired_today=%d cap=%d",
                    symbol, confidence, qty, _fired_today, _cap,
                )
                try:
                    approve_trade(approval.id)
                except Exception:
                    # Never let an auto-approve failure break the scan
                    # loop — the approval card survives for manual use.
                    logger.exception(
                        "autonomous_auto_approve_failed symbol=%s", symbol,
                    )



async def _execute_trade(trade_log: TradeLog, rec: dict, direction: str):
    """Tier 5: Actually submit the order to Alpaca."""
    if state.circuit_breaker_tripped:
        trade_log.status = "failed"
        trade_log.error = "Circuit breaker tripped — daily loss limit reached"
        return

    # v7.15 (2026-05-18): belt-and-suspenders scanner_id assertion.
    # If a TradeLog reached _execute_trade with an empty scanner_id
    # AND the operator has require_scanner_id on (default), refuse
    # the submit. This is the LAST gate before we hit Alpaca — making
    # it impossible for an un-attributed order to land in TradeRecord.
    if settings.experimental_require_scanner_id and not trade_log.scanner_id:
        trade_log.status = "failed"
        trade_log.error = (
            "submit_blocked: missing scanner_id on trade_log. "
            "EXPERIMENTAL_REQUIRE_SCANNER_ID=true rejects un-attributed orders."
        )
        logger.error(
            "autotrade_submit_blocked reason=missing_scanner_id symbol=%s",
            trade_log.symbol,
        )
        emit_trade_event("order_failed", {
            "symbol": trade_log.symbol, "error": trade_log.error,
        })
        _push_status()
        return

    # Cash accounts cannot short — Alpaca rejects the order and it's an easy way
    # to flag the rec as failed before we spend time on further checks. On live
    # (non-paper) we treat the account as cash since this is a cash-account app.
    if direction == "short" and not trading_mode.is_paper():
        trade_log.status = "failed"
        trade_log.error = "Short trades are not supported on a cash account — skipping."
        logger.warning("autotrade_short_blocked symbol=%s reason=cash_account", trade_log.symbol)
        emit_trade_event("order_failed", {"symbol": trade_log.symbol, "error": trade_log.error})
        _push_status()
        return

    # GFV-aware guard: if sizing returned qty=0, all of today's cash is
    # unsettled. Submitting anyway and then selling would trigger a Good Faith
    # Violation — refuse outright and wait for T+1 settlement.
    if not trade_log.qty or trade_log.qty <= 0:
        trade_log.status = "failed"
        trade_log.error = (
            "Skipped to avoid Good Faith Violation — today's sell proceeds "
            "are still unsettled (T+1). Waiting for settled cash."
        )
        logger.warning("autotrade_gfv_guard symbol=%s reason=unsettled_cash", trade_log.symbol)
        emit_trade_event("order_failed", {"symbol": trade_log.symbol, "error": trade_log.error})
        _push_status()
        return

    # PDT guard (margin accounts under $25k only). Cash accounts skip this
    # since they're PDT-exempt, but the check is cheap and defensive against
    # future account-type changes.
    if not trading_mode.is_paper():
        try:
            pdt_client = get_shared_trading_client()
            pdt_acct = await asyncio.to_thread(pdt_client.get_account)
            multiplier = float(pdt_acct.get("multiplier", 1) or 1)
            equity = float(pdt_acct.get("equity", 0) or 0)
            daytrade_count = int(pdt_acct.get("daytrade_count", 0) or 0)
            already_pdt = bool(pdt_acct.get("pattern_day_trader"))
            if multiplier > 1.0 and (already_pdt or (equity < 25_000 and daytrade_count >= 3)):
                trade_log.status = "failed"
                trade_log.error = (
                    "Skipped to avoid Pattern Day Trader flag — "
                    f"daytrade_count={daytrade_count}/3 on a margin account "
                    f"with equity ${equity:,.2f} (below $25,000 PDT threshold)."
                )
                logger.warning(
                    "autotrade_pdt_guard symbol=%s daytrade_count=%d equity=%.2f already_pdt=%s",
                    trade_log.symbol, daytrade_count, equity, already_pdt,
                )
                emit_trade_event("order_failed", {"symbol": trade_log.symbol, "error": trade_log.error})
                _push_status()
                return
        except Exception as exc:
            logger.warning("autotrade_pdt_check_failed error=%s — continuing", exc)

    # Re-check the daily-loss breaker with this trade's worst-case loss projected
    # in. Closes the race where earlier fills moved us near the cap between the
    # scan-time check and now.
    would_breach, breach_reason = await _would_breach_daily_loss(trade_log, direction)
    if would_breach:
        trade_log.status = "failed"
        trade_log.error = breach_reason
        state.circuit_breaker_tripped = True
        state.last_error = breach_reason
        emit_trade_event("circuit_breaker", {"symbol": trade_log.symbol, "reason": breach_reason})
        _push_status()
        return

    # Final duplicate check before execution
    blocked = await _get_blocked_symbols()
    # Remove self from blocked (this trade's symbol was in pending_approvals, now removed)
    blocked.discard(trade_log.symbol)

    # In-memory dedup FIRST (2026-07-06 P1 fix): the monitor's own open-
    # position list needs no API call and therefore cannot fail open. This
    # closes the cooldown(15m) < max-hold(60m) window where a still-open
    # symbol re-qualified while the broker checks below were degraded.
    if _holding_symbol(trade_log.symbol):
        trade_log.status = "failed"
        trade_log.error = f"Already monitoring an open position in {trade_log.symbol}"
        logger.warning("autotrade_duplicate_blocked symbol=%s (monitored position)", trade_log.symbol)
        return

    # Broker-truth checks — FAIL CLOSED (2026-07-06 P1 fix): if Alpaca can't
    # confirm we are NOT already in this symbol, do not submit. Skipping an
    # entry costs one setup; failing open here is how a degraded API cycle
    # doubles a position (all the earlier guards hit the same endpoint and
    # fail together). The setup can re-fire on a later scan cycle.
    trade_client = get_shared_trading_client()
    positions: list = []
    try:
        positions = await asyncio.to_thread(trade_client.list_positions)
        if any(p["symbol"] == trade_log.symbol for p in positions):
            trade_log.status = "failed"
            trade_log.error = f"Already have an open position in {trade_log.symbol}"
            logger.warning("autotrade_duplicate_blocked symbol=%s (existing position)", trade_log.symbol)
            return
    except Exception as exc:
        trade_log.status = "failed"
        trade_log.error = f"Duplicate check unavailable (positions): {exc}"
        logger.warning(
            "autotrade_dup_check_failed_closed symbol=%s check=positions err=%s — entry skipped",
            trade_log.symbol, exc,
        )
        return

    try:
        open_orders = await asyncio.to_thread(trade_client.list_orders, "open")
        if any(o.get("symbol") == trade_log.symbol for o in open_orders):
            trade_log.status = "failed"
            trade_log.error = f"Already have a pending order for {trade_log.symbol}"
            logger.warning("autotrade_duplicate_blocked symbol=%s (pending order)", trade_log.symbol)
            return
    except Exception as exc:
        trade_log.status = "failed"
        trade_log.error = f"Duplicate check unavailable (open orders): {exc}"
        logger.warning(
            "autotrade_dup_check_failed_closed symbol=%s check=orders err=%s — entry skipped",
            trade_log.symbol, exc,
        )
        return

    # ── Correlated position protection: max N positions per sector ──
    try:
        new_sector = get_sector_etf(trade_log.symbol)
        sector_name = _ETF_NAMES.get(new_sector, new_sector)

        # Count open positions in the same sector (from Alpaca + monitored)
        sector_count = 0
        if positions:
            for p in positions:
                if get_sector_etf(p["symbol"]) == new_sector:
                    sector_count += 1

        # Also count monitored positions not yet reflected in Alpaca
        for m in state.monitored_positions:
            if not m.closed and get_sector_etf(m.symbol) == new_sector:
                # Avoid double-counting symbols already in Alpaca positions
                if not positions or not any(p["symbol"] == m.symbol for p in positions):
                    sector_count += 1

        if sector_count >= state.max_per_sector:
            trade_log.status = "failed"
            trade_log.error = (
                f"Sector limit: already {sector_count} position(s) in "
                f"{sector_name} ({new_sector}). Max is {state.max_per_sector}."
            )
            logger.warning(
                "autotrade_sector_blocked symbol=%s sector=%s(%s) count=%d max=%d",
                trade_log.symbol, sector_name, new_sector, sector_count, state.max_per_sector,
            )
            emit_trade_event("order_failed", {
                "symbol": trade_log.symbol,
                "error": trade_log.error,
            })
            _push_status()
            return

        logger.info(
            "autotrade_sector_ok symbol=%s sector=%s(%s) count=%d/%d",
            trade_log.symbol, sector_name, new_sector, sector_count, state.max_per_sector,
        )
    except Exception as exc:
        logger.warning("autotrade_sector_check_error: %s — proceeding anyway", exc)

    # Fetch current price for slippage check (but don't override user's qty choice)
    current_price = await _fetch_symbol_price(trade_log.symbol)

    # ── Slippage-aware entry: reject if price moved past the AI's entry zone ──
    ai_stop_loss = _parse_price(rec.get("stop_loss"))
    ai_take_profit = _parse_price(rec.get("target"))

    if current_price and current_price > 0:
        entry_zone_str = rec.get("entry_zone", "")
        zone_prices = re.findall(r"[\d]+(?:\.[\d]+)?", str(entry_zone_str))
        if len(zone_prices) >= 2:
            zone_low = float(zone_prices[0])
            zone_high = float(zone_prices[1])
            if zone_low > zone_high:
                zone_low, zone_high = zone_high, zone_low
        elif len(zone_prices) == 1:
            # Single price entry zone — treat as a tight band (±0.1%)
            mid = float(zone_prices[0])
            zone_low = mid * 0.999
            zone_high = mid * 1.001
        else:
            zone_low = zone_high = None

        if zone_low is not None and zone_high is not None:
            # Allow a buffer of 0.5% beyond the entry zone before rejecting
            zone_range = zone_high - zone_low if zone_high > zone_low else zone_high * 0.002
            slippage_buffer = max(zone_range * 0.5, zone_high * 0.005)

            if direction == "long" and current_price > zone_high + slippage_buffer:
                slip_pct = (current_price - zone_high) / zone_high * 100
                trade_log.status = "failed"
                trade_log.error = (
                    f"Slippage rejection: price ${current_price:.2f} is {slip_pct:.2f}% "
                    f"above entry zone ${zone_low:.2f}–${zone_high:.2f}"
                )
                logger.warning(
                    "autotrade_slippage_rejected symbol=%s dir=%s price=%.2f zone=%.2f-%.2f slip=%.2f%%",
                    trade_log.symbol, direction, current_price, zone_low, zone_high, slip_pct,
                )
                emit_trade_event("order_failed", {
                    "symbol": trade_log.symbol,
                    "error": trade_log.error,
                })
                _push_status()
                return

            if direction == "short" and current_price < zone_low - slippage_buffer:
                slip_pct = (zone_low - current_price) / zone_low * 100
                trade_log.status = "failed"
                trade_log.error = (
                    f"Slippage rejection: price ${current_price:.2f} is {slip_pct:.2f}% "
                    f"below entry zone ${zone_low:.2f}–${zone_high:.2f}"
                )
                logger.warning(
                    "autotrade_slippage_rejected symbol=%s dir=%s price=%.2f zone=%.2f-%.2f slip=%.2f%%",
                    trade_log.symbol, direction, current_price, zone_low, zone_high, slip_pct,
                )
                emit_trade_event("order_failed", {
                    "symbol": trade_log.symbol,
                    "error": trade_log.error,
                })
                _push_status()
                return

            logger.info(
                "autotrade_slippage_ok symbol=%s price=%.2f zone=%.2f-%.2f",
                trade_log.symbol, current_price, zone_low, zone_high,
            )

    # ── Market-hours gate — autotrade only runs during regular hours ──
    # Applies to both paper and live accounts. Extended-hours orders require
    # limit prices and different routing logic — autotrade isn't designed for that.
    now_et = datetime.now(_ET)
    t_min = now_et.hour * 60 + now_et.minute
    regular_hours = now_et.weekday() < 5 and 570 <= t_min < 960  # 9:30–16:00
    if not regular_hours:
        trade_log.status = "failed"
        trade_log.error = "Market is closed — autotrade only runs during regular hours (9:30 AM – 4 PM ET)."
        logger.warning("autotrade_order_skipped symbol=%s reason=market_closed", trade_log.symbol)
        emit_trade_event("order_failed", {"symbol": trade_log.symbol, "error": trade_log.error})
        _push_status()
        return

    side = OrderSide.BUY if direction == "long" else OrderSide.SELL

    # When trailing stop monitoring is enabled, submit a plain market order
    # (no bracket legs) — the monitor loop manages exits instead.
    # Bracket orders require BOTH legs; fall back to plain market if either is missing.
    use_bracket = (
        not state.enable_trailing_stop
        and ai_stop_loss is not None
        and ai_take_profit is not None
    )
    order_stop_loss = ai_stop_loss if use_bracket else None
    order_take_profit = ai_take_profit if use_bracket else None

    # Guard 1 (2026-06-15): reserve the symbol the instant we commit to
    # submitting, so neither a concurrent path nor the next scan cycle can
    # fire a second order for it within the cooldown window. Set before the
    # submit (covers a rejected order too — conservative, prevents retry spam).
    _recent_entry_ts[trade_log.symbol] = datetime.now(timezone.utc).timestamp()

    try:
        order = await asyncio.to_thread(
            trade_client.submit_order,
            symbol=trade_log.symbol,
            qty=trade_log.qty,
            side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            take_profit=order_take_profit,
            stop_loss=order_stop_loss,
        )
        trade_log.status = order.get("status", "submitted")
        trade_log.order_id = order.get("id")
        logger.info(
            "autotrade_order_placed symbol=%s side=%s qty=%d id=%s trailing=%s",
            trade_log.symbol, side.value, trade_log.qty, order.get("id"),
            state.enable_trailing_stop,
        )
        # v7.16+ (2026-05-14): observability — record the success case
        # so the daily summary can show "X submitted: order_id=..." alongside
        # blocked signals. matched_strategies isn't conveniently in scope
        # here (we've moved past the scanner output), so we pass None and
        # rely on the symbol + setup fields for context.
        from . import matched_outcomes as _matched_outcomes
        _matched_outcomes.record(
            symbol=trade_log.symbol,
            matched_strategies=None,
            outcome="submitted",
            reason="order_placed",
            detail=(
                f"side={side.value} qty={trade_log.qty} setup={trade_log.setup!r}"
            ),
            extra={
                "order_id":  order.get("id"),
                "direction": direction,
                "status":    order.get("status"),
            },
        )

        # ── Tier 6: Register for position monitoring ──
        if state.enable_trailing_stop or state.enable_time_exit:
            entry_price = current_price or trade_log.prediction_price or 0
            # Initial trailing stop: start at AI's stop loss, or fall back to trail %
            initial_stop = ai_stop_loss
            if initial_stop is None:
                if direction == "long":
                    initial_stop = entry_price * (1 - state.trailing_stop_pct)
                else:
                    initial_stop = entry_price * (1 + state.trailing_stop_pct)

            monitored = MonitoredPosition(
                symbol=trade_log.symbol,
                direction=direction,
                entry_price=entry_price,
                qty=trade_log.qty,
                order_id=order.get("id"),
                opened_at=datetime.now(timezone.utc).isoformat(),
                trade_log=trade_log,
                initial_stop=initial_stop,
                trailing_stop=initial_stop,
                highest_price=entry_price if direction == "long" else None,
                lowest_price=entry_price if direction == "short" else None,
                current_price=entry_price,
            )
            state.monitored_positions.append(monitored)
            logger.info(
                "monitor_registered symbol=%s direction=%s entry=%.2f stop=%.2f trail=%.1f%%",
                trade_log.symbol, direction, entry_price, initial_stop,
                state.trailing_stop_pct * 100,
            )

        # Persist to database. 2026-05-28 — isolated try/except: a save
        # failure must NEVER taint trade_log.status (the order ALREADY
        # cleared Alpaca; the row is just missing an audit trail). The
        # original code let a save-time NameError fall through to the
        # outer `except Exception` at the bottom of _execute_trade, which
        # logged ``autotrade_order_failed`` and set status="failed" —
        # the trade then looked rejected even though Alpaca filled it.
        # The ADBE 2026-05-28 09:59 trade was exactly this: -$175.50
        # locked in on Alpaca with zero local audit because
        # ``_build_context_safe(hit, ...)`` raised NameError. Fix
        # captures context_at_firing at scan-time (above, on TradeLog)
        # so this code path no longer references scan-only locals.
        if state.active_user_id:
            try:
                trade_log.db_record_id = save_trade_opened(
                    state.active_user_id,
                    symbol=trade_log.symbol,
                    side=trade_log.side,
                    direction=direction,
                    qty=trade_log.qty,
                    setup=trade_log.setup,
                    confidence=trade_log.confidence,
                    order_type=trade_log.order_type,
                    order_id=trade_log.order_id,
                    status=trade_log.status,
                    entry_price=current_price or trade_log.prediction_price,
                    prediction_price=trade_log.prediction_price,
                    confirmation_price=trade_log.confirmation_price,
                    price_move_pct=trade_log.price_move_pct,
                    entry_zone=trade_log.entry_zone,
                    stop_loss=trade_log.stop_loss,
                    target=trade_log.target,
                    risk_reward=trade_log.risk_reward,
                    reasoning=trade_log.reasoning,
                    analysis_context=trade_log.analysis_context,
                    matched_strategies=trade_log.matched_strategies,
                    # Feature #10 — persist the AI's probability distribution
                    # so calibration (Brier, log-loss) can be computed
                    # post-close.
                    p_target=trade_log.p_target,
                    p_stop=trade_log.p_stop,
                    p_neither=trade_log.p_neither,
                    # v7.13 Phase 1.6 — universe provenance tag for the
                    # 30-day "did dynamic symbols help?" audit.
                    universe_source=trade_log.universe_source,
                    # v7.14a — scan-time context dossier, pre-built at
                    # TradeLog construction (above). Was previously built
                    # inline HERE via _build_context_safe(hit, ...), which
                    # raised NameError because ``hit`` is out of scope by
                    # the time _execute_trade runs (different function,
                    # different coroutine).
                    context_at_firing=trade_log.context_at_firing,
                    # v7.14 / v7.15 — scanner identity.
                    scanner_id=trade_log.scanner_id,
                )
                if trade_log.db_record_id is None:
                    # save_trade_opened swallows its own exceptions and
                    # returns None — log loudly so we notice next time.
                    logger.error(
                        "trade_persist_returned_none symbol=%s order_id=%s "
                        "— Alpaca executed but DB row was not created; "
                        "check trade_history logs",
                        trade_log.symbol, trade_log.order_id,
                    )
            except Exception as save_exc:
                # Re-raised would fall into the outer except and mark
                # the trade as failed — wrong. Log + continue; the order
                # IS live on Alpaca and the in-memory monitor will manage
                # it. The audit-trail row is the only thing missing.
                logger.exception(
                    "trade_persist_raised symbol=%s order_id=%s err=%s "
                    "— Alpaca order is still live; only the local DB row "
                    "failed. Monitor will manage from memory.",
                    trade_log.symbol, trade_log.order_id, save_exc,
                )

        emit_trade_event("order_placed", {
            "symbol": trade_log.symbol, "side": side.value,
            "qty": trade_log.qty, "order_id": order.get("id"),
        })
        emit_wallet_update()
        _push_status()

    except Exception as exc:
        trade_log.status = "failed"
        # Extract Alpaca's rejection reason from the response body
        detail = str(exc)
        if hasattr(exc, "response"):
            try:
                body = exc.response.json()
                detail = body.get("message", body.get("detail", detail))
            except Exception:
                detail = getattr(exc.response, "text", detail)
        trade_log.error = detail
        logger.error("autotrade_order_failed symbol=%s error=%s", trade_log.symbol, detail)
        emit_trade_event("order_failed", {"symbol": trade_log.symbol, "error": detail})
        _push_status()



def _parse_price(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"[\d]+(?:\.[\d]+)?", str(value))
    return float(match.group()) if match else None


# ── Rehydration after server restart ────────────────────────────────────────

def _rehydrate_monitored_positions(user_id: int) -> int:
    """Reconstruct ``state.monitored_positions`` from Alpaca + DB on start.

    After a server restart the in-memory ``state.monitored_positions`` list is
    empty, but Alpaca still holds any positions that were open at shutdown
    time and the DB still has their ``TradeRecord`` rows (closed_at is NULL).
    Without rehydration those positions become "orphans" — visible in the
    Open Positions card but not managed by the trailing-stop / time-exit
    monitor, which is the exact divergence the user spotted after their
    last restart.

    This function is called from ``start()`` so it only runs when the user
    re-enables autotrade (which is also when the monitor loop comes back
    up and will immediately pick up the rehydrated entries on its next
    tick). If rehydration fails for any reason we log and fall through;
    autotrade still starts, the positions just don't re-enter the monitor.

    Skips:
      • symbols already tracked in ``state.monitored_positions`` (a start
        after a stop within the same process — list is non-empty already)
      • Alpaca positions with no matching open ``TradeRecord`` (manual
        trades or legacy positions opened before the persistence layer).
    """
    from sqlalchemy import select
    from .db import SessionLocal
    from .models import TradeRecord

    try:
        client = get_shared_trading_client()
        positions = client.list_positions()
    except Exception as exc:
        logger.warning("rehydrate_list_positions_failed err=%s", exc)
        return 0

    # Passive DB/broker-mismatch warning (Option A telemetry; gated; READ-ONLY —
    # NEVER mutates trade status, NEVER submits an order). Surfaces open DB rows
    # (closed_at IS NULL) with no matching broker position for MANUAL attention.
    # Replaces the rejected auto-recovery sweep — deterministic crash recovery
    # is Option B, a separate project.
    if settings.fill_persist_enabled:
        try:
            # Match on (symbol, side) — not symbol alone — so a DB long open
            # against a broker SHORT in the same symbol is still flagged.
            # (Qty-level / multi-row reconciliation is the aggregate problem
            # deferred to Option B; this stays a read-only manual-attention
            # heuristic, never an action.)
            broker_sides = {
                (str(p.get("symbol", "")).upper(),
                 "short" if str(p.get("side") or "long").lower() == "short" else "long")
                for p in positions
            }
            wdb = SessionLocal()
            try:
                open_rows = wdb.scalars(
                    select(TradeRecord).where(
                        TradeRecord.user_id == user_id,
                        TradeRecord.closed_at.is_(None),
                    )
                ).all()
                for r in open_rows:
                    if (r.symbol.upper(), (r.direction or "long")) not in broker_sides:
                        logger.warning(
                            "startup_db_broker_mismatch record_id=%s symbol=%s dir=%s status=%s — open "
                            "in DB with no matching broker position/side; MANUAL ATTENTION (no action)",
                            r.id, r.symbol, r.direction, r.status,
                        )
            finally:
                wdb.close()
        except Exception as exc:
            logger.warning("startup_mismatch_check_failed err=%s", exc)

    # Flat broker → nothing to re-monitor (pre-patch behavior, unchanged).
    if not positions:
        return 0

    existing_symbols = {m.symbol.upper() for m in state.monitored_positions if not m.closed}

    db = SessionLocal()
    rehydrated = 0
    skipped_no_db = 0
    try:
        for pos in positions:
            symbol = str(pos.get("symbol", "")).upper()
            if not symbol or symbol in existing_symbols:
                continue

            try:
                qty = int(abs(float(pos.get("qty", 0))))
                if qty == 0:
                    continue
                pos_side = str(pos.get("side") or "long").lower()
                direction = "short" if pos_side == "short" else "long"
                entry_price = float(pos.get("avg_entry_price") or 0)
                current_price = float(pos.get("current_price") or entry_price)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "rehydrate_bad_position symbol=%s err=%s", symbol, exc,
                )
                continue

            # Most-recent open TradeRecord for this user/symbol. Matching on
            # symbol + (closed_at IS NULL) rather than order_id because the
            # broker's position-level view doesn't expose the originating
            # order id, and a single symbol should only have one open trade
            # at a time in this system (max_open_trades=3 is a per-account
            # not per-symbol limit, but pending_approvals uses symbol as a
            # de-dup key up the stack).
            stmt = (
                select(TradeRecord)
                .where(
                    TradeRecord.user_id == user_id,
                    TradeRecord.symbol == symbol,
                    TradeRecord.closed_at.is_(None),
                )
                .order_by(TradeRecord.opened_at.desc())
                .limit(1)
            )
            record = db.scalars(stmt).first()
            if record is None:
                skipped_no_db += 1
                logger.info(
                    "rehydrate_skip_no_db symbol=%s qty=%d — manual trade or "
                    "pre-persistence; stays in Open Positions only",
                    symbol, qty,
                )
                continue

            # Reconstruct TradeLog from the persisted TradeRecord. Fields
            # needed by the monitor/exit paths (stop_loss, target, qty,
            # p_target for #10 outcome classification) are preserved; UI-
            # only fields that weren't serialized (regime_detail, news_detail
            # dicts) stay None and degrade gracefully in the approval UI.
            trade_log = TradeLog(
                time=(
                    record.opened_at.isoformat()
                    if record.opened_at
                    else datetime.now(timezone.utc).isoformat()
                ),
                symbol=record.symbol,
                side=record.side or ("buy" if direction == "long" else "sell"),
                qty=record.qty or qty,
                order_type=record.order_type or "market",
                confidence=record.confidence or 0.0,
                setup=record.setup or "",
                status=record.status or "submitted",
                order_id=record.order_id,
                db_record_id=record.id,
                prediction_price=record.prediction_price,
                confirmation_price=record.confirmation_price,
                price_move_pct=record.price_move_pct,
                entry_zone=record.entry_zone,
                stop_loss=record.stop_loss,
                target=record.target,
                risk_reward=record.risk_reward,
                reasoning=record.reasoning,
                analysis_context=record.analysis_context,
                matched_strategies=getattr(record, "matched_strategies", None),
                # Feature #10 probabilities — brier.classify_outcome reads
                # these when the trade closes, so they MUST survive a
                # rehydrate to keep calibration tracking accurate.
                p_target=getattr(record, "p_target", None),
                p_stop=getattr(record, "p_stop", None),
                p_neither=getattr(record, "p_neither", None),
                trailing_stop_final=record.trailing_stop_final,
                highest_price_reached=record.highest_price_reached,
                # Universe provenance tag (v7.13 Phase 1.5). Persisted
                # records pre-dating this field show up with attribute
                # missing — fall back to live lookup so the rehydrated
                # log carries a tag (best-effort; "unknown" is fine).
                universe_source=(
                    getattr(record, "universe_source", None)
                    or get_universe_source(record.symbol)
                ),
            )

            # Stop loss is stored as a string like "$52.50" — parse to float.
            # Falls back to the same trailing-stop-pct default the open-path
            # uses at line ~2473 if the DB doesn't have an explicit AI stop.
            initial_stop = _parse_price(record.stop_loss)
            if initial_stop is None:
                if direction == "long":
                    initial_stop = entry_price * (1 - state.trailing_stop_pct)
                else:
                    initial_stop = entry_price * (1 + state.trailing_stop_pct)

            # Trailing stop state — if the prior process persisted a
            # ratcheted stop (trailing_stop_final) that's later than the
            # initial, prefer it. Otherwise start at the initial stop; the
            # monitor loop will re-ratchet on its next tick.
            prior_trail = record.trailing_stop_final
            if prior_trail is not None:
                if direction == "long":
                    trailing_stop = max(float(prior_trail), initial_stop)
                else:
                    trailing_stop = min(float(prior_trail), initial_stop)
            else:
                trailing_stop = initial_stop

            # High/low-water marks. Prefer persisted values so a ratchet
            # from the prior process isn't lost; otherwise bootstrap from
            # max(entry, current) so a position that has moved favorably
            # since restart isn't under-ratcheted on the first tick.
            prior_high = record.highest_price_reached
            if direction == "long":
                if prior_high is not None:
                    highest_price = max(float(prior_high), current_price, entry_price)
                else:
                    highest_price = max(entry_price, current_price)
                lowest_price = None
            else:
                # Shorts: no dedicated "lowest_price_reached" column exists,
                # so we bootstrap from min(entry, current). If the prior
                # process had ratcheted, the trailing_stop itself captures
                # that progress — this only under-bounds the water mark,
                # which is conservative (no false exit).
                highest_price = None
                lowest_price = min(entry_price, current_price)

            monitored = MonitoredPosition(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                qty=qty,
                order_id=record.order_id,
                opened_at=(
                    record.opened_at.isoformat()
                    if record.opened_at
                    else datetime.now(timezone.utc).isoformat()
                ),
                trade_log=trade_log,
                initial_stop=initial_stop,
                trailing_stop=trailing_stop,
                highest_price=highest_price,
                lowest_price=lowest_price,
                current_price=current_price,
            )
            state.monitored_positions.append(monitored)
            rehydrated += 1
            logger.info(
                "rehydrated symbol=%s direction=%s entry=%.2f current=%.2f "
                "trail=%.2f qty=%d trade_record_id=%d",
                symbol, direction, entry_price, current_price,
                trailing_stop, qty, record.id,
            )
    finally:
        db.close()

    if rehydrated or skipped_no_db:
        logger.info(
            "rehydrate_summary rehydrated=%d skipped_no_db=%d alpaca_positions=%d",
            rehydrated, skipped_no_db, len(positions),
        )
    return rehydrated


# ── Start / Stop ─────────────────────────────────────────────────────────────

def start(user_id: int | None = None):
    if state.running:
        return
    state.running = True
    if user_id:
        state.active_user_id = user_id
    state.last_error = None
    state.circuit_breaker_tripped = False
    state.confirming.clear()
    state.pending_approvals.clear()
    # Keep active (unclosed) monitored positions from a previous run
    state.monitored_positions = [m for m in state.monitored_positions if not m.closed]
    # Rehydrate: after a server restart Alpaca may still hold positions that
    # were actively managed in the prior process but whose in-memory state
    # got wiped. Pull them back into monitored_positions so trailing-stop
    # and time-exit management resumes seamlessly.
    if state.active_user_id:
        try:
            n = _rehydrate_monitored_positions(state.active_user_id)
            if n:
                logger.info("autotrade_rehydrate count=%d", n)
        except Exception as exc:
            # Fail-open: if rehydration errors we still want autotrade to
            # start. Worst case the user's positions show only in Open
            # Positions (pre-rehydration behavior) instead of being lost.
            logger.warning("autotrade_rehydrate_failed err=%s", exc)
    now = datetime.now(timezone.utc)
    if state.trades_today:
        last_trade_time = datetime.fromisoformat(state.trades_today[-1].time)
        if last_trade_time.date() != now.date():
            state.trades_today.clear()
            state.scan_count = 0
    loop = asyncio.get_running_loop()
    state._task = loop.create_task(_run_loop())
    # Start position monitor alongside the scan loop
    if state.enable_trailing_stop or state.enable_time_exit:
        state._monitor_task = loop.create_task(_run_monitor_loop())
    emit_trade_event("started", {})
    _push_status()


def stop():
    state.running = False
    if state._task and not state._task.done():
        state._task.cancel()
    state._task = None
    if state._monitor_task and not state._monitor_task.done():
        state._monitor_task.cancel()
    state._monitor_task = None
    emit_trade_event("stopped", {})
    _push_status()
