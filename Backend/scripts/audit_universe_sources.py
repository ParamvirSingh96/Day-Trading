"""
Universe-Source Audit (v7.13 Phase 2, 2026-05-01)
=============================================================================
Answers the single question this whole 1.6 + 2.0 plumbing exists for:

  Did the morning-universe scanner add edge?

Groups closed trades by ``universe_source`` and reports per-source
trade count, win rate, raw AvgR, and friction-adjusted AvgR (at the
audited 0.10R/trade friction baseline). The dynamic-vs-base delta is
what answers the question; everything else is supporting evidence.

Usage
-----
From ``Backend/`` on the dev folder::

    venv/bin/python -m scripts.audit_universe_sources                 # last 30 days
    venv/bin/python -m scripts.audit_universe_sources --days 60       # wider window
    venv/bin/python -m scripts.audit_universe_sources --csv out.csv   # also write CSV

Pass criteria (per the v7.13 plan, after a 30-day paper run)
------------------------------------------------------------
* ``morning_dynamic`` post-friction AvgR > 0
* ``morning_dynamic`` post-friction AvgR ≥ 80% of ``base`` post-friction AvgR
  (so dynamic symbols aren't dragging down the average)
* No single (universe_source × symbol) cell with friction AvgR < −0.30
  on n ≥ 5 trades — surface those as blacklist candidates per the
  v7.10 surgical pattern.

If those gates clear, dynamic symbols can be promoted incrementally
(e.g. raise n_dynamic from 10 to 15, or add the most-edge-positive
dynamic names to the audited base after their own per-symbol cell
clears the v7.7 promotion gates).
=============================================================================
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

from sqlalchemy import select, func, case
from app.db import SessionLocal
from app.models import TradeRecord


logger = logging.getLogger("audit_universe")
logging.basicConfig(level=logging.INFO, format="%(message)s")


# Friction baseline. Mirrors the audit constant used everywhere else
# in the project (app/strategy_backtest.py + the v7.10/v7.11/v7.12.1
# audit reports). 0.10R/trade is the empirically-anchored cost the
# v7.10.5 execution audit derived from real fills.
FRICTION_R_PER_TRADE = 0.10


def _coalesce_source(s: str | None) -> str:
    """COALESCE(universe_source, 'unknown') in Python.

    Mirrors the SQL pattern recommended in the migration comment
    (app/main.py) so pre-Phase-1.6 rows surface as ``unknown``
    rather than as a silent NULL group that some BI tools drop.
    """
    return s if s else "unknown"


from dataclasses import dataclass
from app.models import ImportedTradeRecord


@dataclass
class AuditRow:
    """Normalized view of a closed trade — covers both native
    ``TradeRecord`` rows AND imported ``ImportedTradeRecord`` rows so
    the rest of the script can treat them uniformly.

    ``source_instance`` is ``"host"`` for native trades (the operator's
    own box) and the friend ID (e.g., ``"alex"``) for imported trades.
    ``source_feed`` carries the data feed each row was produced on
    (``"sip"`` for native trades; from the manifest for imports).
    """
    symbol: str | None
    universe_source: str | None
    r_multiple: float | None
    source_instance: str       # "host" | friend ID
    source_feed: str           # "sip" by default for native trades


def fetch_closed_trades(
    days: int,
    *,
    source_instance: str | None = None,
) -> tuple[list[AuditRow], bool]:
    """Pull closed-trade rows from BOTH ``trade_records`` (native) AND
    ``imported_trade_records`` (friend-cohort imports), within the
    last ``days`` calendar days. Returns ``(rows, feed_mismatch_seen)``.

    ``source_instance`` filter:
      * ``None``  → all rows (host + every imported friend)
      * ``"host"`` → only native rows
      * ``"alex"``  → only imports tagged with ``source_instance="alex"``

    "Closed" = ``r_multiple IS NOT NULL``. Same gate as v7.13 Phase 1.6.

    ``feed_mismatch_seen`` is True if any imported row has
    ``source_feed != "sip"`` — surfaced as a warning header in
    ``render_summary``.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[AuditRow] = []
    feed_mismatch_seen = False

    db = SessionLocal()
    try:
        # Native rows (TradeRecord) — only included when caller wants
        # host or "all"
        if source_instance is None or source_instance == "host":
            q_native = (
                select(TradeRecord)
                .where(TradeRecord.r_multiple.is_not(None))
                .where(TradeRecord.opened_at >= cutoff)
                .order_by(TradeRecord.opened_at)
            )
            for t in db.execute(q_native).scalars():
                rows.append(AuditRow(
                    symbol=t.symbol,
                    universe_source=t.universe_source,
                    r_multiple=t.r_multiple,
                    source_instance="host",
                    source_feed="sip",   # native trades always SIP per .env lock
                ))

        # Imported rows (ImportedTradeRecord) — included unless caller
        # explicitly asked for host only
        if source_instance != "host":
            q_imported = (
                select(ImportedTradeRecord)
                .where(ImportedTradeRecord.r_multiple.is_not(None))
                # Imported rows store the friend's opened_at (when the
                # trade fired on their box). Use that for the window
                # filter so a recent import of an old trade is properly
                # bucketed by the trade date, not the import date.
                .where(ImportedTradeRecord.opened_at >= cutoff)
                .order_by(ImportedTradeRecord.opened_at)
            )
            if source_instance is not None:
                q_imported = q_imported.where(
                    ImportedTradeRecord.source_instance == source_instance,
                )
            for it in db.execute(q_imported).scalars():
                feed = (it.source_feed or "sip").lower()
                if feed != "sip":
                    feed_mismatch_seen = True
                rows.append(AuditRow(
                    symbol=it.symbol,
                    universe_source=it.universe_source,
                    r_multiple=it.r_multiple,
                    source_instance=it.source_instance,
                    source_feed=feed,
                ))
    finally:
        db.close()

    return rows, feed_mismatch_seen


