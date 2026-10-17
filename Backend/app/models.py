from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_salt: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # Operator boundary: only operator accounts may reach the shared broker
    # account (orders, positions, autotrade, trading mode, WebSocket feed).
    is_operator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # When set, performance stats only consider trades opened on/after this timestamp.
    # Trade history is NOT affected — prior trades remain visible in the history list.
    performance_reset_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates="sessions")


class TradeRecord(Base):
    """Persistent record of every trade the AI auto-trader executes."""
    __tablename__ = "trade_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    # Core trade info
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[str] = mapped_column(String(5))            # buy / sell
    direction: Mapped[str] = mapped_column(String(6))       # long / short
    qty: Mapped[int] = mapped_column(Integer)
    setup: Mapped[str] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str] = mapped_column(String(20))     # market / bracket
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)

    # Prices
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prediction_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confirmation_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_move_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # AI recommendation fields
    entry_zone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    stop_loss: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    target: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    risk_reward: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reasoning: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    analysis_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Structured probabilities from the AI (feature #10 — calibration tracking).
    # p_target  = P(target hit before stop)
    # p_stop    = P(stop   hit before target)
    # p_neither = P(neither hit inside holding window)
    # outcome_class: "target_hit" / "stop_hit" / "neither" — filled in at close so we
    # can score Brier + log-loss and build a reliability diagram.
    p_target:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_stop:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_neither:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_class: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Exit tracking
    exit_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    trailing_stop_final: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    highest_price_reached: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hold_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # P&L (computed when trade closes)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Comma-separated PLAYBOOK strategy names that fired during Tier-1 matching.
    # Used by calibration.py to key per-(strategy, symbol) win-rate cells.
    # Nullable because old rows (pre-migration) won't have this set.
    matched_strategies: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Universe provenance (v7.13 Phase 1.6, 2026-05-01) — was this
    # symbol part of the audited base or did it enter via the morning
    # universe scanner? Drives the 30-day "did dynamic symbols help?"
    # audit. Possible values:
    #   "base"            — symbol was in TRAINING_UNIVERSE at firing time
    #   "morning_dynamic" — applied via the morning scanner today
    #   "unknown"         — present in active universe but not tagged
    #                       (e.g. legacy override pre-Phase-1.6, manual
    #                       add). Treated as data-quality flag at audit.
    # Indexed because the primary audit query is `GROUP BY universe_source`.
    # Nullable because pre-migration rows won't have this set; the
    # migration in main.py back-fills the column but not the values.
    universe_source: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, index=True,
    )

    # Scanner identity (v7.14, 2026-05-18) — which scanner produced this
    # trade. Foundation step for the multi-scanner roadmap: v7.15 will
    # add an experimental scanner that places real Alpaca paper orders
    # alongside the ICT one, and every trade must be filterable by which
    # scanner generated it so the v7.12.1 audit baseline (+42.72R post-
    # friction, 126 trades, 6m window) stays comparable.
    #
    # Possible values:
    #   "ict"             — the renamed ICT scanner (everything today)
    #   "experimental_v1" — the v7.15 experimental scanner (when shipped)
    #
    # NOT NULL with default "ict" so pre-migration rows back-fill to
    # the ICT bucket — preserves audit-query parity. Indexed because
    # the multi-scanner audit query is `GROUP BY scanner_id`.
    scanner_id: Mapped[str] = mapped_column(
        String(40), nullable=False, default="ict", server_default="ict", index=True,
    )

    # Trade-context dossier (v7.14a, 2026-05-03) — captured at trade
    # OPEN time from in-memory scan-time context. JSON blob with the
    # structure:
    #   {
    #     "indicators_snapshot": {...},   # RSI, ATR, EMAs, gap_pct, ICT zones
    #     "regime_snapshot":     {...},   # regime label, SPY change %, vol
    #     "news_verdict":        {...},   # impact, severity, ≤ 5 articles
    #     "applied_universe":    {...}    # apply event metadata
    #   }
    # Rich context that today only exists in TradeLog (in-memory) and
    # dies when the process exits. Persisting it here lets the trade
    # exporter (Phase v7.14a) build a complete dossier at close-time
    # without reaching back into volatile state. Bounded at write-time
    # (≤ 32 KB serialized; ≤ 5 news articles; truncated headlines /
    # summaries) so the column never carries unbounded blobs.
    # Nullable because pre-v7.14a rows won't have it set.
    # Stored as TEXT (JSON-serialized string) for SQLite portability.
    context_at_firing: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )

    # v7.16.x (2026-06-12) — order-ID-linked broker fills (telemetry-only).
    # Monitor prices drift from real fills (2026-06-09: $23.58 day-level gap;
    # 2026-06-11's same-day COIN pair broke symbol/day fill matching outright),
    # so close-time persistence links by EXACT order id: entry fill from the
    # entry order, exit fill from the close order Alpaca returns at close
    # submission. Nullable — pre-migration rows and lookup failures stay NULL;
    # analysis falls back to monitor prices and labels them approximate.
    entry_fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Timestamps
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship()


