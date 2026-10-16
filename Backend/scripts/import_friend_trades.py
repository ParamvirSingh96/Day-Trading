"""
Import Friend Trades — v7.14b (2026-05-03)
=============================================================================
Operator-side CLI. Ingests a zip (or folder) produced by
``scripts/package_exports.py`` on a friend's instance into the central
DB's ``imported_trade_records`` table.

Pipeline (matches the plan's manifest-first contract):

  1. Open input (zip → temp dir; folder → use as-is)
  2. Read EXPORT_MANIFEST.json FIRST. Validate:
     - file exists and parses as JSON          → REFUSE if missing
     - schema_version == "1"                    → WARN if differs
     - instance_id matches ^[a-z0-9_-]{1,32}$  → REFUSE if not
     - env_safety.live_trading_enabled == False → REFUSE if true
     - env_safety.alpaca_paper == True          → REFUSE if not
     - env_safety.alpaca_data_feed == "sip"     → WARN (still imports)
     - env_safety.enable_ai_gate == False       → WARN
     - app_version differs from local           → WARN (debug aid)
  3. Iterate dossier files in trade_exports/. For each:
     - Parse JSON
     - Validate schema_version + instance_id matches manifest
     - INSERT into ImportedTradeRecord; catch IntegrityError on the
       ``(source_instance, trade_id_local)`` UNIQUE → counted as
       skipped, no error
  4. Report ``imported / skipped / errors`` counts + feed-mismatch flag

Idempotency: re-running on the same zip produces ``imported=0,
skipped=N`` because the DB UNIQUE rejects every duplicate insert.
The composite key is ``(source_instance, trade_id_local)``.

Telemetry only — this is a CLI invoked by the operator. It never
runs during trading hours, never writes to ``trade_records``, never
touches Alpaca.

Usage:
    venv/bin/python -m scripts.import_friend_trades --from <zip-or-folder>
    venv/bin/python -m scripts.import_friend_trades --from <path> --dry-run
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

from sqlalchemy.exc import IntegrityError
from app.db import SessionLocal, engine, Base
from app.models import ImportedTradeRecord


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("import_friend_trades")


# Friend ID regex — must match the validators in clone_for_friend.sh,
# trade_exporter.py, and settings.py.
_FRIEND_INSTANCE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# Match what the friend's package_exports.py uses
EXPECTED_MANIFEST_NAME = "EXPORT_MANIFEST.json"
EXPECTED_DOSSIER_DIR = "trade_exports"
SUPPORTED_MANIFEST_VERSIONS = ("1",)
SUPPORTED_DOSSIER_VERSIONS = ("1",)


# ── Errors ─────────────────────────────────────────────────────────────────

class ImportError_(Exception):
    """Raised when a hard-fail validation refuses the import.

    Carries an `code` attribute for the CLI to surface as the exit
    reason. Distinct from Python's built-in ImportError to avoid
    confusing module-import errors with this domain error.
    """
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── Manifest validation ────────────────────────────────────────────────────

def read_manifest(root: Path) -> dict[str, Any]:
    """Read + JSON-parse the manifest from ``root/EXPORT_MANIFEST.json``.
    Raises ImportError_ on any issue."""
    path = root / EXPECTED_MANIFEST_NAME
    if not path.exists():
        raise ImportError_(
            "missing_manifest",
            f"Zip is missing {EXPECTED_MANIFEST_NAME}. "
            f"Friend should re-export using `scripts/package_exports.py`.",
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise ImportError_(
            "manifest_parse_failed",
            f"Could not parse {EXPECTED_MANIFEST_NAME}: {exc}",
        )


def validate_manifest(manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate the manifest. Returns (warnings, refusal_reasons).

    Refusal reasons are FATAL — caller raises ImportError_ if any.
    Warnings are LOGGED but the import proceeds.
    """
    refusals: list[str] = []
    warnings: list[str] = []

    # schema_version check (warn-only — old dossiers should still ingest)
    sv = manifest.get("schema_version")
    if sv not in SUPPORTED_MANIFEST_VERSIONS:
        warnings.append(
            f"manifest schema_version={sv!r} not in supported set "
            f"{SUPPORTED_MANIFEST_VERSIONS}; importer will best-effort"
        )

    # instance_id regex (REFUSE on mismatch)
    iid = manifest.get("instance_id") or ""
    if not _FRIEND_INSTANCE_ID_RE.match(iid):
        refusals.append(
            f"manifest.instance_id={iid!r} fails ^[a-z0-9_-]{{1,32}}$"
        )

    # env_safety hard-refusal flags
    es = manifest.get("env_safety") or {}
    if es.get("live_trading_enabled") is True:
        refusals.append(
            "manifest.env_safety.live_trading_enabled=True — "
            "live trading was enabled in source instance, refusing to import"
        )
    if es.get("alpaca_paper") is not True:
        refusals.append(
            f"manifest.env_safety.alpaca_paper={es.get('alpaca_paper')!r} "
            "(must be True — refusing to import non-paper data)"
        )

    # env_safety warn-only flags (refinement #9)
    feed = es.get("alpaca_data_feed")
    if feed != "sip":
        warnings.append(
            f"⚠️  feed mismatch: source instance ran on {feed!r} "
            "(central audit assumes 'sip'). The IEX-vs-SIP parity test "
            "(2026-05-01) showed only 9/126 trades match. Tagging this "
            "batch with feed_mismatch — audit will surface separately."
        )
    if es.get("enable_ai_gate") is True:
        warnings.append(
            "manifest.env_safety.enable_ai_gate=True (source ran with "
            "Layer 2 on; their trade SET differs from yours). Audit "
            "should treat as a separate cohort."
        )

    return warnings, refusals