def _stats_from_rs(rs: list[float]) -> dict:
    """Compute the standard stats dict from a list of R-multiples."""
    n = len(rs)
    total_r = sum(rs)
    avg_r = total_r / n
    wins = sum(1 for r in rs if r > 0)
    return {
        "n":                  n,
        "wins":               wins,
        "win_rate":           wins / n,
        "raw_avg_r":          avg_r,
        "raw_total_r":        total_r,
        "friction_avg_r":     avg_r - FRICTION_R_PER_TRADE,
        "friction_total_r":   total_r - FRICTION_R_PER_TRADE * n,
        "best_r":             max(rs),
        "worst_r":            min(rs),
    }


def aggregate_by_source(rows: list[AuditRow]) -> dict[str, dict]:
    """Per-universe-source aggregate stats (unchanged from Phase 1.6 —
    just operates on AuditRow now). Returns {universe_source: stats}.

    Sums across both native and imported rows; the per-instance split
    is in ``aggregate_by_instance``.
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.r_multiple is None:
            continue
        buckets[_coalesce_source(r.universe_source)].append(r.r_multiple)

    return {src: _stats_from_rs(rs) for src, rs in buckets.items() if rs}


def aggregate_by_instance(rows: list[AuditRow]) -> dict[str, dict]:
    """Per-source-instance aggregate stats (v7.14b).

    Returns ``{instance_id: stats}`` where instance_id is ``"host"``
    for native trades or the friend ID for imports.
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.r_multiple is None:
            continue
        buckets[r.source_instance].append(r.r_multiple)
    return {iid: _stats_from_rs(rs) for iid, rs in buckets.items() if rs}


def aggregate_cross_tab(rows: list[AuditRow]) -> dict[tuple[str, str], dict]:
    """Cross-tab: ``{(instance, universe_source): stats}`` (v7.14b).

    Surfaces per-friend divergence (e.g., friend X's morning_dynamic
    AvgR drifts from yours, suggesting a data-feed or apply-discipline
    difference).
    """
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        if r.r_multiple is None:
            continue
        key = (r.source_instance, _coalesce_source(r.universe_source))
        buckets[key].append(r.r_multiple)
    return {k: _stats_from_rs(rs) for k, rs in buckets.items() if rs}


