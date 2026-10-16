from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    cors_origins: list[str]
    session_cookie_name: str
    session_cookie_secure: bool
    session_max_age_seconds: int
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_market_data_url: str
    alpaca_trading_url: str
    alpaca_paper: bool
    training_universe: list[str]
    market_data_dir: str
    models_dir: str
    anthropic_api_key: str
    openai_api_key: str
    # v7.12.1 (2026-05-01): Layer 2 (Sonnet AI confidence gate + Haiku
    # pre-screen) on/off switch. When False, the autotrade loop trades
    # Layer-1 predicate matches alone — no Sonnet analyze_chart call,
    # no Haiku screen. Stops/targets are computed from a fixed
    # 0.7%-stop / 1.5R-target rule instead of Sonnet's structural
    # zones. The Haiku NEWS classifier still runs (it feeds
    # news_multiplier and is independent of the Layer-2 screen). The
    # 2026-05-01 AI replay justified the default flip: 9.9% direction
    # agreement on the v7.12.1 trade set. Re-enable by setting
    # ENABLE_AI_GATE=true once the AI layer is rehabilitated.
    enable_ai_gate: bool
    # v7.15.4 (2026-05-06): auto-apply the morning universe at 08:32 ET
    # immediately after the scheduled 08:30 build completes. Supersedes
    # the original "manual-only for first 2 weeks" runbook rule because:
    #   * The operational miss (forgetting to click Apply, applying late
    #     after the Judas window closes at 10:00 ET) was producing
    #     INCONSISTENT day-to-day data — worse for the audit than
    #     auto-applying every successful build.
    #   * Auto-apply runs the same orchestrator (`apply_morning_universe`)
    #     with its existing safeguards: snapshot-first atomicity, manifest
    #     validation, idempotent file writes.
    #   * Kill-switchable: set ``AUTO_APPLY_AFTER_BUILD=false`` to revert
    #     to manual-only behavior.
    # Tagged with ``applied_via="auto_after_build"`` in the audit
    # metadata so the per-apply provenance trail distinguishes
    # automated from manual applies.
    auto_apply_after_build: bool
    # v7.13a (2026-05-01): Alpaca market-data feed. ``sip`` =
    # consolidated tape (paid SIP subscription — what the v7.12.1
    # audit ran on); ``iex`` = single-exchange free feed. Threaded
    # through the AlpacaBarsRequest default + the backtest cache-dir
    # selection so flipping ALPACA_DATA_FEED in .env (a) routes the
    # next bar fetch to the chosen feed, and (b) reads/writes a
    # feed-specific cache (``bars_cache_ext_sip/`` vs
    # ``bars_cache_ext_iex/``) so the two feeds never silently mix.
    alpaca_data_feed: str
    # v7.14a (2026-05-03): friend-test-cohort export switch.
    # Default ``False`` on the central (operator) box — no trade
    # exports ever land in ``data/trade_exports/`` on this instance.
    # Friend clones (built via ``scripts/clone_for_friend.sh``) ship
    # with ``EXPORT_TRADES=true`` so each closed trade emits a JSON
    # dossier. The export hook is post-close + non-blocking; a
    # serializer bug NEVER affects the trading loop (refinement #3).
    export_trades: bool
    # v7.14a: friend instance identifier. Stamped on every export
    # filename and inside every dossier (``instance_id`` field).
    # Sanitized hard against ``[a-z0-9_-]{1,32}`` (refinement #5)
    # because it lands in filenames AND DB rows downstream. Empty
    # string on the central box (no exports happen there anyway).
    # Validated at module import time — an invalid non-empty value
    # raises immediately rather than letting downstream code
    # discover the bad ID at write time.
    friend_instance_id: str
    # v7.16+ (2026-05-13): autonomous trading switch. Gates the
    # in-process scheduler's auto-start-at-09:30 event. Default
    # ``False`` — auto-start is opt-in even though auto-apply is on,
    # so the build/apply observation routine can run for a few days
    # without committing to unattended trading. The auto-start event
    # additionally enforces a fail-closed precondition: today's applied
    # universe snapshot must exist on disk before any trades fire.
    auto_start_trading: bool
    # v7.16+ Gmail SMTP credentials for scheduler failure alerts +
    # daily-summary email. ``smtp_app_password`` is a 16-char Google
    # app password (generated at https://myaccount.google.com/apppasswords).
    # The email module checks if password is empty and degrades
    # gracefully — failures are written to logs/state, scheduler keeps
    # running. Don't crash unattended trading on a missing email.
    smtp_user: str
    smtp_app_password: str
    smtp_to: str
    smtp_from: str
    # v7.16+ (2026-05-13): explicit operator email for unattended
    # trading. The auto-start event looks up ``User.email == this``
    # in the DB and uses that user's id for ``autotrade.start``. If
    # empty OR the user doesn't exist, auto-start fails closed and
    # emails an alert. Prevents accidental starts under test/friend
    # accounts.
    scheduler_user_email: str
    # ── v7.15 (2026-05-18): experimental scanner controls ──
    # Master kill switch. Default False — the second scanner is
    # opt-in. When False, ``scan_experimental_symbols`` is never
    # called and no experimental trades fire. The autotrade loop
    # checks this flag on every cycle (not just at start), so
    # operator can toggle live by restarting uvicorn after .env edit.
    experimental_scanner_enabled: bool
    # Max experimental trades per day, independent of the ICT cap.
    # Default 1 — extremely conservative. Operator dials up after
    # observing real fills + slippage on the experimental path.
    experimental_max_trades_per_day: int
    # Position-size multiplier vs ``state.position_size_pct``. 0.25 =
    # experimental trades risk 1/4 of an ICT trade. Stacked with all
    # the other multipliers (confidence, calibration, etc.) — applied
    # to ``risk_budget`` before quantity is computed.
    experimental_risk_multiplier: float
    # Wall-clock budget for the experimental scan call. Wrapped in
    # ``asyncio.wait_for`` — if it exceeds, the cycle logs, skips
    # experimental for that scan, and ICT continues uninterrupted.
    # Default 2.0s — fast enough that any timeout is a real
    # pathology, slow enough that normal variance won't trip it.
    experimental_scan_timeout_seconds: float
    # Belt-and-suspenders: when True, every order submission must
    # carry a non-empty ``scanner_id`` on its hit. Refusing untagged
    # orders prevents a future refactor bug from silently polluting
    # the audit trail with un-attributed trades. Default True.
    experimental_require_scanner_id: bool
    # ── v7.16.x (2026-05-21): autonomous trading ──
    # When True, a matched setup that clears every gate is auto-approved
    # (and thus auto-executed) instead of waiting for a manual dashboard
    # click. This is the BOOT DEFAULT only — the live toggle is
    # ``autotrade.state.autonomous_mode``, flippable from the dashboard
    # without a restart. Default False (autonomous trading is opt-in).
    autonomous_trading_enabled: bool
    # Per-day cap on AUTONOMOUS auto-approvals. With no human gating each
    # trade, this bounds how much an unattended run can do. The manual
    # path is unaffected (still capped by max_trades_per_day=999). When
    # the cap is hit, the approval card is still created for optional
    # manual review — only the auto-approve is skipped.
    autonomous_max_trades_per_day: int
    # v7.16.x (2026-06-12): master switch for order-ID-linked fill-persistence
    # TELEMETRY (Option A — the proven close path is unchanged; deterministic
    # crash recovery is the separate Option B project). Default False — the
    # behavior (validated fill lookups, fill-derived metrics, passive read-only
    # DB/broker mismatch warning) is INERT unless true, so starting the backend
    # on patched files is byte-equivalent to pre-patch (the additive nullable
    # fill columns still migrate). Flipped true only at its own controlled
    # deploy boundary, never bundled with the hosting cutover.
    fill_persist_enabled: bool


