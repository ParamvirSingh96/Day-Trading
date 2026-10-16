"""
Applied-Universe State (v7.13 Phase 1.6, 2026-05-01)
=============================================================================
Persistence layer for the "operator clicked Apply on this morning's universe"
event. Two roles:

1. **Audit trail** — every Apply action writes a timestamped JSON file at
   ``Backend/data/applied_morning_universe_<YYYY-MM-DD>_<HHMMSS>.json``
   capturing the exact ``trading_universe``, ``source_map``, and a
   pointer to the screener snapshot that produced it. Replayable
   provenance: any past day's audit can answer "what universe was
   running, and why?"

2. **Restart restoration** — on backend startup, ``restore_today()``
   looks for the latest applied snapshot whose date == today (ET).
   If found, it restores ``state.trading_universe`` and
   ``state.morning_universe_source_map`` so the autotrade loop picks
   up where it left off. If not found, the autotrade loop falls back
   to the audited base universe — that's the *correct default*: never
   re-apply yesterday's morning picks, never auto-apply today's
   without an explicit operator click.

Design choices captured here so future-you doesn't have to re-derive
them:

* **File-per-apply, not single-file.** Every Apply gets its own
  archival JSON. This keeps history queryable without an extra DB
  table, and the "latest of today" lookup is just a glob + sort.
* **No DB table.** Trade-level provenance lives on
  ``TradeRecord.universe_source`` (added in 1.6); the apply-event
  provenance is operator-action metadata, file-on-disk is sufficient.
* **ET date for the "today" comparison.** A backend that restarts
  at 23:55 UTC on the night of an apply must NOT think the apply is
  for "tomorrow" because UTC has rolled. We always reason in
  America/New_York for trading dates.
* **Write-on-apply, not write-on-restart.** Restart never creates a
  snapshot; it only restores from existing ones. This means a
  backend that restarts WITHOUT a same-day apply correctly defaults
  to base — there's no way for restart logic to "invent" provenance.
=============================================================================
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


# v7.15.4: named under tradehack.* hierarchy so logging_config.py
# elevates these to INFO. Matches scheduler.py pattern.
logger = logging.getLogger("tradehack.applied_universe")

_ET = ZoneInfo("America/New_York")

# Snapshot files live alongside the morning-universe scanner outputs.
# Both live under Backend/data/ which is already gitignored / isolated
# per the Phase-0 dev-folder layout (see DEV_README.md). Resolved at
# import time; safe because data/ is created on first write.
_DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data"
_FILENAME_PREFIX = "applied_morning_universe_"
_FILENAME_GLOB = f"{_FILENAME_PREFIX}*.json"


@dataclass
class AppliedSnapshot:
    """One Apply-event record. Mirrors the JSON-on-disk shape exactly."""
    # ET date when Apply was clicked. Used for the "is this from
    # today?" check on restart. ISO-format string so JSON round-trip
    # is lossless.
    date: str
    # Full ISO timestamp (with ET offset) of the Apply event.
    applied_at: str
    # The full list that was sent to autotrade — base symbols + top-N
    # morning_dynamic, in audit order.
    trading_universe: list[str]
    # {symbol: source} where source is "base" or "morning_dynamic".
    # Drives ``get_universe_source()`` → ``TradeLog.universe_source``.
    source_map: dict[str, str]
    # Path to the screener-output JSON that produced this apply
    # (e.g. data/morning_universe_2026-05-01.json). Lets the audit
    # trail walk back to the original ranking + reason tags.
    source_snapshot_path: str | None = None
    # Caller-supplied metadata for the audit log (ip, user-agent,
    # whether Apply was clicked or auto-fired by a scheduler, etc.).
    # Open-ended; stays out of restart restoration logic.
    metadata: dict[str, Any] = field(default_factory=dict)


def _today_et() -> date:
    """ET-local date for the 'is this snapshot from today?' check."""
    return datetime.now(_ET).date()


def _filename_for(applied_at: datetime) -> str:
    """Filename convention. Date + HHMMSS lets lexicographic sort = chronological."""
    et_dt = applied_at.astimezone(_ET) if applied_at.tzinfo else applied_at.replace(tzinfo=_ET)
    return f"{_FILENAME_PREFIX}{et_dt.date().isoformat()}_{et_dt.strftime('%H%M%S')}.json"


def write_applied_snapshot(
    trading_universe: list[str],
    source_map: dict[str, str],
    *,
    source_snapshot_path: Path | str | None = None,
    metadata: dict[str, Any] | None = None,
    out_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Persist the just-applied universe to disk. Returns the file path.

    Args:
      trading_universe: Symbols sent to ``set_trading_universe`` —
        base + top-N morning_dynamic.
      source_map: ``{symbol: "base"|"morning_dynamic"}``. Persisted
        verbatim; the restart path passes this back to
        ``set_trading_universe`` to recover ``TradeLog.universe_source``
        attribution.
      source_snapshot_path: Optional pointer to the screener output
        JSON the operator was looking at when they clicked Apply.
        Stored as a string for JSON round-trip; the restart path
        doesn't read this — it's purely audit context.
      metadata: Optional extra fields (e.g. ``{"applied_by": "user@host"}``).
        Open-ended; preserved verbatim.
      out_dir: Override the default ``Backend/data/`` location. Used
        by tests; production callers should leave None.
      now: Override ``datetime.now()`` for deterministic testing.
    """
    out_dir = out_dir or _DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    applied_at = (now or datetime.now(_ET)).astimezone(_ET)
    snapshot = AppliedSnapshot(
        date=applied_at.date().isoformat(),
        applied_at=applied_at.isoformat(timespec="seconds"),
        trading_universe=list(trading_universe),
        source_map=dict(source_map),
        source_snapshot_path=str(source_snapshot_path) if source_snapshot_path else None,
        metadata=dict(metadata or {}),
    )
    path = out_dir / _filename_for(applied_at)

    # Atomic write (review feedback Phase 2 #1, 2026-05-01): write to
    # a sibling ``.tmp`` first, fsync the contents to disk, then
    # ``os.replace`` to the final filename. Two failure modes this
    # protects against:
    #   1. Process killed between ``open()`` and the JSON dump
    #      finishing → the final filename never exists, so
    #      ``latest_applied_for_today`` skips the half-written file
    #      instead of treating it as the latest apply.
    #   2. Power loss between dump-complete and file-close → the
    #      fsync ensures bytes are on disk before the rename, so
    #      restart sees either the new file complete or no new file
    #      at all (never a truncated tail).
    # On POSIX, ``os.replace`` is an atomic rename within the same
    # filesystem (verified — ``out_dir`` and tmp file are both under
    # Backend/data/). On Windows it's also atomic per the docs.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(asdict(snapshot), f, indent=2, sort_keys=False, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync isn't supported on all storage layers
                # (some FUSE mounts, network drives); the rename
                # still gives ordering guarantees from the OS's
                # cached metadata, just without the durability one.
                # Don't fail the apply over a missing fsync.
                logger.debug("applied_universe_fsync_unsupported path=%s", tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        # Cleanup on any error — leave no stray .tmp behind.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    logger.info(
        "applied_universe_written path=%s symbols=%d source_map_size=%d",
        path, len(snapshot.trading_universe), len(snapshot.source_map),
    )
    return path


