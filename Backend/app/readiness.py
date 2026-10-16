"""
Morning Readiness Guard (v7.16, 2026-05-07)
==============================================================================
Evaluates whether the system is operationally ready to trade NY_AM.
Surfaces a multi-stage banner via ``GET /trading/readiness`` so the
frontend can warn the operator BEFORE the open if autotrade is off,
the universe wasn't applied, or the backend started late.

This module is operational telemetry only — it never mutates autotrade
state, never fires orders, never modifies any predicate. It just
reads the current state and computes a severity label.

DESIGN PRINCIPLES:
* **Frontend-driven heartbeat is the primary signal.** The frontend
  polls ``/health`` with a 5-sec timeout — if THAT times out, the
  banner goes red regardless of what this module says. This module
  only matters when the backend is actually responsive.
* **Stage matrix is time-of-day driven.** No machine learning, no
  predictive heuristics. Pure deterministic stage labels keyed off
  ET clock time + autotrade running flag + universe applied flag.
* **No side effects on read.** Calling get_readiness_state() must be
  cheap and idempotent. Audit logging happens via the explicit
  ``log_readiness_event()`` helper, called from the dismissal route
  (POST /trading/readiness/event), NOT from the read path.
* **Audit append-only.** Events go to
  ``Backend/data/readiness/readiness_events_<NY date>.jsonl`` —
  one JSON line per event. Used to evaluate over a 2-week window
  whether the guard actually changed operator behavior.

STAGE MATRIX (NY time):
                       autotrade OFF                       autotrade ON
  < 08:35              ok    ("premarket prep")            ok ("ready for NY_AM")
  08:35-08:59          info  (post-build reminder)         ok
  09:00-09:24          warn  (T-30, kick-off needed)       ok
  09:25-09:29          urgent + notify  (T-5)              ok
  09:30-10:59          blocking + notify (NY_AM live)      ok ("trading NY_AM")
  11:00-13:29          warn  (NY_AM done; NY_PM coming)    ok ("midday idle ok")
  13:30-15:59          warn  (NY_PM live, AT off)          ok ("trading NY_PM")
  16:00+               ok    (market closed)               ok ("market closed")

LATE-START OVERLAY:
  If uvicorn started AFTER 09:30 ET on the current trading day, the
  state carries ``backend_started_late_today=True``. Frontend uses
  this to surface "late start risk" copy until the operator dismisses
  the banner after checking the morning manually.
==============================================================================
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.readiness")

_ET = ZoneInfo("America/New_York")
_BASE_DIR = Path(__file__).resolve().parent.parent
_AUDIT_DIR = _BASE_DIR / "data" / "readiness"

# Captured at module-import time so backend_uptime_seconds and
# backend_started_late_today have a stable reference point.
_BACKEND_STARTED_AT_UTC: datetime = datetime.now(timezone.utc)


# ── Stage thresholds (ET minutes-of-day) ─────────────────────────────────────

_T_PREP_END   =  8 * 60 + 35    # 08:35 ET — universe build typically done
_T_T30        =  9 * 60         # 09:00 ET — T-30 to open
_T_T5         =  9 * 60 + 25    # 09:25 ET — T-5 urgent reminder
_T_OPEN       =  9 * 60 + 30    # 09:30 ET — NY_AM opens
_T_NY_AM_END  = 11 * 60         # 11:00 ET — NY_AM closes
_T_NY_PM_OPEN = 13 * 60 + 30    # 13:30 ET — NY_PM opens
_T_CLOSE      = 16 * 60         # 16:00 ET — market closes


# ── Public dataclass ─────────────────────────────────────────────────────────

@dataclass
class ReadinessState:
    """JSON-safe readiness snapshot. ``severity`` and ``notify`` drive
    the frontend banner; ``actions`` lists what the user should do."""
    now_et: str
    is_trading_day: bool
    market_phase: str           # premarket | ny_am | midday | ny_pm | afterhours | closed
    minutes_to_open: int        # negative if open already passed today
    minutes_to_close: int       # negative if close already passed today
    backend_started_at_et: str
    backend_uptime_seconds: int
    backend_started_late_today: bool
    autotrade_running: bool
    universe_applied_today: bool
    universe_size: int
    applied_at_et: str | None
    stage: str                  # internal label, e.g. "ny_am_active_off"
    severity: str               # ok | info | warn | urgent | blocking
    message: str
    notify: bool
    actions: list[str] = field(default_factory=list)


# ── Public API ───────────────────────────────────────────────────────────────

def get_readiness_state() -> ReadinessState:
    """Cheap, idempotent: reads current ET time + in-process autotrade
    state and returns a labelled ReadinessState. No side effects."""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(_ET)
    weekday = now_et.weekday()   # Mon=0 ... Sun=6
    # Holiday-aware trading-day check (2026-07-03): consult the cached Alpaca
    # calendar (trading_calendar caches per-day; failures cache 5 min). The
    # weekday heuristic is the fallback when the lookup fails — fail-open to
    # the old behavior so a calendar outage never blanks the dashboard.
    is_trading_day = weekday < 5   # 5/6 = weekend
    if is_trading_day:
        try:
            from . import trading_calendar
            cal = trading_calendar.get_today()
            if not cal.lookup_failed:
                is_trading_day = cal.is_open
        except Exception as exc:
            logger.debug("readiness_calendar_lookup_failed err=%s", exc)

    minute_of_day = now_et.hour * 60 + now_et.minute
    market_phase = _classify_market_phase(minute_of_day)

    # Open/close deltas (positive = future, negative = past)
    minutes_to_open = _T_OPEN - minute_of_day if is_trading_day else 9999
    minutes_to_close = _T_CLOSE - minute_of_day if is_trading_day else -9999

    # In-process state (lazy import to keep this module standalone)
    autotrade_running = False
    universe_applied_today = False
    universe_size = 0
    applied_at_et: str | None = None
    try:
        from . import autotrade
        autotrade_running = bool(autotrade.state.running)
        meta = autotrade.state.applied_universe_metadata or {}
        applied_at = meta.get("applied_at")
        if applied_at:
            try:
                applied_dt = datetime.fromisoformat(applied_at)
                if applied_dt.tzinfo is None:
                    applied_dt = applied_dt.replace(tzinfo=_ET)
                if applied_dt.astimezone(_ET).date() == now_et.date():
                    universe_applied_today = True
                    applied_at_et = applied_dt.astimezone(_ET).isoformat(timespec="seconds")
            except Exception:
                pass
        universe_size = len(autotrade.state.trading_universe or [])
    except Exception as exc:
        logger.warning("readiness_autotrade_read_failed err=%s", exc)

    backend_started_et = _BACKEND_STARTED_AT_UTC.astimezone(_ET)
    uptime_sec = int((now_utc - _BACKEND_STARTED_AT_UTC).total_seconds())
    started_late = (
        is_trading_day
        and backend_started_et.date() == now_et.date()
        and (backend_started_et.hour * 60 + backend_started_et.minute) > _T_OPEN
    )

    stage, severity, message, notify, actions = _classify_stage(
        is_trading_day=is_trading_day,
        minute_of_day=minute_of_day,
        autotrade_running=autotrade_running,
        universe_applied_today=universe_applied_today,
        backend_started_late_today=started_late,
    )

    return ReadinessState(
        now_et=now_et.isoformat(timespec="seconds"),
        is_trading_day=is_trading_day,
        market_phase=market_phase,
        minutes_to_open=minutes_to_open,
        minutes_to_close=minutes_to_close,
        backend_started_at_et=backend_started_et.isoformat(timespec="seconds"),
        backend_uptime_seconds=uptime_sec,
        backend_started_late_today=started_late,
        autotrade_running=autotrade_running,
        universe_applied_today=universe_applied_today,
        universe_size=universe_size,
        applied_at_et=applied_at_et,
        stage=stage,
        severity=severity,
        message=message,
        notify=notify,
        actions=actions,
    )


def log_readiness_event(
    *,
    severity: str,
    stage: str,
    action: str,           # "system_warning" | "user_dismissed" | "user_acted"
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append-only audit log. Used by the dismissal route + by any
    severity-transition watcher we add later. JSONL format: one
    object per line, atomic-ish via flush+fsync.
    """
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    today_ny = datetime.now(_ET).date().isoformat()
    path = _AUDIT_DIR / f"readiness_events_{today_ny}.jsonl"

    record = {
        "ts_et":    datetime.now(_ET).isoformat(timespec="seconds"),
        "severity": severity,
        "stage":    stage,
        "action":   action,
        "message":  message,
    }
    if extra:
        record.update(extra)

    line = json.dumps(record, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


# ── Internals ────────────────────────────────────────────────────────────────

def _classify_market_phase(minute_of_day: int) -> str:
    if minute_of_day < _T_OPEN:
        return "premarket"
    if minute_of_day < _T_NY_AM_END:
        return "ny_am"
    if minute_of_day < _T_NY_PM_OPEN:
        return "midday"
    if minute_of_day < _T_CLOSE:
        return "ny_pm"
    return "afterhours"


# Severity ordering — used by the overlay-merge logic to ensure overlays
# can ESCALATE the base alert level but never DOWNGRADE it.
_SEVERITY_ORDER = {"ok": 0, "info": 1, "warn": 2, "urgent": 3, "blocking": 4}


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0) else b


