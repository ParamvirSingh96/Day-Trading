"""
Autonomous-trading scheduler (v7.16+, 2026-05-13)
=============================================================
Multi-event in-process scheduler that fires the daily trading routine
end-to-end without operator input.

Events (ET, weekday-only)
-------------------------
  08:30    build       — morning-universe scan (existed since v7.15.3)
  ~08:32   apply       — inline inside build when AUTO_APPLY_AFTER_BUILD=true
  09:30    auto_start  — autotrade scanner start (NEW; fail-closed)
  close+5  auto_stop   — autotrade scanner stop (NEW; fail-OPEN)
  close+30 summary     — daily summary email (NEW)

``close`` is the actual market close pulled from Alpaca's
``/v2/calendar`` via ``trading_calendar.get_today``. So early-close days
(Thanksgiving Friday, July 3, Christmas Eve) yield ``13:05/13:30 ET``
without code changes.

Fail-closed start (operator refinement #3)
------------------------------------------
``_run_auto_start`` refuses to fire if ANY of:

  1. ``AUTO_START_TRADING=false`` in .env
  2. Not paper mode (``ALPACA_PAPER`` != true)
  3. ``LIVE_TRADING_ENABLED`` != "no"
  4. Calendar says market closed today (or calendar lookup failed)
  5. No applied universe snapshot exists for today
  6. ``SCHEDULER_USER_EMAIL`` empty OR no matching DB user

Any failure → log + email + skip. Auto-stop and summary still run.

Fail-open stop (operator refinement #5)
---------------------------------------
``_run_auto_stop`` always attempts. If calendar lookup failed,
``_today_events`` substitutes fixed ``16:05 ET / 16:30 ET`` fallbacks
so stop/summary are always scheduled — the safety net is never absent.

State file
----------
Each event records its outcome to ``~/.tradehack/state/<date>.json``
(via ``scheduler_state.record_event``). Used by:
  * the loop's resume logic — after a uvicorn restart, an already-fired
    event isn't refired
  * the daily summary email — to render per-event status lines

Why in-process and not launchd
------------------------------
launchd-based scheduling was tried first and blocked by macOS TCC
sandboxing when the Dev folder lives under ``~/Documents/``. uvicorn
inherits the operator's shell TCC permissions, so the in-process
scheduler avoids the sandbox entirely. Trade-off: if uvicorn dies,
the scheduler dies with it. The market-awake plist + future backend
supervisor plist mitigate that.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo


# Named under the ``tradehack.*`` hierarchy that logging_config.py
# elevates to INFO. Default ``__name__`` would get filtered to WARNING.
logger = logging.getLogger("tradehack.scheduler")

_ET = ZoneInfo("America/New_York")

# Fixed wall-clock event times (ET).
_BUILD_TIME_ET = time(8, 30)
_START_TIME_ET = time(9, 30)
# Fallbacks used when ``trading_calendar.get_today`` reports
# ``lookup_failed=True``. Ensures stop and summary are always scheduled
# even when we can't ask Alpaca for the actual close.
_STOP_FALLBACK_TIME_ET = time(16, 5)
_SUMMARY_FALLBACK_TIME_ET = time(16, 30)
_STOP_OFFSET = timedelta(minutes=5)
_SUMMARY_OFFSET = timedelta(minutes=30)

# v7.16+ (2026-05-14): grace window for "is this event past?" checks.
# When the loop wakes from the overnight idle-sleep at exactly the next
# event's time (e.g., 08:30:00.000), a strict ``event.when <= now``
# check classifies that event as past and silently skips it. The 60s
# grace covers wake-from-sleep boundary, asyncio scheduling jitter,
# clock skew (DST, NTP corrections), and the natural ~msec drift
# between target time and actual wake time. Symmetrically applied in
# ``_find_next_event`` and ``_record_missed_events_on_startup`` so
# both functions agree on which events should fire vs be marked
# ``skipped_missed``.
_EVENT_GRACE_PERIOD = timedelta(seconds=60)

# v7.16+ (2026-05-14): autotrade watchdog cadence. Catches a silently-dead
# scan loop within one missed scan cycle (scans run every 25-45s). See
# ``_watchdog_loop`` for the conservative recovery conditions.
#
# Root cause that motivated this: 2026-05-14 had the autotrade scan loop
# die silently after a ``--reload`` worker swap (Phase 7 telemetry edits
# triggered WatchFiles). Old worker got SIGTERM, new worker had
# ``state.running=False`` by default, scheduler saw ``start: success`` in
# the state file from the 09:30 fire and did NOT re-fire. Result: 5h 40m
# of no scanning. Watchdog is defense-in-depth so the next such event
# self-heals.
_WATCHDOG_INTERVAL_SECONDS = 60

# v7.16+ (2026-05-15): minimum runway required to recover the 09:30
# auto-start event late. If uvicorn comes up after 09:30 with the rest
# of the trading day still in front of us, the scheduler can catch up.
# But if there's less than this much time until close, starting the
# scanner is too late to be useful — skip the recovery and let
# tomorrow's natural flow handle it.
_START_RECOVERY_MIN_RUNWAY = timedelta(minutes=30)


def _is_past_for_scheduling(event_when: datetime, now: datetime) -> bool:
    """True if ``event_when`` is past the grace window — i.e., the
    event should NOT be fired (too late) and should be recorded as
    ``skipped_missed`` at startup. Events within 60s past are still
    considered fireable."""
    return event_when < now - _EVENT_GRACE_PERIOD


@dataclass
class SchedulerStatus:
    """Snapshot surfaced via /scheduler/status.

    Legacy ``last_fire_*`` fields are retained for backwards compat with
    the dashboard (which polls them today). New multi-event statuses
    live in the state file and are merged into ``get_status``'s return
    value as ``events_today``.
    """
    enabled: bool = True
    next_fire_at: str | None = None
    # Legacy single-event fields (BUILD-specific now).
    last_fire_started_at: str | None = None
    last_fire_finished_at: str | None = None
    last_fire_status: str | None = None
    last_fire_error: str | None = None
    last_fire_summary: dict | None = None
    fires_total: int = 0
    fires_failed: int = 0


_status = SchedulerStatus()
_task: asyncio.Task | None = None
# v7.16+ (2026-05-14): defense-in-depth watchdog task. Independent of
# the main scheduler loop so a hang/crash in one doesn't stop the
# other. Lifecycle managed alongside ``_task`` in ``start()``/``stop()``.
_watchdog_task: asyncio.Task | None = None


def _todays_watchdog_restart_count() -> int:
    """Count watchdog restarts already recorded in TODAY's state file.

    2026-05-19 bug-fix: the previous implementation used a module-level
    ``_watchdog_restart_count`` global. That global resets to 0 on every
    uvicorn restart (the module is re-imported), so four separate
    watchdog restarts on 2026-05-19 — each in a fresh uvicorn process
    after a deploy — all emailed "restart #1" and the body's "Restart
    number today: 1" was simply wrong. The state-file event key
    ``watchdog_restart_1`` also collided every time, overwriting the
    prior record.

    Deriving the count from the persisted state file makes it a true
    per-DAY counter that survives uvicorn restarts. Returns the number
    of ``watchdog_restart_*`` events already in today's state; the
    caller adds 1 to get the current restart's number."""
    from . import scheduler_state
    try:
        events = scheduler_state.read().get("events", {})
        return sum(1 for k in events if k.startswith("watchdog_restart_"))
    except Exception:
        # Telemetry read failure must never block a real recovery —
        # fall back to 0 so the restart still proceeds (it just may
        # reuse an event key; acceptable degraded behavior).
        return 0