def _validate_production_settings(app_env: str, session_cookie_secure: bool, database_url: str) -> None:
    if app_env != "production":
        return
    if not session_cookie_secure:
        raise RuntimeError(
            "SESSION_COOKIE_SECURE must be set to 'true' when APP_ENV=production. "
            "Running production without secure cookies exposes session tokens over plain HTTP."
        )
    if database_url.startswith("sqlite"):
        raise RuntimeError(
            "DATABASE_URL must not use SQLite when APP_ENV=production. "
            "SQLite has write-locking issues under concurrent requests. "
            "Set DATABASE_URL to a PostgreSQL connection string."
        )


def _validate_experimental_scanner(s: "Settings") -> None:
    """Paper-only assertion for the v7.15 experimental scanner.

    When ``EXPERIMENTAL_SCANNER_ENABLED=true``, BOTH of these must hold
    or the process refuses to start::

        ALPACA_PAPER=true
        LIVE_TRADING_ENABLED=no

    Belt-and-suspenders against the operator accidentally enabling the
    experimental scanner against a live-money account. The 5 strategies
    in EXPERIMENTAL_PLAYBOOK are NOT in the v7.12.1 audit baseline; they
    must NEVER fire against real money under any circumstance. Raising
    at startup is the loudest possible failure mode — operator sees
    the error immediately, fixes .env, restarts.

    This is the only place the runtime reads ``LIVE_TRADING_ENABLED``
    via the settings layer; everywhere else reads it via ``os.getenv``
    directly. That's intentional — keeping a single source of truth
    (the env var, not a Settings field) means a runtime change to the
    env doesn't fall out of sync with what was validated at startup.
    """
    if not s.experimental_scanner_enabled:
        return
    if not s.alpaca_paper:
        raise RuntimeError(
            "EXPERIMENTAL_SCANNER_ENABLED=true requires ALPACA_PAPER=true. "
            "The experimental scanner runs the v7.15 EXPERIMENTAL_PLAYBOOK "
            "(ORB / VWAP reclaim / EMA pullback / momentum / failed-breakdown), "
            "none of which are in the v7.12.1 live audit baseline. They are "
            "paper-only until they accumulate their own per-strategy edge."
        )
    live = os.getenv("LIVE_TRADING_ENABLED", "").strip().lower()
    if live != "no":
        raise RuntimeError(
            "EXPERIMENTAL_SCANNER_ENABLED=true requires LIVE_TRADING_ENABLED=no. "
            f"Found LIVE_TRADING_ENABLED={live!r}. Refusing to enable the "
            "experimental scanner alongside a live-trading-eligible env."
        )