def latest_applied_for_today(
    out_dir: Path | None = None,
    today: date | None = None,
) -> Path | None:
    """Find the latest applied-snapshot file dated for today (ET).

    Returns None if no snapshot exists for today. The "latest" is
    determined by lexicographic order on the filename, which works
    because the time component is HHMMSS — chronological ordering
    is preserved even if mtime gets updated by a backup tool.
    """
    out_dir = out_dir or _DEFAULT_OUT_DIR
    today = today or _today_et()
    if not out_dir.exists():
        return None
    prefix = f"{_FILENAME_PREFIX}{today.isoformat()}_"
    matches = sorted(out_dir.glob(f"{prefix}*.json"))
    return matches[-1] if matches else None


def load_snapshot(path: Path) -> AppliedSnapshot:
    """Read a snapshot file off disk. Raises on parse error."""
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return AppliedSnapshot(
        date=raw["date"],
        applied_at=raw["applied_at"],
        trading_universe=list(raw.get("trading_universe", [])),
        source_map=dict(raw.get("source_map", {})),
        source_snapshot_path=raw.get("source_snapshot_path"),
        metadata=dict(raw.get("metadata", {})),
    )


def restore_today(
    *,
    set_universe_fn,           # set_trading_universe(symbols, source_map)
    set_metadata_fn=None,      # callable(metadata: dict) — see apply_morning_universe
    out_dir: Path | None = None,
) -> AppliedSnapshot | None:
    """Restore today's applied universe to autotrade state, if any.

    Called from the FastAPI lifespan startup hook. ``set_universe_fn``
    is dependency-injected (rather than imported here) to avoid a
    circular import — autotrade.py imports things this module wants,
    and vice versa would be cyclic.

    ``set_metadata_fn`` (v7.13 Phase 2) optionally writes
    ``state.applied_universe_metadata`` so the status endpoint can
    surface "restored from disk on startup" loudly to the operator
    (review feedback #4 — never let restored state be invisible).
    The metadata field includes ``restored_from_disk=True`` to
    distinguish a same-session apply from a restart restoration.

    Returns the restored snapshot dataclass on success, or None if
    no same-day snapshot exists (the autotrade loop then defaults to
    the audited base universe — the correct conservative default).

    Failure modes (all return None, all logged):
      * No snapshot directory yet → no apply has ever happened.
      * No today-dated snapshot → backend was offline overnight; do
        NOT auto-apply yesterday's picks.
      * Snapshot file unreadable / malformed → corruption; surface a
        warning and let autotrade fall back to base. Don't crash
        startup over an audit-trail file.
    """
    path = latest_applied_for_today(out_dir=out_dir)
    if path is None:
        logger.info("applied_universe_restore_skip reason=no_today_snapshot")
        return None
    try:
        snapshot = load_snapshot(path)
    except Exception as exc:
        logger.warning(
            "applied_universe_restore_failed path=%s err=%s",
            path, exc,
        )
        return None
    # Belt-and-suspenders: re-check the date even though
    # latest_applied_for_today already filtered by filename. Catches
    # the edge case where someone manually renamed a file.
    if snapshot.date != _today_et().isoformat():
        logger.warning(
            "applied_universe_restore_skip reason=stale_date "
            "snapshot_date=%s today=%s path=%s",
            snapshot.date, _today_et().isoformat(), path,
        )
        return None
    set_universe_fn(snapshot.trading_universe, source_map=snapshot.source_map)
    if set_metadata_fn is not None:
        n_base = sum(1 for v in snapshot.source_map.values() if v == "base")
        n_dynamic = sum(1 for v in snapshot.source_map.values() if v == "morning_dynamic")
        # Schema mirrors apply_morning_universe's metadata exactly so
        # the API consumers don't have to special-case
        # ``restored_from_disk=True``. Only that single bool tells
        # the operator whether they're seeing a same-session apply
        # or a restart restoration.
        set_metadata_fn({
            "applied_at":            snapshot.applied_at,
            "applied_snapshot_path": str(path),
            "source_snapshot_path":  snapshot.source_snapshot_path,
            "restored_from_disk":    True,
            "trading_universe_size": len(snapshot.trading_universe),
            "n_base":                n_base,
            "n_dynamic":             n_dynamic,
        })
    logger.info(
        "applied_universe_restored path=%s symbols=%d applied_at=%s",
        path, len(snapshot.trading_universe), snapshot.applied_at,
    )
    return snapshot