def get_status() -> dict:
    """JSON-safe snapshot. Merges in-memory legacy fields with today's
    per-event statuses from the state file."""
    from . import scheduler_state
    base = asdict(_status)
    base["events_today"] = scheduler_state.read().get("events", {})
    return base


# ── Time math ──────────────────────────────────────────────────────────────

def _today_et() -> date:
    return datetime.now(_ET).date()


def _is_holiday(d: date) -> bool:
    """Best-effort fixed-date holiday filter for the build event.
    Floating holidays are not detected — Alpaca's calendar (queried
    in ``_run_auto_start`` and ``_today_events``) is the authoritative
    source for those. The build runs anyway on floating holidays; the
    screener produces a tiny universe."""
    return (d.month, d.day) in {(1, 1), (7, 4), (12, 25)}


def _next_business_day_at(t: time) -> datetime:
    """Return the next ET weekday at ``t``, strictly after now. Skips
    Saturday/Sunday."""
    candidate = datetime.now(_ET).replace(
        hour=t.hour, minute=t.minute,
        second=0, microsecond=0,
    ) + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


async def _sleep_chunked_until(target: datetime) -> None:
    """Sleep until ``target`` in 1-hour chunks. Chunking lets the asyncio
    task survive DST transitions, clock changes, and wake-from-sleep
    by re-evaluating remaining duration each chunk."""
    while True:
        remaining = (target - datetime.now(_ET)).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 3600.0))


# ── Event scheduling ───────────────────────────────────────────────────────

@dataclass
class _ScheduledEvent:
    name: str
    when: datetime
    runner: Callable[[], Awaitable[None]]


def _today_events() -> list[_ScheduledEvent]:
    """Build today's event list. Stop/summary times derive from the
    Alpaca calendar so early-close days work. On calendar lookup
    failure, fall back to fixed 16:05/16:30 ET so the safety-net stop
    is never absent (operator refinement #5)."""
    from . import trading_calendar
    today = _today_et()
    build_at = datetime.combine(today, _BUILD_TIME_ET, tzinfo=_ET)
    start_at = datetime.combine(today, _START_TIME_ET, tzinfo=_ET)

    cal = trading_calendar.get_today()
    if cal.is_open and cal.market_close_at_et is not None:
        stop_at: datetime | None = cal.market_close_at_et + _STOP_OFFSET
        summary_at: datetime | None = cal.market_close_at_et + _SUMMARY_OFFSET
    elif cal.lookup_failed:
        # Calendar lookup failed — substitute fixed fallbacks so the
        # safety-net stop is never absent.
        stop_at = datetime.combine(today, _STOP_FALLBACK_TIME_ET, tzinfo=_ET)
        summary_at = datetime.combine(today, _SUMMARY_FALLBACK_TIME_ET, tzinfo=_ET)
    else:
        # Market genuinely closed today (weekend / holiday). No stop
        # or summary needed — autotrade was never started.
        stop_at = None
        summary_at = None

    events: list[_ScheduledEvent] = [
        _ScheduledEvent("build", build_at, _run_build),
        _ScheduledEvent("start", start_at, _run_auto_start),
    ]
    if stop_at is not None:
        events.append(_ScheduledEvent("stop", stop_at, _run_auto_stop))
    if summary_at is not None:
        events.append(_ScheduledEvent("summary", summary_at, _run_daily_summary))
    events.sort(key=lambda e: e.when)
    return events


def _find_next_event() -> _ScheduledEvent | None:
    """Return the next fireable event that hasn't fired today. None if
    today's queue is exhausted.

    Uses ``_is_past_for_scheduling`` so an event whose time is within
    60s past (e.g., we just woke from overnight idle-sleep at exactly
    the event boundary) is still treated as the next event to fire.
    Without this grace, the build at 08:30 would be silently dropped
    on every natural day-rollover because the wake-from-sleep target
    equals the event time.
    """
    from . import scheduler_state
    now = datetime.now(_ET)
    for event in _today_events():
        if _is_past_for_scheduling(event.when, now):
            continue
        if scheduler_state.event_fired(event.name):
            continue
        return event
    return None


def _can_recover_event(event: _ScheduledEvent, now: datetime) -> bool:
    """Return True if a past event should be RECOVERED (fired now,
    late) instead of marked ``skipped_missed`` (operator request,
    2026-05-15: "if laptop wasn't open at 08:30, apply automatically
    when uvicorn starts late").

    build → recoverable if market is open today + no applied universe
    snapshot already on disk + we're before market close. The screener
    cost is one Alpaca call; building late still gives the rest of the
    day a universe to scan.

    start → recoverable if AUTO_START_TRADING=true + market is open +
    applied universe exists + ``state.running=False`` + at least
    ``_START_RECOVERY_MIN_RUNWAY`` remains until close. The runway
    gate prevents starting the scanner with too little market time
    left to be useful.

    stop / summary → never recovered (they fire on their own schedule
    and don't make sense to retry early).
    """
    from . import autotrade, trading_calendar
    from .applied_universe import latest_applied_for_today
    from .settings import settings

    if event.name == "build":
        cal = trading_calendar.get_today()
        if not cal.is_open or cal.market_close_at_et is None:
            return False
        if now >= cal.market_close_at_et:
            return False
        data_dir = Path(__file__).resolve().parent.parent / "data"
        # Skip if an applied universe already exists (e.g., from a
        # manual ⟳ Build click before uvicorn was started).
        if latest_applied_for_today(out_dir=data_dir) is not None:
            return False
        return True

    if event.name == "start":
        # Stable gates only — these don't change between this pre-check
        # (at scheduler startup) and the fire time (a minute or two later
        # after build recovery completes):
        if not settings.auto_start_trading:
            return False
        cal = trading_calendar.get_today()
        if not cal.is_open or cal.market_close_at_et is None:
            return False
        if now >= cal.market_close_at_et - _START_RECOVERY_MIN_RUNWAY:
            return False
        # Transient gates (``applied universe exists``,
        # ``autotrade.state.running``) are NOT pre-checked here —
        # they'd be False at startup when build recovery hasn't run
        # yet, but True by the time the start recovery actually fires.
        # ``_run_auto_start`` re-checks all 6 preconditions at fire
        # time (Phase 4), so transient state is handled there.
        return True

    return False  # stop / summary never recovered