class ImportedTradeRecord(Base):
    """Trade rows imported from a friend's instance via the
    v7.14b ``import_friend_trades.py`` CLI. Mirrors ``TradeRecord``'s
    columns 1:1 (so the audit query can UNION them) but kept SEPARATE
    from ``TradeRecord`` for two reasons (refinement #2):

    1. Dashboard / wallet / position-reconciliation code assumes
       every ``TradeRecord`` row corresponds to a real trade made on
       THIS instance's Alpaca account. Mixing imported rows in would
       confuse those code paths silently.
    2. Imports lack ``user_id`` (the friend's local user ID isn't
       meaningful in this DB) and need provenance fields
       (``source_instance``, ``trade_id_local``, ``imported_at``) that
       don't belong on native trades.

    The audit script (``scripts/audit_universe_sources.py``) UNIONs
    both tables and segments by ``COALESCE(source_instance, 'host')``
    so the cross-cohort comparison surfaces naturally.

    Composite UNIQUE on (source_instance, trade_id_local) enforces
    idempotent imports at the DB level — re-running the importer on
    the same zip is guaranteed to insert each row at most once.
    """

    __tablename__ = "imported_trade_records"
    __table_args__ = (
        UniqueConstraint(
            "source_instance", "trade_id_local",
            name="uq_imported_trade_source_local_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # ── Provenance (v7.14b-only fields) ──
    # Friend ID (matches ``[a-z0-9_-]{1,32}`` per refinement #5).
    # Indexed because audit GROUP BY on this is the primary query.
    source_instance: Mapped[str] = mapped_column(String(40), index=True)
    # Friend's local TradeRecord.id at export time. Together with
    # source_instance, forms the dedup key.
    trade_id_local: Mapped[int] = mapped_column(Integer)
    # When the import landed in this DB.
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True,
    )
    # Friend's data feed at export time (from manifest's env_safety).
    # Indexed because the audit script's feed-mismatch warning
    # filters on this. Default ``"sip"`` for backfill; nullable for
    # ultra-defensive parsing of malformed manifests.
    source_feed: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True, index=True,
    )
    # Friend's app version at export time (from manifest). Helps
    # debug schema drift across instances. Free-text, no validation.
    source_app_version: Mapped[Optional[str]] = mapped_column(
        String(40), nullable=True,
    )

    # ── Mirror of TradeRecord columns (same names, same types) ──
    # Order matches TradeRecord for easy diff. NO user_id FK (friend's
    # user IDs don't exist in this DB).
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[str] = mapped_column(String(5))
    direction: Mapped[str] = mapped_column(String(6))
    qty: Mapped[int] = mapped_column(Integer)
    setup: Mapped[str] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str] = mapped_column(String(20))
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)

    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prediction_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confirmation_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_move_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    entry_zone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    stop_loss: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    target: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    risk_reward: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reasoning: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)

    p_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_neither: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_class: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    exit_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    trailing_stop_final: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    highest_price_reached: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hold_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    matched_strategies: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    universe_source: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, index=True,
    )
    # Scanner identity (v7.14, mirrors TradeRecord.scanner_id) — copied
    # verbatim from the friend's local TradeRecord row via the dossier
    # so the cross-cohort audit can segment by ICT vs experimental the
    # same way native trades do. Default "ict" handles dossiers from
    # friend instances that pre-date v7.14 (won't carry the field).
    scanner_id: Mapped[str] = mapped_column(
        String(40), nullable=False, default="ict", server_default="ict", index=True,
    )
    context_at_firing: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Friend's original timestamps (kept distinct from imported_at —
    # opened_at/closed_at are the trade's actual lifecycle timestamps
    # on the friend's box).
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class SimulatedTrade(Base):
    """Hypothetical trade — what would have happened if the AI recommendation was followed.

    Every AI recommendation above min_confidence is logged here, regardless of
    whether the user approved it. Market prices are tracked to compute the
    hypothetical outcome (stop hit, target hit, or time exit).
    """
    __tablename__ = "simulated_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    # Core trade info (from AI recommendation)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    direction: Mapped[str] = mapped_column(String(6))       # long / short
    setup: Mapped[str] = mapped_column(String(120), index=True)
    confidence: Mapped[float] = mapped_column(Float)

    # AI recommendation fields
    entry_price: Mapped[float] = mapped_column(Float)                     # price at time of recommendation
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_zone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    risk_reward: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reasoning: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)

    # Outcome tracking (filled in by the monitor)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open / won / lost / expired
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # target_hit / stop_hit / time_exit / eod_exit
    highest_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lowest_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Tracking
    qty: Mapped[int] = mapped_column(Integer, default=1)  # hypothetical share count
    was_approved: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # "yes" / "no" / "expired"

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TradeLesson(Base):
    """AI-generated lesson from a losing trade. Fed back into future analysis prompts."""
    __tablename__ = "trade_lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trade_records.id", ondelete="CASCADE"), index=True)

    symbol: Mapped[str] = mapped_column(String(12), index=True)
    setup: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    direction: Mapped[str] = mapped_column(String(6))
    pnl: Mapped[float] = mapped_column(Float)
    lesson: Mapped[str] = mapped_column(String(2000))       # AI-written summary of what went wrong
    tags: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # comma-separated categories

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class StudyInsight(Base):
    """After-hours AI study — patterns and setups the AI found by reviewing the full day's action."""
    __tablename__ = "study_insights"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    study_date: Mapped[str] = mapped_column(String(10), index=True)   # YYYY-MM-DD
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    direction: Mapped[str] = mapped_column(String(6))                  # long / short
    setup_name: Mapped[str] = mapped_column(String(120))               # pattern that formed
    insight: Mapped[str] = mapped_column(Text)                         # full AI analysis
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    optimal_stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    optimal_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    potential_pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Merging / repetition tracking — every time a matching pattern recurs, we bump the count
    # and refresh the insight instead of inserting a duplicate row.
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    last_seen_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
