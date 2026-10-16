"""
brier.py — Calibration scoring for Claude's probability estimates (feature #10).

When the analyzer asks Claude for (p_target, p_stop, p_neither) instead of a
vague "confidence" scalar, every closed trade turns into a graded prediction:

    prediction: (p_target, p_stop, p_neither)   — what Claude said up front
    actual:     one-hot ∈ {target, stop, neither}  — what actually happened

This module computes two standard proper scoring rules over that pair:

    - **Brier score** (lower is better):
        BS = (1/K) · Σ_k (p_k − y_k)²     where K=3 classes, y is one-hot
      Range [0, 2]. A perfect predictor scores 0; maximally wrong scores 2.
      An uninformed uniform (1/3, 1/3, 1/3) scores ≈0.44 when the right class
      has probability 1 (baseline).

    - **Log loss** (a.k.a. cross-entropy, lower is better):
        LL = −log(p_actual)               with small eps to avoid log(0)
      Heavily punishes high-confidence wrong answers — saying p=0.01 on what
      turns out to be the actual outcome costs ~4.6, vs. ~0.69 for saying 0.50.

A well-calibrated predictor has:
  - mean Brier ≈ minimal for its accuracy band
  - reliability curve (predicted bucket → empirical hit rate) on the diagonal

This module also owns the **outcome classifier**: given the closed trade's
high/low water marks, target, stop, and exit reason, it assigns one of the
three outcome labels so the ``actual`` vector can be scored.

Degraded paths (all return ``None`` rather than raise):
  - missing probabilities → no score
  - missing price data    → no outcome classification
  - empty trade list      → empty summary
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


# ── Outcome taxonomy ─────────────────────────────────────────────────────────

Outcome = Literal["target_hit", "stop_hit", "neither"]

OUTCOMES: tuple[Outcome, ...] = ("target_hit", "stop_hit", "neither")

# For Brier / log-loss scoring we need a canonical index order. Matching the
# prediction tuple (p_target, p_stop, p_neither) keeps the math readable.
_INDEX: dict[Outcome, int] = {"target_hit": 0, "stop_hit": 1, "neither": 2}


# ── Public types ─────────────────────────────────────────────────────────────

@dataclass
class PredictionScore:
    """Graded result of a single prediction."""
    p_target:   float
    p_stop:     float
    p_neither:  float
    outcome:    Outcome
    brier:      float
    log_loss:   float


@dataclass
class CalibrationSummary:
    """Aggregate calibration stats over a batch of graded predictions."""
    count:           int
    mean_brier:      float | None
    mean_log_loss:   float | None
    # Reliability buckets: key is the p_target bucket ("0.0-0.1", ..., "0.9-1.0"),
    # value is {predicted_mean, empirical_hit_rate, n}. The diagonal (predicted
    # ≈ empirical) is the "well-calibrated" line.
    reliability:     dict[str, dict]
    # Rolling accuracy broken out by predicted outcome
    accuracy_by_class: dict[str, dict]


# ── Scoring primitives ───────────────────────────────────────────────────────

def brier_score(
    p_target: float,
    p_stop: float,
    p_neither: float,
    outcome: Outcome,
) -> float:
    """Brier score: mean squared error vs the one-hot actual, range [0, 2].

    K=3 class Brier is **the sum over classes, mean over predictions**.
    Per the Rufibach convention (commonly used for multi-class Brier),
    we compute the sum of squared differences across all K classes.
    """
    probs = (p_target, p_stop, p_neither)
    idx = _INDEX[outcome]
    target = tuple(1.0 if i == idx else 0.0 for i in range(3))
    return sum((p - y) ** 2 for p, y in zip(probs, target))


def log_loss(
    p_target: float,
    p_stop: float,
    p_neither: float,
    outcome: Outcome,
    eps: float = 1e-9,
) -> float:
    """Log loss (cross-entropy): −log(p_of_actual_class), clipped to avoid log(0)."""
    probs = (p_target, p_stop, p_neither)
    idx = _INDEX[outcome]
    p_actual = max(min(probs[idx], 1.0 - eps), eps)
    return -math.log(p_actual)


def score_prediction(
    p_target: float,
    p_stop: float,
    p_neither: float,
    outcome: Outcome,
) -> PredictionScore:
    """Grade one prediction, returning both Brier + log loss."""
    return PredictionScore(
        p_target=p_target,
        p_stop=p_stop,
        p_neither=p_neither,
        outcome=outcome,
        brier=brier_score(p_target, p_stop, p_neither, outcome),
        log_loss=log_loss(p_target, p_stop, p_neither, outcome),
    )


# ── Outcome classification ───────────────────────────────────────────────────

def classify_outcome(
    *,
    direction: str,                       # "long" | "short"
    target: float | None,                 # target price (parsed)
    stop: float | None,                   # initial stop price (parsed)
    highest_price: float | None,          # high-water mark during hold
    lowest_price: float | None,           # low-water mark during hold
    exit_reason: str | None,              # trailing_stop | max_hold_time | eod_exit | ...
    exit_price: float | None = None,
) -> Outcome | None:
    """Assign one of {target_hit, stop_hit, neither} to a closed trade.

    Classification rules:

    1. **target_hit** — the favorable-extreme price breached the target line
       at any point during the hold. For longs that's ``highest_price >= target``;
       for shorts ``lowest_price <= target``. The actual exit may have been at
       the trailing stop (price spiked to target, retraced, then trailed out)
       — we still call that target-reached, because Claude's prediction
       "P(target before stop)" is about **whether target got touched first**,
       not whether we captured it cleanly. We only track the favorable extreme
       here (longs have ``highest_price``, shorts have ``lowest_price``);
       the adverse extreme isn't needed because it's signaled by the exit.

    2. **stop_hit** — NOT target_hit AND the exit was driven by a stop event
       (``exit_reason == 'trailing_stop'``). The initial stop could have been
       ratcheted to a trailing one by the time it fired, but if the favorable
       extreme never crossed target, the adverse side won.

    3. **neither** — the holding window closed (max_hold_time / eod_exit) or
       the trade was manually closed without either extreme taking out a level.

    Returns ``None`` if we don't have enough data (missing target/stop/prices) —
    caller should skip scoring that trade rather than create a noisy label.
    """
    if direction not in ("long", "short"):
        return None
    if target is None or stop is None:
        return None

    # Favorable-extreme check first: did target ever get touched?
    if direction == "long":
        if highest_price is not None and highest_price >= target:
            return "target_hit"
    else:  # short
        if lowest_price is not None and lowest_price <= target:
            return "target_hit"

    # Target wasn't reached. Look at the exit reason.
    if exit_reason == "trailing_stop":
        return "stop_hit"

    # Time-based or manual close. Label as "neither" — the position ran out
    # the clock without either extreme doing its job.
    if exit_reason in ("max_hold_time", "eod_exit", "manual", "external", None):
        # Final sanity check: if exit_price actually crossed the initial stop
        # (e.g. a hard gap on a manual close), promote to stop_hit to avoid
        # silently mislabeling a stopped-out trade as "neither".
        if exit_price is not None:
            if direction == "long" and exit_price <= stop:
                return "stop_hit"
            if direction == "short" and exit_price >= stop:
                return "stop_hit"
        return "neither"

    # Unknown exit_reason — fall back to neither so we don't invent an outcome.
    logger.debug("brier_classify_unknown_exit_reason reason=%r", exit_reason)
    return "neither"


# ── Aggregation ──────────────────────────────────────────────────────────────

def calibration_summary(
    records: list[dict],
) -> CalibrationSummary:
    """Aggregate calibration over a list of record dicts.

    Each record must have ``p_target``, ``p_stop``, ``p_neither``, and
    ``outcome_class``. Records missing any of those are skipped — typically
    open trades, old rows pre-migration, or ``wait`` recommendations that
    never opened a position.

    The reliability diagram buckets ``p_target`` into 10 deciles and, for
    each bucket, reports the mean predicted probability and the empirical
    target-hit rate. A well-calibrated predictor has the two tracking
    closely across all buckets.
    """
    scored: list[PredictionScore] = []
    for r in records:
        p_t = r.get("p_target")
        p_s = r.get("p_stop")
        p_n = r.get("p_neither")
        oc  = r.get("outcome_class")
        if p_t is None or p_s is None or p_n is None or oc not in OUTCOMES:
            continue
        try:
            scored.append(score_prediction(float(p_t), float(p_s), float(p_n), oc))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            continue

    if not scored:
        return CalibrationSummary(
            count=0,
            mean_brier=None,
            mean_log_loss=None,
            reliability={},
            accuracy_by_class={},
        )

    mean_brier = sum(s.brier for s in scored) / len(scored)
    mean_log   = sum(s.log_loss for s in scored) / len(scored)

    # Reliability diagram — decile buckets over p_target.
    buckets: dict[str, list[PredictionScore]] = {}
    for s in scored:
        bucket_idx = min(int(s.p_target * 10), 9)
        key = f"{bucket_idx/10:.1f}-{(bucket_idx+1)/10:.1f}"
        buckets.setdefault(key, []).append(s)

    reliability: dict[str, dict] = {}
    for key in sorted(buckets.keys()):
        bs = buckets[key]
        reliability[key] = {
            "n": len(bs),
            "predicted_mean": round(sum(b.p_target for b in bs) / len(bs), 4),
            "empirical_hit_rate": round(
                sum(1 for b in bs if b.outcome == "target_hit") / len(bs), 4,
            ),
        }

    # Per-actual-outcome breakdown — lets the UI highlight "Claude says target
    # 70% of the time but only 45% actually hit" vs "stop predictions land".
    by_class: dict[str, dict] = {}
    for oc in OUTCOMES:
        in_class = [s for s in scored if s.outcome == oc]
        if not in_class:
            continue
        p_idx = _INDEX[oc]
        p_for_actual = [
            (s.p_target, s.p_stop, s.p_neither)[p_idx] for s in in_class
        ]
        by_class[oc] = {
            "n": len(in_class),
            "mean_p_for_actual": round(sum(p_for_actual) / len(p_for_actual), 4),
        }

    return CalibrationSummary(
        count=len(scored),
        mean_brier=round(mean_brier, 4),
        mean_log_loss=round(mean_log, 4),
        reliability=reliability,
        accuracy_by_class=by_class,
    )


def summary_dict(s: CalibrationSummary) -> dict:
    """JSON-safe dict for websockets / dashboard / logs."""
    return {
        "count":             s.count,
        "mean_brier":        s.mean_brier,
        "mean_log_loss":     s.mean_log_loss,
        "reliability":       s.reliability,
        "accuracy_by_class": s.accuracy_by_class,
    }


# ── Feedback-loop multiplier ─────────────────────────────────────────────────

def calibration_multiplier(
    p_target: float | None,
    summary: CalibrationSummary | None,
    *,
    min_bucket_n: int = 10,
    min_mult: float = 0.5,
    max_mult: float = 1.2,
) -> tuple[float, dict]:
    """Brier-derived confidence multiplier — closes the feedback loop for #10.

    Claude's ``p_target`` is bucketed into the same deciles used by the
    reliability diagram. If that bucket holds enough graded trades to be
    trusted (``n >= min_bucket_n``), we return the ratio of **empirical hit
    rate to predicted mean**, clamped to ``[min_mult, max_mult]``:

        multiplier = clamp(empirical_hit_rate / predicted_mean)

    This is the "if Claude says 80% and actually hits 60%, adjust" step —
    a 0.85-predicted bucket that empirically hits at 0.60 yields a 0.706×
    multiplier, correctly damping the next over-confident 0.80+ call toward
    the historical hit rate. A bucket that consistently outperforms
    (predicted 0.65 → empirical 0.80) gets boosted up to the 1.2× ceiling.

    Degraded-no-op semantics (returns 1.0) when:
      • ``p_target`` is None or out of range — trade has no probability to
        bucket by.
      • ``summary`` is None — no calibration snapshot yet (pre-start,
        refresh failed).
      • The matching decile bucket is missing — nothing from the last
        30 days landed in that confidence band.
      • Bucket has fewer than ``min_bucket_n`` trades — empirical rate is
        too noisy to trust. n=10 is a practical floor for a single-user
        setup; ~30 would be statistically cleaner but would keep the
        multiplier dormant for weeks.
      • ``predicted_mean`` is zero (degenerate — shouldn't happen with
        normalized Pydantic output, but the guard prevents a zero-divide).

    The clamp is deliberately narrow:
      • ``min_mult=0.5`` caps the damp at 2× — a single bad streak can't
        zero-out otherwise-valid setups. Downstream, combined with the
        0.80 gate, a 0.5× multiplier typically drops a candidate from
        consideration anyway.
      • ``max_mult=1.2`` caps the boost at 20% — prevents a lucky streak
        in a low-sample bucket from promoting marginal 0.70 calls into
        the 0.80+ action zone.

    Composes multiplicatively with #6 (per-strategy), #8 (regime), and #9
    (news catalyst). Final scan-loop product is::

        confidence = raw × cal × regime × news × brier_cal  (clamped to [0,1])

    Returns ``(multiplier, metadata)`` — metadata always includes ``reason``
    so the approval UI can explain *why* the multiplier is what it is
    ("Claude's 0.8-0.9 bucket hits at 60%, n=24 → damped 0.71×").
    """
    if p_target is None or not 0.0 <= float(p_target) <= 1.0:
        return 1.0, {"reason": "no_prob", "multiplier": 1.0}
    if summary is None:
        return 1.0, {"reason": "no_summary", "multiplier": 1.0}

    # Identical decile convention to ``calibration_summary``.
    bucket_idx = min(int(float(p_target) * 10), 9)
    bucket_key = f"{bucket_idx/10:.1f}-{(bucket_idx+1)/10:.1f}"
    bucket = summary.reliability.get(bucket_key)
    if not bucket:
        return 1.0, {"reason": "no_bucket", "bucket": bucket_key, "multiplier": 1.0}

    try:
        n = int(bucket.get("n", 0))
        predicted = float(bucket["predicted_mean"])
        empirical = float(bucket["empirical_hit_rate"])
    except (KeyError, TypeError, ValueError):
        return 1.0, {"reason": "bucket_malformed", "bucket": bucket_key, "multiplier": 1.0}

    if n < min_bucket_n:
        return 1.0, {
            "reason": "low_n", "bucket": bucket_key, "n": n,
            "predicted_mean": round(predicted, 4),
            "empirical_hit_rate": round(empirical, 4),
            "multiplier": 1.0,
        }
    if predicted <= 0.0:
        return 1.0, {
            "reason": "zero_predicted", "bucket": bucket_key, "n": n,
            "multiplier": 1.0,
        }

    raw_ratio = empirical / predicted
    clamped = max(min_mult, min(max_mult, raw_ratio))
    return clamped, {
        "reason": "bucket",
        "bucket":              bucket_key,
        "predicted_mean":      round(predicted, 4),
        "empirical_hit_rate":  round(empirical, 4),
        "n":                   n,
        "raw_ratio":           round(raw_ratio, 4),
        "multiplier":          round(clamped, 4),
    }


# ── DB-aware helpers ─────────────────────────────────────────────────────────

def calibration_summary_from_db(
    user_id: int,
    *,
    days: int = 30,
    min_trades: int = 5,
) -> CalibrationSummary:
    """Pull recent closed trades for a user and compute the calibration summary.

    Kept here (rather than in ``trade_history.py``) so the Brier logic lives in
    one place. Returns an empty summary if the user has fewer than ``min_trades``
    graded trades in the window — small-N aggregate Brier is noisy.
    """
    from datetime import datetime, timedelta, timezone  # local to avoid cycles
    from sqlalchemy import desc, select

    from .db import SessionLocal
    from .models import TradeRecord

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        q = (
            select(TradeRecord)
            .where(
                TradeRecord.user_id == user_id,
                TradeRecord.closed_at.is_not(None),
                TradeRecord.opened_at >= cutoff,
            )
            .order_by(desc(TradeRecord.closed_at))
        )
        records = db.scalars(q).all()

        graded = [
            {
                "p_target":      r.p_target,
                "p_stop":        r.p_stop,
                "p_neither":     r.p_neither,
                "outcome_class": r.outcome_class,
            }
            for r in records
            if r.p_target is not None and r.outcome_class is not None
        ]
        if len(graded) < min_trades:
            return CalibrationSummary(
                count=len(graded), mean_brier=None, mean_log_loss=None,
                reliability={}, accuracy_by_class={},
            )
        return calibration_summary(graded)
    finally:
        db.close()