def aggregate_cells(rows: list[AuditRow]) -> list[dict]:
    """Per-(universe_source × symbol) cells. Drives bleed-cell flagging.

    Returns a list ordered by (source, friction_avg_r asc) so the
    "worst cells" surface at the top of each section. (Unchanged from
    Phase 1.6 — operates on AuditRow now.)
    """
    cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        if r.r_multiple is None:
            continue
        key = (_coalesce_source(r.universe_source), (r.symbol or "?").upper())
        cells[key].append(r.r_multiple)

    out: list[dict] = []
    for (source, symbol), rs in cells.items():
        n = len(rs)
        avg_r = sum(rs) / n
        out.append({
            "source":          source,
            "symbol":          symbol,
            "n":               n,
            "raw_avg_r":       avg_r,
            "friction_avg_r":  avg_r - FRICTION_R_PER_TRADE,
            "raw_total_r":     sum(rs),
        })
    out.sort(key=lambda c: (c["source"], c["friction_avg_r"]))
    return out


def render_summary(
    by_source: dict[str, dict],
    cells: list[dict],
    days: int,
    *,
    by_instance: dict[str, dict] | None = None,
    cross_tab: dict[tuple[str, str], dict] | None = None,
    feed_mismatch_seen: bool = False,
    source_instance_filter: str | None = None,
) -> str:
    """Human-readable text report. Returned as a single string.

    v7.14b extensions (all optional, default to no-op so callers
    that don't pass them get the v7.13 Phase 1.6 behavior):
      * ``by_instance`` — per-source-instance summary table (host
        vs friend cohorts)
      * ``cross_tab`` — instance × universe_source breakdown
      * ``feed_mismatch_seen`` — surfaces a warning header if any
        imported batch was on a non-SIP feed
      * ``source_instance_filter`` — annotates the title when the
        operator drilled into one friend's trades
    """
    lines: list[str] = []
    lines.append("=" * 78)
    title = f"Universe-source audit — last {days} days"
    if source_instance_filter:
        title += f"  [filter: source_instance={source_instance_filter}]"
    lines.append(title)
    lines.append("=" * 78)
    lines.append(f"Generated:    {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Friction:     {FRICTION_R_PER_TRADE:.2f}R per trade (audited baseline)")
    lines.append("")

    # ── Feed-mismatch warning (refinement #9) ────
    # Surfaced loudly at the top so it's the first thing the operator
    # sees if a friend's instance was running on the wrong feed.
    if feed_mismatch_seen:
        lines.append("⚠️  " + ("─" * 75))
        lines.append("⚠️  FEED MISMATCH: at least one imported batch ran on a non-SIP feed.")
        lines.append("⚠️  The IEX-vs-SIP parity test (2026-05-01) showed only 9/126 trades")
        lines.append("⚠️  match across feeds. Treat those rows as a SEPARATE cohort — do")
        lines.append("⚠️  not pool them with SIP-fed trades when computing pass criteria.")
        lines.append("⚠️  " + ("─" * 75))
        lines.append("")

    if not by_source:
        lines.append("No closed trades in window. Nothing to audit.")
        return "\n".join(lines)

    # ── Per-source headline ────
    lines.append("─" * 78)
    lines.append("Per-source headline")
    lines.append("─" * 78)
    header = f"{'source':<18} {'n':>4} {'wins':>4} {'win%':>6} {'raw_AvgR':>10} {'fric_AvgR':>11} {'fric_TotR':>11}"
    lines.append(header)
    lines.append("-" * len(header))
    # Stable order: base first, then morning_dynamic, then everything
    # else (incl. unknown) alphabetically.
    order_priority = {"base": 0, "morning_dynamic": 1, "unknown": 2}
    sorted_sources = sorted(
        by_source.keys(),
        key=lambda s: (order_priority.get(s, 3), s),
    )
    for source in sorted_sources:
        s = by_source[source]
        lines.append(
            f"{source:<18} {s['n']:>4} {s['wins']:>4} "
            f"{s['win_rate']*100:>5.1f}% {s['raw_avg_r']:>+10.3f} "
            f"{s['friction_avg_r']:>+11.3f} {s['friction_total_r']:>+11.2f}"
        )
    lines.append("")

    # ── Pass-criteria gate (the question this script exists to answer) ────
    if "morning_dynamic" in by_source and "base" in by_source:
        dyn = by_source["morning_dynamic"]
        base = by_source["base"]
        gate1 = dyn["friction_avg_r"] > 0
        gate2 = (
            dyn["friction_avg_r"] >= 0.80 * base["friction_avg_r"]
            if base["friction_avg_r"] > 0 else False
        )
        verdict = "PASS" if gate1 and gate2 else "FAIL"
        lines.append("─" * 78)
        lines.append("Pass-criteria gate (morning_dynamic vs base)")
        lines.append("─" * 78)
        lines.append(
            f"  morning_dynamic post-friction AvgR > 0              : "
            f"{dyn['friction_avg_r']:+.3f}  → {'PASS' if gate1 else 'FAIL'}"
        )
        if base["friction_avg_r"] > 0:
            ratio = dyn["friction_avg_r"] / base["friction_avg_r"]
            lines.append(
                f"  morning_dynamic >= 80% of base post-friction AvgR   : "
                f"{ratio:.2f}× ({dyn['friction_avg_r']:+.3f} vs {base['friction_avg_r']:+.3f}) "
                f"→ {'PASS' if gate2 else 'FAIL'}"
            )
        else:
            lines.append(
                f"  morning_dynamic >= 80% of base post-friction AvgR   : "
                f"base AvgR is non-positive ({base['friction_avg_r']:+.3f}) — "
                "gate is undefined; review the base bucket before promoting dynamic."
            )
        lines.append(f"\n  VERDICT: {verdict}")
    else:
        lines.append(
            "Pass-criteria gate skipped — need both 'base' AND "
            "'morning_dynamic' buckets with closed trades."
        )
    lines.append("")

    # ── Per-source-instance headline (v7.14b) ────────────────────────────
    if by_instance:
        lines.append("─" * 78)
        lines.append("Per-instance headline (host = your central box; others = friends)")
        lines.append("─" * 78)
        header = f"{'instance':<18} {'n':>4} {'wins':>4} {'win%':>6} {'raw_AvgR':>10} {'fric_AvgR':>11} {'fric_TotR':>11}"
        lines.append(header)
        lines.append("-" * len(header))
        # host first, then friends alphabetically
        for iid in sorted(by_instance.keys(), key=lambda i: (i != "host", i)):
            s = by_instance[iid]
            lines.append(
                f"{iid:<18} {s['n']:>4} {s['wins']:>4} "
                f"{s['win_rate']*100:>5.1f}% {s['raw_avg_r']:>+10.3f} "
                f"{s['friction_avg_r']:>+11.3f} {s['friction_total_r']:>+11.2f}"
            )
        lines.append("")

    # ── Cross-tab: instance × universe_source (v7.14b) ───────────────────
    if cross_tab:
        # Collect the unique axes in stable order
        instances = sorted({k[0] for k in cross_tab.keys()},
                           key=lambda i: (i != "host", i))
        universe_order = {"base": 0, "morning_dynamic": 1, "unknown": 2}
        sources = sorted({k[1] for k in cross_tab.keys()},
                         key=lambda s: (universe_order.get(s, 3), s))

        lines.append("─" * 78)
        lines.append("Cross-tab: source_instance × universe_source  (cells = n / fric_AvgR)")
        lines.append("─" * 78)
        header = f"{'instance':<18}"
        for src in sources:
            header += f"  {src[:14]:<14}"
        lines.append(header)
        lines.append("-" * len(header))
        for iid in instances:
            row_parts = [f"{iid:<18}"]
            for src in sources:
                stats = cross_tab.get((iid, src))
                if stats is None:
                    row_parts.append(f"  {'—':<14}")
                else:
                    cell = f"{stats['n']:>3}/{stats['friction_avg_r']:>+.2f}"
                    row_parts.append(f"  {cell:<14}")
            lines.append("".join(row_parts))
        lines.append("")

    # ── Bleed cells (review feedback #5 — surface where the friction loss is) ────
    BLEED_THRESHOLD = -0.30
    bleed_cells = [c for c in cells if c["n"] >= 5 and c["friction_avg_r"] < BLEED_THRESHOLD]
    lines.append("─" * 78)
    lines.append(f"Bleed cells (n >= 5 AND post-friction AvgR < {BLEED_THRESHOLD:+.2f})")
    lines.append("─" * 78)
    if bleed_cells:
        lines.append(f"{'source':<18} {'symbol':<8} {'n':>4} {'fric_AvgR':>11} {'raw_TotR':>11}")
        lines.append("-" * 60)
        for c in bleed_cells:
            lines.append(
                f"{c['source']:<18} {c['symbol']:<8} {c['n']:>4} "
                f"{c['friction_avg_r']:>+11.3f} {c['raw_total_r']:>+11.2f}"
            )
        lines.append(
            "\n  → Consider blacklisting these (source × symbol) cells via "
            "the existing per-strategy ``symbol_blacklist`` mechanism, "
            "or removing dynamic symbols from the candidate pool."
        )
    else:
        lines.append("  None — no source × symbol cell is bleeding past the threshold.")
    lines.append("")

    return "\n".join(lines)