# ── Dossier validation + ingest ────────────────────────────────────────────

def validate_dossier(dossier: dict, expected_instance_id: str) -> str | None:
    """Return None if the dossier is OK, or a string error code.

    Cheap structural check — full schema validation isn't needed here
    because we built these dossiers ourselves on the friend's box, but
    a malformed file (corrupt zip, manual edit) shouldn't crash the
    importer. Caller logs the error and skips the dossier.
    """
    if not isinstance(dossier, dict):
        return "not_a_dict"
    sv = dossier.get("schema_version")
    if sv not in SUPPORTED_DOSSIER_VERSIONS:
        return f"unsupported_schema_version_{sv}"
    if dossier.get("instance_id") != expected_instance_id:
        # Manifest says this batch is from instance X but a dossier
        # claims instance Y — likely a corrupted zip with mixed batches.
        return f"instance_mismatch_{dossier.get('instance_id')!r}"
    trade = dossier.get("trade")
    if not isinstance(trade, dict):
        return "missing_trade_dict"
    if trade.get("id") is None:
        return "missing_trade_id"
    if trade.get("symbol") is None:
        return "missing_trade_symbol"
    return None


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort parse of an ISO-8601 timestamp into a tz-aware datetime.
    Returns None on parse failure (the column is nullable)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def build_imported_row(
    dossier: dict, manifest: dict, *, imported_at: datetime,
) -> ImportedTradeRecord:
    """Construct (but DON'T commit) an ImportedTradeRecord row from a
    parsed dossier + the batch manifest.

    The dedup key (source_instance, trade_id_local) is set from the
    manifest's instance_id + the dossier's trade.id. ``source_feed``
    + ``source_app_version`` come from the manifest, NOT the dossier
    — those are batch-level provenance.
    """
    trade = dossier["trade"]
    es = manifest.get("env_safety") or {}

    # JSON-stringify context_at_firing (it lives in the dossier as a
    # nested dict, but TradeRecord stores it as a TEXT blob to keep
    # SQLite-compatibility).
    ctx_at_firing = dossier.get("context_at_firing")
    if isinstance(ctx_at_firing, dict):
        ctx_at_firing_str = json.dumps(ctx_at_firing, default=str)
    elif isinstance(ctx_at_firing, str):
        ctx_at_firing_str = ctx_at_firing
    else:
        ctx_at_firing_str = None

    return ImportedTradeRecord(
        source_instance=manifest["instance_id"],
        trade_id_local=int(trade["id"]),
        imported_at=imported_at,
        source_feed=es.get("alpaca_data_feed"),
        source_app_version=manifest.get("app_version"),
        # Mirror of TradeRecord columns (use .get so missing fields
        # land as None — defensive against a dossier from an older
        # schema that didn't include every column)
        symbol=str(trade.get("symbol") or "").upper(),
        side=str(trade.get("side") or ""),
        direction=str(trade.get("direction") or ""),
        qty=int(trade.get("qty") or 0),
        setup=str(trade.get("setup") or "")[:120],
        confidence=float(trade.get("confidence") or 0.0),
        order_type=str(trade.get("order_type") or "market"),
        order_id=trade.get("order_id"),
        status=str(trade.get("status") or "imported"),
        entry_price=trade.get("entry_price"),
        exit_price=trade.get("exit_price"),
        prediction_price=trade.get("prediction_price"),
        confirmation_price=trade.get("confirmation_price"),
        price_move_pct=trade.get("price_move_pct"),
        entry_zone=trade.get("entry_zone"),
        stop_loss=trade.get("stop_loss"),
        target=trade.get("target"),
        risk_reward=trade.get("risk_reward"),
        reasoning=(trade.get("reasoning") or None),
        p_target=trade.get("p_target"),
        p_stop=trade.get("p_stop"),
        p_neither=trade.get("p_neither"),
        outcome_class=trade.get("outcome_class"),
        exit_reason=trade.get("exit_reason"),
        trailing_stop_final=trade.get("trailing_stop_final"),
        highest_price_reached=trade.get("highest_price_reached"),
        hold_duration_seconds=trade.get("hold_duration_seconds"),
        pnl=trade.get("pnl"),
        pnl_pct=trade.get("pnl_pct"),
        r_multiple=trade.get("r_multiple"),
        matched_strategies=trade.get("matched_strategies"),
        universe_source=trade.get("universe_source"),
        context_at_firing=ctx_at_firing_str,
        opened_at=_parse_iso(trade.get("opened_at")),
        closed_at=_parse_iso(trade.get("closed_at")),
        created_at=_parse_iso(trade.get("created_at")),
    )