def _record_missed_events_on_startup() -> list[_ScheduledEvent]:
    """At scheduler startup, scan today's events whose time has passed
    the grace window (without a state-file record).

    For each:
      * If ``_can_recover_event`` says yes → queue it for late recovery
        (returned to ``start()`` and drained by ``_drain_recovery_queue``).
      * Otherwise → record as ``skipped_missed`` so the daily summary
        email surfaces what was missed.

    Events within the 60s grace are NOT touched — the main loop will
    fire them normally. Keeps this function consistent with
    ``_find_next_event``: an event one treats as "still fireable" must
    not be marked as "missed" by the other.

    Returns the recovery queue in event-time order (build before start,
    so build's auto-apply lands the snapshot before start fires).
    """
    from . import scheduler_state
    now = datetime.now(_ET)
    recovery_queue: list[_ScheduledEvent] = []
    # 2026-05-18: track every event's classification so we can emit a
    # single-line recovery summary at the end. Today's 09:43 restart
    # logged nothing about the start event being "already fired today";
    # the operator had to grep ict_scan output to confirm trading was
    # running. This summary makes the post-restart decision visible
    # in one grep ("scheduler_recovery_decision").
    queued_for_recovery: list[str] = []
    already_fired_today: list[str] = []
    marked_missed: list[str] = []
    still_pending: list[str] = []
    for event in _today_events():
        if not _is_past_for_scheduling(event.when, now):
            # Still within grace — main loop will fire it normally.
            still_pending.append(event.name)
            continue
        if scheduler_state.event_fired(event.name):
            # 2026-05-18: previously a silent ``continue``. Now we log
            # the existing status so the operator can confirm whether
            # the event already succeeded (no action needed) vs failed
            # before the restart (may need manual intervention).
            prev_status: str | None = None
            try:
                prev_event = scheduler_state.read().get("events", {}).get(event.name, {})
                prev_status = prev_event.get("status")
            except Exception:
                prev_status = None
            logger.info(
                "scheduler_event_already_fired_today name=%s prev_status=%s — recovery skipped",
                event.name, prev_status or "unknown",
            )
            already_fired_today.append(event.name)
            continue
        if _can_recover_event(event, now):
            recovery_queue.append(event)
            queued_for_recovery.append(event.name)
            logger.warning(
                "scheduler_event_recover_queued name=%s scheduled=%s now=%s",
                event.name, event.when.isoformat(),
                now.isoformat(timespec="seconds"),
            )
            continue
        scheduler_state.record_event(
            event.name,
            status="skipped_missed",
            reason="uvicorn was not running at the scheduled time",
            scheduled_for=event.when.isoformat(timespec="seconds"),
        )
        marked_missed.append(event.name)
        logger.warning(
            "scheduler_event_missed name=%s scheduled=%s",
            event.name, event.when.isoformat(),
        )
    # One-line recovery decision summary. Grep this on every restart to
    # immediately see what the scheduler decided to do with each event.
    logger.info(
        "scheduler_recovery_decision queued=%s already_fired_today=%s missed=%s still_pending=%s",
        queued_for_recovery or "[]",
        already_fired_today or "[]",
        marked_missed or "[]",
        still_pending or "[]",
    )
    return recovery_queue


async def _drain_recovery_queue(recovery_queue: list[_ScheduledEvent]) -> None:
    """Fire each recovery event in event-time order, sequentially.
    Build must complete (auto-apply included) before start fires so
    the applied-universe precondition is met.

    Failures are caught + logged + recorded — the main loop still
    starts even if a recovery fails, so we don't strand the scheduler.

    2026-05-18: emit explicit ``scheduler_event_recovered`` lines after
    each successful runner so a post-restart operator can grep "recover"
    and see exactly which events fired late (instead of guessing from
    downstream side effects like the first ict_scan_complete line).
    """
    from . import scheduler_state
    if not recovery_queue:
        return
    logger.warning(
        "scheduler_recovery_start count=%d names=%s",
        len(recovery_queue), [e.name for e in recovery_queue],
    )
    for event in recovery_queue:
        try:
            logger.warning("scheduler_event_recovering name=%s", event.name)
            await event.runner()
            # Success log — pairs 1:1 with the "recovering" line above.
            # The runner has already recorded its outcome in
            # scheduler_state (e.g. _run_auto_start → "success" /
            # "refused" / "skipped"); this log just says "the recovery
            # call returned without raising".
            logger.warning(
                "scheduler_event_recovered name=%s scheduled=%s",
                event.name, event.when.isoformat(),
            )
        except Exception as exc:
            logger.exception(
                "scheduler_event_recovery_failed name=%s", event.name,
            )
            try:
                scheduler_state.record_event(
                    event.name,
                    status="recovery_failed",
                    error=str(exc),
                )
            except Exception:
                pass
    logger.warning("scheduler_recovery_done")


# ── Event runners ──────────────────────────────────────────────────────────

async def _run_build() -> None:
    """Existing morning build + optional auto-apply (unchanged), then
    mirror the legacy ``_status.last_fire_*`` outcome to the state
    file so the daily summary can render it."""
    from . import scheduler_state
    await _run_one_build()  # populates _status.last_fire_*
    status = _status.last_fire_status or "failed"
    detail: dict[str, Any] = {}
    if _status.last_fire_error:
        detail["error"] = _status.last_fire_error
    if _status.last_fire_summary:
        detail["summary"] = _status.last_fire_summary
    scheduler_state.record_event("build", status=status, **detail)