def write_csv(path: Path, by_source: dict[str, dict], cells: list[dict]) -> None:
    """Write a per-cell CSV for spreadsheet drilling. Headers are stable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "tier", "source", "symbol", "n", "wins", "win_rate",
            "raw_avg_r", "friction_avg_r", "raw_total_r", "friction_total_r",
            "best_r", "worst_r",
        ])
        # Tier 1: per-source aggregates (symbol left blank)
        for source, s in sorted(by_source.items()):
            writer.writerow([
                "source", source, "", s["n"], s["wins"], f"{s['win_rate']:.4f}",
                f"{s['raw_avg_r']:+.4f}", f"{s['friction_avg_r']:+.4f}",
                f"{s['raw_total_r']:+.3f}", f"{s['friction_total_r']:+.3f}",
                f"{s['best_r']:+.3f}", f"{s['worst_r']:+.3f}",
            ])
        # Tier 2: per-cell drilldown
        for c in cells:
            writer.writerow([
                "cell", c["source"], c["symbol"], c["n"], "", "",
                f"{c['raw_avg_r']:+.4f}", f"{c['friction_avg_r']:+.4f}",
                f"{c['raw_total_r']:+.3f}", "", "", "",
            ])
    logger.info("audit_csv_written path=%s", path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Per-source audit of closed trades — answers 'did dynamic symbols help?'",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Lookback window in calendar days. Default: 30.",
    )
    parser.add_argument(
        "--csv", type=Path, default=None,
        help="Optional CSV output path. Two tiers: per-source + per-cell.",
    )
    parser.add_argument(
        "--source-instance", type=str, default=None,
        help=(
            "Filter to one source instance. Use 'host' for your "
            "native trades only, or a friend ID (e.g., 'alex') for that "
            "friend's imported trades only. Default: include everything "
            "(host + every imported friend, segmented by instance + "
            "universe_source in the cross-tab)."
        ),
    )
    args = parser.parse_args()

    logger.info(
        "audit_start days=%d friction=%.2fR/trade source_instance=%s",
        args.days, FRICTION_R_PER_TRADE, args.source_instance,
    )
    rows, feed_mismatch_seen = fetch_closed_trades(
        args.days, source_instance=args.source_instance,
    )
    logger.info(
        "audit_rows_fetched n=%d feed_mismatch=%s",
        len(rows), feed_mismatch_seen,
    )

    by_source = aggregate_by_source(rows)
    by_instance = aggregate_by_instance(rows)
    cross_tab = aggregate_cross_tab(rows)
    cells = aggregate_cells(rows)

    print(render_summary(
        by_source, cells, args.days,
        by_instance=by_instance,
        cross_tab=cross_tab,
        feed_mismatch_seen=feed_mismatch_seen,
        source_instance_filter=args.source_instance,
    ))

    if args.csv:
        write_csv(args.csv, by_source, cells)
        print(f"\nCSV: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
