"""
walk_forward.py — Feature #7: Walk-forward validation.
======================================================

Don't trust a backtest that was tuned on the same data it ran on.
Split the simulated trade list into three chronological windows:

  • **Train**   (up to ``train_end``)           — where we fit knobs and
                                                   #6's per-(strategy, symbol) calibration table.
  • **Validate** (``train_end`` → ``val_end``)  — unseen bars; nothing
                                                   gets re-tuned here.
  • **Paper**    (``val_end`` onwards)           — reserved for live
                                                   paper-trading (usually
                                                   not yet present in a
                                                   backtest CSV, but the
                                                   same code path handles
                                                   it if the trades exist).

If the strategy ranking + portfolio expectancy hold up across train
and validate, the edge is real. If they flip between windows, the
backtest was curve-fit to the training noise and the live
min-confidence gate should stay skeptical.

Acceptance criteria (from planned-features memo, item #7):

  • **Rank stability**: each of the top-3 strategies by expectancy on
    train must remain in the top-5 on validate. Ranking stability
    matters more than absolute numbers — expectancy naturally
    degrades on unseen data, but the *ordering* should hold.
  • **Expectancy floor**: validate portfolio expectancy ≥ 70% of
    train's. A cliff means overfitting; a gentle degradation is
    expected and acceptable.

This module does NOT re-run the backtest — it partitions an existing
``BacktestResult`` by entry date and re-aggregates each window. That
keeps walk-forward cheap (~milliseconds) and cleanly composable with
the existing CLI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from . import calibration
from .strategy_backtest import (
    BacktestResult,
    StrategyStats,
    Trade,
    _aggregate,
)

logger = logging.getLogger("tradehack.walk_forward")


# ── Tuning knobs ─────────────────────────────────────────────────────────────

# Top-N strategies on train that must remain visible on validate
_TOP_TRAIN_N = 3
# ...within the top-N on validate
_TOP_VALIDATE_N = 5
# Validate portfolio expectancy must stay this fraction of train's
_EXPECTANCY_FLOOR = 0.70

# Hard-gate simulation: drop validate trades whose (strategy, symbol) train-only
# multiplier is below this threshold. 1.0 is the "live gate replicated" cut —
# anything the train-only table would scale *down* is filtered out, anything
# at-or-above the break-even calibration stays. Mirrors the fact that the
# live min_confidence=0.70 floor is hit when raw × mult >= 0.70, i.e. mult >= 1.
# (Gate lowered from 0.80 → 0.70 on 2026-04-24 based on the Tier-2 lift study —
#  see autotrade.AutoTradeState.min_confidence for the data behind that choice.)
_GATED_MIN_MULTIPLIER = 1.0


# ── Types ────────────────────────────────────────────────────────────────────

@dataclass
class Window:
    """One chronological slice of a walk-forward split."""
    label: str                              # "train" | "validate" | "paper"
    start: date | None                      # None = "beginning of data"
    end: date | None                        # exclusive; None = "end of data"
    result: BacktestResult

    @property
    def trade_count(self) -> int:
        return len(self.result.trades)

    @property
    def portfolio_expectancy(self) -> float:
        """Trade-weighted average R across every strategy in this window."""
        tot_trades = sum(s.trades for s in self.result.by_strategy.values())
        tot_r = sum(s.total_r for s in self.result.by_strategy.values())
        return tot_r / tot_trades if tot_trades else 0.0


@dataclass
class GatedValidateStats:
    """Result of applying the train-only #6 calibration table to validate trades.

    Two lenses on the same question — "does the calibration layer generalize?":

      • **weighted**  — every validate trade kept, but its R scaled by its
        (strategy, symbol) train-only multiplier. Answers "does the
        multiplier predict relative ordering of validate performance?"
      • **gated**     — validate trades dropped if their (strategy, symbol)
        train-only multiplier is below ``_GATED_MIN_MULTIPLIER``. Answers
        "would the live min_confidence gate have helped on unseen data?"

    Both views are computed from the *same* train-only snapshot, so the
    numbers are directly comparable to the raw validate expectancy in the
    parent ``WalkForwardReport``.
    """
    # Snapshot stats — set once by _compute_gated_validate
    train_end: date
    cells_in_snapshot: int                  # number of (strategy, symbol) cells
    validate_trade_count: int               # raw count before any filtering

    # Hard-gate view
    gated_trade_count: int
    gated_total_r: float
    gated_dropped_count: int                # validate trades filtered out
    gated_dropped_strategies: list[str]     # strategies with mult < threshold (any cell)

    # Weighted view
    weighted_total_r: float                 # Σ (trade.r × trade_mult)

    # Coverage — how often we actually had data vs. fell back to 1.0
    cell_hits: int                          # cell-level lookup succeeded (n ≥ MIN_TRADES_PER_CELL)
    strategy_fallback_hits: int             # strategy-wide fallback used
    no_data_hits: int                       # neutral 1.0 because no train evidence

    @property
    def gated_expectancy(self) -> float:
        return self.gated_total_r / self.gated_trade_count if self.gated_trade_count else 0.0

    @property
    def weighted_expectancy(self) -> float:
        return self.weighted_total_r / self.validate_trade_count if self.validate_trade_count else 0.0


@dataclass
class WalkForwardReport:
    windows: list[Window]
    # Rank-stability check
    top_train: list[str]                    # top-N strategy names on train
    top_validate: list[str]                 # top-M strategy names on validate
    rank_missing: list[str]                 # top-train not in top-validate
    rank_stable: bool
    # Expectancy-ratio check
    train_expectancy: float
    validate_expectancy: float | None
    validate_expectancy_ratio: float | None
    expectancy_stable: bool
    # Overall
    passes: bool
    # Walk-forward #7: gated/weighted validate using a train-only #6 table.
    # None when there's no validate window to score, or calibration plumbing
    # is unavailable (e.g. CSV-less smoke test).
    gated_validate: GatedValidateStats | None = None


# ── Splitting ────────────────────────────────────────────────────────────────

def split_result(
    result: BacktestResult,
    *,
    train_end: date,
    val_end: date | None = None,
) -> WalkForwardReport:
    """Partition ``result.trades`` by entry date and re-aggregate each window.

    ``train_end`` and ``val_end`` are **exclusive** upper bounds on the
    trade's entry date — a trade at exactly ``train_end`` goes into the
    validate bucket.

    If ``val_end`` is ``None``, there are only two windows (train +
    validate). Anything after ``train_end`` is treated as validate.
    This is the common case today: our v3 backtest covers a single
    ~6-month window with no paper-trade phase yet, so the verdict is
    train-vs-validate only.
    """
    train_trades: list[Trade] = []
    val_trades: list[Trade] = []
    paper_trades: list[Trade] = []

    for t in result.trades:
        d = t.entry_ts.date()
        if d < train_end:
            train_trades.append(t)
        elif val_end is None or d < val_end:
            val_trades.append(t)
        else:
            paper_trades.append(t)

    windows: list[Window] = [
        Window(
            label="train",
            start=None,
            end=train_end,
            result=_aggregate(train_trades),
        ),
    ]
    if val_end is not None:
        windows.append(
            Window(
                label="validate",
                start=train_end,
                end=val_end,
                result=_aggregate(val_trades),
            )
        )
        windows.append(
            Window(
                label="paper",
                start=val_end,
                end=None,
                result=_aggregate(paper_trades),
            )
        )
    else:
        windows.append(
            Window(
                label="validate",
                start=train_end,
                end=None,
                result=_aggregate(val_trades),
            )
        )

    report = _build_report(windows)

    # Walk-forward #7 critical piece: actually APPLY a train-only #6 table
    # to the validate trades. This is what makes the holdout real — without
    # it the report only tells us whether raw strategy edges generalize,
    # not whether the full gate-with-calibration pipeline does.
    validate = next((w for w in windows if w.label == "validate"), None)
    if validate is not None and validate.trade_count > 0:
        try:
            report.gated_validate = _compute_gated_validate(
                validate_trades=validate.result.trades,
                train_end=train_end,
            )
        except Exception as exc:
            # Calibration plumbing can degrade gracefully — the raw
            # walk-forward report is still valuable on its own.
            logger.warning("gated_validate_compute_failed err=%s", exc)

    return report


def _compute_gated_validate(
    *,
    validate_trades: list[Trade],
    train_end: date,
) -> GatedValidateStats:
    """Fit the #6 calibration table on TRAIN ONLY, then score validate trades.

    Enforces the walk-forward discipline: multipliers come from data
    strictly before ``train_end``, then are applied *unchanged* to the
    validate window. ``calibration.refresh_calibration_table`` with
    ``entry_date_max`` set explicitly does NOT poison the shared live
    cache, so this runs safely alongside a live autotrade process.
    """
    snapshot = calibration.refresh_calibration_table(entry_date_max=train_end)

    gated_trade_count = 0
    gated_total_r = 0.0
    dropped_count = 0
    weighted_total_r = 0.0
    dropped_strategies: set[str] = set()

    cell_hits = 0
    strategy_fallback_hits = 0
    no_data_hits = 0

    for t in validate_trades:
        r = t.r_multiple or 0.0
        mult, meta = calibration.get_multiplier(
            t.strategy, t.symbol, table=snapshot,
        )
        reason = meta.get("reason", "no_data")
        if reason == "cell":
            cell_hits += 1
        elif reason == "strategy_fallback":
            strategy_fallback_hits += 1
        else:
            no_data_hits += 1

        # Weighted — every trade kept, R scaled by multiplier
        weighted_total_r += r * mult

        # Hard gate — drop trades where the train table is telling us to
        # discount (mult < 1.0 means the cell/strategy underperformed
        # break-even on train)
        if mult >= _GATED_MIN_MULTIPLIER:
            gated_trade_count += 1
            gated_total_r += r
        else:
            dropped_count += 1
            dropped_strategies.add(t.strategy)

    return GatedValidateStats(
        train_end=train_end,
        cells_in_snapshot=len(snapshot.cells),
        validate_trade_count=len(validate_trades),
        gated_trade_count=gated_trade_count,
        gated_total_r=gated_total_r,
        gated_dropped_count=dropped_count,
        gated_dropped_strategies=sorted(dropped_strategies),
        weighted_total_r=weighted_total_r,
        cell_hits=cell_hits,
        strategy_fallback_hits=strategy_fallback_hits,
        no_data_hits=no_data_hits,
    )


def _build_report(windows: list[Window]) -> WalkForwardReport:
    train = windows[0]
    validate = next((w for w in windows if w.label == "validate"), None)

    # Top-N train strategies by expectancy (strategies with zero trades skip)
    train_ranked = sorted(
        (s for s in train.result.by_strategy.values() if s.trades),
        key=lambda s: s.expectancy,
        reverse=True,
    )
    top_train = [s.strategy for s in train_ranked[:_TOP_TRAIN_N]]

    # Top-M validate strategies by expectancy
    top_validate: list[str] = []
    rank_stable = False
    rank_missing: list[str] = []
    if validate is not None and validate.result.by_strategy:
        val_ranked = sorted(
            (s for s in validate.result.by_strategy.values() if s.trades),
            key=lambda s: s.expectancy,
            reverse=True,
        )
        top_validate = [s.strategy for s in val_ranked[:_TOP_VALIDATE_N]]
        rank_missing = [s for s in top_train if s not in top_validate]
        rank_stable = bool(top_train) and not rank_missing

    # Expectancy ratio
    train_exp = train.portfolio_expectancy
    val_exp: float | None = None
    ratio: float | None = None
    expectancy_stable = False
    if validate is not None and validate.trade_count > 0:
        val_exp = validate.portfolio_expectancy
        if train_exp > 0:
            ratio = val_exp / train_exp
            expectancy_stable = ratio >= _EXPECTANCY_FLOOR
        elif train_exp < 0 and val_exp < 0:
            # Both negative — "stable" just means validate isn't MUCH worse
            # than train. Ratio convention inverts: we want val_exp / train_exp
            # ≥ 1.0 (i.e. val_exp >= train_exp, meaning less bleeding).
            ratio = val_exp / train_exp
            expectancy_stable = ratio >= 1.0
        elif train_exp == 0:
            ratio = None
            expectancy_stable = val_exp >= 0  # zero-train is a weird edge case

    passes = rank_stable and expectancy_stable

    return WalkForwardReport(
        windows=windows,
        top_train=top_train,
        top_validate=top_validate,
        rank_missing=rank_missing,
        rank_stable=rank_stable,
        train_expectancy=train_exp,
        validate_expectancy=val_exp,
        validate_expectancy_ratio=ratio,
        expectancy_stable=expectancy_stable,
        passes=passes,
    )


# ── Reporting ────────────────────────────────────────────────────────────────

def format_walk_forward_report(report: WalkForwardReport) -> str:
    """Render the side-by-side comparison + pass/fail verdict as text."""
    lines: list[str] = []
    width = 108

    lines.append("=" * width)
    lines.append("WALK-FORWARD VALIDATION (#7)")
    lines.append("=" * width)
    lines.append("")

    # Window summary
    lines.append(
        f"{'Window':<10} {'Date range':<26} {'Trades':>8} "
        f"{'PortfExp(R)':>12} {'Strategies':>11}"
    )
    lines.append("-" * 72)
    for w in report.windows:
        # Cast to str first — date.__format__ swallows width specs as strftime
        # patterns, which renders "<10" literally instead of left-padding.
        start_s = str(w.start) if w.start else "—"
        end_s = str(w.end) if w.end else "—"
        span = f"{start_s:<10} → {end_s:<10}"
        n_trades = w.trade_count
        exp = w.portfolio_expectancy
        n_strats = sum(1 for s in w.result.by_strategy.values() if s.trades)
        lines.append(
            f"{w.label:<10} {span:<26} {n_trades:>8d} "
            f"{exp:>12.4f} {n_strats:>11d}"
        )
    lines.append("")

    # Per-strategy side-by-side, ranked by train expectancy
    all_strats: set[str] = set()
    for w in report.windows:
        all_strats.update(w.result.by_strategy.keys())

    train = report.windows[0].result

    def _train_exp(name: str) -> float:
        s = train.by_strategy.get(name)
        return s.expectancy if s and s.trades else float("-inf")

    ranked = sorted(all_strats, key=_train_exp, reverse=True)

    lines.append("PER-STRATEGY EXPECTANCY BY WINDOW  (ranked by train)")
    lines.append("-" * width)
    header = f"{'Strategy':<40}"
    for w in report.windows:
        header += f" {w.label[:9] + '_R':>11}"
        header += f" {w.label[:9] + '_n':>9}"
    lines.append(header)
    lines.append("-" * width)

    for name in ranked:
        row = f"{name[:40]:<40}"
        for w in report.windows:
            s = w.result.by_strategy.get(name)
            if s and s.trades:
                row += f" {s.avg_r:>11.3f}"
                row += f" {s.trades:>9d}"
            else:
                row += f" {'—':>11}"
                row += f" {'—':>9}"
        lines.append(row)
    lines.append("")

    # Verdict
    lines.append("=" * width)
    lines.append("VERDICT")
    lines.append("=" * width)

    # Rank stability
    lines.append("")
    lines.append(f"Top-{_TOP_TRAIN_N} TRAIN strategies by expectancy:")
    if report.top_train:
        for i, name in enumerate(report.top_train, 1):
            s = train.by_strategy.get(name)
            exp = s.expectancy if s else 0.0
            lines.append(f"  {i}. {name}  ({exp:+.3f}R, n={s.trades if s else 0})")
    else:
        lines.append("  (no trades in train window)")

    if report.top_validate:
        validate = next(w for w in report.windows if w.label == "validate")
        lines.append("")
        lines.append(f"Top-{_TOP_VALIDATE_N} VALIDATE strategies by expectancy:")
        for i, name in enumerate(report.top_validate, 1):
            s = validate.result.by_strategy.get(name)
            exp = s.expectancy if s else 0.0
            tag = "  ← top-3 train" if name in report.top_train else ""
            lines.append(f"  {i}. {name}  ({exp:+.3f}R, n={s.trades if s else 0}){tag}")

        if report.rank_missing:
            lines.append("")
            lines.append("Top-train strategies MISSING from validate top-5:")
            for name in report.rank_missing:
                lines.append(f"  ✗ {name}")

    lines.append("")
    r_mark = "✓" if report.rank_stable else "✗"
    lines.append(
        f"{r_mark} Rank stability: top-{_TOP_TRAIN_N} train ⊆ top-{_TOP_VALIDATE_N} validate "
        f"→ {report.rank_stable}"
    )

    if report.validate_expectancy_ratio is not None:
        e_mark = "✓" if report.expectancy_stable else "✗"
        lines.append(
            f"{e_mark} Expectancy ratio: validate/train = "
            f"{report.validate_expectancy_ratio:+.3f}  "
            f"(floor ≥ {_EXPECTANCY_FLOOR:.2f})  "
            f"train={report.train_expectancy:+.4f}R → "
            f"validate={report.validate_expectancy:+.4f}R"
        )
    elif report.validate_expectancy is not None:
        lines.append(
            f"✗ Expectancy ratio: train={report.train_expectancy:+.4f}R, "
            f"validate={report.validate_expectancy:+.4f}R "
            "(ratio undefined — train has zero expectancy)"
        )
    else:
        lines.append("✗ Expectancy ratio: no validate trades")

    lines.append("")
    if report.passes:
        lines.append("VERDICT: PASS — ranking + expectancy both held up on unseen bars.")
        lines.append("         The strategy set looks real. Promote to paper-trade next.")
    else:
        lines.append("VERDICT: FAIL — curve-fit suspected.")
        if not report.rank_stable:
            lines.append("         Top-train strategies dropped out of validate top-5.")
        if not report.expectancy_stable:
            lines.append(
                f"         Validate expectancy dropped below "
                f"{int(_EXPECTANCY_FLOOR * 100)}% of train."
            )
        lines.append("         Re-tune on a wider window or prune overfit strategies before going live.")
    lines.append("")

    # Gated-validate block — does the #6 calibration layer generalize?
    # Answers a separate question from rank/expectancy stability: the
    # multiplier table was fit on train trades only; when we apply it
    # blind to the validate window, does it lift performance or drag it?
    if report.gated_validate is not None:
        gv = report.gated_validate
        lines.append("=" * width)
        lines.append("CALIBRATION LAYER HOLDOUT (#7 × #6)")
        lines.append("=" * width)
        lines.append(
            f"Train-only snapshot: {gv.cells_in_snapshot} cells fit on trades "
            f"strictly before {gv.train_end.isoformat()}."
        )
        lines.append(
            f"Applied blind to the {gv.validate_trade_count} validate trades — "
            "no re-fitting on validate, no lookahead."
        )
        lines.append("")

        # Raw (from the windows block up top) for side-by-side reading
        raw_exp = report.validate_expectancy if report.validate_expectancy is not None else 0.0
        lines.append(
            f"{'View':<22} {'Trades':>8} {'AvgR':>10} {'Δ vs raw':>12}"
        )
        lines.append("-" * 54)
        lines.append(
            f"{'raw validate':<22} {gv.validate_trade_count:>8d} "
            f"{raw_exp:>+10.4f} {'—':>12}"
        )

        w_exp = gv.weighted_expectancy
        w_delta = w_exp - raw_exp
        lines.append(
            f"{'weighted (R × mult)':<22} {gv.validate_trade_count:>8d} "
            f"{w_exp:>+10.4f} {w_delta:>+12.4f}"
        )

        g_exp = gv.gated_expectancy
        g_delta = g_exp - raw_exp
        lines.append(
            f"{'gated (mult ≥ 1.0)':<22} {gv.gated_trade_count:>8d} "
            f"{g_exp:>+10.4f} {g_delta:>+12.4f}"
        )
        lines.append("")

        # Coverage — how often the calibration layer actually had something to say
        total = gv.validate_trade_count or 1
        lines.append(
            f"Coverage:  cell={gv.cell_hits} ({gv.cell_hits/total:.0%})  "
            f"strategy_fallback={gv.strategy_fallback_hits} "
            f"({gv.strategy_fallback_hits/total:.0%})  "
            f"no_data={gv.no_data_hits} ({gv.no_data_hits/total:.0%})"
        )
        lines.append(
            f"Gate dropped {gv.gated_dropped_count} of {gv.validate_trade_count} "
            f"validate trades  ({gv.gated_dropped_count/total:.0%})"
        )
        if gv.gated_dropped_strategies:
            lines.append(
                f"Strategies filtered out at least once: "
                f"{', '.join(gv.gated_dropped_strategies)}"
            )

        lines.append("")
        # Calibration-layer generalization read
        if g_delta > 0.01:
            lines.append(
                "Read: calibration LIFTED validate expectancy — the train-only table "
                "picked winners that stayed hot."
            )
        elif g_delta < -0.01:
            lines.append(
                "Read: calibration HURT validate expectancy — the train-only table "
                "picked cells that cooled off. Curve-fit risk in the multiplier layer."
            )
        else:
            lines.append(
                "Read: calibration was ~neutral on validate. Lift within noise — "
                "more trades needed before trusting the layer directionally."
            )
        lines.append("")

    return "\n".join(lines)
