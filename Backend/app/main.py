from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import logging

from .alpaca import AlpacaTradeStream
from .db import Base, engine
from .limiter import limiter
from .logging_config import configure_logging
from .routers import auth, market, trading
from .settings import settings
from .ws import emit_trade_event, emit_wallet_update, websocket_endpoint

configure_logging(settings.app_env)
logger = logging.getLogger("tradehack.main")


def _migrate_sqlite_columns() -> None:
    """Add new columns to existing SQLite tables that SQLAlchemy's create_all won't alter."""
    from sqlalchemy import text
    migrations = [
        # table, column, column_def_sql
        ("study_insights", "occurrence_count", "INTEGER DEFAULT 1"),
        ("study_insights", "first_seen_date", "VARCHAR(10)"),
        ("study_insights", "last_seen_date", "VARCHAR(10)"),
        ("users", "performance_reset_at", "DATETIME"),
        ("trade_records", "matched_strategies", "VARCHAR(500)"),
        # Feature #10 — structured probabilities + outcome class for calibration tracking.
        ("trade_records", "p_target",      "FLOAT"),
        ("trade_records", "p_stop",        "FLOAT"),
        ("trade_records", "p_neither",     "FLOAT"),
        ("trade_records", "outcome_class", "VARCHAR(20)"),
        # v7.13 Phase 1.6 (2026-05-01) — universe provenance for the
        # 30-day "did dynamic symbols help?" audit. Pre-migration rows
        # stay NULL; the audit query MUST do
        # ``COALESCE(universe_source, 'unknown')`` so those rows
        # surface as a distinct ``unknown`` cell rather than appearing
        # as a silent NULL group that gets dropped by GROUP BY in
        # some DB engines (review feedback #5, 2026-05-01).
        ("trade_records", "universe_source", "VARCHAR(20)"),
        # v7.14a (2026-05-03) — trade-context dossier captured at
        # trade-open from in-memory scan-time context. JSON blob,
        # bounded at write-time (≤ 32 KB; ≤ 5 news articles). Used
        # by the trade-exporter (friend test cohort) at close-time
        # to build a per-trade JSON dossier. Pre-migration rows
        # stay NULL; the exporter writes a partial dossier with
        # `context_at_firing: null` for those (still useful — the
        # other TradeRecord fields are present).
        ("trade_records", "context_at_firing", "TEXT"),
        # v7.14 (2026-05-18) — scanner identity. Foundation step for
        # the multi-scanner roadmap (v7.15 ships the second scanner).
        # NOT NULL with default 'ict' so pre-migration rows back-fill
        # to the ICT bucket, preserving v7.12.1 audit baseline parity.
        # The audit query for the multi-scanner cohort uses
        # ``GROUP BY scanner_id``; the index below makes that fast.
        ("trade_records", "scanner_id", "VARCHAR(40) NOT NULL DEFAULT 'ict'"),
        # Same scanner identity column for friend-imported rows. This
        # table may already exist on dev boxes from the earlier friend
        # cohort work, and ``create_all`` will not alter it. Keep the
        # default aligned with TradeRecord so older dossiers land in
        # the ICT bucket.
        ("imported_trade_records", "scanner_id", "VARCHAR(40) NOT NULL DEFAULT 'ict'"),
        # v7.16.x (2026-06-12) — order-ID-linked fill columns. These ALWAYS
        # migrate (additive, nullable, no default) so the ORM model and the DB
        # stay consistent — a gated migration would break TradeRecord inserts
        # ("no such column") whenever the flag is off. The columns are inert
        # while ``fill_persist_enabled`` is False: nothing reads or writes them,
        # so trading BEHAVIOR is byte-identical; only 3 empty columns appear.
        # The flag gates the BEHAVIOR (validated fill lookups, fill-derived
        # metrics, passive DB/broker mismatch warning).
        ("trade_records", "entry_fill_price", "FLOAT"),
        ("trade_records", "exit_fill_price",  "FLOAT"),
        ("trade_records", "exit_order_id",    "VARCHAR(64)"),
    ]
    # Indexes that `create_all` won't backfill on an existing DB.
    index_migrations = [
        # index_name, table, column
        ("ix_simulated_trades_setup", "simulated_trades", "setup"),
        # v7.13 Phase 1.6 — index the audit-grouping column. Without
        # this, the 30-day audit query does a full scan on every
        # invocation; with it, GROUP BY is O(rows-per-source).
        ("ix_trade_records_universe_source", "trade_records", "universe_source"),
        # v7.14 (2026-05-18) — index the multi-scanner audit grouping
        # column. Same rationale: GROUP BY scanner_id must not full-scan.
        ("ix_trade_records_scanner_id", "trade_records", "scanner_id"),
        ("ix_imported_trade_records_scanner_id", "imported_trade_records", "scanner_id"),
    ]
    with engine.begin() as conn:
        for table, col, definition in migrations:
            try:
                existing = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                cols = {row[1] for row in existing}
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {definition}"))
                    logger.info("migration_added table=%s column=%s", table, col)
            except Exception as exc:
                logger.warning("migration_failed table=%s column=%s error=%s", table, col, exc)

        for idx_name, table, col in index_migrations:
            try:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({col})"))
            except Exception as exc:
                logger.warning("index_migration_failed index=%s error=%s", idx_name, exc)