# v7.14a: friend instance ID regex (review refinement #5).
# Used at clone-time, settings-load-time, and trade-export-time.
# Matches: lowercase alphanumerics, underscore, hyphen; 1-32 chars.
# Empty string is accepted at settings-load (the central box leaves
# this unset), but the export hook will never run with an empty ID
# because EXPORT_TRADES defaults to False there.
import re as _re  # local import — keeps the top-of-file imports clean
_FRIEND_INSTANCE_ID_RE = _re.compile(r"^[a-z0-9_-]{1,32}$")


def _validate_friend_instance_id(raw: str) -> str:
    """Normalize + validate the FRIEND_INSTANCE_ID env value.

    Empty string / unset → returned as ``""`` (central-box default).
    Non-empty values must match the regex; invalid IDs raise so the
    misconfiguration surfaces at startup, not at the first trade
    close (which would silently fail to export).
    """
    val = (raw or "").strip()
    if not val:
        return ""
    if not _FRIEND_INSTANCE_ID_RE.match(val):
        raise RuntimeError(
            f"FRIEND_INSTANCE_ID must match ^[a-z0-9_-]{{1,32}}$; "
            f"got {val!r}. Friend IDs end up in filenames and DB rows; "
            f"arbitrary strings would break dedup keys downstream."
        )
    return val


def _resolve_sqlite_url(url: str) -> str:
    """Turn relative SQLite paths into absolute ones (relative to Backend/)."""
    if url.startswith("sqlite:///./") or url.startswith("sqlite:///data"):
        # Strip the sqlite:/// prefix, resolve against BASE_DIR, reattach
        rel_path = url.replace("sqlite:///", "")
        abs_path = (BASE_DIR / rel_path).resolve().as_posix()
        return f"sqlite:///{abs_path}"
    return url