# ── Apply orchestrator ──────────────────────────────────────────────────────

# How old the screener snapshot can be (by ``generated_at``) before we
# refuse to apply it. 12 hours covers the realistic operator window
# (scanner runs ~08:30 ET, operator clicks Apply by ~14:00 ET worst
# case) without letting yesterday's 23:00 snapshot sneak through if
# the operator scrolls back to it. Per review feedback #3.
_DEFAULT_MAX_SCREENER_AGE_HOURS = 12


class ApplyError(Exception):
    """Raised when the apply pipeline refuses to proceed.

    Carries an HTTP-style error code so the API layer can map directly:

      * ``"not_found"``   → screener JSON missing → 404
      * ``"stale_date"``  → screener date != today → 400
      * ``"stale_age"``   → screener generated_at older than cutoff → 400
      * ``"empty"``       → screener has no trading_universe → 400
      * ``"snapshot_write_failed"`` → could not write the apply file
                                       → 500 (state untouched, see
                                       below)
    """
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _load_screener_snapshot(snapshot_path: Path) -> dict:
    """Read + JSON-parse the screener output. Raises ApplyError on miss/parse."""
    if not snapshot_path.exists():
        raise ApplyError(
            "not_found",
            f"Screener snapshot not found at {snapshot_path}. "
            "Run `venv/bin/python -m scripts.build_morning_universe` first.",
        )
    try:
        with snapshot_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise ApplyError(
            "not_found",
            f"Could not read screener snapshot at {snapshot_path}: {exc}",
        )


