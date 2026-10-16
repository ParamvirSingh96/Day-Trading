"""
Append-only scan observability JSONL for the experimental scanner
(v7.15, 2026-05-18)
==================================================================

Mirrors :mod:`app.scan_stats` 1:1 except for the output directory
(``~/.tradehack/scan_stats_experimental/<YYYY-MM-DD>.jsonl``) and a
``timeout_skipped`` boolean field that indicates a cycle where the
experimental scan exceeded ``EXPERIMENTAL_SCAN_TIMEOUT_SECONDS`` and
was cancelled to protect ICT cycle latency.

Why a separate directory?  The v7.15 plan explicitly keeps the ICT
audit cohort isolated from the experimental one. Two folders means
audit queries point at one or the other, never both by accident.

Failure-on-write is non-blocking: any exception is caught + logged at
WARNING and the function returns cleanly. Telemetry that breaks the
scan loop is worse than telemetry that's silently missing for a day.

Schema (one row per experimental scan cycle):
    ts                ISO timestamp with ET offset
    scan_n            scan counter (parent ICT scan's state.scan_count)
    symbols           number of symbols the experimental scan saw
    matched_symbols   count with ≥ 1 experimental match
    matched_setups    total experimental setups matched
    matched           list of matched symbol tickers
    dropped_dupe      list of symbols dropped because ICT already matched
                      the same symbol this cycle (ICT-wins-duplicates)
    latest_bar_et     ISO timestamp of the most recent bar
    timeout_skipped   True when EXPERIMENTAL_SCAN_TIMEOUT_SECONDS fired
                      (in which case matched_symbols / matched / etc. are 0)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.scan_stats_experimental")

_ET = ZoneInfo("America/New_York")
_OUT_DIR = Path.home() / ".tradehack" / "scan_stats_experimental"


def append_scan(
    *,
    scan_n: int,
    symbols: int,
    matched_symbols: int,
    matched_setups: int,
    matched: list[str],
    dropped_dupe: list[str] | None = None,
    latest_bar_et: str | None = None,
    timeout_skipped: bool = False,
) -> None:
    """Append one experimental scan row to today's JSONL.

    Never raises. On any failure logs at WARNING and returns so the
    scan loop keeps running. ICT cycle latency is the top priority —
    this function must never be the reason a cycle slows down or
    fails.
    """
    try:
        now_et = datetime.now(_ET)
        row: dict[str, Any] = {
            "ts":               now_et.isoformat(timespec="seconds"),
            "scan_n":           scan_n,
            "symbols":          symbols,
            "matched_symbols":  matched_symbols,
            "matched_setups":   matched_setups,
            "matched":          matched,
            "dropped_dupe":     dropped_dupe or [],
            "latest_bar_et":    latest_bar_et,
            "timeout_skipped":  timeout_skipped,
        }
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUT_DIR / f"{now_et.date().isoformat()}.jsonl"
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        logger.warning("scan_stats_experimental_write_failed err=%s", exc, exc_info=True)


def out_dir() -> Path:
    """Return the JSONL output directory. Used by the EOD summary
    aggregator (``app/scheduler.py``) to surface "experimental scanner
    activity" alongside the ICT scan stats."""
    return _OUT_DIR