def _on_alpaca_trade_update(data: dict) -> None:
    """Callback for Alpaca trade_updates stream → pushes events to browser clients.

    data shape (from Alpaca):
        {"event": "fill"|"partial_fill"|"new"|"canceled"|..., "order": {...},
         "timestamp": "...", "price": "...", "qty": "...", ...}
    """
    try:
        event_name = data.get("event") or "unknown"
        order = data.get("order") or {}
        # Invalidate the /account + /positions TTL cache BEFORE telling
        # browsers to refresh — otherwise a fresh GET would return a
        # stale snapshot cached moments before the fill.
        try:
            from .routers.trading import _cache_invalidate
            _cache_invalidate("account", "positions")
        except Exception:
            pass
        emit_trade_event(f"alpaca_{event_name}", {
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": data.get("qty"),
            "price": data.get("price"),
            "order_id": order.get("id"),
            "status": order.get("status"),
        })
        # Any trade update likely moves the wallet — tell browsers to refresh.
        emit_wallet_update()
    except Exception as exc:
        logger.warning("alpaca_trade_update_callback_error error=%s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_columns()
    logger.info("startup env=%s database=%s", settings.app_env, settings.database_url.split("://")[0])

    # v7.13 Phase 1.6 — restore today's applied morning universe if
    # one exists on disk. Conservative default (do nothing → fall
    # back to audited base in TRAINING_UNIVERSE) when there's no
    # same-day snapshot. Imports kept local to avoid a circular
    # import at module load (applied_universe → autotrade → ...).
    #
    # Phase 2: also publishes the restoration to
    # ``state.applied_universe_metadata`` (with
    # ``restored_from_disk=True``) so the status endpoint can show
    # the operator that they're running on a restored snapshot, not
    # default base.
    try:
        from .applied_universe import restore_today
        from . import autotrade as _autotrade

        def _set_metadata(md: dict) -> None:
            _autotrade.state.applied_universe_metadata = md

        restore_today(
            set_universe_fn=_autotrade.set_trading_universe,
            set_metadata_fn=_set_metadata,
        )
    except Exception as exc:
        # Audit-trail file shouldn't block server startup. Log loudly
        # so it shows up in the morning operational check.
        logger.warning("applied_universe_restore_skipped err=%s", exc)

    # Start Alpaca trade-updates WebSocket stream — pushes fills to frontend
    # instead of relying on 1-second polling.
    stream = AlpacaTradeStream(on_trade_update=_on_alpaca_trade_update)
    stream_task = asyncio.create_task(stream.run_forever(), name="alpaca_trade_stream")

    # v7.15.3 (2026-05-06): in-process daily scheduler. Replaces the
    # macOS launchd auto-build that was blocked by TCC sandboxing on
    # ``~/Documents/``. Fires ``build_morning_universe`` at 08:30 ET
    # on weekdays. Shares uvicorn's user-context privileges → no
    # sandbox issues. Cancellable cleanly on shutdown so an in-flight
    # build doesn't leave a half-written snapshot.
    try:
        from . import scheduler as _scheduler
        _scheduler.start()
    except Exception as exc:
        logger.warning("scheduler_start_failed err=%s", exc)

    try:
        yield
    finally:
        logger.info("shutdown_begin")
        # Stop the scheduler before tearing down the WS stream — its
        # in-flight build (if any) holds an Alpaca client that we
        # want to close cleanly.
        try:
            from . import scheduler as _scheduler
            await _scheduler.stop()
        except Exception as exc:
            logger.debug("scheduler_stop_error error=%s", exc)
        try:
            await stream.stop()
        except Exception as exc:
            logger.debug("stream_stop_error error=%s", exc)
        stream_task.cancel()
        try:
            await stream_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("shutdown")


app = FastAPI(
    title="TradeHack API",
    description=(
        "Stock day-trading scanner and paper autotrader: ICT setup detection "
        "on Alpaca minute bars, approval/autonomous execution on an Alpaca "
        "paper account, software-managed exits, and trade telemetry."
    ),
    version="7.16",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(auth.router)
app.include_router(market.router)
app.include_router(trading.router)

# WebSocket endpoint for real-time push updates
app.add_api_websocket_route("/ws", websocket_endpoint)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "tradehack",
        "environment": settings.app_env,
    }
