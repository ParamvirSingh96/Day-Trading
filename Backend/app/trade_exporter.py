"""
Trade Exporter (v7.14a, 2026-05-03)
=============================================================================
Per-closed-trade JSON dossier writer for the friend-test-cohort feature.
On a friend's instance (``EXPORT_TRADES=true`` in ``.env``), every trade
that closes with a graded R-multiple lands as a self-contained JSON file
under ``Backend/data/trade_exports/``. Friends periodically zip that
directory + an ``EXPORT_MANIFEST.json`` (built by
``scripts/package_exports.py``) and AirDrop/email the zip back to the
operator. The operator's central instance (``EXPORT_TRADES=false`` —
the default) never writes anything here.

Operating principle: **telemetry only**. The export hook runs AFTER the
trade is fully closed and persisted to the local DB. Any exception in
this module is caught and logged at the call site (autotrade.py); a
malformed dossier MUST NOT delay or fail order monitoring, exits, or
the close-time DB update. (Refinement #3 from the v7.14 review.)

Hard caps (refinement #4):
  - Total dossier JSON ≤ ``MAX_DOSSIER_BYTES`` (32 KB)
  - News articles ≤ ``MAX_NEWS_ARTICLES`` (5)
  - Each article: headline ≤ 200 chars, summary ≤ 500 chars; no URL,
    no full body, no tags
  - ICT zones: structural fields only (price/timestamp/level)
  - Over-cap: drop news articles first, then ICT zone detail; log
    warning; populate ``_truncations`` field. Critical fields (R,
    prices, timestamps, IDs) are NEVER truncated.

Friend ID validation (refinement #5):
  - Validated against ``^[a-z0-9_-]{1,32}$`` BEFORE writing
  - An invalid ID is a hard error — refuses to write the dossier and
    raises ``ValueError`` so the caller's try/except logs it loudly.
=============================================================================
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


# ── Constants ──────────────────────────────────────────────────────────────

# Schema version stamped on every dossier. Importer (v7.14b) reads this
# first; future schema changes bump the version + the importer carries
# a per-version reader so old dossiers stay ingestible.
DOSSIER_SCHEMA_VERSION = "1"

# Hard caps (refinement #4 — bounded context)
MAX_DOSSIER_BYTES       = 32_768   # 32 KB serialized JSON
MAX_NEWS_ARTICLES       = 5
MAX_HEADLINE_CHARS      = 200
MAX_SUMMARY_CHARS       = 500

# Friend ID regex (refinement #5). Mirrors ``settings._FRIEND_INSTANCE_ID_RE``
# — duplicated here so this module can be imported in tooling that doesn't
# load settings. A mismatch is a programming error caught by the smoke test.
_FRIEND_INSTANCE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


# ── Public API ─────────────────────────────────────────────────────────────

def export_closed_trade(
    *,
    trade_record: Any,           # ORM TradeRecord — typed via duck-typing to avoid models import cycle
    instance_id: str,
    out_dir: Path | str,
    now: datetime | None = None,
) -> Path | None:
    """Build + atomically write a per-trade dossier JSON. Returns the path,
    or None when ``instance_id`` is empty (export is a no-op).

    Caller must have already verified ``settings.export_trades is True``.
    This function performs its own friend-ID regex validation and raises
    ``ValueError`` on a bad ID — the caller's try/except logs it.

    Args:
      trade_record: SQLAlchemy ``TradeRecord`` instance for the just-closed
        trade. Must have ``r_multiple is not None`` (caller's responsibility
        — we don't gate on that here, but the audit script will skip rows
        where it's NULL).
      instance_id: Friend ID, e.g. ``"alex"``. Validated against
        ``^[a-z0-9_-]{1,32}$``. Empty string → no-op (returns None).
      out_dir: Where to write. Typically
        ``Backend/data/trade_exports/``. Created if missing.
      now: Override ``datetime.now(_ET)`` for deterministic testing.

    Returns:
      The absolute Path of the written dossier, OR None when
      ``instance_id`` is empty. On any other error, raises — the caller
      MUST wrap this in try/except per refinement #3 (telemetry only).
    """
    if not instance_id:
        return None
    if not _FRIEND_INSTANCE_ID_RE.match(instance_id):
        raise ValueError(
            f"instance_id {instance_id!r} fails ^[a-z0-9_-]{{1,32}}$ — "
            "refusing to write dossier (refinement #5, friend ID sanitization)"
        )

    exported_at = (now or datetime.now(_ET)).astimezone(_ET)

    dossier, truncations = _build_dossier(
        trade_record=trade_record,
        instance_id=instance_id,
        exported_at=exported_at,
    )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    symbol = (getattr(trade_record, "symbol", None) or "UNKNOWN").upper()
    fname = (
        f"trade_{instance_id}_{exported_at.date().isoformat()}_"
        f"{exported_at.strftime('%H%M%S')}_{symbol}.json"
    )
    path = out_dir / fname
    _atomic_write_json(path, dossier)

    if truncations:
        logger.warning(
            "trade_export_truncated path=%s symbol=%s truncations=%s",
            path.name, symbol, truncations,
        )
    logger.info(
        "trade_exported path=%s instance=%s symbol=%s r=%s",
        path.name, instance_id, symbol,
        getattr(trade_record, "r_multiple", None),
    )
    return path


# ── Dossier construction ───────────────────────────────────────────────────

# TradeRecord columns we serialize verbatim. Keeping this list explicit
# (not ``__dict__``) so a future column addition doesn't silently land
# in dossiers without review — and keeps SQLAlchemy internals
# (``_sa_instance_state``, relationships) out of the JSON.
_TRADE_FIELDS = (
    "id", "symbol", "side", "direction", "qty", "setup", "confidence",
    "order_type", "order_id", "status",
    "entry_price", "exit_price", "prediction_price", "confirmation_price",
    "price_move_pct",
    "entry_zone", "stop_loss", "target", "risk_reward",
    "reasoning",
    "p_target", "p_stop", "p_neither", "outcome_class",
    "exit_reason", "trailing_stop_final", "highest_price_reached",
    "hold_duration_seconds",
    "pnl", "pnl_pct", "r_multiple",
    "matched_strategies",
    "universe_source",
    # Order-ID-linked broker fills (v7.16.x) — NULL on monitor-priced rows.
    "entry_fill_price", "exit_fill_price", "exit_order_id",
    "opened_at", "closed_at", "created_at",
)


def _build_dossier(
    *,
    trade_record: Any,
    instance_id: str,
    exported_at: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """Build the dossier dict and apply hard caps.

    Returns ``(dossier, truncations)``. ``truncations`` is a list of
    string codes describing what (if anything) had to be dropped to fit
    under MAX_DOSSIER_BYTES — surfaces in the dossier itself as
    ``_truncations`` and gets logged at WARNING.

    Truncation priority (most-droppable first):
      1. ``news_articles`` — drop oldest articles first, then drop the
         whole ``articles`` list, then drop the entire ``news_verdict``.
      2. ``ict_zones`` — drop the four ICT zone dicts (kill_zone /
         pd_zone / last_mss / last_sweep).
    Critical fields (trade prices, R-multiple, timestamps, IDs) are
    NEVER truncated — if even an empty-context dossier is over-budget,
    we still write it and log a hard warning. In practice the trade
    dict alone is < 4 KB so this never happens.
    """
    truncations: list[str] = []

    # 1. Trade core (TradeRecord columns — never truncated)
    trade = {f: _jsonable(getattr(trade_record, f, None)) for f in _TRADE_FIELDS}

    # 2. context_at_firing — captured at open-time, persisted as JSON string
    context_raw = getattr(trade_record, "context_at_firing", None)
    context: dict[str, Any] = {}
    if context_raw:
        try:
            context = json.loads(context_raw) if isinstance(context_raw, str) else dict(context_raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "trade_export_context_parse_failed trade_id=%s err=%s",
                getattr(trade_record, "id", None), exc,
            )
            context = {}

    # Apply per-field caps (idempotent — already capped at write-time
    # but be defensive in case the open-time bound was bypassed).
    context = _bound_context(context)

    dossier = {
        "schema_version": DOSSIER_SCHEMA_VERSION,
        "instance_id":    instance_id,
        "exported_at":    exported_at.isoformat(timespec="seconds"),
        "trade":          trade,
        "context_at_firing": context,
        "_truncations":   truncations,
    }
    # Compute size + escalate truncation if needed
    dossier = _enforce_size_cap(dossier, truncations)
    dossier["_size_bytes"] = len(_serialize(dossier))
    return dossier, truncations


def _bound_context(context: dict[str, Any]) -> dict[str, Any]:
    """Apply per-field caps to context_at_firing.

    Same caps as the open-time capture (autotrade.py). Defensive — a
    pre-bound context coming through the DB column is already small,
    but if the open-time bound is ever bypassed (e.g., a test inserts
    a raw dossier into the DB), the export still produces a sane file.
    """
    out = dict(context)

    # News articles — cap count + truncate text
    nv = out.get("news_verdict")
    if isinstance(nv, dict):
        articles = nv.get("articles") or []
        if isinstance(articles, list):
            bounded_articles = []
            for art in articles[:MAX_NEWS_ARTICLES]:
                if not isinstance(art, dict):
                    continue
                bounded_articles.append({
                    "headline": _truncate(art.get("headline"), MAX_HEADLINE_CHARS),
                    "summary":  _truncate(art.get("summary"),  MAX_SUMMARY_CHARS),
                    "source":   art.get("source") or "",
                    "ts":       art.get("ts") or art.get("created_at") or "",
                })
            nv = {**nv, "articles": bounded_articles}
            out["news_verdict"] = nv
    return out


def _enforce_size_cap(
    dossier: dict[str, Any], truncations: list[str],
) -> dict[str, Any]:
    """If the serialized dossier exceeds ``MAX_DOSSIER_BYTES``, drop
    droppable fields in priority order until it fits — or log a hard
    warning if even an empty-context dossier is over budget.

    Mutates ``truncations`` to record what was dropped.
    """
    def size_of(d: dict[str, Any]) -> int:
        return len(_serialize(d))

    if size_of(dossier) <= MAX_DOSSIER_BYTES:
        return dossier

    # Tier 1: drop news articles
    ctx = dossier.get("context_at_firing") or {}
    nv = ctx.get("news_verdict")
    if isinstance(nv, dict) and (nv.get("articles") or []):
        nv = {**nv, "articles": []}
        dossier["context_at_firing"] = {**ctx, "news_verdict": nv}
        truncations.append("news_articles_dropped")
        if size_of(dossier) <= MAX_DOSSIER_BYTES:
            return dossier

    # Tier 2: drop entire news_verdict
    ctx = dossier.get("context_at_firing") or {}
    if "news_verdict" in ctx:
        ctx = {k: v for k, v in ctx.items() if k != "news_verdict"}
        dossier["context_at_firing"] = ctx
        truncations.append("news_verdict_dropped")
        if size_of(dossier) <= MAX_DOSSIER_BYTES:
            return dossier

    # Tier 3: drop ICT zone detail
    ctx = dossier.get("context_at_firing") or {}
    snap = ctx.get("indicators_snapshot")
    if isinstance(snap, dict):
        snap = {k: v for k, v in snap.items() if not k.startswith("ict_")}
        ctx = {**ctx, "indicators_snapshot": snap}
        dossier["context_at_firing"] = ctx
        truncations.append("ict_zones_dropped")
        if size_of(dossier) <= MAX_DOSSIER_BYTES:
            return dossier

    # If still over budget, write it anyway with a loud warning.
    # Critical trade fields are never truncated — better to have an
    # oversized dossier than to lose the trade's R-multiple / prices.
    logger.warning(
        "trade_export_oversized size=%d limit=%d truncations=%s",
        size_of(dossier), MAX_DOSSIER_BYTES, truncations,
    )
    truncations.append("over_budget_after_full_truncation")
    return dossier


# ── Atomic write ───────────────────────────────────────────────────────────

def _serialize(obj: Any) -> str:
    """JSON-serialize with stable key order + 2-space indent (readable diffs)."""
    return json.dumps(obj, indent=2, sort_keys=False, default=str)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Mirror ``applied_universe.write_applied_snapshot``'s atomic-write
    pattern: write to a sibling ``.tmp``, fsync, ``os.replace`` to final.

    Two failure modes this protects against:
      1. Process killed between ``open()`` and the JSON dump finishing
         → final filename never exists → friend's package_exports.py
         skips the half-written file.
      2. Power loss between dump-complete and file-close → fsync
         ensures bytes are on disk before rename → restart sees either
         the new file complete or no new file at all (never a
         truncated tail).

    Cleanup orphan ``.tmp`` on any error so a chmod-locked dir or
    full-disk doesn't leave debris.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(_serialize(payload))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync isn't supported on all storage layers (some
                # FUSE / network mounts). The rename still gives ordering
                # guarantees from cached metadata; don't fail over a
                # missing durability hint.
                logger.debug("trade_export_fsync_unsupported path=%s", tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# ── Helpers ────────────────────────────────────────────────────────────────

def _jsonable(value: Any) -> Any:
    """Coerce DB-row values into JSON-safe primitives.

    ``datetime`` → ISO string; ``Decimal`` / numeric types → float;
    SQLAlchemy ORM relationships → skipped (caller's job to pass scalar
    fields only). Most TradeRecord columns are already primitives, so
    this is a thin safety net.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return asdict(value)
    # Best-effort fallback — JSON encoder will use ``default=str``
    return value


def _truncate(value: Any, max_chars: int) -> str:
    """Truncate a string to ``max_chars`` (with a trailing ``…`` marker
    when actually truncated). None → empty string."""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= max_chars:
        return s
    # Reserve one char for the ellipsis marker so the result is still
    # ≤ max_chars total
    return s[: max_chars - 1] + "…"


# ── Open-time context capture ──────────────────────────────────────────────
#
# Used by ``autotrade.py`` at the trade-open call site. Builds the dict
# that gets stored in ``TradeRecord.context_at_firing`` (as a JSON
# string). Bounded the same way as ``_bound_context`` above so the
# stored blob is always ≤ MAX_DOSSIER_BYTES even if the input dicts
# are large.

def build_context_at_firing(
    *,
    indicators_snapshot: dict[str, Any] | None,
    regime_snapshot: dict[str, Any] | None,
    news_verdict: dict[str, Any] | None,
    applied_universe: dict[str, Any] | None,
) -> str | None:
    """Compose the context_at_firing JSON string for storage.

    All inputs optional — missing pieces just produce a sparser dossier
    later. Returns the serialized JSON string ready for direct insert
    into ``TradeRecord.context_at_firing``, or None when every input is
    empty (no point storing an all-null blob).

    Bounded inline (refinement #4):
      - Articles capped to MAX_NEWS_ARTICLES, fields truncated
      - Final JSON capped to MAX_DOSSIER_BYTES; over-budget input drops
        articles first, then news_verdict, then ICT zones
    """
    if not any((indicators_snapshot, regime_snapshot, news_verdict, applied_universe)):
        return None

    ctx: dict[str, Any] = {}
    if indicators_snapshot:
        ctx["indicators_snapshot"] = dict(indicators_snapshot)
    if regime_snapshot:
        ctx["regime_snapshot"] = dict(regime_snapshot)
    if news_verdict:
        ctx["news_verdict"] = dict(news_verdict)
    if applied_universe:
        ctx["applied_universe"] = dict(applied_universe)

    ctx = _bound_context(ctx)

    # Use the same size enforcement as the export path, against the
    # ctx alone (no _truncations marker — that's a dossier-level field).
    placeholder = {"context_at_firing": ctx, "_truncations": []}
    placeholder = _enforce_size_cap(placeholder, placeholder["_truncations"])
    return _serialize(placeholder.get("context_at_firing") or {})
