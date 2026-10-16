"""
Package Exports — v7.14a (2026-05-03)
=============================================================================
Friend-side CLI. Bundles ``data/trade_exports/*.json`` + an
``EXPORT_MANIFEST.json`` into a single zip the friend AirDrops/emails
back to the operator. The manifest captures env-safety flags so the
operator's importer can validate provenance BEFORE ingesting any
trades (per the v7.14 plan refinement #6).

Usage from Backend/:
    venv/bin/python -m scripts.package_exports
    # → writes "trade_exports_<instance_id>_<YYYY-MM-DD>.zip" to ../

    venv/bin/python -m scripts.package_exports --out /path/to/file.zip
    # → custom output path

    venv/bin/python -m scripts.package_exports --note "no trades on holiday"
    # → adds operator_note to the manifest

The script refuses to run on the operator's central box (where
``settings.export_trades=False``) — that instance has nothing to
package.

Manifest schema (matches the v7.14 plan):
    {
      "schema_version": "1",
      "instance_id":   "alex",
      "app_version":   "v7.14a-2026-05-03",
      "generated_at":  ISO datetime ET,
      "trade_count":   N,
      "first_trade_at": ISO datetime | null,
      "last_trade_at":  ISO datetime | null,
      "env_safety": {
        "alpaca_paper":         true,
        "live_trading_enabled": false,
        "enable_ai_gate":       false,
        "alpaca_data_feed":     "sip",
        "training_universe_size": 23
      },
      "operator_note": "..."  # optional free-text
    }
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


# Make Backend/ importable when running as ``-m scripts.package_exports``.
_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

from app.settings import settings


# ── Constants ──────────────────────────────────────────────────────────────

# v7.14a app version stamped into every manifest. Bump this string when
# the dossier or manifest schema changes — the importer reads it to
# detect schema drift across instances.
APP_VERSION = "v7.14a-2026-05-03"

# Manifest schema version. This is the OUTER manifest version (the
# zip-level format), independent of the per-dossier schema_version.
MANIFEST_SCHEMA_VERSION = "1"

_ET = ZoneInfo("America/New_York")
_TRADE_EXPORTS_DIR = _REPO_BACKEND / "data" / "trade_exports"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("package_exports")


# ── Manifest construction ──────────────────────────────────────────────────

def _scan_trade_exports() -> tuple[list[Path], str | None, str | None]:
    """Return (sorted dossier paths, earliest trade_ts, latest trade_ts).

    Reads ``data/trade_exports/*.json`` lexicographically (which is
    chronological because filenames embed YYYY-MM-DD_HHMMSS). Skips
    ``.tmp`` orphans (the atomic-write pattern's transient files).
    """
    if not _TRADE_EXPORTS_DIR.exists():
        return [], None, None

    paths = sorted(
        p for p in _TRADE_EXPORTS_DIR.glob("trade_*.json")
        if not p.name.endswith(".tmp")
    )
    if not paths:
        return [], None, None

    # Pull first/last trade timestamps from the dossiers themselves
    # (filename has the EXPORT timestamp; we want the trade's
    # ``opened_at`` for the audit window).
    first_ts: str | None = None
    last_ts: str | None = None
    for p in paths:
        try:
            with p.open("r", encoding="utf-8") as f:
                d = json.load(f)
            opened = (d.get("trade") or {}).get("opened_at")
            if opened:
                if first_ts is None or opened < first_ts:
                    first_ts = opened
                if last_ts is None or opened > last_ts:
                    last_ts = opened
        except Exception as exc:
            # Don't fail packaging because one file is malformed —
            # log + skip that dossier's timestamp contribution. The
            # file still gets included in the zip; the importer will
            # surface the parse error at ingest time.
            logger.warning("dossier_parse_failed path=%s err=%s", p.name, exc)

    return paths, first_ts, last_ts


def build_manifest(
    *,
    operator_note: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Build the EXPORT_MANIFEST dict from current settings + on-disk state.

    Refuses to build on the central operator box (export_trades=False).
    Validates the friend instance ID is set; otherwise the resulting
    manifest would be unimportable.
    """
    if not settings.export_trades:
        raise RuntimeError(
            "EXPORT_TRADES=false on this instance — nothing to package. "
            "This script is intended for FRIEND boxes only."
        )
    if not settings.friend_instance_id:
        raise RuntimeError(
            "FRIEND_INSTANCE_ID is empty in .env. The packager refuses to "
            "build a manifest without an instance ID — the importer would "
            "reject it anyway."
        )

    paths, first_ts, last_ts = _scan_trade_exports()
    generated_at = (now or datetime.now(_ET)).astimezone(_ET)

    # Universe size (from comma-separated TRAINING_UNIVERSE)
    training_universe_size = len(settings.training_universe)

    manifest = {
        "schema_version":  MANIFEST_SCHEMA_VERSION,
        "instance_id":     settings.friend_instance_id,
        "app_version":     APP_VERSION,
        "generated_at":    generated_at.isoformat(timespec="seconds"),
        "trade_count":     len(paths),
        "first_trade_at":  first_ts,
        "last_trade_at":   last_ts,
        "env_safety": {
            # Importer rejects/refuses based on these flags
            "alpaca_paper":          settings.alpaca_paper,
            "live_trading_enabled":  False,  # v7.14a: dev folder always paper-only;
                                              # this flag is separate from ALPACA_PAPER and
                                              # is enforced via Backend/.env (we surface
                                              # the canonical "no" value here).
            "enable_ai_gate":        settings.enable_ai_gate,
            "alpaca_data_feed":      settings.alpaca_data_feed,
            "training_universe_size": training_universe_size,
        },
    }
    # Re-derive live_trading_enabled from .env directly (it isn't on
    # Settings — it's just an env flag the deploy honors). Be defensive:
    # if anything goes wrong, default to "no" (the safe value).
    import os as _os
    live = _os.getenv("LIVE_TRADING_ENABLED", "no").strip().lower()
    manifest["env_safety"]["live_trading_enabled"] = (live in ("yes", "true", "1"))

    if operator_note:
        manifest["operator_note"] = operator_note[:1000]  # cap length

    return manifest


# ── Zip writer ─────────────────────────────────────────────────────────────

def package_exports(
    *,
    out_path: Path,
    operator_note: str | None = None,
    now: datetime | None = None,
) -> tuple[Path, dict]:
    """Build the manifest, zip it + every dossier, return (zip_path, manifest).

    Idempotent on re-run with the same out_path: a stale zip gets
    overwritten. The friend can run this multiple times in a day
    (e.g., before market close, then end-of-day) and the latest zip is
    always the canonical batch.

    Atomic write: builds at ``<out_path>.tmp`` and ``os.replace``s on
    success, so a killed process can't leave a half-written zip the
    operator might import.
    """
    manifest = build_manifest(operator_note=operator_note, now=now)
    paths, _, _ = _scan_trade_exports()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Manifest at the zip ROOT (importer reads this first)
            zf.writestr(
                "EXPORT_MANIFEST.json",
                json.dumps(manifest, indent=2, sort_keys=False, default=str),
            )
            # Each dossier under trade_exports/<filename>.json (preserves
            # original filename so dedup keys work cleanly)
            for p in paths:
                arcname = f"trade_exports/{p.name}"
                zf.write(p, arcname=arcname)
        import os as _os
        _os.replace(tmp_path, out_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    logger.info(
        "package_done out=%s instance=%s trades=%d manifest_size=%d",
        out_path, manifest["instance_id"], manifest["trade_count"],
        len(json.dumps(manifest)),
    )
    return out_path, manifest


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bundle this instance's trade-export dossiers + manifest into a zip for the operator.",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help=(
            "Output zip path. Default: "
            "../../trade_exports_<instance_id>_<YYYY-MM-DD>.zip "
            "(at the parent of the Backend/ folder)."
        ),
    )
    parser.add_argument(
        "--note", type=str, default=None,
        help="Optional free-text note to include in the manifest (≤ 1000 chars).",
    )
    args = parser.parse_args()

    if not settings.export_trades:
        print(
            "ERROR: EXPORT_TRADES=false on this instance. Nothing to package.",
            file=sys.stderr,
        )
        print(
            "       This script is intended for FRIEND boxes (where exports "
            "are written on every trade close). The operator's central box "
            "doesn't generate dossiers.",
            file=sys.stderr,
        )
        return 2

    if not settings.friend_instance_id:
        print(
            "ERROR: FRIEND_INSTANCE_ID is empty in .env. Set it to your "
            "friend ID (must match ^[a-z0-9_-]{1,32}$) before packaging.",
            file=sys.stderr,
        )
        return 2

    if args.out is None:
        today = date.today().isoformat()
        # Default: write to parent of Dev folder so it sits alongside
        # the cloned folder (operator picks it up from there)
        repo_parent = _REPO_BACKEND.parent.parent
        args.out = repo_parent / f"trade_exports_{settings.friend_instance_id}_{today}.zip"

    try:
        out_path, manifest = package_exports(
            out_path=args.out,
            operator_note=args.note,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.exception("package_failed")
        print(f"ERROR: package failed: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"✅ Packaged {manifest['trade_count']} trade dossier(s) into {out_path}")
    print()
    print("Manifest summary:")
    print(f"  instance_id:    {manifest['instance_id']}")
    print(f"  app_version:    {manifest['app_version']}")
    print(f"  generated_at:   {manifest['generated_at']}")
    print(f"  trade window:   {manifest.get('first_trade_at') or '—'} → {manifest.get('last_trade_at') or '—'}")
    print(f"  env_safety:")
    for k, v in manifest["env_safety"].items():
        print(f"    {k}: {v}")
    print()
    print(f"AirDrop or email {out_path.name} back to the operator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