async def _run_auto_start() -> None:
    """Auto-start the autotrade scanner. Six fail-closed preconditions
    (any failure → log + email + skip)."""
    from sqlalchemy import select
    from . import autotrade, email_alerts, scheduler_state, trading_calendar
    from .applied_universe import latest_applied_for_today
    from .db import SessionLocal
    from .models import User
    from .settings import settings

    def _refuse(reason: str, *, status: str = "refused",
                send_email: bool = True, **extra: Any) -> None:
        logger.info("scheduler_auto_start_%s reason=%s", status, reason)
        scheduler_state.record_event("start", status=status, reason=reason, **extra)
        if send_email:
            email_alerts.send_failure_alert(
                f"auto-start {status}: {reason}",
                f"The auto-start event was {status}.\n\nReason: {reason}\n",
            )

    # 1. Opt-in flag
    if not settings.auto_start_trading:
        # Silent skip — not an error, just not enabled. No email.
        logger.info("scheduler_auto_start_skipped reason=AUTO_START_TRADING=false")
        scheduler_state.record_event(
            "start",
            status="skipped",
            reason="AUTO_START_TRADING=false",
        )
        return

    # 2. Paper mode
    if not settings.alpaca_paper:
        _refuse("ALPACA_PAPER is not true (live mode not allowed)")
        return

    # 3. Explicit live-disable flag (belt-and-suspenders)
    if os.getenv("LIVE_TRADING_ENABLED", "").lower() != "no":
        _refuse("LIVE_TRADING_ENABLED is not 'no'")
        return

    # 4. Calendar — market open today?
    cal = trading_calendar.get_today()
    if not cal.is_open:
        if cal.lookup_failed:
            _refuse(
                "calendar lookup failed (cannot verify today is a trading day)",
                status="skipped_lookup_failed",
            )
        else:
            logger.info("scheduler_auto_start_skipped reason=market_closed")
            scheduler_state.record_event(
                "start",
                status="skipped_market_closed",
                reason="market closed today",
            )
        return

    # 5. Applied universe snapshot present?
    data_dir = Path(__file__).resolve().parent.parent / "data"
    snap = latest_applied_for_today(out_dir=data_dir)
    if snap is None:
        _refuse(
            f"no applied morning-universe snapshot for {cal.date.isoformat()}",
        )
        return

    # 6. Operator user resolution
    operator_email = (settings.scheduler_user_email or "").strip()
    if not operator_email:
        _refuse("SCHEDULER_USER_EMAIL is empty in .env")
        return

    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == operator_email))
    except Exception as exc:
        logger.exception("scheduler_auto_start_db_failed")
        scheduler_state.record_event(
            "start", status="failed",
            reason="DB error during user lookup", error=str(exc),
        )
        email_alerts.send_failure_alert(
            "auto-start failed: DB error",
            f"Failed to look up operator user {operator_email!r}.\n\nError: {exc}",
        )
        return

    if user is None:
        _refuse(f"no user with email={operator_email!r} in DB")
        return

    # All preconditions passed.
    try:
        autotrade.start(user_id=user.id)
        logger.info(
            "scheduler_auto_start_success user_id=%d email=%s",
            user.id, operator_email,
        )
        scheduler_state.record_event(
            "start", status="success",
            user_id=user.id, user_email=operator_email,
            applied_snapshot=str(snap),
        )
    except Exception as exc:
        logger.exception("scheduler_auto_start_failed")
        scheduler_state.record_event(
            "start", status="failed",
            reason="autotrade.start raised",
            error=str(exc), user_email=operator_email,
        )
        email_alerts.send_failure_alert(
            "auto-start failed: autotrade.start raised",
            f"User: {operator_email}\nError: {exc}",
        )


async def _run_auto_stop() -> None:
    """Always attempt to stop autotrade (fail-OPEN, refinement #5).

    If autotrade isn't running (start never fired or it stopped early),
    record ``skipped_not_running``. That's not an error, just truthful.
    """
    from . import autotrade, email_alerts, scheduler_state

    if not autotrade.state.running:
        logger.info("scheduler_auto_stop_skip reason=autotrade_not_running")
        scheduler_state.record_event(
            "stop",
            status="skipped_not_running",
            reason="autotrade.state.running was False",
        )
        return

    try:
        autotrade.stop()
        logger.info("scheduler_auto_stop_success")
        scheduler_state.record_event("stop", status="success")
    except Exception as exc:
        logger.exception("scheduler_auto_stop_failed")
        scheduler_state.record_event(
            "stop", status="failed", error=str(exc),
        )
        email_alerts.send_failure_alert(
            "auto-stop failed: autotrade.stop raised",
            f"Error: {exc}",
        )


async def _run_daily_summary() -> None:
    """Compose + send the EOD digest. Best-effort: a single source's
    failure adds to an Errors section but doesn't block the send."""
    from . import email_alerts, scheduler_state
    # v7.16+ (2026-05-14): record "running" BEFORE composing the body so
    # the body's "Scheduler events" section sees ``summary: running``
    # (accurate at compose time — this email IS the summary running)
    # instead of "not run". The success/failed outcome overwrites this
    # record after the email send completes below.
    scheduler_state.record_event("summary", status="running")
    errors: list[str] = []
    try:
        body = await asyncio.to_thread(_build_daily_summary_body, errors)
    except Exception as exc:
        logger.exception("scheduler_summary_aggregation_failed")
        body = (
            f"Daily summary aggregation failed entirely: {exc}\n\n"
            "Check uvicorn logs for the full traceback."
        )
        errors.append(f"aggregation: {exc}")

    sent = email_alerts.send_daily_summary(
        f"{_today_et().isoformat()} EOD",
        body,
    )
    if sent:
        scheduler_state.record_event(
            "summary", status="success",
            errors=errors or None,
        )
    else:
        # Either unconfigured email OR send failure. Both end with
        # send_daily_summary returning False; distinguish via the
        # configured-check.
        from .email_alerts import _is_configured
        if _is_configured():
            scheduler_state.record_event(
                "summary", status="failed",
                reason="email send returned False",
                errors=errors or None,
            )
        else:
            scheduler_state.record_event(
                "summary", status="skipped_no_email_config",
                reason="SMTP not configured",
                errors=errors or None,
            )