def _validate_screener_freshness(
    payload: dict,
    *,
    today: date,
    max_age_hours: float,
    now: datetime | None = None,
) -> None:
    """Refuse the apply if the screener output is for a different day or too old.

    Two independent checks:
      * ``payload["date"]`` MUST equal today_et — protects against
        scrolling back to yesterday's snapshot or running the apply
        on a stale file the operator forgot to refresh.
      * ``payload["generated_at"]`` MUST be within ``max_age_hours``
        of now — guards against running today's screener at 04:00
        ET and then applying it at 22:00 ET (gap data is hours
        stale by then).

    Raises ApplyError with code="stale_date" or "stale_age".
    """
    snapshot_date = (payload.get("date") or "").strip()
    if snapshot_date != today.isoformat():
        raise ApplyError(
            "stale_date",
            f"Screener snapshot date {snapshot_date!r} != today {today.isoformat()!r}. "
            "Re-run the morning scanner before applying.",
        )

    generated_at_raw = payload.get("generated_at")
    if not generated_at_raw:
        raise ApplyError(
            "stale_age",
            "Screener snapshot is missing generated_at — refusing to apply.",
        )
    try:
        generated_at = datetime.fromisoformat(generated_at_raw)
    except ValueError as exc:
        raise ApplyError(
            "stale_age",
            f"Screener generated_at {generated_at_raw!r} is unparseable: {exc}",
        )
    # Make both sides timezone-aware so the subtraction is unambiguous.
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=_ET)
    now = now or datetime.now(_ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    age = now - generated_at
    if age.total_seconds() > max_age_hours * 3600:
        raise ApplyError(
            "stale_age",
            f"Screener generated_at {generated_at.isoformat()} is "
            f"{age.total_seconds() / 3600:.1f}h old "
            f"(max age {max_age_hours:.1f}h). Re-run the morning scanner.",
        )


def _build_apply_payload(payload: dict) -> tuple[list[str], dict[str, str]]:
    """Extract (trading_universe, source_map) from the screener JSON.

    Mirrors the schema written by ``screener_morning.candidates_to_dict``:
      * ``base[]`` → all tagged ``"base"``
      * ``morning_watchlist[]`` where ``in_trading_universe=True`` →
        tagged ``"morning_dynamic"``
      * Watchlist-only entries are excluded — by design, they're
        informational and never enter the trading universe.

    Returns trading_universe in audit order (base first, then dynamic
    by rank) and source_map mapping every symbol to its tag.
    """
    base_entries = payload.get("base") or []
    watchlist = payload.get("morning_watchlist") or []
    base_symbols = [c["symbol"] for c in base_entries if c.get("symbol")]
    dynamic = [
        c for c in watchlist
        if c.get("in_trading_universe") and c.get("symbol")
    ]
    dynamic.sort(key=lambda c: c.get("rank") or 999_999)
    dynamic_symbols = [c["symbol"] for c in dynamic]

    trading_universe = base_symbols + dynamic_symbols
    if not trading_universe:
        raise ApplyError(
            "empty",
            "Screener snapshot has no symbols to apply (base AND "
            "trading-universe slice are both empty).",
        )

    source_map: dict[str, str] = {s.upper(): "base" for s in base_symbols}
    for s in dynamic_symbols:
        source_map[s.upper()] = "morning_dynamic"
    return trading_universe, source_map


def apply_morning_universe(
    *,
    set_universe_fn,                         # set_trading_universe(symbols, source_map)
    set_metadata_fn=None,                    # callable({...applied metadata...})
    screener_snapshot_path: Path | None = None,
    out_dir: Path | None = None,
    max_age_hours: float = _DEFAULT_MAX_SCREENER_AGE_HOURS,
    now: datetime | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """End-to-end apply of today's morning universe. Returns a result dict.

    Pipeline (review feedback #1 — snapshot-first, then runtime state):

      1. Load the screener JSON (default:
         ``data/morning_universe_<today>.json``).
      2. Validate ``date == today_et`` AND ``generated_at`` is within
         ``max_age_hours``. Reject (raise ApplyError) on either.
      3. Build (trading_universe, source_map) from the screener
         payload.
      4. Write the audit snapshot to disk
         (``applied_morning_universe_<date>_<time>.json``). If THIS
         step fails, ``set_universe_fn`` is NEVER called — runtime
         state stays on whatever was active before. This is the
         atomic-provenance guarantee.
      5. Call ``set_universe_fn`` to mutate runtime state.
      6. Call ``set_metadata_fn`` (if supplied) to publish apply
         metadata for the status endpoint.
      7. Return the response payload (review feedback #2 — UI gets
         the exact ``applied_snapshot_path`` it can display).

    Returns:
      ``{"applied_snapshot_path": str, "applied_at": ISO str,
         "trading_universe": [...], "source_map_size": int,
         "n_base": int, "n_dynamic": int,
         "source_snapshot_path": str | None}``

    Raises ApplyError on validation failures. The router maps the
    ``code`` field to HTTP status (404 / 400 / 500).
    """
    today = (now.astimezone(_ET).date() if now and now.tzinfo else _today_et())
    snapshot_path = (
        Path(screener_snapshot_path) if screener_snapshot_path
        else _DEFAULT_OUT_DIR / f"morning_universe_{today.isoformat()}.json"
    )

    payload = _load_screener_snapshot(snapshot_path)
    _validate_screener_freshness(
        payload, today=today, max_age_hours=max_age_hours, now=now,
    )
    trading_universe, source_map = _build_apply_payload(payload)

    # Snapshot-FIRST. If write_applied_snapshot raises, runtime
    # state is untouched (set_universe_fn never called) — atomic
    # provenance from the operator's perspective.
    try:
        applied_path = write_applied_snapshot(
            trading_universe,
            source_map,
            source_snapshot_path=snapshot_path,
            metadata=extra_metadata,
            out_dir=out_dir,
            now=now,
        )
    except Exception as exc:
        logger.error(
            "apply_morning_universe_snapshot_write_failed err=%s state_untouched=true",
            exc,
        )
        raise ApplyError(
            "snapshot_write_failed",
            f"Could not write apply snapshot: {exc}. Runtime state was NOT changed.",
        )

    # Snapshot exists on disk — now mutate runtime state.
    set_universe_fn(trading_universe, source_map=source_map)

    n_base = sum(1 for v in source_map.values() if v == "base")
    n_dynamic = sum(1 for v in source_map.values() if v == "morning_dynamic")
    applied_at_iso = (now or datetime.now(_ET)).astimezone(_ET).isoformat(timespec="seconds")
    metadata = {
        "applied_at":            applied_at_iso,
        "applied_snapshot_path": str(applied_path),
        "source_snapshot_path":  str(snapshot_path),
        "restored_from_disk":    False,
        "trading_universe_size": len(trading_universe),
        "n_base":                n_base,
        "n_dynamic":             n_dynamic,
    }
    if set_metadata_fn is not None:
        set_metadata_fn(metadata)

    logger.info(
        "apply_morning_universe_done path=%s n_base=%d n_dynamic=%d",
        applied_path, n_base, n_dynamic,
    )
    return {
        "applied_snapshot_path": str(applied_path),
        "applied_at":            applied_at_iso,
        "trading_universe":      trading_universe,
        "source_map_size":       len(source_map),
        "n_base":                n_base,
        "n_dynamic":             n_dynamic,
        "source_snapshot_path":  str(snapshot_path),
    }