def _classify_stage(
    *,
    is_trading_day: bool,
    minute_of_day: int,
    autotrade_running: bool,
    universe_applied_today: bool,
    backend_started_late_today: bool,
) -> tuple[str, str, str, bool, list[str]]:
    """Return (stage, severity, message, notify, actions).

    Severity levels:
      ok       — green or hidden banner
      info     — soft toast, no notify
      warn     — yellow banner, no notify
      urgent   — orange banner + browser notification
      blocking — red banner + notification + sound

    Composition rule (v7.16.1, 2026-05-07 fix): the BASE stage from the
    time-of-day × autotrade matrix is the source of truth for the
    blocking-alert path. Overlays (late-start, universe-not-applied)
    NEVER replace the base — they can only ESCALATE severity, decorate
    the message, or union into the actions list.

    Why this matters: a backend that starts at 09:35 with autotrade off
    must NOT lose the red `ny_am_idle` blocking alert just because the
    `late_start_today` overlay is also true. The fix is: compute the
    base first, then layer overlays as decorations.
    """
    # Weekend / holiday short-circuit — there's nothing to alert on, so
    # overlays don't apply either. Returns immediately.
    if not is_trading_day:
        return "non_trading_day", "ok", "Market closed (weekend/holiday).", False, []

    # ── Step 1: BASE stage from clock × autotrade ──
    base_stage, base_severity, base_message, base_notify, base_actions = \
        _base_stage_for_clock(minute_of_day, autotrade_running)

    # ── Step 2: Layer overlays as decorations on top of the base ──
    severity = base_severity
    notify = base_notify
    message_parts: list[str] = [base_message]
    actions: list[str] = list(base_actions)

    if not universe_applied_today and minute_of_day < _T_NY_AM_END:
        # Even if autotrade is running, we'd be running on .env defaults
        # only — the dynamic add-list isn't live. Escalate ok/info → warn.
        severity = _max_severity(severity, "warn")
        message_parts.append(
            "Universe NOT applied today — autotrade running on .env defaults only "
            "(dynamic add-list missing)."
        )
        if "apply_morning_universe" not in actions:
            actions.append("apply_morning_universe")

    if backend_started_late_today and minute_of_day < _T_CLOSE:
        severity = _max_severity(severity, "warn")
        message_parts.append(
            "Backend started AFTER 09:30 ET — early NY_AM matches that fired "
            "before startup are uncaptured; review the morning manually."
        )

    return (
        base_stage,
        severity,
        " · ".join(message_parts),
        notify,
        actions,
    )