def _build_daily_summary_body(errors: list[str]) -> str:
    """Compose plaintext summary body. Each section wraps its own data
    fetch in try/except so a single source failure adds to ``errors``
    rather than tanking the whole email — partial info beats silence."""
    from . import autotrade, scheduler_state, trading_calendar
    from .alpaca import get_shared_trading_client
    from .settings import settings

    today = _today_et()
    cal = trading_calendar.get_today()

    lines: list[str] = []
    lines.append(f"Date:               {today.isoformat()}")
    if cal.is_open and cal.open_et and cal.close_et:
        hours = f"{cal.open_et.strftime('%H:%M')}–{cal.close_et.strftime('%H:%M')} ET"
        if cal.is_early_close:
            hours += "  (EARLY CLOSE)"
        lines.append(f"Market hours:       {hours}")
    elif cal.lookup_failed:
        lines.append("Market hours:       (calendar lookup failed)")
    else:
        lines.append("Market hours:       (market closed today)")

    # AUTO_START_TRADING explicitly surfaced so "no trades" isn't mysterious.
    lines.append("")
    lines.append(f"AUTO_START_TRADING: {settings.auto_start_trading}")
    if not settings.auto_start_trading:
        lines.append("  -> unattended trading is OFF. Flip in .env to enable.")

    # Per-event statuses from the state file
    lines.append("")
    lines.append("Scheduler events:")
    state = scheduler_state.read(today).get("events", {})
    for name in ("build", "start", "stop", "summary"):
        evt = state.get(name)
        if evt is None:
            lines.append(f"  {name:<10s} not run")
        else:
            ts = evt.get("ts", "?")
            status = evt.get("status", "?")
            # The summary event is still "running" while THIS body is being
            # composed — it only flips to "success" AFTER the email sends
            # (chicken-and-egg: it can't report its own completion inside its
            # own report). The state file records the true "success" moments
            # later. Relabel so the report doesn't look like the summary hung.
            if name == "summary" and status == "running":
                status = "sending (this report)"
            extra_parts = []
            if evt.get("reason"):
                extra_parts.append(evt["reason"])
            if evt.get("error"):
                extra_parts.append(f"error={evt['error']}")
            extra = f"  ({'; '.join(extra_parts)})" if extra_parts else ""
            lines.append(f"  {name:<10s} {status:<24s} at {ts}{extra}")

    # Universe applied
    lines.append("")
    try:
        meta = autotrade.state.applied_universe_metadata or {}
        n_base = meta.get("n_base", "?")
        n_dyn = meta.get("n_dynamic", "?")
        lines.append(f"Universe applied:   {n_base} base + {n_dyn} morning dynamic")
        symbols = (
            meta.get("morning_dynamic_symbols")
            or meta.get("dynamic_symbols")
            or meta.get("symbols")
        )
        if symbols:
            lines.append(f"Dynamic symbols:    {', '.join(sorted(symbols))}")
    except Exception as exc:
        errors.append(f"universe metadata: {exc}")
        lines.append("Universe applied:   (read failed)")

    # Scan stats (from JSONL)
    lines.append("")
    try:
        from .scan_stats import out_dir as _scan_stats_dir
        path = _scan_stats_dir() / f"{today.isoformat()}.jsonl"
        if path.exists():
            import json as _json
            rows: list[dict[str, Any]] = []
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(_json.loads(line))
                        except Exception:
                            pass
            matched_rows = [r for r in rows if r.get("matched_symbols", 0) > 0]
            forming_rows = [r for r in rows if r.get("forming_symbols", 0) > 0]
            lines.append(f"Scanner activity:   {len(rows)} scans")

            # v7.16+ (2026-05-15): coverage diagnostics. Surfaces
            # laptop-sleep / process-suspend issues that "all scheduler
            # events success" otherwise hides. Two signals:
            #   1. first/last scan timestamps + "stopped early" warning
            #      if last scan is > 5 min before market close
            #   2. coverage gaps > 3 min between consecutive scans
            #      (3 min sits above the normal slow-cadence ceiling
            #      ~2.5 min, so only real anomalies show)
            if rows:
                try:
                    first_ts = datetime.fromisoformat(rows[0]["ts"])
                    last_ts = datetime.fromisoformat(rows[-1]["ts"])
                    lines.append(
                        f"  first scan:       {first_ts.strftime('%H:%M:%S')} ET"
                    )
                    last_line = (
                        f"  last scan:        {last_ts.strftime('%H:%M:%S')} ET"
                    )
                    if cal.market_close_at_et is not None:
                        mins_short = (
                            cal.market_close_at_et - last_ts
                        ).total_seconds() / 60
                        if mins_short > 5:
                            last_line += (
                                f"  -> {mins_short:.0f} min before close "
                                f"(scanner stopped early)"
                            )
                    lines.append(last_line)
                except Exception as exc:
                    errors.append(f"scan_stats first/last: {exc}")

            lines.append(f"  matched fires:    {len(matched_rows)}")
            lines.append(f"  forming-only:     {len(forming_rows)}")

            # Coverage gaps > 3 min — laptop sleep, process suspend, etc.
            try:
                gaps: list[tuple[datetime, datetime, float]] = []
                prev_ts: datetime | None = None
                for r in rows:
                    try:
                        ts = datetime.fromisoformat(r["ts"])
                    except Exception:
                        continue
                    if prev_ts is not None:
                        delta = (ts - prev_ts).total_seconds()
                        if delta > 180:
                            gaps.append((prev_ts, ts, delta))
                    prev_ts = ts
                if gaps:
                    gaps.sort(key=lambda g: g[2], reverse=True)
                    n_show = min(3, len(gaps))
                    lines.append("")
                    lines.append(
                        f"Coverage gaps (>3 min, top {n_show} of {len(gaps)}):"
                    )
                    for prev, curr, dt in gaps[:n_show]:
                        lines.append(
                            f"  {prev.strftime('%H:%M:%S')} -> "
                            f"{curr.strftime('%H:%M:%S')}  ({dt/60:.1f} min)"
                        )
            except Exception as exc:
                errors.append(f"scan_stats gaps: {exc}")
        else:
            lines.append("Scanner activity:   no JSONL for today (uvicorn was down?)")
    except Exception as exc:
        errors.append(f"scan_stats: {exc}")
        lines.append("Scanner activity:   (read failed)")

    # v7.16+ (2026-05-14): Matched outcomes. Tells the operator what
    # happened to each scanner-matched signal (submitted vs blocked +
    # the gate that blocked it + machine-readable reason + human detail).
    # Sourced from ``~/.tradehack/matched_outcomes/<date>.jsonl`` which is
    # appended to by the autotrade rejection/success hook sites.
    lines.append("")
    try:
        import json as _json
        from .matched_outcomes import out_dir as _outcomes_dir
        outcomes_path = _outcomes_dir() / f"{today.isoformat()}.jsonl"
        if outcomes_path.exists():
            outcome_rows: list[dict[str, Any]] = []
            with open(outcomes_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            row = _json.loads(line)
                            if (
                                row.get("matched_strategies")
                                or row.get("reason") != "no_result"
                            ):
                                outcome_rows.append(row)
                        except Exception:
                            pass
            if outcome_rows:
                lines.append(f"Matched outcomes ({len(outcome_rows)}):")
                for r in outcome_rows:
                    ts_full = r.get("ts", "?")
                    hhmm = (
                        ts_full.split("T")[-1][:5]
                        if "T" in ts_full else ts_full
                    )
                    symbol = r.get("symbol", "?")
                    outcome = r.get("outcome", "?")
                    reason = r.get("reason", "?")
                    detail = r.get("detail")
                    line_str = (
                        f"  {hhmm}  {symbol:<6s} {outcome:<10s} {reason}"
                    )
                    if detail:
                        line_str += f"  ({detail})"
                    lines.append(line_str)
            else:
                lines.append("Matched outcomes:   (no rows recorded today)")
        else:
            lines.append("Matched outcomes:   (no JSONL — no matched signals processed by autotrade)")
    except Exception as exc:
        errors.append(f"matched_outcomes: {exc}")
        lines.append("Matched outcomes:   (read failed)")

    # Trades today (from DB) — last 24h as a tz-tolerant approximation.
    # v7.14 (2026-05-18): grouped by ``scanner_id`` under a "Trades — by
    # scanner" subsection. Today there's only one scanner (ICT) so the
    # email shows a single section; v7.15 will add an "Experimental
    # Scanner" section here when its trades start landing.
    lines.append("")
    try:
        from sqlalchemy import select
        from .db import SessionLocal
        from .models import TradeRecord
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        with SessionLocal() as db:
            trades = db.scalars(
                select(TradeRecord)
                .where(TradeRecord.opened_at >= since)
                .order_by(TradeRecord.opened_at)
            ).all()
        if trades:
            # Group rows by scanner_id. Default-bucket missing values
            # (legacy/pre-migration rows) into "ict" — that matches the
            # column default and keeps the v7.12.1 baseline comparable.
            by_scanner: dict[str, list] = {}
            for t in trades:
                sid = (t.scanner_id or "ict") if hasattr(t, "scanner_id") else "ict"
                by_scanner.setdefault(sid, []).append(t)

            # Header line: total count + per-scanner breakdown
            scanner_counts = ", ".join(
                f"{sid}={len(rows)}" for sid, rows in sorted(by_scanner.items())
            )
            lines.append(f"Trades:             {len(trades)} ({scanner_counts})")

            # Pretty labels for the per-scanner subsections. Anything
            # not in this map (e.g. a future "experimental_v2") falls
            # back to the raw scanner_id capitalised.
            _SCANNER_LABELS = {
                "ict": "ICT Scanner",
                "experimental_v1": "Experimental Scanner",
            }

            # Sort scanner subsections so ICT renders first (audit
            # anchor), then anything else alphabetically.
            def _scanner_order(sid: str) -> tuple[int, str]:
                return (0 if sid == "ict" else 1, sid)

            # v7.15: per-scanner aggregates need a family lookup. Build a
            # name → family map from the live + experimental playbooks
            # so the per-trade row can be tagged "[ORB Long, classic]".
            # The map is rebuilt every email send — tiny cost; avoids
            # a stale-import problem if strategies.py changes between
            # restarts.
            try:
                from .strategies import PLAYBOOK as _SCH_PLAYBOOK
                from .strategies import EXPERIMENTAL_PLAYBOOK as _SCH_EXP_PLAYBOOK
                _NAME_TO_FAMILY = {
                    s.name: s.family
                    for s in list(_SCH_PLAYBOOK) + list(_SCH_EXP_PLAYBOOK)
                }
            except Exception:
                _NAME_TO_FAMILY = {}

            def _primary_strategy(matched_str: str | None) -> str | None:
                """``matched_strategies`` is a CSV like 'ORB Long, VWAP Reclaim Long';
                we render only the first one to keep the row compact."""
                if not matched_str:
                    return None
                first = matched_str.split(",")[0].strip()
                return first or None

            for sid in sorted(by_scanner.keys(), key=_scanner_order):
                rows = by_scanner[sid]
                label = _SCANNER_LABELS.get(sid, sid.replace("_", " ").title())
                lines.append("")
                # Per-scanner aggregates (total R, win rate). Trades
                # without an r_multiple (still open at email time) are
                # excluded from both. Win = r_multiple > 0.
                graded = [t for t in rows if t.r_multiple is not None]
                total_r = sum(t.r_multiple for t in graded)
                wins = sum(1 for t in graded if t.r_multiple > 0)
                win_rate = (wins / len(graded) * 100.0) if graded else 0.0
                agg = (
                    f"n={len(rows)} closed={len(graded)} "
                    f"win_rate={win_rate:.0f}% totalR={total_r:+.2f}R"
                )
                lines.append(f"{label} (scanner_id={sid}): {agg}")

                # v7.15: experimental section also shows per-family
                # rollups so the reader can see "classic 1W/2L, momentum
                # 0W/1L" at a glance. Skipped for ICT (single family).
                if sid == "experimental_v1":
                    family_rows: dict[str, list] = {}
                    for t in rows:
                        primary = _primary_strategy(t.matched_strategies)
                        fam = _NAME_TO_FAMILY.get(primary, "unknown")
                        family_rows.setdefault(fam, []).append(t)
                    family_lines: list[str] = []
                    for fam in sorted(family_rows.keys()):
                        f_rows = family_rows[fam]
                        f_graded = [t for t in f_rows if t.r_multiple is not None]
                        f_wins = sum(1 for t in f_graded if t.r_multiple > 0)
                        f_wr = (f_wins / len(f_graded) * 100.0) if f_graded else 0.0
                        f_totalR = sum(t.r_multiple for t in f_graded)
                        family_lines.append(
                            f"{fam}: n={len(f_rows)} wr={f_wr:.0f}% totalR={f_totalR:+.2f}R"
                        )
                    if family_lines:
                        lines.append("  by family: " + " · ".join(family_lines))

                for t in rows:
                    opened_dt = t.opened_at
                    if opened_dt and opened_dt.tzinfo is None:
                        opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                    opened = opened_dt.astimezone(_ET).strftime("%H:%M") if opened_dt else "?"
                    closed = "—"
                    if t.closed_at:
                        closed_dt = t.closed_at
                        if closed_dt.tzinfo is None:
                            closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                        closed = closed_dt.astimezone(_ET).strftime("%H:%M")
                    r = f"{t.r_multiple:+.2f}R" if t.r_multiple is not None else "?"
                    pnl = f"${t.pnl:+.0f}" if t.pnl is not None else "?"
                    # v7.15: append the primary matched strategy + family
                    # to the row so experimental trades are self-describing
                    # ("[ORB Long, classic]"). ICT rows also get the tag
                    # so the email's a uniform line shape across scanners.
                    primary = _primary_strategy(t.matched_strategies)
                    fam = _NAME_TO_FAMILY.get(primary, "?") if primary else "?"
                    tag = f"  [{primary}, {fam}]" if primary else ""
                    lines.append(
                        f"  {opened}-{closed:<5s} {(t.symbol or '?'):<6s} "
                        f"{(t.direction or '?'):<5s}  {r:<8s} {pnl}{tag}"
                    )
        else:
            lines.append("Trades:             none today")
    except Exception as exc:
        errors.append(f"trades DB: {exc}")
        lines.append("Trades:             (read failed)")

    # Wallet snapshot
    lines.append("")
    try:
        acct = get_shared_trading_client().get_account()
        equity = float(acct.get("equity", 0))
        last_equity = float(acct.get("last_equity", equity))
        pnl = equity - last_equity
        lines.append("Wallet (Alpaca paper):")
        lines.append(f"  Start equity:     ${last_equity:,.2f}")
        lines.append(f"  End equity:       ${equity:,.2f}")
        lines.append(f"  Today's P&L:      ${pnl:+,.2f}")
    except Exception as exc:
        errors.append(f"wallet: {exc}")
        lines.append("Wallet:             (read failed)")

    # Errors footer
    lines.append("")
    if errors:
        lines.append(f"Errors during aggregation ({len(errors)}):")
        for e in errors:
            lines.append(f"  * {e}")
    else:
        lines.append("Errors:             none")

    return "\n".join(lines)


# ── Build invocation (legacy v7.15.4 logic — kept intact) ──────────────────

async def _run_one_build() -> None:
    """Execute the morning-universe build for today. Populates the
    legacy ``_status.last_fire_*`` fields. ``_run_build`` wraps this
    and mirrors the outcome to the daily state file.
    """
    from .alpaca import AlpacaMarketDataClient
    from .screener_morning import (
        DEFAULT_CANDIDATE_POOL,
        DEFAULT_MIN_PM_DOLLAR_VOL,
        DEFAULT_N_DYNAMIC,
        ScreenerConfig,
        build_morning_universe,
        write_snapshot_json,
    )
    from .settings import settings

    today_et = _today_et()
    started_at = datetime.now(_ET).isoformat(timespec="seconds")
    _status.last_fire_started_at = started_at
    _status.last_fire_status = None
    _status.last_fire_error = None
    _status.last_fire_summary = None

    if _is_holiday(today_et):
        logger.info("scheduler_skip_holiday date=%s", today_et.isoformat())
        _status.fires_total += 1
        _status.last_fire_status = "skipped_holiday"
        _status.last_fire_finished_at = datetime.now(_ET).isoformat(timespec="seconds")
        return

    base_symbols = list(settings.training_universe)
    candidate_pool = list(DEFAULT_CANDIDATE_POOL)
    cfg = ScreenerConfig(
        n_dynamic=DEFAULT_N_DYNAMIC,
        min_pm_dollar_vol=DEFAULT_MIN_PM_DOLLAR_VOL,
    )

    try:
        client = AlpacaMarketDataClient()
    except Exception as exc:
        logger.error("scheduler_alpaca_credentials_failed err=%s", exc)
        _status.fires_total += 1
        _status.fires_failed += 1
        _status.last_fire_status = "failed"
        _status.last_fire_error = f"Alpaca credentials: {exc}"
        _status.last_fire_finished_at = datetime.now(_ET).isoformat(timespec="seconds")
        return

    def _run_in_thread() -> dict[str, Any]:
        try:
            candidates, cfg_used = build_morning_universe(
                today_et,
                base_symbols=base_symbols,
                candidate_pool=candidate_pool,
                config=cfg,
                market_data_client=client,
            )
            data_dir = Path(__file__).resolve().parent.parent / "data"
            path = write_snapshot_json(today_et, candidates, cfg_used, data_dir)
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
        _status.fires_total += 1
        _status.last_fire_status = "success"
        _status.last_fire_summary = result
        _status.last_fire_finished_at = datetime.now(_ET).isoformat(timespec="seconds")
        logger.info(
            "scheduler_build_done n_base=%d n_dynamic=%d n_watchlist=%d",
            result["n_base"], result["n_dynamic"], result["n_watchlist"],
        )

        if settings.auto_apply_after_build:
            try:
                from . import autotrade as _autotrade
                from .applied_universe import apply_morning_universe

                def _set_metadata(md: dict) -> None:
                    _autotrade.state.applied_universe_metadata = md

                apply_result = apply_morning_universe(
                    set_universe_fn=_autotrade.set_trading_universe,
                    set_metadata_fn=_set_metadata,
                    extra_metadata={
                        "applied_via":         "auto_after_build",
                        "scheduled_fire_time": "08:30 ET",
                    },
                )
                _status.last_fire_summary = {
                    **(result or {}),
                    "auto_applied":          True,
                    "applied_at":            apply_result.get("applied_at"),
                    "applied_snapshot_path": apply_result.get("applied_snapshot_path"),
                    "applied_n_base":        apply_result.get("n_base"),
                    "applied_n_dynamic":     apply_result.get("n_dynamic"),
                }
                logger.info(
                    "scheduler_auto_applied n_base=%d n_dynamic=%d snapshot=%s",
                    apply_result["n_base"], apply_result["n_dynamic"],
                    apply_result["applied_snapshot_path"],
                )
            except Exception as exc:
                logger.warning("scheduler_auto_apply_failed err=%s", exc)
                _status.last_fire_summary = {
                    **(_status.last_fire_summary or {}),
                    "auto_apply_attempted":  True,
                    "auto_apply_error":      str(exc),
                }
    except Exception as exc:
        logger.exception("scheduler_build_failed")
        _status.fires_total += 1
        _status.fires_failed += 1
        _status.last_fire_status = "failed"
        _status.last_fire_error = str(exc)
        _status.last_fire_finished_at = datetime.now(_ET).isoformat(timespec="seconds")


# ── Loop ───────────────────────────────────────────────────────────────────

async def _loop(recovery_queue: list[_ScheduledEvent] | None = None) -> None:
    """Multi-event scheduler loop. Iterates: find next event → sleep
    until it → fire → update state → repeat. When today is done,
    sleeps until tomorrow's 08:30 build.

    v7.16+ (2026-05-15): if ``recovery_queue`` is non-empty, drains it
    sequentially BEFORE entering the normal scheduling loop. This
    handles the "laptop wasn't open at 08:30" case — when uvicorn
    starts late, build (and optionally start) fire immediately if
    their recovery conditions are met.
    """
    if recovery_queue:
        try:
            await _drain_recovery_queue(recovery_queue)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler_recovery_drain_error")
    while True:
        try:
            event = _find_next_event()
            if event is None:
                # Today exhausted. Sleep until tomorrow's build.
                target = _next_business_day_at(_BUILD_TIME_ET)
                _status.next_fire_at = target.isoformat(timespec="seconds")
                logger.info(
                    "scheduler_idle until=%s seconds=%.0f",
                    target.isoformat(),
                    (target - datetime.now(_ET)).total_seconds(),
                )
                await _sleep_chunked_until(target)
                continue

            _status.next_fire_at = event.when.isoformat(timespec="seconds")
            logger.info(
                "scheduler_next_event name=%s when=%s seconds=%.0f",
                event.name, event.when.isoformat(),
                (event.when - datetime.now(_ET)).total_seconds(),
            )
            await _sleep_chunked_until(event.when)
            await event.runner()
        except asyncio.CancelledError:
            logger.info("scheduler_cancelled")
            raise
        except Exception:
            logger.exception("scheduler_loop_error")
            await asyncio.sleep(60)


# ── Watchdog (v7.16+, 2026-05-14) ──────────────────────────────────────────

async def _watchdog_loop() -> None:
    """Defense-in-depth: detect when the autotrade scan loop has silently
    died during market hours and recover by re-firing ``autotrade.start``.

    Conservative — only acts if ALL of:

      1. ``AUTO_START_TRADING=true`` (operator opted in)
      2. Today's ``start`` event already succeeded (state file)
      3. Today's ``stop`` event has NOT yet been recorded
      4. Market is still open per Alpaca calendar (now < close)
      5. ``autotrade.state.running == False`` (the scanner is actually dead)
      6. ``autotrade.state.circuit_breaker_tripped == False`` (breaker
         was supposed to stay tripped today — don't silently un-trip)

    This is a RECOVERY mechanism, NOT a second trading trigger. It uses
    the same ``user_id`` recorded by the original ``start`` event. Every
    restart emits a failure-alert email so the operator investigates
    *why* autotrade stopped (worker reload? unhandled exception?).
    """
    from . import autotrade, email_alerts, scheduler_state, trading_calendar
    from .settings import settings

    while True:
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)

            # (1) opt-in flag
            if not settings.auto_start_trading:
                continue

            today_state = scheduler_state.read().get("events", {})
            start_evt = today_state.get("start") or {}
            stop_evt = today_state.get("stop")

            # (2) start must have succeeded today
            if start_evt.get("status") != "success":
                continue

            # (3) stop must NOT have run yet
            if stop_evt is not None:
                continue

            # (4) market still open
            cal = trading_calendar.get_today()
            if not cal.is_open or cal.market_close_at_et is None:
                continue
            now = datetime.now(_ET)
            if now >= cal.market_close_at_et:
                continue

            # (5) autotrade must be actually dead
            if autotrade.state.running:
                continue

            # (6) circuit breaker must not be tripped — if it is, we
            # respect it and don't re-fire (the breaker is supposed to
            # block trading for the rest of the day).
            if getattr(autotrade.state, "circuit_breaker_tripped", False):
                continue

            # All preconditions met — recover.
            user_id = start_evt.get("user_id")
            user_email = start_evt.get("user_email") or "(unknown)"
            if not user_id:
                logger.warning("scheduler_watchdog_skip reason=no_user_id_in_state")
                continue

            # 2026-05-19 bug-fix: derive the restart number from today's
            # persisted state file, not a per-process global. ``restart_n``
            # is the TRUE count of watchdog restarts today (this one
            # included). The event key ``watchdog_restart_<restart_n>`` is
            # now unique per restart — no more overwriting.
            restart_n = _todays_watchdog_restart_count() + 1
            logger.warning(
                "scheduler_watchdog_restart "
                "count=%d reason=autotrade_dead_during_market_hours "
                "user_id=%s user=%s",
                restart_n, user_id, user_email,
            )
            try:
                autotrade.start(user_id=int(user_id))
                scheduler_state.record_event(
                    f"watchdog_restart_{restart_n}",
                    status="success",
                    user_id=user_id,
                    user_email=user_email,
                    reason="autotrade_dead_during_market_hours",
                )
                email_alerts.send_failure_alert(
                    f"autotrade watchdog restart #{restart_n}",
                    (
                        f"The scheduler watchdog detected autotrade had stopped "
                        f"during market hours and re-started it.\n\n"
                        f"User: {user_email} (id={user_id})\n"
                        f"Restart number today: {restart_n}\n\n"
                        f"This is defense-in-depth. Investigate the uvicorn log "
                        f"for the root cause — common culprits:\n"
                        f"  * a deliberate uvicorn restart during market hours "
                        f"(deploys, etc.) — autotrade isn't auto-resumed by the "
                        f"boot path once today's start event has already fired, "
                        f"so the watchdog re-starts it ~60s later. Expected.\n"
                        f"  * --reload triggered a worker swap (don't use "
                        f"--reload during market hours)\n"
                        f"  * an unhandled exception in the scan loop\n"
                        f"  * manual stop via the dashboard\n"
                    ),
                )
            except Exception as exc:
                logger.exception("scheduler_watchdog_restart_failed")
                scheduler_state.record_event(
                    f"watchdog_restart_{restart_n}",
                    status="failed",
                    error=str(exc),
                )
                email_alerts.send_failure_alert(
                    f"autotrade watchdog FAILED to restart (#{restart_n})",
                    (
                        f"The scheduler watchdog tried to restart autotrade "
                        f"after detecting it had stopped during market hours, "
                        f"but the restart itself failed.\n\nError: {exc}\n\n"
                        f"Manual intervention required."
                    ),
                )
        except asyncio.CancelledError:
            logger.info("scheduler_watchdog_cancelled")
            raise
        except Exception:
            # Catch-all so the watchdog itself doesn't die on transient errors.
            logger.exception("scheduler_watchdog_loop_error")
            await asyncio.sleep(30)


