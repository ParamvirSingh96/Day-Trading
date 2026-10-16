"""
Matched-signal outcome JSONL (v7.16+, 2026-05-14)
==================================================
For every scanner-matched signal, record what the autotrade pipeline
decided to do with it. So the daily summary email can say
"XLF blocked: no_result (direction=null)" instead of just "matched=XLF"
with no follow-through.

Hook sites in ``app/autotrade.py``:
  * predicate_only_skip → outcome="blocked", reason="no_result"
  * regime_veto         → outcome="blocked", reason="regime_veto"
  * order_placed        → outcome="submitted", reason="order_placed"

Other rejection sites (low_confidence, daily_cap, duplicate_position,
zero_size) can be added later — start with the three patterns we've
seen in production logs.

Atomic append, non-blocking failure: same pattern as ``scan_stats``.
A write failure logs a WARNING and returns; the autotrade loop is
never blocked by telemetry.

Path: ``~/.tradehack/matched_outcomes/<YYYY-MM-DD>.jsonl``

Row shape::

    {
      "ts": "2026-05-14T09:48:03-04:00",
      "scan_n": 43,
      "symbol": "XLF",
      "matched_strategies": [
        {"name": "ICT Killzone Context", "direction": null}
      ],
      "outcome": "blocked",                  // submitted | blocked | failed
      "reason": "no_result",                 // short machine-readable enum
      "detail": "matched[0].direction=None",  // human-readable
      "extra": {"raw_confidence": 0.75}       // optional, reason-specific
    }
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.matched_outcomes")

_ET = ZoneInfo("America/New_York")
_OUT_DIR = Path.home() / ".tradehack" / "matched_outcomes"

# ── Blocked-outcome dedup (2026-05-29) ──────────────────────────────────────
# A persistent setup that keeps firing + getting vetoed spams the JSONL:
# e.g. 14× "AAPL ICT Power of 3 (Short) blocked regime_veto" in one 10-min
# window. Collapse repeats of the same (symbol, reason, strategies) tuple to
# ONE row per window. This is pure noise reduction — the "how many times did
# it fire" signal lives in scan_stats (the daily summary's "matched fires"
# headline reads from THERE, not from this file), so nothing is lost.
#
# ONLY "blocked" outcomes are deduped. "submitted" + "failed" are rare and
# always worth a row, so they always write. In-memory + per-process (resets
# on restart) — telemetry dedup needs no persistence. Uses monotonic time so
# a wall-clock adjustment can't widen/narrow the window.
_BLOCKED_DEDUP_WINDOW_S = 600  # 10 minutes
_blocked_last_ts: dict[tuple, float] = {}
_blocked_suppressed: dict[tuple, int] = {}


def _blocked_dedup_key(symbol: str, reason: str,
                       matched_strategies: list[dict] | None) -> tuple:
    """Identity for dedup: same symbol + reason + set of strategy names."""
    names = tuple(sorted(
        str(s.get("name", "")) for s in (matched_strategies or [])
        if isinstance(s, dict)
    ))
    return (symbol, reason, names)


def _slim_strategies(matched_strategies: list[dict] | None) -> list[dict]:
    """Reduce ``matched_strategies`` to just ``(name, direction)`` per
    entry. The daily summary only cares about these fields; dumping full
    strategy metadata would balloon the JSONL with confidence vectors,
    indicator snapshots, etc."""
    if not matched_strategies:
        return []
    return [
        {"name": s.get("name"), "direction": s.get("direction")}
        for s in matched_strategies
    ]


def record(
    *,
    symbol: str,
    matched_strategies: list[dict] | None,
    outcome: str,                       # "submitted" | "blocked" | "failed"
    reason: str,                        # short machine-readable enum
    detail: str | None = None,          # human-readable explanation
    scan_n: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one outcome row. Never raises — logs and returns on failure
    so the autotrade loop continues even if disk telemetry breaks."""
    try:
        # ── Blocked-outcome dedup: skip repeats within the window ──
        suppressed_prior = 0
        if outcome == "blocked":
            key = _blocked_dedup_key(symbol, reason, matched_strategies)
            mono = time.monotonic()
            last = _blocked_last_ts.get(key)
            if last is not None and (mono - last) < _BLOCKED_DEDUP_WINDOW_S:
                # Same setup blocked again inside the window — count + skip.
                _blocked_suppressed[key] = _blocked_suppressed.get(key, 0) + 1
                return
            # First in a new window (or ever): write, carrying any count of
            # repeats suppressed since the previous window's row so the file
            # still shows the setup was persistent.
            suppressed_prior = _blocked_suppressed.pop(key, 0)
            _blocked_last_ts[key] = mono

        now_et = datetime.now(_ET)
        row: dict[str, Any] = {
            "ts":                 now_et.isoformat(timespec="seconds"),
            "scan_n":             scan_n,
            "symbol":             symbol,
            "matched_strategies": _slim_strategies(matched_strategies),
            "outcome":            outcome,
            "reason":             reason,
        }
        if detail:
            row["detail"] = detail
        if suppressed_prior:
            # N further identical blocks were collapsed in the prior window.
            row["suppressed_since_prev"] = suppressed_prior
        if extra:
            row["extra"] = extra
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUT_DIR / f"{now_et.date().isoformat()}.jsonl"
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        logger.warning("matched_outcome_write_failed err=%s", exc, exc_info=True)


def out_dir() -> Path:
    """Return the JSONL output directory. Used by the daily summary
    aggregator (``app/scheduler.py``) to find today's outcome rows."""
    return _OUT_DIR