def _base_stage_for_clock(
    minute_of_day: int,
    autotrade_running: bool,
) -> tuple[str, str, str, bool, list[str]]:
    """Pure time-of-day × autotrade matrix. No overlays. The blocking-
    alert path lives here — `_classify_stage` calls this first and then
    layers overlays on top so they can never mask a blocking state.
    """
    actions = ["start_autotrade"]

    if minute_of_day < _T_PREP_END:                          # < 08:35
        if autotrade_running:
            return ("premarket_ready", "ok",
                    "Premarket. Autotrade is running and ready for NY_AM at 09:30 ET.",
                    False, [])
        return ("premarket_idle", "ok",
                "Premarket — start AI Trading any time before 09:30 ET.",
                False, actions)

    if minute_of_day < _T_T30:                                # 08:35 – 08:59
        if autotrade_running:
            return ("post_build_ready", "ok",
                    "Universe applied. Autotrade running. Ready for NY_AM in "
                    f"{_T_OPEN - minute_of_day} min.",
                    False, [])
        return ("post_build_idle", "info",
                "Morning universe applied. Click Start AI Trading "
                f"({_T_OPEN - minute_of_day} min to open).",
                False, actions)

    if minute_of_day < _T_T5:                                 # 09:00 – 09:24
        if autotrade_running:
            return ("t30_ready", "ok",
                    f"Autotrade running. NY_AM opens in {_T_OPEN - minute_of_day} min.",
                    False, [])
        return ("t30_idle", "warn",
                f"T-{_T_OPEN - minute_of_day}: AI Trading is OFF. NY_AM opens in "
                f"{_T_OPEN - minute_of_day} min — start it now.",
                False, actions)

    if minute_of_day < _T_OPEN:                               # 09:25 – 09:29
        if autotrade_running:
            return ("t5_ready", "ok",
                    f"Autotrade running. NY_AM opens in {_T_OPEN - minute_of_day} min.",
                    False, [])
        return ("t5_idle", "urgent",
                f"⚠ T-{_T_OPEN - minute_of_day}: AI Trading is OFF. "
                "Start it NOW — NY_AM opens in less than 5 min.",
                True, actions)

    if minute_of_day < _T_NY_AM_END:                          # 09:30 – 10:59
        if autotrade_running:
            return ("ny_am_active", "ok",
                    "Trading NY_AM — peak ICT setup window.", False, [])
        return ("ny_am_idle", "blocking",
                "🚨 NY_AM is OPEN and AI Trading is OFF. Setups are firing — "
                "click Start AI Trading immediately.",
                True, actions)

    if minute_of_day < _T_NY_PM_OPEN:                         # 11:00 – 13:29
        if autotrade_running:
            return ("midday_active", "ok",
                    "Lunch dead zone — autotrade can stay on; few setups fire here.",
                    False, [])
        return ("midday_idle", "warn",
                f"Lunch hours. NY_PM opens in {_T_NY_PM_OPEN - minute_of_day} min — "
                "start AI Trading before then.",
                False, actions)

    if minute_of_day < _T_CLOSE:                              # 13:30 – 15:59
        if autotrade_running:
            return ("ny_pm_active", "ok",
                    "Trading NY_PM — afternoon setup window.", False, [])
        return ("ny_pm_idle", "warn",
                f"NY_PM is OPEN and AI Trading is OFF. {_T_CLOSE - minute_of_day} "
                "min of trading time left.",
                False, actions)

    # 16:00+
    return ("market_closed", "ok",
            "Market closed for the day.", False, [])


def list_events_for_date(date_iso: str) -> list[dict[str, Any]]:
    """Read all readiness events for a given YYYY-MM-DD (NY date)."""
    path = _AUDIT_DIR / f"readiness_events_{date_iso}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