# ── Pipeline ───────────────────────────────────────────────────────────────

def _open_input(path: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """If ``path`` is a zip, extract to a temp dir and return (extracted_root, tmpdir).
    Otherwise treat it as a folder and return (path, None).

    Caller is responsible for the lifetime of ``tmpdir`` (when not None).
    """
    if path.is_dir():
        return path, None
    if not path.exists():
        raise ImportError_("input_not_found", f"--from {path} does not exist")
    if not zipfile.is_zipfile(path):
        raise ImportError_(
            "input_not_supported",
            f"--from {path} is neither a directory nor a zip file",
        )
    tmpdir = tempfile.TemporaryDirectory(prefix="friend_import_")
    with zipfile.ZipFile(path, "r") as zf:
        zf.extractall(tmpdir.name)
    return Path(tmpdir.name), tmpdir


def import_from(
    path: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the full import pipeline. Returns counts + flags.

    Raises ImportError_ on hard-refusal (missing/invalid manifest,
    live trading enabled, etc.). Soft errors (per-dossier parse
    failures, UNIQUE conflicts) accumulate into the counts.
    """
    extracted_root, tmpdir = _open_input(path)
    try:
        manifest = read_manifest(extracted_root)
        warnings, refusals = validate_manifest(manifest)
        for w in warnings:
            logger.warning(w)
        if refusals:
            for r in refusals:
                logger.error("refusal: %s", r)
            raise ImportError_(
                "manifest_refused",
                "; ".join(refusals),
            )

        instance_id = manifest["instance_id"]
        feed = (manifest.get("env_safety") or {}).get("alpaca_data_feed")
        feed_mismatch = (feed != "sip")

        # Find dossiers
        dossier_dir = extracted_root / EXPECTED_DOSSIER_DIR
        if not dossier_dir.exists():
            # Maybe the friend zipped the trade_exports/ contents
            # directly without the wrapping folder. Fall back to
            # globbing the root.
            dossier_paths = sorted(extracted_root.glob("trade_*.json"))
        else:
            dossier_paths = sorted(dossier_dir.glob("trade_*.json"))

        if not dossier_paths:
            logger.warning("No dossier files found under %s", dossier_dir)
            return {
                "instance_id": instance_id,
                "imported": 0, "skipped": 0, "errors": 0,
                "feed_mismatch": feed_mismatch,
                "manifest": manifest,
            }

        logger.info(
            "manifest_read instance=%s app_version=%s trade_count=%d",
            instance_id, manifest.get("app_version"),
            manifest.get("trade_count", len(dossier_paths)),
        )

        imported = 0
        skipped = 0
        errors = 0
        imported_at = datetime.now(timezone.utc)

        if dry_run:
            logger.info("--dry-run: validating without writing")

        # Use a single session, commit-per-row so a single bad row
        # doesn't poison the whole batch.
        db = SessionLocal()
        try:
            for p in dossier_paths:
                try:
                    with p.open("r", encoding="utf-8") as f:
                        dossier = json.load(f)
                except Exception as exc:
                    logger.warning(
                        "dossier_parse_failed path=%s err=%s", p.name, exc,
                    )
                    errors += 1
                    continue

                problem = validate_dossier(dossier, instance_id)
                if problem:
                    logger.warning(
                        "dossier_invalid path=%s reason=%s", p.name, problem,
                    )
                    errors += 1
                    continue

                if dry_run:
                    imported += 1   # would-have-imported count
                    continue

                row = build_imported_row(
                    dossier, manifest, imported_at=imported_at,
                )
                try:
                    db.add(row)
                    db.commit()
                    imported += 1
                except IntegrityError:
                    db.rollback()
                    skipped += 1   # already imported (UNIQUE conflict)
                except Exception as exc:
                    db.rollback()
                    logger.warning(
                        "dossier_insert_failed path=%s err=%s", p.name, exc,
                    )
                    errors += 1
        finally:
            db.close()

        return {
            "instance_id": instance_id,
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "feed_mismatch": feed_mismatch,
            "manifest": manifest,
        }
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import a friend's trade-export zip (or folder) into the "
            "central imported_trade_records table."
        ),
    )
    parser.add_argument(
        "--from", dest="src", type=Path, required=True,
        help="Path to a friend's trade-export zip or extracted folder.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate manifest + dossiers but do not insert anything into the DB.",
    )
    args = parser.parse_args()

    # Make sure the table exists (idempotent — does nothing if already present)
    Base.metadata.create_all(bind=engine)

    try:
        result = import_from(args.src, dry_run=args.dry_run)
    except ImportError_ as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.exception("import_failed_unexpected")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"Source instance:  {result['instance_id']}")
    print(f"Imported:         {result['imported']}")
    print(f"Skipped (dup):    {result['skipped']}")
    print(f"Errors:           {result['errors']}")
    if result["feed_mismatch"]:
        print()
        print("⚠️  Feed mismatch: source ran on a non-SIP feed. "
              "Audit will segment this batch separately.")
    if args.dry_run:
        print()
        print("(dry-run — no rows were inserted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
