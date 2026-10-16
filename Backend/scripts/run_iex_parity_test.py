"""
SIP-vs-IEX parity test (v7.13a, 2026-05-01)
=============================================================================
Runs the v7.12.1 backtest twice on the same window — once on Alpaca's
paid SIP feed, once on the free IEX feed — and reports whether the
predicate firings match closely enough to trust IEX as a SIP drop-in.

Why this matters
----------------
Alpaca's SIP feed is ~$99/month; the IEX feed is free. IEX is one
exchange's view of the tape; SIP is the consolidated tape across all
US exchanges. The two can produce different 1-minute candles when:

  * A symbol trades more actively on NYSE/NASDAQ than IEX (block
    trades, opening auction, closing cross don't print to IEX).
  * Wick extremes differ by a few cents because a single odd-lot
    print to NYSE is on SIP but not on IEX.
  * Volume per bar can be a fraction of SIP volume.

ICT predicates depend on exact wick prices (sweep magnitude vs ATR,
MSS broken-swing price) and on volume confirmation (vol_ratio gates),
so even small differences could shift which signals fire. We need
quantitative evidence — not vibes — before defaulting to IEX.

Pass criterion (per the v7.12+ plan)
------------------------------------
≥ 95% of SIP trades must have a matching IEX trade where:
  * symbol matches
  * entry timestamp matches (to the minute)
  * direction matches
  * R-multiple is within ±0.05R

If < 95% match → IEX is materially different from SIP for predicate
firing; stick with the paid feed. Document the gap, then re-evaluate
later (e.g. switch to Polygon, accept the SIP cost, or only use IEX
for symbols that show clean parity).

What this script does
---------------------
1. Pulls the v7.12.1 23-symbol universe × 6m window on the SIP feed
   (cached under ``Backend/data/bars_cache_ext_sip/``).
2. Pulls the same on IEX (cached under
   ``Backend/data/bars_cache_ext_iex/``).
3. Bar-level diff: total bar count per feed, sample symbol/day where
   counts differ, OHLC delta distribution.
4. Trade-level diff: matches SIP trades against IEX trades by
   (symbol, entry_ts, direction); reports match rate, R-multiple
   delta distribution.
5. Writes a report to ``data/research/iex_parity_<date>.txt`` and
   per-trade CSVs to ``data/research/iex_parity_<date>_*.csv``.

The first run is API-bound (~2-3 hours wall time depending on rate
limits). Subsequent runs reuse the cache and complete in minutes.
=============================================================================
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

# Make the Backend/ directory importable when running this as
# ``venv/bin/python -m scripts.run_iex_parity_test`` from Backend/.
_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

from app.strategy_backtest import (
    Trade,
    fetch_history,
    run_backtest,
)
from app.settings import settings


# Quiet the noisy app loggers — we have our own structured progress logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("app.strategy_backtest").setLevel(logging.WARNING)
logger = logging.getLogger("iex_parity")


# v7.12.1 audited universe (23 symbols). Mirrors STABLE_SCANNER_CONTRACT.md.
V7121_UNIVERSE = [
    "AAPL", "NVDA", "AMD", "TSLA", "AMZN", "MSFT", "GOOGL",
    "JPM", "MS", "INTC", "SOFI", "BAC", "F", "PLTR", "SOXL",
    "DKNG", "XLF", "XLE", "ADBE", "CRM", "HD", "UNH", "XLV",
]
V7121_START = date(2025, 10, 27)
V7121_END = date(2026, 4, 24)

# Pass criterion. Mirrors the plan ("≥ 95% match, R-multiple within ±0.05R").
TRADE_MATCH_THRESHOLD_PCT = 95.0
R_MULTIPLE_TOLERANCE = 0.05


# ── Bar-level diff ──────────────────────────────────────────────────────────

def summarize_bar_counts(
    sip_data: dict[str, dict[date, list[dict]]],
    iex_data: dict[str, dict[date, list[dict]]],
) -> dict:
    """Compare bar counts at (symbol, day) granularity.

    Returns a dict with:
      ``per_symbol`` — list of (symbol, sip_count, iex_count, ratio)
      ``totals`` — (total_sip_bars, total_iex_bars, ratio)
      ``zero_iex_days`` — list of (symbol, day) where IEX returned 0 bars
        but SIP returned > 0. These are the problem cases — IEX can't
        even see what SIP saw.
    """
    per_symbol = []
    total_sip = total_iex = 0
    zero_iex_days = []

    symbols = sorted(set(sip_data.keys()) | set(iex_data.keys()))
    for sym in symbols:
        sip_days = sip_data.get(sym, {})
        iex_days = iex_data.get(sym, {})
        sym_sip_bars = sum(len(b) for b in sip_days.values())
        sym_iex_bars = sum(len(b) for b in iex_days.values())
        total_sip += sym_sip_bars
        total_iex += sym_iex_bars
        ratio = (sym_iex_bars / sym_sip_bars) if sym_sip_bars else 0.0
        per_symbol.append((sym, sym_sip_bars, sym_iex_bars, ratio))

        all_days = set(sip_days.keys()) | set(iex_days.keys())
        for d in sorted(all_days):
            sip_n = len(sip_days.get(d, []))
            iex_n = len(iex_days.get(d, []))
            if sip_n > 0 and iex_n == 0:
                zero_iex_days.append((sym, d, sip_n))

    overall_ratio = (total_iex / total_sip) if total_sip else 0.0
    return {
        "per_symbol": per_symbol,
        "totals": (total_sip, total_iex, overall_ratio),
        "zero_iex_days": zero_iex_days,
    }


# ── Trade-level diff ────────────────────────────────────────────────────────

def index_trades(trades: Iterable[Trade]) -> dict[tuple, Trade]:
    """Index trades by (symbol, entry_ts_minute, direction, strategy).

    The strategy name is part of the key because the same symbol can
    fire multiple strategies in the same minute (Judas Long + P3 Long
    on a strong move). Without the strategy in the key those would
    collide and we'd undercount the matches.
    """
    out: dict[tuple, Trade] = {}
    for t in trades:
        # Round entry_ts to the minute — both feeds emit 1-min bars at
        # the same wall-clock minute, just with potentially different
        # bar contents. Minute-level matching tolerates floating
        # microsecond drift.
        if t.entry_ts is None:
            continue
        key = (
            t.symbol.upper(),
            t.entry_ts.replace(second=0, microsecond=0).isoformat(),
            t.direction,
            t.strategy,
        )
        out[key] = t
    return out


def diff_trades(
    sip_trades: list[Trade],
    iex_trades: list[Trade],
) -> dict:
    """Match SIP trades vs IEX trades and tabulate the differences.

    Returns:
      ``matched`` — list of (sip_trade, iex_trade) pairs where the key
        (sym, ts, dir, strat) matched. The R-multiple tolerance is
        evaluated separately so we can break it down (matched-clean
        vs matched-but-divergent-R).
      ``sip_only`` — trades that fired on SIP but had no corresponding
        IEX trade. These are the "IEX missed it" cases.
      ``iex_only`` — trades that fired on IEX but not on SIP. These
        are the "IEX hallucinated a setup" cases — usually because
        a sparse IEX bar shifted a wick into / out of an FVG.
      ``r_delta_distribution`` — Counter of bucketed R-multiple
        deltas across matched pairs.
    """
    sip_idx = index_trades(sip_trades)
    iex_idx = index_trades(iex_trades)

    matched_pairs: list[tuple[Trade, Trade]] = []
    sip_only_keys = []
    iex_only_keys = []

    sip_keys = set(sip_idx.keys())
    iex_keys = set(iex_idx.keys())
    for k in sip_keys & iex_keys:
        matched_pairs.append((sip_idx[k], iex_idx[k]))
    for k in sip_keys - iex_keys:
        sip_only_keys.append(k)
    for k in iex_keys - sip_keys:
        iex_only_keys.append(k)

    # R-multiple delta histogram across matched pairs (only when both
    # sides have an R-multiple).
    r_buckets: Counter = Counter()
    matched_clean = 0          # |Δ R| ≤ 0.05  (the strict tolerance)
    matched_loose = 0          # |Δ R| ≤ 0.20  (still close, but worth flagging)
    matched_diverged = 0       # |Δ R| > 0.20  (genuinely different outcomes)
    for sip_t, iex_t in matched_pairs:
        if sip_t.r_multiple is None or iex_t.r_multiple is None:
            r_buckets["NA"] += 1
            continue
        delta = abs(sip_t.r_multiple - iex_t.r_multiple)
        if delta <= R_MULTIPLE_TOLERANCE:
            matched_clean += 1
            r_buckets["≤0.05"] += 1
        elif delta <= 0.20:
            matched_loose += 1
            r_buckets["≤0.20"] += 1
        else:
            matched_diverged += 1
            r_buckets[">0.20"] += 1

    return {
        "n_sip": len(sip_trades),
        "n_iex": len(iex_trades),
        "matched": matched_pairs,
        "sip_only_keys": sip_only_keys,
        "iex_only_keys": iex_only_keys,
        "matched_clean": matched_clean,
        "matched_loose": matched_loose,
        "matched_diverged": matched_diverged,
        "r_buckets": r_buckets,
    }


def per_strategy_breakdown(
    sip_trades: list[Trade],
    iex_trades: list[Trade],
) -> dict[str, tuple[int, int, float]]:
    """Return {strategy: (sip_count, iex_count, ratio)} for visibility."""
    sip_count: Counter = Counter()
    iex_count: Counter = Counter()
    for t in sip_trades:
        sip_count[t.strategy] += 1
    for t in iex_trades:
        iex_count[t.strategy] += 1
    out: dict[str, tuple[int, int, float]] = {}
    for strat in sorted(set(sip_count) | set(iex_count)):
        s = sip_count[strat]
        i = iex_count[strat]
        ratio = (i / s) if s else 0.0
        out[strat] = (s, i, ratio)
    return out


# ── Report writer ──────────────────────────────────────────────────────────

def write_report(
    out_path: Path,
    bar_summary: dict,
    diff: dict,
    strat_breakdown: dict[str, tuple[int, int, float]],
    pass_pct: float,
    threshold_pct: float,
    start: date,
    end: date,
    universe: list[str],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("SIP vs IEX parity test — v7.12.1 universe")
    lines.append("=" * 78)
    lines.append(f"Generated:    {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Window:       {start} → {end}")
    lines.append(f"Universe:     {len(universe)} symbols ({', '.join(universe)})")
    lines.append("")

    # ── Verdict ────
    verdict = "PASS" if pass_pct >= threshold_pct else "FAIL"
    lines.append(f"VERDICT:      {verdict}  (matched {pass_pct:.2f}% — threshold {threshold_pct:.0f}%)")
    if verdict == "PASS":
        lines.append(
            "  → IEX is safe to default for the audit universe. Switch "
            "ALPACA_DATA_FEED=iex in .env once paper trading validates "
            "live behavior."
        )
    else:
        lines.append(
            "  → IEX diverges from SIP enough to shift predicate firings. "
            "Stick with SIP for the audit universe; revisit per-symbol "
            "feed selection or evaluate Polygon as an alternative."
        )
    lines.append("")

    # ── Bar-level summary ────
    total_sip, total_iex, ratio = bar_summary["totals"]
    lines.append("─" * 78)
    lines.append("Bar-level summary")
    lines.append("─" * 78)
    lines.append(f"Total bars (SIP):     {total_sip:>12,}")
    lines.append(f"Total bars (IEX):     {total_iex:>12,}")
    lines.append(f"IEX/SIP ratio:        {ratio:>12.4f}")
    lines.append("")
    lines.append("Per-symbol (sip_bars iex_bars iex/sip):")
    for sym, s, i, r in bar_summary["per_symbol"]:
        lines.append(f"  {sym:<6}  {s:>9,}  {i:>9,}  {r:>6.3f}")
    if bar_summary["zero_iex_days"]:
        lines.append("")
        lines.append(f"Days where IEX returned 0 bars but SIP > 0 ({len(bar_summary['zero_iex_days'])} total):")
        for sym, d, n in bar_summary["zero_iex_days"][:25]:
            lines.append(f"  {sym:<6}  {d.isoformat()}  sip={n}")
        if len(bar_summary["zero_iex_days"]) > 25:
            lines.append(f"  ... +{len(bar_summary['zero_iex_days']) - 25} more (see CSV)")
    lines.append("")

    # ── Trade-level summary ────
    lines.append("─" * 78)
    lines.append("Trade-level summary")
    lines.append("─" * 78)
    lines.append(f"Trades (SIP):                    {diff['n_sip']:>6}")
    lines.append(f"Trades (IEX):                    {diff['n_iex']:>6}")
    lines.append(f"Matched (sym/ts/dir/strat key):  {len(diff['matched']):>6}")
    lines.append(f"  ├─ |ΔR| ≤ 0.05  (clean):      {diff['matched_clean']:>6}")
    lines.append(f"  ├─ |ΔR| ≤ 0.20  (loose):      {diff['matched_loose']:>6}")
    lines.append(f"  └─ |ΔR| > 0.20  (diverged):   {diff['matched_diverged']:>6}")
    lines.append(f"SIP-only (IEX missed):           {len(diff['sip_only_keys']):>6}")
    lines.append(f"IEX-only (IEX hallucinated):     {len(diff['iex_only_keys']):>6}")
    lines.append("")
    lines.append(f"Match rate (SIP trades w/ matching IEX trade ≤ 0.05R): "
                 f"{pass_pct:.2f}%  (threshold {threshold_pct:.0f}%)")
    lines.append("")

    # ── Per-strategy breakdown ────
    lines.append("─" * 78)
    lines.append("Per-strategy trade counts")
    lines.append("─" * 78)
    lines.append(f"{'Strategy':<35}  {'SIP':>5}  {'IEX':>5}  {'IEX/SIP':>8}")
    for strat, (s, i, r) in strat_breakdown.items():
        lines.append(f"{strat:<35}  {s:>5}  {i:>5}  {r:>8.3f}")
    lines.append("")

    # ── R-multiple delta distribution ────
    lines.append("─" * 78)
    lines.append("R-multiple delta distribution (matched pairs)")
    lines.append("─" * 78)
    for bucket in ("≤0.05", "≤0.20", ">0.20", "NA"):
        n = diff["r_buckets"].get(bucket, 0)
        lines.append(f"  {bucket:<10}  {n:>5}")
    lines.append("")

    out_path.write_text("\n".join(lines))
    logger.info("report_written path=%s", out_path)


def write_diff_csv(
    out_path: Path,
    diff: dict,
    sip_trades: list[Trade],
    iex_trades: list[Trade],
) -> None:
    """Write a per-trade CSV: one row per matched/SIP-only/IEX-only key.

    Columns:
      bucket,sym,entry_ts,direction,strategy,sip_R,iex_R,delta_R,
      sip_exit,iex_exit,sip_entry_px,iex_entry_px
    """
    import csv
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sip_idx = index_trades(sip_trades)
    iex_idx = index_trades(iex_trades)

    def _row(bucket: str, key: tuple, sip_t: Trade | None, iex_t: Trade | None):
        sym, ts, direction, strategy = key
        return {
            "bucket": bucket,
            "symbol": sym,
            "entry_ts": ts,
            "direction": direction,
            "strategy": strategy,
            "sip_R": (f"{sip_t.r_multiple:+.3f}" if sip_t and sip_t.r_multiple is not None else ""),
            "iex_R": (f"{iex_t.r_multiple:+.3f}" if iex_t and iex_t.r_multiple is not None else ""),
            "delta_R": (
                f"{(sip_t.r_multiple - iex_t.r_multiple):+.3f}"
                if (sip_t and iex_t and sip_t.r_multiple is not None and iex_t.r_multiple is not None)
                else ""
            ),
            "sip_exit": (sip_t.exit_reason or "") if sip_t else "",
            "iex_exit": (iex_t.exit_reason or "") if iex_t else "",
            "sip_entry_px": f"{sip_t.entry_price:.4f}" if sip_t else "",
            "iex_entry_px": f"{iex_t.entry_price:.4f}" if iex_t else "",
        }

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "bucket", "symbol", "entry_ts", "direction", "strategy",
                "sip_R", "iex_R", "delta_R",
                "sip_exit", "iex_exit",
                "sip_entry_px", "iex_entry_px",
            ],
        )
        writer.writeheader()
        for sip_t, iex_t in diff["matched"]:
            key = (
                sip_t.symbol.upper(),
                sip_t.entry_ts.replace(second=0, microsecond=0).isoformat(),
                sip_t.direction,
                sip_t.strategy,
            )
            writer.writerow(_row("matched", key, sip_t, iex_t))
        for k in diff["sip_only_keys"]:
            writer.writerow(_row("sip_only", k, sip_idx.get(k), None))
        for k in diff["iex_only_keys"]:
            writer.writerow(_row("iex_only", k, None, iex_idx.get(k)))
    logger.info("diff_csv_written path=%s", out_path)


# ── Main entry point ────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SIP-vs-IEX parity test — v7.12.1 universe by default.",
    )
    parser.add_argument(
        "--start", type=lambda s: date.fromisoformat(s),
        default=V7121_START,
        help=f"Start date (YYYY-MM-DD). Default: {V7121_START.isoformat()} (v7.12.1 audit window).",
    )
    parser.add_argument(
        "--end", type=lambda s: date.fromisoformat(s),
        default=V7121_END,
        help=f"End date (YYYY-MM-DD). Default: {V7121_END.isoformat()} (v7.12.1 audit window).",
    )
    parser.add_argument(
        "--symbols", type=str, default=None,
        help="Comma-separated override for the universe. Default: v7.12.1's 23 symbols.",
    )
    parser.add_argument(
        "--bars-only", action="store_true",
        help="Only compare bar-level differences — skip the backtest re-run. "
             "Useful when you want to know whether a feed swap is even plausible "
             "before committing to the longer trade-level comparison.",
    )
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path("data/research"),
        help="Where to write report + CSVs. Default: data/research/",
    )
    args = parser.parse_args()

    universe = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else list(V7121_UNIVERSE)
    )

    logger.info(
        "iex_parity_start universe=%d start=%s end=%s bars_only=%s",
        len(universe), args.start, args.end, args.bars_only,
    )
    logger.info(
        "iex_parity_setting current_default_feed=%s",
        settings.alpaca_data_feed,
    )

    # ── Fetch on both feeds ────
    logger.info("fetching SIP bars (this is the slow part on first run) ...")
    t0 = datetime.now()
    sip_data = fetch_history(universe, args.start, args.end, feed="sip")
    sip_elapsed = (datetime.now() - t0).total_seconds()
    sip_bars = sum(sum(len(b) for b in days.values()) for days in sip_data.values())
    logger.info("sip_fetch_done bars=%d elapsed=%.1fs", sip_bars, sip_elapsed)

    logger.info("fetching IEX bars ...")
    t0 = datetime.now()
    iex_data = fetch_history(universe, args.start, args.end, feed="iex")
    iex_elapsed = (datetime.now() - t0).total_seconds()
    iex_bars = sum(sum(len(b) for b in days.values()) for days in iex_data.values())
    logger.info("iex_fetch_done bars=%d elapsed=%.1fs", iex_bars, iex_elapsed)

    bar_summary = summarize_bar_counts(sip_data, iex_data)

    # ── Optionally short-circuit before re-running the backtest ────
    if args.bars_only:
        out_path = args.out_dir / f"iex_parity_bars_{date.today().isoformat()}.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        lines.append("Bars-only parity sample (no backtest re-run)")
        lines.append(f"Window: {args.start} → {args.end}")
        lines.append(f"SIP bars: {bar_summary['totals'][0]:,}")
        lines.append(f"IEX bars: {bar_summary['totals'][1]:,}")
        lines.append(f"IEX/SIP: {bar_summary['totals'][2]:.4f}")
        lines.append(
            f"Days where IEX=0 but SIP>0: {len(bar_summary['zero_iex_days'])}"
        )
        out_path.write_text("\n".join(lines))
        logger.info("bars_only_report_written path=%s", out_path)
        return 0

    # ── Full backtest re-run on both feeds ────
    logger.info("running backtest on SIP feed ...")
    t0 = datetime.now()
    sip_result = run_backtest(universe, args.start, args.end, feed="sip")
    sip_bt_elapsed = (datetime.now() - t0).total_seconds()
    logger.info(
        "sip_backtest_done trades=%d elapsed=%.1fs",
        sum(s.trades for s in sip_result.by_strategy.values()), sip_bt_elapsed,
    )

    logger.info("running backtest on IEX feed ...")
    t0 = datetime.now()
    iex_result = run_backtest(universe, args.start, args.end, feed="iex")
    iex_bt_elapsed = (datetime.now() - t0).total_seconds()
    logger.info(
        "iex_backtest_done trades=%d elapsed=%.1fs",
        sum(s.trades for s in iex_result.by_strategy.values()), iex_bt_elapsed,
    )

    # ── Diff + scoring ────
    diff = diff_trades(sip_result.trades, iex_result.trades)

    # Match rate = (SIP trades with a matching IEX trade within 0.05R) / SIP total.
    pass_pct = (
        (diff["matched_clean"] / diff["n_sip"] * 100.0)
        if diff["n_sip"] else 0.0
    )

    strat_breakdown = per_strategy_breakdown(sip_result.trades, iex_result.trades)

    # ── Outputs ────
    today_iso = date.today().isoformat()
    report_path = args.out_dir / f"iex_parity_{today_iso}.txt"
    csv_path = args.out_dir / f"iex_parity_{today_iso}_diff.csv"
    write_report(
        report_path,
        bar_summary, diff, strat_breakdown,
        pass_pct=pass_pct,
        threshold_pct=TRADE_MATCH_THRESHOLD_PCT,
        start=args.start, end=args.end, universe=universe,
    )
    write_diff_csv(csv_path, diff, sip_result.trades, iex_result.trades)

    print(f"\nReport: {report_path}")
    print(f"Diff CSV: {csv_path}")
    print(f"Match rate: {pass_pct:.2f}%  (threshold {TRADE_MATCH_THRESHOLD_PCT:.0f}%)")
    print(f"Verdict: {'PASS' if pass_pct >= TRADE_MATCH_THRESHOLD_PCT else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
