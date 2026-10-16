"""
Daily scheduler state file (v7.16+, 2026-05-13)
=================================================
Lightweight per-day JSON state at ``~/.tradehack/state/<YYYY-MM-DD>.json``
that records which scheduler events fired and their outcomes.

Single-writer (only ``scheduler.py`` writes); read by ``scheduler.py`` for
resume-after-restart logic and by the daily summary email body
composition.

This file is for **operator truth**, not for trading correctness — the
on-disk artifacts (applied_universe snapshot, scan_stats JSONL, DB rows)
remain authoritative for everything trading-relevant. The state file's
job is to answer "what did the scheduler attempt today, and how did it
go?" even when uvicorn restarted mid-day and lost in-memory status.

Atomicity: every write is `.tmp` + ``os.replace`` so a killed scheduler
can't leave a half-written file. Failure is non-blocking — a write error
logs at WARNING and returns; the scheduler keeps running.

Schema::

    {
      "date": "2026-05-13",
      "events": {
        "build":   {"status": "success", "ts": "...", ...},
        "start":   {"status": "refused", "ts": "...", "reason": "..."},
        "stop":    {"status": "success", "ts": "..."},
        "summary": {"status": "success", "ts": "..."}
      }
    }

Status values used in this codebase (loose enum):
    success, failed, refused, skipped, skipped_missed, skipped_holiday,
    skipped_market_closed, skipped_not_running, skipped_lookup_failed,
    skipped_no_email_config.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.scheduler_state")

_ET = ZoneInfo("America/New_York")
_STATE_DIR = Path.home() / ".tradehack" / "state"


def _today_et() -> date:
    return datetime.now(_ET).date()


def _state_path(d: date | None = None) -> Path:
    d = d or _today_et()
    return _STATE_DIR / f"{d.isoformat()}.json"


def read(d: date | None = None) -> dict[str, Any]:
    """Read the daily state file. Returns an empty skeleton if no file
    exists yet today. Never raises."""
    d = d or _today_et()
    path = _state_path(d)
    if not path.exists():
        return {"date": d.isoformat(), "events": {}}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("scheduler_state_read_failed err=%s", exc, exc_info=True)
        return {"date": d.isoformat(), "events": {}}


def record_event(name: str, status: str, **detail: Any) -> None:
    """Update one event's record in today's state file. ``detail`` is
    merged into the event record alongside ``status`` and ``ts``.

    Atomic: writes to ``<file>.tmp`` then ``os.replace``. Failure is
    non-blocking and logged at WARNING.
    """
    try:
        d = _today_et()
        state = read(d)
        ts = datetime.now(_ET).isoformat(timespec="seconds")
        event_record: dict[str, Any] = {"status": status, "ts": ts}
        for k, v in detail.items():
            if v is not None:
                event_record[k] = v
        state["events"][name] = event_record
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _state_path(d)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning(
            "scheduler_state_write_failed name=%s err=%s",
            name, exc, exc_info=True,
        )


def event_fired(name: str, d: date | None = None) -> bool:
    """True if the event has been recorded with any terminal status.
    Used by the loop's resume logic to skip already-fired events
    after a uvicorn restart."""
    state = read(d)
    return name in state.get("events", {})


def state_dir() -> Path:
    """Return the state file directory. Used by tests and tooling that
    need to enumerate or clean up state files."""
    return _STATE_DIR