def get_settings() -> Settings:
    market_data_dir = (BASE_DIR / "data" / "market_data").as_posix()
    models_dir = (BASE_DIR / "data" / "models").as_posix()
    raw_db_url = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{(BASE_DIR / 'data' / 'tradehack.db').as_posix()}",
    )
    s = Settings(
        app_env=os.getenv("APP_ENV", "development"),
        database_url=_resolve_sqlite_url(raw_db_url),
        cors_origins=_split_csv(
            os.getenv(
                "CORS_ORIGINS",
                "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:8080,http://localhost:8080",
            )
        ),
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "tradehack_session"),
        session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
        session_max_age_seconds=int(os.getenv("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 24 * 7))),
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        alpaca_market_data_url=os.getenv("ALPACA_MARKET_DATA_URL", "https://data.alpaca.markets/v2"),
        alpaca_trading_url=os.getenv(
            "ALPACA_TRADING_URL",
            "https://paper-api.alpaca.markets",   # paper by default — safe
        ),
        alpaca_paper=os.getenv("ALPACA_PAPER", "true").lower() == "true",
        training_universe=_split_csv(
            os.getenv(
                "TRAINING_UNIVERSE",
                "AAPL,NVDA,AMD,TSLA,META,AMZN,MSFT,GOOGL,SPY,QQQ,"
                "INTC,SOFI,DKNG,XLF",
            )
        ),
        market_data_dir=market_data_dir,
        models_dir=models_dir,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        # v7.12.1: Layer-2 AI gate. Default False in Dev (per .env);
        # the autotrade loop runs predicate-only when this is off. Flip
        # to ENABLE_AI_GATE=true to restore Sonnet + Haiku screening.
        enable_ai_gate=(
            os.getenv("ENABLE_AI_GATE", "false").lower() == "true"
        ),
        # v7.15.4: auto-apply after scheduler-fired build. Default
        # False to keep the runbook posture for first-time installs;
        # the operator's .env enables it explicitly when they've
        # decided manual apply is too brittle.
        auto_apply_after_build=(
            os.getenv("AUTO_APPLY_AFTER_BUILD", "false").lower() == "true"
        ),
        # v7.13a: Alpaca data feed. Default ``sip`` (matches the
        # v7.12.1 audit baseline). ``iex`` flips both the live
        # bar-fetch path AND the backtest cache directory so the
        # parity test can compare cleanly. Anything other than
        # ``sip`` / ``iex`` is treated as ``sip`` (defensive — Alpaca
        # rejects unknown feeds with HTTP 422).
        alpaca_data_feed=(
            os.getenv("ALPACA_DATA_FEED", "sip").lower()
            if os.getenv("ALPACA_DATA_FEED", "sip").lower() in ("sip", "iex")
            else "sip"
        ),
        # v7.14a: friend-test-cohort export switch. Defaults to False
        # (central box). Friend clones flip to True via .env.
        export_trades=(
            os.getenv("EXPORT_TRADES", "false").lower() == "true"
        ),
        # v7.14a: friend instance ID. Validated against regex; empty
        # on the central box (export off → ID unused).
        friend_instance_id=_validate_friend_instance_id(
            os.getenv("FRIEND_INSTANCE_ID", "")
        ),
        # v7.16.x: fill-persistence telemetry master switch (Option A).
        # Default False keeps the behavior inert (see field comment).
        fill_persist_enabled=(
            os.getenv("FILL_PERSIST_ENABLED", "false").lower() == "true"
        ),
        # v7.16+: autonomous trading. Default False — flip to True only
        # after you've confirmed build/apply automation is reliable.
        auto_start_trading=(
            os.getenv("AUTO_START_TRADING", "false").lower() == "true"
        ),
        # v7.16+ Gmail SMTP for scheduler alerts. All four fields are
        # optional; if SMTP_APP_PASSWORD is empty the email module
        # short-circuits without crashing the scheduler.
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_app_password=os.getenv("SMTP_APP_PASSWORD", ""),
        smtp_to=os.getenv("SMTP_TO", ""),
        smtp_from=os.getenv("SMTP_FROM", ""),
        # v7.16+: explicit operator account for unattended trading.
        # Auto-start fails closed if empty or user not in DB.
        scheduler_user_email=os.getenv("SCHEDULER_USER_EMAIL", "").strip(),
        # ── v7.15 (2026-05-18): experimental scanner controls ──
        # Master kill switch, default False. Operator opts in
        # deliberately by setting EXPERIMENTAL_SCANNER_ENABLED=true.
        experimental_scanner_enabled=(
            os.getenv("EXPERIMENTAL_SCANNER_ENABLED", "false").lower() == "true"
        ),
        # Per-day cap, independent of ICT's daily cap. Default 1
        # (extremely conservative); dial up after fills look sane.
        experimental_max_trades_per_day=int(
            os.getenv("EXPERIMENTAL_MAX_TRADES_PER_DAY", "1")
        ),
        # Risk multiplier vs ``state.position_size_pct``. Applied
        # to the risk budget in ``_compute_position_qty`` when the
        # hit's ``scanner_id == "experimental_v1"``. Default 0.25
        # (quarter-risk per experimental trade).
        experimental_risk_multiplier=float(
            os.getenv("EXPERIMENTAL_RISK_MULTIPLIER", "0.25")
        ),
        # Wall-clock budget for the experimental scan call. ICT
        # cycle latency is the top priority — if experimental work
        # exceeds this, the whole cycle's experimental output is
        # cancelled and ICT continues. Default 2.0s.
        experimental_scan_timeout_seconds=float(
            os.getenv("EXPERIMENTAL_SCAN_TIMEOUT_SECONDS", "2.0")
        ),
        # Refuse to submit any order whose hit lacks a ``scanner_id``
        # when this is True. Default True (the safe posture).
        experimental_require_scanner_id=(
            os.getenv("EXPERIMENTAL_REQUIRE_SCANNER_ID", "true").lower() == "true"
        ),
        # v7.16.x: autonomous trading boot default. Default False —
        # the operator opts in (and can flip it live from the dashboard).
        autonomous_trading_enabled=(
            os.getenv("AUTONOMOUS_TRADING_ENABLED", "false").lower() == "true"
        ),
        # v7.16.x: per-day autonomous auto-approval cap. Default 5.
        autonomous_max_trades_per_day=int(
            os.getenv("AUTONOMOUS_MAX_TRADES_PER_DAY", "5")
        ),
    )
    _validate_production_settings(s.app_env, s.session_cookie_secure, s.database_url)
    _validate_experimental_scanner(s)
    return s


settings = get_settings()