# ── Lifecycle hooks (called from main.py::lifespan) ────────────────────────

def start() -> None:
    """Begin the scheduler loop and the autotrade watchdog. Idempotent.

    On startup, scans today's events and marks any past-time events
    that didn't fire as ``skipped_missed`` in the daily state file.
    The daily summary email surfaces these so the operator knows what
    was missed (e.g., uvicorn was down at 08:30).
    """
    global _task, _watchdog_task
    if _task is not None and not _task.done():
        return
    recovery_queue: list[_ScheduledEvent] = []
    try:
        recovery_queue = _record_missed_events_on_startup()
    except Exception as exc:
        # Non-blocking — startup proceeds even if missed-event logging fails.
        logger.warning("scheduler_record_missed_failed err=%s", exc)
    _task = asyncio.create_task(
        _loop(recovery_queue),
        name="autonomous_trading_scheduler",
    )
    _watchdog_task = asyncio.create_task(_watchdog_loop(), name="autotrade_watchdog")
    logger.info(
        "scheduler_started build=%s start=%s stop=close+%dmin summary=close+%dmin "
        "watchdog_interval=%ds",
        _BUILD_TIME_ET.isoformat(timespec="minutes"),
        _START_TIME_ET.isoformat(timespec="minutes"),
        int(_STOP_OFFSET.total_seconds() / 60),
        int(_SUMMARY_OFFSET.total_seconds() / 60),
        _WATCHDOG_INTERVAL_SECONDS,
    )


