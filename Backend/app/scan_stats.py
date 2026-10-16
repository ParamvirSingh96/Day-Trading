"""
Append-only scan observability JSONL (v7.16+, 2026-05-13)
==========================================================
Records one JSON line per scan into ``~/.tradehack/scan_stats/<YYYY-MM-DD>.jsonl``
so "did I miss any trades on day X?" is a one-grep answer instead of a
terminal-scrollback hunt.

Failure-on-write is non-blocking: any exception is caught + logged at WARNING
and the function returns cleanly. Telemetry that breaks trading is worse than
telemetry that's silently missing for a day.

Schema (one row per scan):
    ts              ISO timestamp with ET offset
    scan_n          monotonically-increasing scan counter (state.scan_count)
    symbols         number of symbols scanned
    matched_symbols count of symbols with at least one full-predicate match
    matched_setups  total setups matched across all symbols
    forming_symbols count of symbols with forming-only setups (no full match)
    forming_setups  total forming setups across all symbols
    matched         list of matched symbol tickers
    forming         list of forming-only symbol tickers
    latest_bar_et   ISO timestamp of the most recent bar that fed the scan
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.scan_stats")

_ET = ZoneInfo("America/New_York")
_OUT_DIR = Path.home() / ".tradehack" / "scan_stats"


def append_scan(
    *,
    scan_n: int,
    symbols: int,
    matched_symbols: int,
    matched_setups: int,
    forming_symbols: int,
    forming_setups: int,
    matched: list[str],
    forming: list[str],
    latest_bar_et: str | None,
) -> None:
    """Append one scan row to today's JSONL.

    Never raises. On any failure logs at WARNING and returns so the
    scan loop keeps running.
    """
    try:
        now_et = datetime.now(_ET)
        row: dict[str, Any] = {
            "ts":              now_et.isoformat(timespec="seconds"),
            "scan_n":          scan_n,
            "symbols":         symbols,
            "matched_symbols": matched_symbols,
            "matched_setups":  matched_setups,
            "forming_symbols": forming_symbols,
            "forming_setups":  forming_setups,
            "matched":         matched,
            "forming":         forming,
            "latest_bar_et":   latest_bar_et,
        }
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _OUT_DIR / f"{now_et.date().isoformat()}.jsonl"
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        logger.warning("scan_stats_write_failed err=%s", exc, exc_info=True)


def out_dir() -> Path:
    """Return the JSONL output directory. Used by the daily summary
    aggregator (Phase 4) to find today's rows for the email body."""
    return _OUT_DIR