async def stop() -> None:
    """Cancel both the scheduler loop and the watchdog. Awaits
    cancellation so any in-flight event completes cleanly (atomicity
    matters — no half-written snapshots)."""
    global _task, _watchdog_task
    for t in (_task, _watchdog_task):
        if t is None:
            continue
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
    _watchdog_task = None
    logger.info("scheduler_stopped")


# ── Manual triggers (for smoke-testing and the dashboard) ──────────────────

async def fire_now() -> None:
    """Run the BUILD event immediately. Preserves v7.15.3 behavior
    where ``/scheduler/fire`` triggered the build only."""
    await _run_build()


async def fire_event(name: str) -> str:
    """Run a single event immediately by name. Used by tests and the
    /scheduler/fire-event endpoint (if added later). Returns the
    recorded status from the state file."""
    from . import scheduler_state
    runners: dict[str, Callable[[], Awaitable[None]]] = {
        "build":   _run_build,
        "start":   _run_auto_start,
        "stop":    _run_auto_stop,
        "summary": _run_daily_summary,
    }
    if name not in runners:
        raise ValueError(
            f"unknown event: {name!r}; expected one of {list(runners)}"
        )
    await runners[name]()
    state = scheduler_state.read().get("events", {})
    return (state.get(name) or {}).get("status", "unknown")
