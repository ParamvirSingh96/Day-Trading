"""
strategy_backtest.py — Walk-forward replay of the live PLAYBOOK.
=================================================================
Replays each strategy in ``strategies.PLAYBOOK`` across historical
1-minute bars and simulates entry / stop / target with a *uniform*
ATR-based model. The point is to answer the only question that
matters before launch: **which strategies actually make money?**

The simulation is deliberately uniform (same entry rule, same stop
distance, same target distance for every strategy) so differences
in the resulting stats reflect the edge of each pre-screen predicate,
not quirks of how each strategy's stop wording happens to map onto
price. Per-strategy stop rules in ``strategies.py`` are what Claude
uses at Tier 2; at Tier 1 we just want a fair horse race.

Output: per-strategy stats (trades, win%, avg R, profit factor,
max-drawdown-R) plus a per-strategy × hour-of-day breakdown so
"X works at 09:30 ET but loses money after lunch" is visible.

Historical 1-min bars are downloaded from Alpaca and cached as
gzipped JSON under ``Backend/data/bars_cache/<SYMBOL>/<YYYY-MM-DD>.json.gz``
so re-runs don't re-hit the wire. JSON keeps the cache inspectable
with standard tools and avoids pulling in pyarrow just for parquet.

Usage:
    from app.strategy_backtest import run_backtest, format_report
    result = run_backtest(
        symbols=["AAPL","TSLA","NVDA"],
        start=date(2025, 10, 1),
        end=date(2026, 4, 1),
    )
    print(format_report(result))

For the CLI see ``Backend/scripts/run_strategy_backtest.py``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import os

from .alpaca import AlpacaBarsRequest, AlpacaMarketDataClient, convert_alpaca_bars
from .analyzer import compute_indicators
from .ict import compute_ict_features
from .regime import classify as classify_regime
from .settings import settings
from .strategies import PLAYBOOK, Strategy, screen

# ── Live↔backtest parity diagnostic (2026-06-04) ───────────────────────────
# When ICT_PARITY_DIAG=1, every backtest predicate match ALSO recomputes the
# `ind` the LIVE scanner would have built from the SAME in-memory session
# bars — an 8h-ending-at-this-bar slice (live's `_fetch_bars` window) + the
# live daily-ctx (prev_* only, NO 20d_high/20d_low) — runs `screen()` on it,
# and appends a parity row to data/research/parity_diag.jsonl. Both sides come
# from the backtester's real pipeline; only the window-slice + seed differ
# (the variables under test). Default OFF → normal backtests are unchanged.
# Pure research diagnostic; never touches live trading.
_PARITY_DIAG = os.getenv("ICT_PARITY_DIAG") == "1"
# Per-run output file (run_id from env) so a fresh run never appends onto a
# stale one. Set ICT_PARITY_RUN_ID per launch; defaults to "adhoc".
_PARITY_RUN_ID = os.getenv("ICT_PARITY_RUN_ID", "adhoc")
_PARITY_PATH = Path(f"data/research/parity_diag_{_PARITY_RUN_ID}.jsonl")
_PARITY_LIVE_LOOKBACK_S = 8 * 3600  # mirrors autotrade._LOOKBACK_HOURS = 8
_parity_errors = 0  # probe failures counted + logged, never silently swallowed
# When set, the probe's live-ctx ALSO includes 20d_high/20d_low — i.e. it
# models the FIXED live scanner (post-2026-06-04 _get_daily_ctx_map). Lets us
# re-run the harness and confirm Multi-Day PD reproduction jumps 0% -> ~100%
# WITHOUT touching/restarting the live trading path.
_PARITY_FIX_20D = os.getenv("ICT_PARITY_FIX_20D") == "1"
# ICT_PARITY_WINDOW selects how the probe INDEPENDENTLY constructs the live
# window from bars[:i+1] — never by copying the backtester's `window` (that
# would be circular: feeding back the very slice that already matched). Four
# arms decompose the proposed fix into its two separable parts (fetch anchor ×
# 200-bar cap), each built from scratch:
#   current    — 8h ending at bar, uncapped      (= live today)
#   caponly    — 8h ending at bar, last 200       (isolates the 200-bar cap)
#   anchoronly — prior 18:00 ET → bar, uncapped   (isolates added overnight history)
#   proposed   — prior 18:00 ET → bar, last 200   (the combined proposed fix)
# Default "current". Research-only; never touches live.
_PARITY_WINDOW_MODE = os.getenv("ICT_PARITY_WINDOW", "current")
_parity_ict_errors = 0          # compute_ict_features failures — counted, never swallowed
_parity_stop_errors = 0         # structural_stop failures in the probe — counted (Finding 4)
_parity_symbols_seen: set = set()  # symbol-coverage witness for the report's gate


def _prior_1800_et_epoch(bar_t: float) -> float:
    """Epoch(s) of the most recent 18:00 ET boundary at or before ``bar_t``.

    Models a live session-anchored fetch (the overnight session opens 18:00
    ET). Clock-based, NOT trading-session-aware — exactly what a simple
    ``_fetch_bars`` anchor change would compute. Any weekend/holiday divergence
    from the backtester's session cache is therefore VISIBLE via the eq_bt
    witness in each row, never hidden.
    """
    dt_et = datetime.fromtimestamp(bar_t, tz=timezone.utc).astimezone(_ET)
    anchor = dt_et.replace(hour=18, minute=0, second=0, microsecond=0)
    if dt_et < anchor:
        anchor -= timedelta(days=1)
    return anchor.timestamp()


def parity_diag_summary() -> dict:
    """Run-level diagnostic totals for the report's completion / error gate."""
    return {
        "run_id": _PARITY_RUN_ID,
        "window_mode": _PARITY_WINDOW_MODE,
        "fix20d": _PARITY_FIX_20D,
        "probe_errors": _parity_errors,
        "ict_feature_errors": _parity_ict_errors,
        "stop_errors": _parity_stop_errors,
        "symbols_seen": sorted(_parity_symbols_seen),
        "output": str(_PARITY_PATH),
    }


# ── Counterfactual replay (research-only) ──────────────────────────────────
# ICT_REPLAY_WINDOW makes the BACKTESTER ITSELF build its indicator window per
# the named mode instead of the native bars[:i+1][-200:]. Unlike the read-only
# parity probe (which only fires AFTER the native window already matched, and so
# is blind to NEW matches), this drives the FULL pipeline — matching, structural
# stops, per-model cooldown, trade simulation — so each arm yields its OWN trades
# CSV: new false-positive entries, removed entries, cooldown interactions, and
# each arm's own simulated R. Default None = native (validated +42.72R baseline,
# byte-identical). `bars` is the T-1 18:00→T 16:00 session, so native == proposed
# (prior-18:00, last-200) — the replay's self-correctness gate. Never touches live.
_REPLAY_WINDOW_MODE = os.getenv("ICT_REPLAY_WINDOW")
_replay_symbols_processed: set = set()
_replay_sessions_processed: set = set()  # (symbol, day) pairs — full session-coverage witness (Finding 5)
_REPLAY_HASH_BARS = False  # run_replay_arm flips this on so manifests hash actual bar PAYLOADS (Finding 1)
_replay_session_bar_sha: dict = {}  # (symbol, day) -> sha over that session's OHLCV bars


def _window_for_mode(bars, i, mode):
    """Indicator window for bar ``i``. ``mode`` None → native ``bars[:i+1][-200:]``.
    Counterfactual arms build INDEPENDENTLY from ``bars[:i+1]``:
      current/caponly     = 8h-ending slice (uncapped / last-200)
      anchoronly/proposed = prior-18:00 ET slice (uncapped / last-200)
    """
    prefix = bars[: i + 1]
    if not mode:
        return prefix[-200:]
    bar_t = bars[i]["time"]
    if mode == "current":
        return [b for b in prefix if b["time"] >= bar_t - _PARITY_LIVE_LOOKBACK_S]
    if mode == "caponly":
        return [b for b in prefix if b["time"] >= bar_t - _PARITY_LIVE_LOOKBACK_S][-200:]
    if mode == "anchoronly":
        return [b for b in prefix if b["time"] >= _prior_1800_et_epoch(bar_t)]
    if mode == "proposed":
        return [b for b in prefix if b["time"] >= _prior_1800_et_epoch(bar_t)][-200:]
    raise ValueError(f"unknown ICT_REPLAY_WINDOW={mode!r}")


def _session_bar_sha(bars) -> str:
    """SHA256 over a session's actual OHLCV bar payloads (Finding 1 — proves arms
    processed identical bar CONTENT, not merely identical session names)."""
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b['time']},{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']};".encode())
    return h.hexdigest()


def replay_diag_summary() -> dict:
    """Coverage + data-identity witnesses for the replay report's gate: symbols/
    sessions PROCESSED (Findings 3/5) and, when bar-hashing is on, a hash over the
    actual bar payloads (Finding 1)."""
    sess = sorted(f"{s}|{d}" for s, d in _replay_sessions_processed)
    bars_sha = None
    if _replay_session_bar_sha:
        items = sorted(f"{s}|{d}|{h}" for (s, d), h in _replay_session_bar_sha.items())
        bars_sha = hashlib.sha256("\n".join(items).encode()).hexdigest()
    return {
        "replay_window_mode": _REPLAY_WINDOW_MODE,
        "symbols_processed": sorted(_replay_symbols_processed),
        "n_sessions_processed": len(sess),
        "sessions_sha": hashlib.sha256("\n".join(sess).encode()).hexdigest(),
        "bars_sha": bars_sha,
    }


def _parity_probe(strat, ind_bt, window, i, bars, day_ctx, symbol, day):
    """Recompute the LIVE-path ind on the same bars; record divergence.

    NOTE — this fires at the PREDICATE-MATCH level (right after
    ``strat.matches(ind)``), before cooldown / entry-resolution /
    ``_simulate_trade``. So each row is a *predicate-match* parity record,
    NOT a *recorded-trade* parity record. Rows carry ``bar_ts_et`` + ``i``
    so trade-survival can be joined post-hoc against the trades CSV. The
    ``level`` field labels this explicitly.
    """
    global _parity_errors, _parity_ict_errors, _parity_stop_errors
    try:
        _parity_symbols_seen.add(symbol)
        bar_t = bars[i]["time"]
        prefix = bars[: i + 1]
        # INDEPENDENT live-window construction per arm — built from bars[:i+1],
        # NEVER `list(window)` (that would be circular — Finding 1).
        lb8 = [b for b in prefix if b["time"] >= bar_t - _PARITY_LIVE_LOOKBACK_S]
        if _PARITY_WINDOW_MODE == "current":
            live_window = lb8                        # 8h, uncapped (= live today)
        elif _PARITY_WINDOW_MODE == "caponly":
            live_window = lb8[-200:]                 # 8h, last 200
        elif _PARITY_WINDOW_MODE == "anchoronly":
            live_window = [b for b in prefix if b["time"] >= _prior_1800_et_epoch(bar_t)]
        elif _PARITY_WINDOW_MODE == "proposed":
            live_window = [b for b in prefix if b["time"] >= _prior_1800_et_epoch(bar_t)][-200:]
        else:
            raise ValueError(f"unknown ICT_PARITY_WINDOW={_PARITY_WINDOW_MODE!r}")
        if len(live_window) < 30:
            return
        # Live daily-ctx mirror: prev_* + avg_daily_volume. By default NO
        # 20d_high/low (models CURRENT live). With ICT_PARITY_FIX_20D=1, add
        # them (models the FIXED live ctx) to verify the Multi-Day PD recovery.
        _keys = ["prev_close", "prev_high", "prev_low", "avg_daily_volume"]
        if _PARITY_FIX_20D:
            _keys += ["20d_high", "20d_low"]
        live_ctx = {k: (day_ctx or {}).get(k) for k in _keys}
        live_ind = compute_indicators(
            live_window, prev_close=live_ctx["prev_close"],
            prev_high=live_ctx["prev_high"], prev_low=live_ctx["prev_low"],
            avg_daily_volume=live_ctx["avg_daily_volume"])
        ict_err = 0
        try:
            live_ind.update(compute_ict_features(live_window, live_ctx))
        except Exception as exc:  # count + log — never swallow (Finding 5)
            ict_err = 1
            _parity_ict_errors += 1
            if _parity_ict_errors <= 5 or _parity_ict_errors % 50 == 0:
                logger.warning("parity_ict_features_failed n=%d symbol=%s day=%s err=%s",
                               _parity_ict_errors, symbol, day, exc)
        live_ind["symbol"] = symbol
        live_ind["recent_bars"] = live_window
        live_names = sorted(s.name for s in screen(live_ind))
        live_matched = strat.name in live_names
        try:  # decision feature — structural stop drives R + position sizing (Finding 2)
            live_stop = strat.structural_stop(live_ind)
        except Exception as exc:  # count + log — never swallow (Finding 4)
            live_stop = None
            _parity_stop_errors += 1
            if _parity_stop_errors <= 5 or _parity_stop_errors % 50 == 0:
                logger.warning("parity_stop_failed n=%d symbol=%s day=%s strat=%s err=%s",
                               _parity_stop_errors, symbol, day, strat.name, exc)
        bar_ts_et = datetime.fromtimestamp(bar_t, tz=timezone.utc).astimezone(_ET).isoformat()

        def age(d, k):
            v = d.get(k)
            return v.get("bars_ago") if isinstance(v, dict) else None

        row = {
            "level": "predicate_match",
            "symbol": symbol, "day": str(day), "bar_ts_et": bar_ts_et, "i": i,
            "strategy": strat.name,
            "live_matched": live_matched,
            "live_matched_names": live_names,
            "bt_pd": ind_bt.get("ict_pd_zone"), "lv_pd": live_ind.get("ict_pd_zone"),
            "bt_mdpd": ind_bt.get("ict_multi_day_pd_zone"), "lv_mdpd": live_ind.get("ict_multi_day_pd_zone"),
            "bt_bull_sw": age(ind_bt, "ict_last_bull_sweep"), "lv_bull_sw": age(live_ind, "ict_last_bull_sweep"),
            "bt_bull_mss": age(ind_bt, "ict_last_bull_mss"), "lv_bull_mss": age(live_ind, "ict_last_bull_mss"),
            "bt_bear_sw": age(ind_bt, "ict_last_bear_sweep"), "lv_bear_sw": age(live_ind, "ict_last_bear_sweep"),
            "bt_bear_mss": age(ind_bt, "ict_last_bear_mss"), "lv_bear_mss": age(live_ind, "ict_last_bear_mss"),
            "n_bt": len(window), "n_lv": len(live_window),
            "mode": _PARITY_WINDOW_MODE,
            "cfg": _PARITY_WINDOW_MODE + ("/fix20d" if _PARITY_FIX_20D else "/base"),
            "err": ict_err,
            "atr": live_ind.get("atr"),
            "stop": live_stop,
            "lv_start": (live_window[0]["time"] if live_window else None),
            # eq_bt: did this arm's INDEPENDENT window land on the backtester's
            # own `window`? The non-circularity witness — for `proposed` it tells
            # us how often the clean 18:00 anchor truly reproduces the backtester
            # (and exposes weekend/holiday edges where it does not).
            "eq_bt": (len(live_window) == len(window) and bool(window)
                      and live_window[0]["time"] == window[0]["time"]
                      and live_window[-1]["time"] == window[-1]["time"]),
        }
        with open(_PARITY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as exc:
        # Count + log — a research diagnostic must not fail silently.
        _parity_errors += 1
        if _parity_errors <= 5 or _parity_errors % 50 == 0:
            logger.warning(
                "parity_probe_failed n=%d symbol=%s day=%s strat=%s err=%s",
                _parity_errors, symbol, day, getattr(strat, "name", "?"), exc,
                exc_info=(_parity_errors <= 3),
            )

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
# Cache directory bumped 2026-04-26 from `bars_cache` → `bars_cache_ext`
# alongside the session-window extension that pulls in overnight (18:00
# prev → 04:00 ET) bars. The old `bars_cache/` only stored 09:30-16:00
# ET so loading it under the new code would silently leave Asian-Range
# Breakout broken (no overnight = no asian_high/low). New dir = forced
# re-fetch on first run with the wider window. Old dir is preserved for
# rollback if needed.
#
# v7.13a (2026-05-01): cache dir is now feed-aware
# (``bars_cache_ext_sip/`` vs ``bars_cache_ext_iex/``) so the SIP-vs-IEX
# parity test can keep both caches side-by-side, and so flipping
# ALPACA_DATA_FEED in .env never silently mixes feeds in the same
# parquet file. Resolved at function call time via _cache_dir() so the
# env can be flipped between back-to-back script runs without restart.

def _cache_dir(feed: str | None = None) -> Path:
    """Return the active per-feed bars cache directory.

    ``feed`` (None default) lets the parity test pass an explicit
    feed without juggling environment variables — None falls back to
    ``settings.alpaca_data_feed``. Falls back to the legacy unsuffixed
    ``bars_cache_ext`` only when reading SIP cache AND a pre-v7.13a
    parquet file already exists there — keeps audited v7.12.1 caches
    loadable without forced re-fetch.
    """
    base = Path(__file__).resolve().parent.parent / "data"
    resolved = (feed or settings.alpaca_data_feed or "sip").lower()
    suffixed = base / f"bars_cache_ext_{resolved}"
    legacy = base / "bars_cache_ext"
    # If the suffixed dir already has data, use it. Otherwise, if the
    # legacy unsuffixed dir has data AND we're on the historical
    # default (sip), prefer that to avoid forcing a re-pull of every
    # symbol-day on first run after upgrade. New (iex) feed always
    # uses the suffixed dir.
    if suffixed.exists() and any(suffixed.iterdir()):
        return suffixed
    if resolved == "sip" and legacy.exists() and any(legacy.iterdir()):
        return legacy
    return suffixed


# Backwards-compat: some scripts import _CACHE_DIR directly. Resolve at
# module import time using the current setting; per-call resolution
# inside fetch_history goes through _cache_dir() to stay env-responsive.
_CACHE_DIR = _cache_dir()

# ── Simulation parameters (uniform across strategies) ──────────────────────
# These are intentionally one set of numbers, not per-strategy. Goal is a
# fair horse-race across the PLAYBOOK. Tune here if you want a different
# risk profile, but don't move them per strategy — that defeats the point.
WARMUP_BARS = 15              # 1-min bars before we try to fire any predicate.
                              # Lowered 60 → 15 on 2026-04-26 so Judas Swing
                              # (gated to ``ict_rth_minute < 30``) can fire —
                              # at 60 the first eligible entry was 10:30 ET,
                              # already past the 09:30-10:00 Judas window. 15
                              # matches the live scanner floor and the
                              # compute_ict_features minimum, so backtest
                              # behavior tracks production. Classic indicators
                              # (EMA20, ATR14) won't have a full window for
                              # the first ~5 bars but the post-2026-04-24
                              # ICT-only pivot means they only feed
                              # stop/target sizing, not strategy matching.
# Stop / target distances — v2 tuned for 1-min bars. v1 used 1.0 / 2.0, which
# had a 68-78% stop-out rate across every strategy (stops = typical bar noise).
# Widening stop to 1.5× ATR cuts chop-out without changing R:R much (1.67 vs 2.0).
STOP_ATR_MULT = 1.5           # stop = 1.5× ATR from entry
TARGET_ATR_MULT = 2.5         # target = 2.5× ATR from entry → 1.67R reward-to-risk
MAX_HOLD_BARS = 60            # force-close after 60 minutes (1 hour)
COOLDOWN_BARS = 15            # after a trade closes, skip same strategy for 15 bars
ENTRY_SLIPPAGE_PCT = 0.0002   # 2 bps slippage on entry (market order)
EXIT_SLIPPAGE_PCT = 0.0002    # 2 bps slippage on exit

# Entry window = regular session, cut off 30 min before close so there is room
# for the stop/target to resolve before EOD forces a timeout.
_SESSION_OPEN = time(9, 30)
_SESSION_ENTRY_CUTOFF = time(15, 30)


# ── Types ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeExitModel:
    """One stop/target/entry-timing model to replay against the same signal.

    The Phase-2 work needs a clean separation between "did the setup fire?"
    and "did our exit assumptions make sense?" Keeping the model explicit lets
    us run a stop matrix without changing the underlying strategy predicates.
    """

    name: str
    stop_atr_mult: float = STOP_ATR_MULT
    target_atr_mult: float = TARGET_ATR_MULT
    max_hold_bars: int = MAX_HOLD_BARS
    partial_take_r: float | None = None
    partial_fraction: float = 0.0
    move_stop_to_breakeven: bool = False
    confirmation_bars: int = 0
    # Minimum stop distance, expressed as a multiple of ATR, used ONLY
    # when a Strategy supplies a structural stop anchor via
    # ``structural_stop_fn``. The simulator places the stop at
    # ``max(structural_distance_from_entry, atr * stop_atr_floor)`` so a
    # structural level that's two ticks from entry (the typical "stop
    # one tick beyond the swept wick" case after a sharp breakout-fail)
    # still has a sane minimum size — i.e. we don't get clipped by
    # noise on the entry bar itself. 0.5 = half the baseline 1.5×ATR
    # stop, which is the floor the post-fail wick distances cluster
    # around in the Phase-2 sample. Ignored when ``structural_stop_fn``
    # is None (ATR-only stops use ``stop_atr_mult`` exclusively).
    # Added v7.7 (2026-04-27).
    stop_atr_floor: float = 0.5

    # ── Trailing-stop research fields (2026-06-10, exit-parity replay) ────
    # ALL default-inert: models without trail fields keep byte-identical
    # behavior (proven by the canonical 126/+55.3268R gate). Semantics mirror
    # live's monitor: the trail SEEDS AT the model's stop (live seeds
    # trailing_stop=initial_stop) and only ever tightens; distance is
    # ``trail_pct`` of the favorable extreme (live: price*(1±pct)) or
    # ``trail_atr_mult``×entry-ATR. ``trail_activate_r`` arms the ratchet only
    # after favorable excursion ≥ X·R (0 = armed from entry, like live).
    # ``trail_replaces_target=True`` disables target enforcement (live never
    # takes the target). 1-min bars cannot reveal whether the favorable or
    # adverse extreme printed first, so every trail model must be run under
    # BOTH ``trail_ordering`` policies and reported as {conservative,
    # optimistic} BOUNDS — bar-local ordering brackets, not a global proof.
    # Trail models do not support partials (partials are force-disabled).
    trail_pct: float | None = None
    trail_atr_mult: float | None = None
    trail_activate_r: float = 0.0
    trail_replaces_target: bool = False
    trail_ordering: str = "conservative"
    # live_policy_approx extras (2026-06-10): ``fixed_stop_pct`` anchors the
    # stop a fixed % from the SIGNAL bar's close (live anchors 0.7% at the
    # scan-time price — labeled approximation). ``trail_price_basis="closes"``
    # is the SAMPLED-trail approximation: live polls ~10s and misses intrabar
    # extremes, so only bar closes are treated as observed — ratchet from the
    # close, breach-test the same close, exit AT the observed price (fills can
    # be well through the trail level, like live's market closes). With one
    # observed price per bar the cons/opti ordering question vanishes.
    # ``time_exit_priority`` mirrors live's monitor order (time exit checked
    # BEFORE the trail) at the max-hold boundary.
    fixed_stop_pct: float | None = None
    trail_price_basis: str = "extremes"
    time_exit_priority: bool = False

    # ── Re-match management research fields (2026-06-19) ──────────────────
    # ALL default-inert: with every field at its default the simulator is
    # byte-identical (proven by the canonical 126/+55.3268R gate). A
    # "re-match" = a later DISTINCT same-strategy predicate fire (cooldown-
    # spaced, ≥ COOLDOWN_BARS apart) that lands while a position is still
    # open — i.e. the setup re-confirms. These model re-confirmation as a
    # position-MANAGEMENT trigger; the entry set stays the validated
    # baseline (entry-identity gate enforces it). The firing bars are
    # supplied to ``_simulate_trade`` via ``rematch_indices``.
    #   rematch_extend_hold_bars — on a re-match, extend the max-hold horizon
    #     to at least (rematch_bar + N), capped at session end. 0 = inert.
    #     Models "hold longer while the setup keeps re-confirming."
    #   rematch_move_stop_to_be / rematch_be_buffer_pct — on a re-match, if
    #     price is already beyond entry, ratchet the stop up to
    #     entry*(1±buffer) ("a little over entry"), only ever tightening.
    #     Acts the bar AFTER the signal (next open) so there is no
    #     look-ahead. Models "protect profit on re-confirmation."
    rematch_extend_hold_bars: int = 0
    rematch_move_stop_to_be: bool = False
    rematch_be_buffer_pct: float = 0.0


DEFAULT_EXIT_MODEL = TradeExitModel(
    name="baseline_s15_t25",
)


def default_exit_model_matrix() -> list[TradeExitModel]:
    """Ranked Phase-2 exit experiments.

    Names encode stop/target as ATR multiples: ``s15_t25`` means
    1.5xATR stop and 2.5xATR target. The order mirrors the current research
    ranking: stop model first, then smaller targets/partials, then
    post-touch confirmation.
    """

    return [
        DEFAULT_EXIT_MODEL,
        TradeExitModel(name="s15_t15", stop_atr_mult=1.5, target_atr_mult=1.5),
        TradeExitModel(name="s15_t20", stop_atr_mult=1.5, target_atr_mult=2.0),
        TradeExitModel(name="s20_t20", stop_atr_mult=2.0, target_atr_mult=2.0),
        TradeExitModel(name="s20_t25", stop_atr_mult=2.0, target_atr_mult=2.5),
        TradeExitModel(name="s20_t30", stop_atr_mult=2.0, target_atr_mult=3.0),
        TradeExitModel(name="s25_t25", stop_atr_mult=2.5, target_atr_mult=2.5),
        TradeExitModel(
            name="s15_t25_partial1r50_be",
            stop_atr_mult=1.5,
            target_atr_mult=2.5,
            partial_take_r=1.0,
            partial_fraction=0.5,
            move_stop_to_breakeven=True,
        ),
        TradeExitModel(
            name="s15_t20_partial1r50_be",
            stop_atr_mult=1.5,
            target_atr_mult=2.0,
            partial_take_r=1.0,
            partial_fraction=0.5,
            move_stop_to_breakeven=True,
        ),
        TradeExitModel(
            name="s20_t25_partial1r50_be",
            stop_atr_mult=2.0,
            target_atr_mult=2.5,
            partial_take_r=1.0,
            partial_fraction=0.5,
            move_stop_to_breakeven=True,
        ),
        TradeExitModel(
            name="confirm1_s15_t25",
            stop_atr_mult=1.5,
            target_atr_mult=2.5,
            confirmation_bars=1,
        ),
        TradeExitModel(
            name="confirm2_s15_t25",
            stop_atr_mult=1.5,
            target_atr_mult=2.5,
            confirmation_bars=2,
        ),
        TradeExitModel(
            name="confirm1_s15_t20_partial1r50_be",
            stop_atr_mult=1.5,
            target_atr_mult=2.0,
            partial_take_r=1.0,
            partial_fraction=0.5,
            move_stop_to_breakeven=True,
            confirmation_bars=1,
        ),
    ]


def _build_exit_models_registry() -> dict[str, TradeExitModel]:
    """Map exit-model name → TradeExitModel for per-strategy overrides.

    Strategies in ``app/strategies.py`` declare a preferred exit model
    via ``Strategy.exit_model_name`` (a string, not a TradeExitModel
    instance, to avoid a circular import — ``strategy_backtest.py``
    imports PLAYBOOK from there). The backtester resolves the string
    against this registry at replay time. Includes
    ``DEFAULT_EXIT_MODEL`` plus everything in
    ``default_exit_model_matrix()``; unknown names fall back to the
    backtester's caller-supplied default with a logged warning.

    Added v7.7 (2026-04-27).
    """
    registry: dict[str, TradeExitModel] = {DEFAULT_EXIT_MODEL.name: DEFAULT_EXIT_MODEL}
    for m in default_exit_model_matrix():
        registry[m.name] = m
    return registry


EXIT_MODELS: dict[str, TradeExitModel] = _build_exit_models_registry()

@dataclass
class Trade:
    strategy: str
    symbol: str
    direction: str                          # "long" | "short"
    exit_model: str
    entry_ts: datetime                      # in America/New_York
    entry_price: float
    stop_price: float
    target_price: float
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None          # "target" | "stop" | "timeout" | "eod"
    r_multiple: float | None = None
    pnl_pct: float | None = None
    risk_per_share: float | None = None
    mae_r: float | None = None              # maximum adverse excursion, in R
    mfe_r: float | None = None              # maximum favorable excursion, in R
    max_adverse_price: float | None = None
    max_favorable_price: float | None = None
    partial_taken: bool = False
    partial_exit_ts: datetime | None = None
    partial_exit_price: float | None = None
    partial_fraction: float | None = None
    entry_delay_bars: int = 0
    confirmation_bars: int = 0
    # Structural-stop telemetry (added 2026-04-27, v7.7).
    #
    # The simulator silently falls back to an ATR-only stop when the
    # strategy supplied no ``structural_stop_fn``, when the function
    # returned None, or when the anchor price is on the wrong side of
    # entry. Without per-trade telemetry that fallback is invisible —
    # the experiment scoreboard would conflate "structural stop
    # worked" with "structural stop wasn't applied". These three
    # fields make the choice auditable in the per-trade CSV so the
    # factorial inversion matrix can attribute lift to the stop model
    # independently from predicate / direction.
    #
    # ``structural_stop_used`` — True iff the strategy supplied a
    # structural anchor AND it was on the correct side of entry AND
    # the simulator placed the stop using the structural distance
    # (subject to the ``stop_atr_floor`` minimum). False on ATR-only
    # paths.
    #
    # ``structural_stop_price`` — the raw anchor price the strategy
    # returned (e.g. swept-wick extreme, asian_high). Recorded even
    # when the floor caused the actual stop to be placed at a wider
    # distance, so the ex-post analysis can see when the floor bit.
    # None when the strategy didn't return an anchor.
    #
    # ``structural_stop_distance_r`` — the structural distance
    # ``(entry − anchor) / risk_per_share`` for longs (mirror short),
    # i.e. the would-be R-distance if the floor hadn't kicked in. < 1.0
    # means the floor likely bumped the stop out; ≥ 1.0 means the
    # structural anchor was the binding constraint. None when no
    # anchor was supplied.
    structural_stop_used: bool = False
    structural_stop_price: float | None = None
    structural_stop_distance_r: float | None = None
    # Phase-2 mining (added 2026-04-26): a flat snapshot of the
    # ``ind`` dict at the moment the predicate fired, plus a few
    # derived quantities (regime label, breaker geometry,
    # is_first_fire_on_breaker). None for trades captured by older
    # backtest runs. ``save_trades_csv`` flattens this into named
    # columns; the mining script segments over it. Keys are listed
    # in ``ENTRY_FEATURE_COLUMNS`` below to keep CSV column order
    # stable across runs.
    entry_features: dict[str, object] | None = None


@dataclass
class StrategyStats:
    strategy: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    total_pnl_pct: float = 0.0
    total_mae_r: float = 0.0
    total_mfe_r: float = 0.0
    r_values: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_r(self) -> float:
        return self.total_r / self.trades if self.trades else 0.0

    @property
    def avg_mae_r(self) -> float:
        return self.total_mae_r / self.trades if self.trades else 0.0

    @property
    def avg_mfe_r(self) -> float:
        return self.total_mfe_r / self.trades if self.trades else 0.0

    @property
    def expectancy(self) -> float:
        """Average R per trade — the single most useful summary number.

        Positive = edge; zero = noise; negative = bleeding. Raise the live
        min_confidence floor on anything that ends up below ~0.1R here.
        """
        return self.avg_r

    @property
    def profit_factor(self) -> float:
        wins_r = sum(r for r in self.r_values if r > 0)
        losses_r = abs(sum(r for r in self.r_values if r <= 0))
        if losses_r > 0:
            return wins_r / losses_r
        return float("inf") if wins_r > 0 else 0.0

    @property
    def max_drawdown_r(self) -> float:
        """Worst peak-to-trough drawdown in R along the equity curve.

        Measured in R units (not dollars) so it's comparable across
        strategies with different ATRs / price levels.
        """
        peak = 0.0
        equity = 0.0
        worst = 0.0
        for r in self.r_values:
            equity += r
            if equity > peak:
                peak = equity
            drawdown = peak - equity
            if drawdown > worst:
                worst = drawdown
        return worst


@dataclass
class BacktestResult:
    trades: list[Trade]
    by_strategy: dict[str, StrategyStats]
    by_strategy_hour: dict[tuple[str, int], StrategyStats]
    by_symbol: dict[str, StrategyStats]


# ── Historical data fetch + parquet cache ──────────────────────────────────

def _cache_path(symbol: str, day: date, feed: str | None = None) -> Path:
    # v7.13a (2026-05-01): per-call cache dir resolution (not from a
    # cached module global) so a parity test that flips
    # ``ALPACA_DATA_FEED`` between back-to-back runs — or passes
    # ``feed=`` explicitly — hits the right per-feed directory each
    # time.
    return _cache_dir(feed) / symbol.upper() / f"{day.isoformat()}.json.gz"


def _load_cached_day(
    symbol: str, day: date, feed: str | None = None,
) -> list[dict] | None:
    path = _cache_path(symbol, day, feed)
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # corrupt file → drop and re-fetch
        logger.warning("bars_cache_read_failed path=%s err=%s", path, exc)
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _save_cached_day(
    symbol: str, day: date, bars: list[dict], feed: str | None = None,
) -> None:
    path = _cache_path(symbol, day, feed)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Empty days (weekends/holidays) are cached as empty files so we don't
    # re-ask Alpaca every run.
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(bars, f)


def _session_ts_range(day: date) -> tuple[datetime, datetime]:
    """Return the (start_utc, end_utc) covering one ET session including
    its preceding overnight window.

    Widened 2026-04-26 from (09:30 ET, 16:00 ET) to
    (18:00 ET prev → 16:00 ET ``day``). The Asian-Range Breakout
    predicates need ``asian_high`` / ``asian_low`` from the 18:00 (T-1)
    → 04:00 T window (see ``app/ict/__init__.py:_is_asian``); without
    those overnight bars in the fetch range, ``ict_levels.asian_high``
    is permanently None and the predicates self-skip → 0 trades.

    The session-day partition downstream still keys by ``day`` (so each
    cache file holds T-1 18:00 → T 16:00 — about 22 hours of 1-min
    bars). This roughly 4× the per-day bar count vs. the old RTH-only
    fetch but the cache is still small (~5MB per symbol-year). The
    matching cache-dir bump (`bars_cache_ext`) prevents loading the
    old RTH-only files under the new code.
    """
    start_et = datetime.combine(day - timedelta(days=1), time(18, 0), tzinfo=_ET)
    end_et = datetime.combine(day, time(16, 0), tzinfo=_ET)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def fetch_history(
    symbols: list[str],
    start: date,
    end: date,
    client: AlpacaMarketDataClient | None = None,
    feed: str | None = None,
) -> dict[str, dict[date, list[dict]]]:
    """Fetch 1-min bars for every (symbol, weekday) in [start, end].

    Missing days are downloaded from Alpaca and written to parquet under
    ``Backend/data/bars_cache_ext_<feed>``. Days that already have a
    parquet are served from disk. Weekends are skipped. Holidays are
    detected only via Alpaca returning zero bars — those are cached as
    empty files.

    ``feed`` (None default → ``settings.alpaca_data_feed``) routes both
    the API request AND the cache directory. Pass explicitly from the
    SIP-vs-IEX parity test so back-to-back calls don't collide caches.
    """
    own_client = client is None
    client = client or AlpacaMarketDataClient()
    resolved_feed = (feed or settings.alpaca_data_feed or "sip").lower()
    try:
        result: dict[str, dict[date, list[dict]]] = {s.upper(): {} for s in symbols}

        days: list[date] = []
        cur = start
        while cur <= end:
            if cur.weekday() < 5:   # Mon–Fri
                days.append(cur)
            cur += timedelta(days=1)

        for symbol in result.keys():
            for day in days:
                cached = _load_cached_day(symbol, day, resolved_feed)
                if cached is not None:
                    result[symbol][day] = cached
                    continue

                utc_start, utc_end = _session_ts_range(day)
                try:
                    resp = client.fetch_stock_bars(AlpacaBarsRequest(
                        symbols=[symbol],
                        timeframe="1Min",
                        start=utc_start,
                        end=utc_end,
                        feed=resolved_feed,
                    ))
                except Exception as exc:
                    logger.warning(
                        "backtest_fetch_failed symbol=%s day=%s feed=%s err=%s",
                        symbol, day, resolved_feed, exc,
                    )
                    # Don't cache on failure — retry next run.
                    continue

                raw = resp.get(symbol, [])
                bars = convert_alpaca_bars(raw) if raw else []
                _save_cached_day(symbol, day, bars, resolved_feed)
                result[symbol][day] = bars

        return result
    finally:
        if own_client:
            client.close()


# ── Daily-bars fetch (for prev-day levels / gap detection) ─────────────────

def _daily_cache_dir(feed: str | None = None) -> Path:
    """Per-feed on-disk cache for split-adjusted daily bars (opt-in).

    Lives beside the minute cache (``data/daily_cache_<feed>/``). Daily bars
    are normally re-fetched live every run — but Alpaca re-issues
    split-adjusted history over time, which silently shifts the
    20d/prev-day context Multi-Day PD keys on. The minute ``bars_sha`` does
    NOT cover daily data, so that drift is invisible to the bar-identity
    gate (diagnosed 2026-06-19: the canonical baseline moved 126→127 with
    identical minute bars). Freezing the first fetch makes a backtest
    reproducible run-to-run. Opt-in via ``cache_daily=True`` so every other
    caller keeps its existing fetch-live behavior byte-for-byte.
    """
    resolved = (feed or settings.alpaca_data_feed or "sip").lower()
    return _cache_dir(feed).parent / f"daily_cache_{resolved}"


def _fetch_daily_history(
    symbols: list[str],
    start: date,
    end: date,
    client: AlpacaMarketDataClient | None = None,
    feed: str | None = None,
    cache_daily: bool = False,
) -> dict[str, list[dict]]:
    """Fetch daily bars per symbol covering [start − buffer, end].

    We widen the window back by two weeks so that even the first session
    in the range (which might be a Monday after a holiday) has a
    prev-day bar available.

    ``feed`` (None default → ``settings.alpaca_data_feed``) lets the
    parity test pass an explicit feed override.

    ``cache_daily`` (default False — preserves the historical fetch-live
    behavior for every existing caller byte-for-byte): when True, freeze
    daily bars on disk per (symbol, start, end, feed), write-once. Later
    runs reuse the frozen bars instead of re-fetching, so the 20d/prev-day
    context — and therefore the trade set — is reproducible even as Alpaca
    revises split-adjusted daily history. See ``_daily_cache_dir``.
    """
    resolved_feed = (feed or settings.alpaca_data_feed or "sip").lower()
    cdir = _daily_cache_dir(feed) if cache_daily else None
    if cdir is not None:
        cdir.mkdir(parents=True, exist_ok=True)

    def _cfile(sym: str) -> Path:
        return cdir / f"{sym.upper()}__{start.isoformat()}__{end.isoformat()}.json"

    out: dict[str, list[dict]] = {}
    to_fetch: list[str] = []
    for symbol in symbols:
        if cdir is not None and _cfile(symbol).exists():
            try:
                cached = json.loads(_cfile(symbol).read_text())
                # A pin with <2 bars can't build prev-day context — treat it
                # as a miss and refetch (self-heal; see write guard below).
                if len(cached) >= 2:
                    out[symbol.upper()] = cached
                    continue
                logger.warning(
                    "daily_cache_thin_pin symbol=%s bars=%d — refetching (self-heal)",
                    symbol, len(cached),
                )
            except Exception as exc:
                logger.warning("daily_cache_read_failed symbol=%s err=%s", symbol, exc)
        to_fetch.append(symbol)

    if not to_fetch:
        return out

    own_client = client is None
    client = client or AlpacaMarketDataClient()
    try:
        buffer_start = start - timedelta(days=14)
        utc_start = datetime.combine(buffer_start, time(0, 0), tzinfo=_ET).astimezone(timezone.utc)
        utc_end   = datetime.combine(end,          time(23, 59), tzinfo=_ET).astimezone(timezone.utc)

        # One request per symbol keeps the payload bounded and lets
        # us skip symbols whose history is genuinely missing without
        # failing the whole batch.
        for symbol in to_fetch:
            try:
                resp = client.fetch_stock_bars(AlpacaBarsRequest(
                    symbols=[symbol],
                    timeframe="1Day",
                    start=utc_start,
                    end=utc_end,
                    feed=resolved_feed,
                ))
            except Exception as exc:
                logger.warning(
                    "backtest_daily_fetch_failed symbol=%s feed=%s err=%s",
                    symbol, resolved_feed, exc,
                )
                continue
            raw = resp.get(symbol.upper(), []) or resp.get(symbol, [])
            bars = convert_alpaca_bars(raw) if raw else []
            out[symbol.upper()] = bars
            # Write-once freeze guard (2026-07-03 review): never pin an
            # empty/thin payload — a transient empty API response would
            # otherwise permanently disable prev-day/20d context for this
            # (symbol, range) and silently drop MDPD/Gap trades from every
            # future reproducible run. <2 bars → fall through to live-fetch
            # behavior this run; a later healthy fetch pins normally.
            if cdir is not None:
                if len(bars) >= 2:
                    try:
                        _cfile(symbol).write_text(json.dumps(bars))
                    except Exception as exc:
                        logger.warning("daily_cache_write_failed symbol=%s err=%s", symbol, exc)
                else:
                    logger.warning(
                        "daily_cache_skip_write symbol=%s bars=%d — refusing to freeze empty/thin payload",
                        symbol, len(bars),
                    )
        return out
    finally:
        if own_client:
            client.close()


_MULTI_DAY_LOOKBACK = 20      # daily bars used for the 20-day high/low band.


def _build_prev_day_lookup(daily_bars: list[dict]) -> dict[date, dict]:
    """Map session_date → {prev_close, prev_high, prev_low, 20d_high, 20d_low}.

    For each daily bar at index i, the NEXT trading day's prev-day
    context is this bar. Building the map forward (rather than scanning
    backwards per session) is O(N) instead of O(N²) and handles
    weekends/holidays naturally — bar[i+1].date might be two or three
    calendar days after bar[i].date, but it's still the immediate next
    session.

    Added 2026-04-26: ``20d_high`` / ``20d_low`` over the trailing
    ``_MULTI_DAY_LOOKBACK`` daily bars (inclusive of ``this_bar``, so the
    band describes "the multi-day range as of yesterday's close").
    ``compute_ict_features._multi_day_range`` consumes these to build
    ``ict_multi_day_range``, which the Multi-Day PD predicates gate on.
    Without them, ``ict_multi_day_pd_zone`` was permanently None and
    those predicates never fired in the backtest. Partial windows
    (early days in the range with < 20 prior bars) just compute over
    whatever's available — that's the same conservative behavior
    `_multi_day_range` itself uses for its bar-frame fallback.
    """
    if len(daily_bars) < 2:
        return {}

    sorted_bars = sorted(daily_bars, key=lambda b: b["time"])
    lookup: dict[date, dict] = {}
    for i in range(len(sorted_bars) - 1):
        this_bar = sorted_bars[i]
        next_bar = sorted_bars[i + 1]
        next_date = datetime.fromtimestamp(next_bar["time"], tz=_ET).date()
        # Trailing 20-day window — sortedbars[max(0,i-19) .. i] inclusive.
        # Includes this_bar so the window represents the *most recent*
        # 20 closed sessions as of next_date's open.
        lookback_start = max(0, i - (_MULTI_DAY_LOOKBACK - 1))
        window = sorted_bars[lookback_start : i + 1]
        # avg_daily_volume (2026-06-04): mean of the trailing-window daily
        # volumes (>0), matching autotrade._get_daily_ctx_map exactly. Only
        # consumed by the parity probe's live-ind reconstruction; the
        # backtest's own compute_indicators call does NOT read it, so normal
        # backtest output is unchanged.
        _vols = [float(b.get("volume") or 0) for b in window]
        _vols = [v for v in _vols if v > 0]
        lookup[next_date] = {
            "prev_close": float(this_bar["close"]),
            "prev_high":  float(this_bar["high"]),
            "prev_low":   float(this_bar["low"]),
            "20d_high":   max(float(b["high"]) for b in window),
            "20d_low":    min(float(b["low"])  for b in window),
            "avg_daily_volume": (sum(_vols) / len(_vols)) if _vols else None,
        }
    return lookup


# ── Phase-2 entry-feature capture ──────────────────────────────────────────
#
# These columns get appended to the per-trade CSV alongside the existing
# outcome columns. The capture happens at predicate-fire time inside
# ``replay_symbol`` — see ``_capture_entry_features`` below. Order is
# pinned here so column layout is stable across runs (and so a CSV
# written without the new columns can still be read by older scripts).
ENTRY_FEATURE_COLUMNS: tuple[str, ...] = (
    # classic indicators
    "vol_ratio",
    "rsi",
    "atr_pct",                  # atr / entry_price
    # ICT context
    "ict_pd_zone",
    "ict_multi_day_pd_zone",
    "ict_killzone",
    "ict_in_ote_long",
    "ict_in_ote_short",
    # MSS / sweep recency
    "ict_last_bull_mss_bars_ago",
    "ict_last_bear_mss_bars_ago",
    "ict_last_bull_sweep_bars_ago",
    "ict_last_bear_sweep_bars_ago",
    # Sweep price geometry (2026-05-29) — wick_extreme is the deepest
    # excursion of the manipulation leg (the "sweep low" for a bull
    # sweep); level_price is the liquidity level that got swept. Needed
    # for the P3 Long "no-chase" guard: (entry - sweep_wick) / ATR
    # measures how much of the post-sweep move was already gone at entry.
    "ict_last_bull_sweep_wick",
    "ict_last_bull_sweep_level",
    "ict_last_bear_sweep_wick",
    "ict_last_bear_sweep_level",
    # Breaker geometry (only populated on Breaker Block trades)
    "breaker_bars_ago_mitigated",
    "breaker_zone_width_atr",
    "breaker_distance_into_zone_atr",
    "is_first_fire_on_breaker",
    # Regime (per (symbol, session) classification on daily bars)
    "regime",
    "regime_realized_vol",
)


def _capture_entry_features(
    strat: Strategy,
    ind: dict,
    *,
    atr: float,
    entry_price: float,
    bar_idx: int,
    breaker_history: dict[str, set[int]],
    regime_label: str | None,
    regime_vol: float | None,
) -> dict[str, object]:
    """Snapshot the ``ind`` dict + derived geometry at predicate-fire time.

    Universal fields (vol_ratio, rsi, regime, killzone, …) are captured
    for every trade. Breaker-geometry fields are only filled on Breaker
    Block (Long/Short) trades — for any other strategy they're left
    None and the mining script's ``.get()`` reads tolerate that.

    ``breaker_history`` is the per-session map ``{strategy_name → set
    of mitigation_idx already fired on}``. We add the fired-on
    mitigation_idx to the set after recording ``is_first_fire_on_breaker``
    so the next fire on the same breaker reads ``False``.

    Returned dict has exactly the keys in ``ENTRY_FEATURE_COLUMNS`` —
    missing values are None, never absent — so the CSV writer's
    ``getattr(t.entry_features, k)`` style read is safe.
    """
    feats: dict[str, object] = {k: None for k in ENTRY_FEATURE_COLUMNS}

    feats["vol_ratio"] = ind.get("vol_ratio")
    feats["rsi"] = ind.get("rsi")
    if atr > 0 and entry_price:
        feats["atr_pct"] = atr / entry_price

    feats["ict_pd_zone"] = ind.get("ict_pd_zone")
    feats["ict_multi_day_pd_zone"] = ind.get("ict_multi_day_pd_zone")
    feats["ict_killzone"] = ind.get("ict_killzone")
    feats["ict_in_ote_long"] = ind.get("ict_in_ote_long")
    feats["ict_in_ote_short"] = ind.get("ict_in_ote_short")

    bull_mss = ind.get("ict_last_bull_mss") or {}
    bear_mss = ind.get("ict_last_bear_mss") or {}
    feats["ict_last_bull_mss_bars_ago"] = bull_mss.get("bars_ago") if bull_mss else None
    feats["ict_last_bear_mss_bars_ago"] = bear_mss.get("bars_ago") if bear_mss else None

    bull_sw = ind.get("ict_last_bull_sweep") or {}
    bear_sw = ind.get("ict_last_bear_sweep") or {}
    feats["ict_last_bull_sweep_bars_ago"] = bull_sw.get("bars_ago") if bull_sw else None
    feats["ict_last_bear_sweep_bars_ago"] = bear_sw.get("bars_ago") if bear_sw else None
    # Sweep price levels (2026-05-29) for the no-chase guard. wick_extreme
    # = manipulation-leg extreme (bar.low on a bull sweep); level_price =
    # the swept liquidity level (PDL / session_low / asian_low / ...).
    feats["ict_last_bull_sweep_wick"]  = bull_sw.get("wick_extreme") if bull_sw else None
    feats["ict_last_bull_sweep_level"] = bull_sw.get("level_price")  if bull_sw else None
    feats["ict_last_bear_sweep_wick"]  = bear_sw.get("wick_extreme") if bear_sw else None
    feats["ict_last_bear_sweep_level"] = bear_sw.get("level_price")  if bear_sw else None

    # ── Breaker-only geometry ───────────────────────────────────────
    if strat.name == "ICT Breaker Block (Long)":
        brk = ind.get("ict_nearest_bull_breaker")
        near_edge_is_high = True   # long enters from above the box
    elif strat.name == "ICT Breaker Block (Short)":
        brk = ind.get("ict_nearest_bear_breaker")
        near_edge_is_high = False  # short enters from below the box
    else:
        brk = None
        near_edge_is_high = None

    if brk:
        low = brk.get("low")
        high = brk.get("high")
        bars_ago = brk.get("bars_ago_mitigated")
        feats["breaker_bars_ago_mitigated"] = bars_ago
        if low is not None and high is not None and atr > 0:
            feats["breaker_zone_width_atr"] = (high - low) / atr
            # distance_into_zone_atr: how far past the *near edge* the
            # entry sits, measured in ATRs. Larger = price has pushed
            # deeper into the breaker before we entered → tighter stop
            # by construction (Phase 1 finding).
            if near_edge_is_high:
                feats["breaker_distance_into_zone_atr"] = (high - entry_price) / atr
            else:
                feats["breaker_distance_into_zone_atr"] = (entry_price - low) / atr
        # First-fire flag — the absolute mitigation_idx is stable for
        # one breaker across bars within the session, so we use it as
        # the identifier. (low, high) alone could collide if a separate
        # OB happened to mitigate at identical edges.
        if bars_ago is not None:
            mitigation_idx = bar_idx - int(bars_ago)
            seen = breaker_history.setdefault(strat.name, set())
            feats["is_first_fire_on_breaker"] = mitigation_idx not in seen
            seen.add(mitigation_idx)

    feats["regime"] = regime_label
    feats["regime_realized_vol"] = regime_vol

    return feats


# ── Trade simulation ───────────────────────────────────────────────────────

def _entry_fill_price(raw_price: float, direction: str) -> float:
    if direction == "long":
        return raw_price * (1 + ENTRY_SLIPPAGE_PCT)
    return raw_price * (1 - ENTRY_SLIPPAGE_PCT)


def _exit_fill_price(raw_price: float, direction: str) -> float:
    if direction == "long":
        return raw_price * (1 - EXIT_SLIPPAGE_PCT)
    return raw_price * (1 + EXIT_SLIPPAGE_PCT)


def _per_share_pnl(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "long":
        return exit_price - entry_price
    return entry_price - exit_price


def _resolve_entry_index(
    bars: list[dict],
    signal_idx: int,
    direction: str,
    exit_model: TradeExitModel,
) -> tuple[int, int] | None:
    """Return (entry_idx, delay_bars) after optional post-touch confirmation.

    Baseline behavior enters on the next bar's open. Confirmation models wait
    up to N bars for a candle to close in the trade direction relative to the
    original touch bar, then enter on the following bar's open. If no
    confirmation appears, the setup is skipped for that model.
    """

    if signal_idx >= len(bars) - 1:
        return None

    if exit_model.confirmation_bars <= 0:
        return signal_idx + 1, 0

    try:
        trigger_close = float(bars[signal_idx]["close"])
    except (TypeError, ValueError, KeyError):
        return None

    search_end = min(signal_idx + exit_model.confirmation_bars, len(bars) - 2)
    for confirm_idx in range(signal_idx + 1, search_end + 1):
        bar = bars[confirm_idx]
        try:
            open_ = float(bar["open"])
            close = float(bar["close"])
        except (TypeError, ValueError, KeyError):
            continue

        if direction == "long" and close > trigger_close and close > open_:
            return confirm_idx + 1, confirm_idx - signal_idx
        if direction == "short" and close < trigger_close and close < open_:
            return confirm_idx + 1, confirm_idx - signal_idx

    return None

def _simulate_trade(
    bars: list[dict],
    entry_idx: int,
    direction: str,
    atr: float,
    *,
    exit_model: TradeExitModel = DEFAULT_EXIT_MODEL,
    entry_delay_bars: int = 0,
    structural_stop_price: float | None = None,
    rematch_indices: frozenset[int] | None = None,
) -> Trade | None:
    """Simulate one trade opening at ``bars[entry_idx]`` open.

    Uses a supplied ATR-based stop/target model. Exit = first of: stop hit,
    target hit, ``max_hold_bars`` elapsed, end-of-session.

    When both stop and target are touched in the same bar, we
    conservatively assume the stop fires first — this is the standard
    pessimistic assumption for bar-level simulation and avoids
    flattering stats for fast-moving bars.

    ``structural_stop_price`` (added v7.7, 2026-04-27): optional anchor
    price for the stop. When supplied AND it is on the correct side of
    entry (below for long, above for short), the stop is placed at
    ``max(structural_distance_from_entry, atr × stop_atr_floor)`` —
    i.e. the larger of the structural distance and the ATR floor, so a
    tight structural anchor still gets a sane minimum size and we
    don't get clipped on the entry-bar's noise. When None or on the
    wrong side of entry, falls back to the ATR-only behavior using
    ``stop_atr_mult``. Target is always ATR-based (structural targets
    are noisier and the simulator already favors structure-aware
    target placement at the strategy level via ``target_rules`` —
    that's a Tier-2 concern, not Tier-1).
    """
    if atr <= 0 or entry_idx >= len(bars):
        return None
    if exit_model.stop_atr_mult <= 0 or exit_model.target_atr_mult <= 0:
        return None

    entry_bar = bars[entry_idx]
    raw_entry = float(entry_bar["open"])
    entry_price = _entry_fill_price(raw_entry, direction)

    # Stop placement: structural if available and on the correct side
    # of entry, otherwise ATR-only (current global behavior).
    use_structural = (
        structural_stop_price is not None
        and (
            (direction == "long" and structural_stop_price < entry_price)
            or (direction == "short" and structural_stop_price > entry_price)
        )
    )
    # Track the raw structural distance (before the floor bumps it
    # out) so the per-trade telemetry can show whether the floor was
    # the binding constraint.
    raw_structural_dist: float | None = None
    if exit_model.fixed_stop_pct is not None:
        # Live predicate-only geometry: stop = fixed % from the scan-time
        # price. Bar-level analogue = the signal bar's close (approximation,
        # declared in the replay report). Overrides structural/ATR placement.
        sig_idx = max(0, entry_idx - 1 - entry_delay_bars)
        ref_close = float(bars[sig_idx]["close"])
        if direction == "long":
            stop_price = ref_close * (1 - exit_model.fixed_stop_pct)
        else:
            stop_price = ref_close * (1 + exit_model.fixed_stop_pct)
        use_structural = False
    elif use_structural:
        floor_dist = atr * exit_model.stop_atr_floor
        if direction == "long":
            raw_structural_dist = entry_price - float(structural_stop_price)
            stop_dist = max(raw_structural_dist, floor_dist)
            stop_price = entry_price - stop_dist
        else:  # short
            raw_structural_dist = float(structural_stop_price) - entry_price
            stop_dist = max(raw_structural_dist, floor_dist)
            stop_price = entry_price + stop_dist
    else:
        if direction == "long":
            stop_price = entry_price - atr * exit_model.stop_atr_mult
        else:  # short
            stop_price = entry_price + atr * exit_model.stop_atr_mult

    if direction == "long":
        target_price = entry_price + atr * exit_model.target_atr_mult
    else:  # short
        target_price = entry_price - atr * exit_model.target_atr_mult

    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return None

    # Compute structural-stop telemetry against the (now-known)
    # ``risk_per_share``. ``structural_stop_distance_r`` reports the
    # RAW structural distance in R units — not the actual stop
    # distance, which may be wider when the ATR floor binds. A value
    # < 1.0 means the floor was the binding constraint; ≥ 1.0 means
    # the structural anchor itself sized the stop.
    if structural_stop_price is not None:
        if direction == "long":
            telemetry_dist = float(entry_price) - float(structural_stop_price)
        else:
            telemetry_dist = float(structural_stop_price) - float(entry_price)
        structural_distance_r: float | None = (
            telemetry_dist / risk_per_share if risk_per_share > 0 else None
        )
        anchor_price_for_telemetry: float | None = float(structural_stop_price)
    else:
        structural_distance_r = None
        anchor_price_for_telemetry = None

    trade = Trade(
        strategy="",          # caller fills in
        symbol="",            # caller fills in
        direction=direction,
        exit_model=exit_model.name,
        entry_ts=datetime.fromtimestamp(entry_bar["time"], tz=timezone.utc).astimezone(_ET),
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        risk_per_share=risk_per_share,
        entry_delay_bars=entry_delay_bars,
        confirmation_bars=exit_model.confirmation_bars,
        structural_stop_used=use_structural,
        structural_stop_price=anchor_price_for_telemetry,
        structural_stop_distance_r=structural_distance_r,
    )

    end_idx = min(entry_idx + exit_model.max_hold_bars, len(bars) - 1)
    exit_hit = False
    dynamic_stop = stop_price
    partial_fraction = (
        exit_model.partial_fraction
        if exit_model.partial_take_r is not None
        and 0.0 < exit_model.partial_fraction < 1.0
        else 0.0
    )
    reward_r = abs(target_price - entry_price) / risk_per_share
    partial_enabled = (
        partial_fraction > 0.0
        and exit_model.partial_take_r is not None
        and 0.0 < exit_model.partial_take_r < reward_r
    )
    if direction == "long":
        partial_price = entry_price + risk_per_share * float(exit_model.partial_take_r or 0.0)
    else:
        partial_price = entry_price - risk_per_share * float(exit_model.partial_take_r or 0.0)
    partial_realized_pnl = 0.0
    remaining_fraction = 1.0
    max_favorable_r = 0.0
    max_adverse_r = 0.0
    max_favorable_price = entry_price
    max_adverse_price = entry_price

    # Trailing-stop research path (default-inert). The trail reuses
    # ``dynamic_stop`` — seeded at the model stop, only ever tightened —
    # mirroring live's trailing_stop=initial_stop seeding. Partials are
    # unsupported in trail models (research scope).
    trail_enabled = (
        exit_model.trail_pct is not None or exit_model.trail_atr_mult is not None
    )
    trail_armed = trail_enabled and exit_model.trail_activate_r <= 0.0
    trail_ratcheted = False
    if trail_enabled:
        partial_enabled = False

    # while-loop (not range) so a re-match can push end_idx out mid-trade.
    # With re-match inert (end_idx never mutated) this iterates exactly
    # range(entry_idx+1, end_idx+1) — verified identical on the XLE repro slice.
    j = entry_idx
    while True:
        j += 1
        if j > end_idx:
            break
        bar = bars[j]
        high = float(bar["high"])
        low = float(bar["low"])
        if direction == "long":
            favorable = high - entry_price
            adverse = entry_price - low
            fav_price = high
            adv_price = low
        else:
            favorable = entry_price - low
            adverse = high - entry_price
            fav_price = low
            adv_price = high

        favorable_r = max(0.0, favorable / risk_per_share)
        adverse_r = max(0.0, adverse / risk_per_share)
        if favorable_r > max_favorable_r:
            max_favorable_r = favorable_r
            max_favorable_price = fav_price
        if adverse_r > max_adverse_r:
            max_adverse_r = adverse_r
            max_adverse_price = adv_price

        # ── Re-match management (default-inert) ───────────────────────────
        # Triggered by a same-strategy re-fire (bar indices in rematch_indices)
        # while this position is open. EXTEND acts on the signal bar itself
        # (it only lengthens the horizon — closed-bar info, never look-ahead).
        # The BE ratchet acts the bar AFTER the signal (uses this bar's open
        # as the observed price), only ever tightens, and only when price is
        # already beyond the breakeven+ level (so it can't fabricate an
        # instant fill below the market).
        if rematch_indices:
            if exit_model.rematch_extend_hold_bars > 0 and j in rematch_indices:
                extended = min(j + exit_model.rematch_extend_hold_bars, len(bars) - 1)
                if extended > end_idx:
                    end_idx = extended
            if exit_model.rematch_move_stop_to_be and (j - 1) in rematch_indices:
                cur = float(bar["open"])
                if direction == "long":
                    be = entry_price * (1.0 + exit_model.rematch_be_buffer_pct)
                    if cur > be and be > dynamic_stop:
                        dynamic_stop = be
                else:
                    be = entry_price * (1.0 - exit_model.rematch_be_buffer_pct)
                    if cur < be and be < dynamic_stop:
                        dynamic_stop = be

        if trail_enabled and exit_model.time_exit_priority \
                and j >= entry_idx + exit_model.max_hold_bars:
            # Live checks the time exit BEFORE the trail each monitor cycle —
            # at the max-hold boundary the timeout wins. Fall to the
            # force-close path below.
            break
        elif trail_enabled and exit_model.trail_price_basis == "closes":
            # SAMPLED-trail approximation: only bar closes are "observed"
            # (live polls ~10s; intrabar extremes between samples are
            # invisible). Mirrors live's per-cycle order — ratchet from the
            # observed price, then breach-test that SAME price — and exits AT
            # the observed price, which can be well through the trail level,
            # exactly like live's market-order fills. One observed price per
            # bar ⇒ no cons/opti ordering split for this basis.
            close_px = float(bar["close"])
            fav_close_r = ((close_px - entry_price) if direction == "long"
                           else (entry_price - close_px)) / risk_per_share
            if not trail_armed and fav_close_r >= exit_model.trail_activate_r:
                trail_armed = True
            if trail_armed:
                if exit_model.trail_pct is not None:
                    dist = close_px * exit_model.trail_pct
                else:
                    dist = atr * float(exit_model.trail_atr_mult)
                cand = close_px - dist if direction == "long" else close_px + dist
                if direction == "long" and cand > dynamic_stop:
                    dynamic_stop, trail_ratcheted = cand, True
                elif direction == "short" and cand < dynamic_stop:
                    dynamic_stop, trail_ratcheted = cand, True
            breached = (close_px <= dynamic_stop if direction == "long"
                        else close_px >= dynamic_stop)
            if not breached:
                continue
            exit_price = _exit_fill_price(close_px, direction)
            reason = "trail" if trail_ratcheted else "stop"
        elif trail_enabled:
            # OHLC-extremes basis: continuous-observation model of the trail,
            # run under BOTH intrabar ordering scenarios (cons/opti).
            # Arm once favorable excursion reaches the threshold (0 = at entry).
            if not trail_armed and max_favorable_r >= exit_model.trail_activate_r:
                trail_armed = True

            def _ratchet():
                """Tighten dynamic_stop toward the bar's favorable extreme."""
                nonlocal dynamic_stop, trail_ratcheted
                if not trail_armed:
                    return
                if exit_model.trail_pct is not None:
                    dist = fav_price * exit_model.trail_pct
                else:
                    dist = atr * float(exit_model.trail_atr_mult)
                cand = fav_price - dist if direction == "long" else fav_price + dist
                if direction == "long" and cand > dynamic_stop:
                    dynamic_stop, trail_ratcheted = cand, True
                elif direction == "short" and cand < dynamic_stop:
                    dynamic_stop, trail_ratcheted = cand, True

            target_enforced = not exit_model.trail_replaces_target
            if direction == "long":
                tgt_hit = target_enforced and high >= target_price
            else:
                tgt_hit = target_enforced and low <= target_price

            if exit_model.trail_ordering == "optimistic":
                # Favorable-first: target exits first; the trail ratchets
                # BEFORE the adverse extreme is tested (exit at the tighter,
                # better level).
                if tgt_hit:
                    exit_price = _exit_fill_price(target_price, direction)
                    reason = "target"
                else:
                    _ratchet()
                    hit = (low <= dynamic_stop if direction == "long"
                           else high >= dynamic_stop)
                    if not hit:
                        continue
                    exit_price = _exit_fill_price(dynamic_stop, direction)
                    reason = "trail" if trail_ratcheted else "stop"
            else:
                # Conservative adverse-first: the PRE-bar level is tested
                # first (worse exit, before target even when both touched);
                # the ratchet applies only at end of bar.
                pre_hit = (low <= dynamic_stop if direction == "long"
                           else high >= dynamic_stop)
                if pre_hit:
                    exit_price = _exit_fill_price(dynamic_stop, direction)
                    reason = "trail" if trail_ratcheted else "stop"
                elif tgt_hit:
                    exit_price = _exit_fill_price(target_price, direction)
                    reason = "target"
                else:
                    _ratchet()
                    continue
        elif direction == "long":
            stop_hit = low <= dynamic_stop
            target_hit = high >= target_price
            partial_hit = partial_enabled and not trade.partial_taken and high >= partial_price
            if stop_hit:
                exit_price = _exit_fill_price(dynamic_stop, direction)
                reason = "stop"
            elif target_hit:
                if partial_enabled and not trade.partial_taken:
                    fill = _exit_fill_price(partial_price, direction)
                    partial_realized_pnl += partial_fraction * _per_share_pnl(direction, entry_price, fill)
                    remaining_fraction = 1.0 - partial_fraction
                    trade.partial_taken = True
                    trade.partial_exit_ts = datetime.fromtimestamp(
                        bar["time"], tz=timezone.utc,
                    ).astimezone(_ET)
                    trade.partial_exit_price = fill
                    trade.partial_fraction = partial_fraction
                exit_price = _exit_fill_price(target_price, direction)
                reason = "target"
            elif partial_hit:
                fill = _exit_fill_price(partial_price, direction)
                partial_realized_pnl += partial_fraction * _per_share_pnl(direction, entry_price, fill)
                remaining_fraction = 1.0 - partial_fraction
                trade.partial_taken = True
                trade.partial_exit_ts = datetime.fromtimestamp(
                    bar["time"], tz=timezone.utc,
                ).astimezone(_ET)
                trade.partial_exit_price = fill
                trade.partial_fraction = partial_fraction
                if exit_model.move_stop_to_breakeven:
                    dynamic_stop = entry_price
                continue
            else:
                continue
        else:
            stop_hit = high >= dynamic_stop
            target_hit = low <= target_price
            partial_hit = partial_enabled and not trade.partial_taken and low <= partial_price
            if stop_hit:
                exit_price = _exit_fill_price(dynamic_stop, direction)
                reason = "stop"
            elif target_hit:
                if partial_enabled and not trade.partial_taken:
                    fill = _exit_fill_price(partial_price, direction)
                    partial_realized_pnl += partial_fraction * _per_share_pnl(direction, entry_price, fill)
                    remaining_fraction = 1.0 - partial_fraction
                    trade.partial_taken = True
                    trade.partial_exit_ts = datetime.fromtimestamp(
                        bar["time"], tz=timezone.utc,
                    ).astimezone(_ET)
                    trade.partial_exit_price = fill
                    trade.partial_fraction = partial_fraction
                exit_price = _exit_fill_price(target_price, direction)
                reason = "target"
            elif partial_hit:
                fill = _exit_fill_price(partial_price, direction)
                partial_realized_pnl += partial_fraction * _per_share_pnl(direction, entry_price, fill)
                remaining_fraction = 1.0 - partial_fraction
                trade.partial_taken = True
                trade.partial_exit_ts = datetime.fromtimestamp(
                    bar["time"], tz=timezone.utc,
                ).astimezone(_ET)
                trade.partial_exit_price = fill
                trade.partial_fraction = partial_fraction
                if exit_model.move_stop_to_breakeven:
                    dynamic_stop = entry_price
                continue
            else:
                continue

        trade.exit_ts = datetime.fromtimestamp(bar["time"], tz=timezone.utc).astimezone(_ET)
        trade.exit_price = exit_price
        trade.exit_reason = reason
        exit_hit = True
        break

    if not exit_hit:
        # Force-close at last available bar's close (timeout or EOD).
        last_bar = bars[end_idx]
        close = float(last_bar["close"])
        exit_price = _exit_fill_price(close, direction)
        trade.exit_ts = datetime.fromtimestamp(last_bar["time"], tz=timezone.utc).astimezone(_ET)
        trade.exit_price = exit_price
        trade.exit_reason = "timeout" if (end_idx - entry_idx) >= exit_model.max_hold_bars else "eod"

    pnl = (
        partial_realized_pnl
        + remaining_fraction * _per_share_pnl(direction, entry_price, trade.exit_price)
    )

    trade.r_multiple = pnl / risk_per_share
    trade.pnl_pct = (pnl / entry_price) * 100 if entry_price else 0.0
    trade.mae_r = max_adverse_r
    trade.mfe_r = max_favorable_r
    trade.max_adverse_price = max_adverse_price
    trade.max_favorable_price = max_favorable_price
    return trade


def _within_entry_window(bar_time_et: datetime) -> bool:
    t = bar_time_et.time()
    return _SESSION_OPEN <= t <= _SESSION_ENTRY_CUTOFF


# ── Walk-forward replay ────────────────────────────────────────────────────

def replay_symbol_exit_models(
    symbol: str,
    sessions: dict[date, list[dict]],
    prev_day_lookup: dict[date, dict] | None = None,
    *,
    daily_bars: list[dict] | None = None,
    playbook_override: list[Strategy] | None = None,
    exit_models: list[TradeExitModel] | None = None,
    respect_strategy_overrides: bool = False,
    exit_model_transform: object | None = None,
    collect_fire_bars: dict | None = None,
    rematch_fire_timeline: dict | None = None,
) -> dict[str, list[Trade]]:
    """Replay every strategy once, then simulate each exit model per signal.

    For each session-day:
      1. For each bar past the warmup, call ``compute_indicators(window)``
         on the rolling window (capped at 200 bars for speed).
      2. For each strategy in the PLAYBOOK, call ``strategy.matches(ind)``.
      3. On a match, simulate every supplied ``TradeExitModel`` against the
         same signal. Baseline enters at the next open; confirmation models
         wait for post-touch confirmation before entering.
      4. Apply a per-strategy cooldown so one persistent condition
         doesn't spawn 50 trades in a row.

    Cooldown is per-strategy, not per-(strategy, direction), because
    each ``Strategy`` has a fixed direction.

    ``prev_day_lookup`` maps session_date → {prev_close, prev_high,
    prev_low}; when supplied, the matching day's entry is forwarded to
    ``compute_indicators`` so Gap-and-Go / Prior-Day-Break predicates
    can fire. Sessions missing from the lookup still run the other
    eight strategies — those predicates self-skip on ind.get(...) None.

    ``daily_bars`` (optional) — the same daily-bar list used to build
    ``prev_day_lookup``. Passed through so the regime classifier
    (``app.regime.classify``) can be called once per session and the
    label stamped into each trade's ``entry_features`` for Phase-2
    mining. None → regime captured as None on every trade.

    ``playbook_override`` (optional) — when provided, replay against
    this list of strategies instead of the live PLAYBOOK. Used by the
    Phase-2 mining run to re-include suspended strategies (Breaker
    Block) without un-suspending them in the source PLAYBOOK.

    ``exit_models`` — stop/target/partial/confirmation variants to test
    against each fired setup. This keeps stop-model matrices cheap: feature
    detection runs once, not once per exit model.

    ``respect_strategy_overrides`` (added v7.7, 2026-04-27) — when
    True, each fired signal runs against the strategy's preferred
    ``exit_model_name`` instead of every model in the
    ``exit_models`` list. Looks up the override in the global
    ``EXIT_MODELS`` registry; unknown names log a warning and fall
    back to the first model in ``exit_models``. Strategies with no
    override also fall back to the first model in the list. This is
    set True by ``replay_symbol`` (single-exit-model replay path)
    and remains False by default for ``run_exit_model_matrix`` —
    the matrix runner needs uniform per-signal scoring across every
    model for a fair horse race. Trades from the same predicate-fire
    bar still appear under the resolved model's key in the returned
    dict, so downstream aggregation works unchanged.
    """
    exit_models = list(exit_models or [DEFAULT_EXIT_MODEL])
    model_names = [m.name for m in exit_models]
    if len(model_names) != len(set(model_names)):
        raise ValueError("exit model names must be unique")

    trades_by_model: dict[str, list[Trade]] = {m.name: [] for m in exit_models}
    prev_day_lookup = prev_day_lookup or {}
    playbook = playbook_override if playbook_override is not None else PLAYBOOK
    daily_bars = daily_bars or []

    for day, bars in sessions.items():
        if len(bars) < WARMUP_BARS + 5:
            continue
        _replay_symbols_processed.add(symbol)  # symbol-coverage witness (Finding 3; inert in native runs)
        _replay_sessions_processed.add((symbol, str(day)))  # full session coverage (Finding 5)
        if _REPLAY_HASH_BARS:
            _replay_session_bar_sha[(symbol, str(day))] = _session_bar_sha(bars)  # bar-payload hash (Finding 1)

        day_ctx = prev_day_lookup.get(day) or {}

        # Regime label for this session = classify on all daily bars
        # *strictly before* this session's date. We slice up to ``day``
        # exclusive so we don't peek at ``day``'s own daily bar (which
        # incorporates close info for the session we're about to
        # replay). Failure → label is None and the trades carry None.
        regime_label: str | None = None
        regime_vol: float | None = None
        if daily_bars:
            try:
                day_start_ts = datetime.combine(day, time(0, 0), tzinfo=_ET).timestamp()
                prior_daily = [b for b in daily_bars if b["time"] < day_start_ts]
                if len(prior_daily) >= 5:
                    snap = classify_regime(prior_daily)
                    regime_label = snap.regime
                    regime_vol = snap.realized_vol_20d
            except Exception as exc:
                logger.debug(
                    "regime_classify_failed symbol=%s day=%s err=%s",
                    symbol, day, exc,
                )

        # Index until which (strategy_name, exit_model_name) is on
        # cooldown; entry idx must be > this. Keyed per-model so a
        # permissive matrix model (no confirmation) entering at bar i
        # doesn't lock a stricter model out of a later predicate-fire
        # at bar i+5 — the stricter model has its own clock and is
        # only blocked by its own successful fires. Single-model
        # replays (``replay_symbol`` → ``respect_strategy_overrides=
        # True``) collapse to one (strategy, model) key per strategy
        # and behave exactly as the old strategy-only cooldown did.
        cooldown: dict[tuple[str, str], int] = {}
        # mitigation indices already fired on this session, per strategy
        # — used to set is_first_fire_on_breaker on entry-feature capture.
        # Resets each session; breakers don't carry across sessions in the
        # current ICT feature layer (200-bar replay window).
        breaker_history: dict[str, set[int]] = {}

        for i in range(WARMUP_BARS, len(bars) - 1):
            bar_time_et = datetime.fromtimestamp(
                bars[i]["time"], tz=timezone.utc,
            ).astimezone(_ET)
            if not _within_entry_window(bar_time_et):
                continue

            window = _window_for_mode(bars, i, _REPLAY_WINDOW_MODE)  # native [-200:]; replay arms override
            try:
                ind = compute_indicators(
                    window,
                    prev_close=day_ctx.get("prev_close"),
                    prev_high=day_ctx.get("prev_high"),
                    prev_low=day_ctx.get("prev_low"),
                )
            except Exception as exc:
                logger.debug(
                    "indicators_failed symbol=%s day=%s idx=%d err=%s",
                    symbol, day, i, exc,
                )
                continue

            # Merge ICT features into ind so the ICT strategies in PLAYBOOK
            # (Sweep+MSS Long/Short, Silver Bullet) can fire during replay.
            # Wrapped so a detector bug can't kill the whole backtest —
            # non-ICT strategies keep running with their own predicates.
            #
            # ``20d_high`` / ``20d_low`` (added 2026-04-26) feed
            # ``compute_ict_features._multi_day_range`` so Multi-Day PD
            # (Long/Short) gets a populated ``ict_multi_day_pd_zone``;
            # without these it stayed None and the predicate always
            # rejected.
            try:
                ict_seed = {
                    "prev_close": day_ctx.get("prev_close"),
                    "prev_high":  day_ctx.get("prev_high"),
                    "prev_low":   day_ctx.get("prev_low"),
                    "20d_high":   day_ctx.get("20d_high"),
                    "20d_low":    day_ctx.get("20d_low"),
                } if day_ctx else None
                ind.update(compute_ict_features(window, ict_seed))
            except Exception as exc:
                logger.debug(
                    "ict_features_failed symbol=%s day=%s idx=%d err=%s",
                    symbol, day, i, exc,
                )

            atr = float(ind.get("atr") or 0.0)
            if atr <= 0:
                continue

            ind["symbol"] = symbol
            # v7.15 (2026-05-18): thread the bar window into ind so the
            # EXPERIMENTAL_PLAYBOOK predicates that need recent-bar
            # context (VWAP Reclaim's prev-bar close, EMA Pullback's
            # 5-bar low touch, Failed Breakdown's 10-bar break-lookback)
            # have the data they need. The live scanner_experimental.py
            # already adds this; the backtester needs to mirror it so
            # both code paths see the same ind contract. ICT predicates
            # don't read ``recent_bars`` (they use ICT-feature derived
            # fields like ``ict_last_sweep``) so this is additive.
            ind["recent_bars"] = window
            for strat in playbook:
                if not strat.matches(ind):
                    continue

                # Live↔backtest parity diagnostic (gated; default off).
                if _PARITY_DIAG:
                    _parity_probe(strat, ind, window, i, bars, day_ctx, symbol, day)

                # v7.7: structural-stop anchor is computed once per
                # predicate-fire (same ``ind`` snapshot for every exit
                # model variant). Strategies without a
                # ``structural_stop_fn`` return None here and the
                # simulator falls back to ATR-only stops.
                structural_stop_price = strat.structural_stop(ind)

                # v7.7: per-strategy override resolution. When
                # respect_strategy_overrides=True (single-model replay
                # path), each strategy runs against its preferred
                # exit model only — not every variant in the matrix.
                # Matrix runs (run_exit_model_matrix) keep the
                # original behavior: every model on every signal.
                if respect_strategy_overrides and strat.exit_model_name:
                    resolved = EXIT_MODELS.get(strat.exit_model_name)
                    if resolved is None:
                        logger.warning(
                            "unknown_exit_model strat=%s name=%s falling_back=%s",
                            strat.name, strat.exit_model_name,
                            exit_models[0].name,
                        )
                        models_for_this_signal = [exit_models[0]]
                    elif resolved.name in {m.name for m in exit_models}:
                        models_for_this_signal = [resolved]
                    else:
                        # Override not in the caller-supplied list
                        # (which is the common case in the live
                        # single-model replay — caller passes one
                        # baseline model, strategies override it).
                        # We respect the override by routing those
                        # trades into the override model's bucket;
                        # the bucket gets created lazily below.
                        models_for_this_signal = [resolved]
                else:
                    models_for_this_signal = exit_models

                # Exit-parity replay (2026-06-10): optional transform applied
                # AFTER override resolution — preserves each strategy's
                # resolved entry timing (confirmation_bars) and stop geometry
                # while replacing post-entry exit behavior (e.g. grafting a
                # trail, stripping partials). Renames deterministically, so
                # per-run cooldown keys stay self-consistent and entry sets
                # remain identical to baseline. None = validated behavior.
                if exit_model_transform is not None:
                    models_for_this_signal = [
                        exit_model_transform(m) for m in models_for_this_signal
                    ]

                # Per-model cooldown filter (v7.8 fix, 2026-04-28).
                # Drop any model that's still on cooldown for THIS
                # strategy from a previous successful fire of that
                # same model. Models with independent clocks were
                # previously sharing a single per-strategy clock,
                # which let a permissive matrix model (no confirmation
                # required) lock a stricter model out of a later
                # predicate-fire bar that the stricter model would
                # otherwise have caught — the matrix vs single-model
                # discrepancy diagnosed on the 2025-10-27 → 2026-04-24
                # post-filter run for ICT Power of 3 (Short).
                models_for_this_signal = [
                    m for m in models_for_this_signal
                    if cooldown.get((strat.name, m.name), -1) < i
                ]
                if not models_for_this_signal:
                    continue

                fired_any_model = False
                first_trade_for_history: Trade | None = None
                breaker_history_before = {
                    key: set(value) for key, value in breaker_history.items()
                }
                # Re-match timeline (research): later same-strategy fires from
                # the baseline run; only those after THIS fire bar i can manage
                # the trade we're about to open. None unless a timeline is passed.
                rematch_idx_for_signal: frozenset[int] | None = None
                if rematch_fire_timeline is not None:
                    fires = rematch_fire_timeline.get((symbol, str(day), strat.name))
                    if fires:
                        later = frozenset(f for f in fires if f > i)
                        if later:
                            rematch_idx_for_signal = later
                for exit_model in models_for_this_signal:
                    resolved_entry = _resolve_entry_index(bars, i, strat.direction, exit_model)
                    if resolved_entry is None:
                        continue
                    entry_idx, delay_bars = resolved_entry
                    trade = _simulate_trade(
                        bars,
                        entry_idx=entry_idx,
                        direction=strat.direction,
                        atr=atr,
                        exit_model=exit_model,
                        entry_delay_bars=delay_bars,
                        structural_stop_price=structural_stop_price,
                        rematch_indices=rematch_idx_for_signal,
                    )
                    if trade is None:
                        continue
                    trade.strategy = strat.name
                    trade.symbol = symbol
                    if first_trade_for_history is None:
                        first_trade_for_history = trade
                    # Phase-2 entry-feature capture. We compute on the
                    # firing bar's ``ind`` (the one that triggered the
                    # match) and on the simulated entry_price. Confirmation
                    # models may enter later, so atr_pct reflects their
                    # actual fill while the structural context stays anchored
                    # to the predicate-fire bar.
                    try:
                        feature_history = {
                            key: set(value) for key, value in breaker_history_before.items()
                        }
                        trade.entry_features = _capture_entry_features(
                            strat,
                            ind,
                            atr=atr,
                            entry_price=trade.entry_price,
                            bar_idx=i,
                            breaker_history=feature_history,
                            regime_label=regime_label,
                            regime_vol=regime_vol,
                        )
                    except Exception as exc:
                        logger.debug(
                            "entry_feature_capture_failed symbol=%s day=%s strat=%s err=%s",
                            symbol, day, strat.name, exc,
                        )
                    # ``setdefault`` covers the v7.7 case where a
                    # strategy's ``exit_model_name`` override resolves
                    # to a model NOT in the caller-supplied
                    # ``exit_models`` list (typical: caller passes one
                    # baseline, the strategy wants its preferred
                    # variant). The bucket is lazily created so the
                    # caller still sees those trades in the returned
                    # dict.
                    trades_by_model.setdefault(exit_model.name, []).append(trade)
                    fired_any_model = True
                    # v7.8 (2026-04-28): cooldown is per-model — only
                    # the model that actually entered locks itself out
                    # of subsequent fires.
                    cooldown[(strat.name, exit_model.name)] = i + COOLDOWN_BARS

                if fired_any_model:
                    # Advance first-fire state exactly once per actual setup
                    # event. Each exit model receives the same first-fire flag
                    # above via a snapshot copy; the real history is only
                    # mutated after the matrix variants have been recorded.
                    if first_trade_for_history is not None:
                        try:
                            _capture_entry_features(
                                strat,
                                ind,
                                atr=atr,
                                entry_price=first_trade_for_history.entry_price,
                                bar_idx=i,
                                breaker_history=breaker_history,
                                regime_label=regime_label,
                                regime_vol=regime_vol,
                            )
                        except Exception:
                            pass

                if fired_any_model and collect_fire_bars is not None:
                    # Record the predicate-fire bar for the re-match timeline
                    # (research hook; inert unless a collector dict is passed).
                    # One append per successful fire ⇒ Σ counts == baseline
                    # trade count — a built-in cross-check in the driver.
                    collect_fire_bars.setdefault(
                        (symbol, str(day), strat.name), [],
                    ).append(i)

    return trades_by_model


def replay_symbol(
    symbol: str,
    sessions: dict[date, list[dict]],
    prev_day_lookup: dict[date, dict] | None = None,
    *,
    daily_bars: list[dict] | None = None,
    playbook_override: list[Strategy] | None = None,
    exit_model: TradeExitModel = DEFAULT_EXIT_MODEL,
    respect_strategy_overrides: bool = True,
    exit_model_transform: object | None = None,
    collect_fire_bars: dict | None = None,
    rematch_fire_timeline: dict | None = None,
) -> list[Trade]:
    """Backward-compatible one-exit-model replay for existing callers.

    v7.7 (2026-04-27): when a Strategy declares
    ``exit_model_name``, those trades are simulated against the
    strategy's preferred exit model instead of the caller-supplied
    ``exit_model``. The caller still gets a flat list of trades; each
    trade's ``exit_model`` field records which model actually ran. Use
    ``run_exit_model_matrix`` (which keeps every model in play
    uniformly) to score exit-model alternatives, not this function.

    ``respect_strategy_overrides=False`` (2026-06-10, exit-parity replay)
    forces the supplied ``exit_model`` onto EVERY trade — modeling live,
    which applies one exit engine regardless of strategy. Default True
    preserves the validated baseline behavior.
    """

    by_model = replay_symbol_exit_models(
        symbol,
        sessions,
        prev_day_lookup=prev_day_lookup,
        daily_bars=daily_bars,
        playbook_override=playbook_override,
        exit_models=[exit_model],
        respect_strategy_overrides=respect_strategy_overrides,
        exit_model_transform=exit_model_transform,
        collect_fire_bars=collect_fire_bars,
        rematch_fire_timeline=rematch_fire_timeline,
    )
    # Flatten — strategies with overrides routed their trades into
    # other model buckets; the caller wants one consolidated list.
    all_trades: list[Trade] = []
    for trades in by_model.values():
        all_trades.extend(trades)
    return all_trades


# ── Aggregation ────────────────────────────────────────────────────────────

def _aggregate(trades: list[Trade]) -> BacktestResult:
    by_strategy: dict[str, StrategyStats] = defaultdict(lambda: StrategyStats(strategy=""))
    by_sh: dict[tuple[str, int], StrategyStats] = defaultdict(lambda: StrategyStats(strategy=""))
    by_symbol: dict[str, StrategyStats] = defaultdict(lambda: StrategyStats(strategy=""))

    def _add_trade(stats: StrategyStats, trade: Trade) -> None:
        stats.trades += 1
        stats.total_r += trade.r_multiple or 0.0
        stats.total_pnl_pct += trade.pnl_pct or 0.0
        stats.total_mae_r += trade.mae_r or 0.0
        stats.total_mfe_r += trade.mfe_r or 0.0
        stats.r_values.append(trade.r_multiple or 0.0)
        if (trade.r_multiple or 0.0) > 0:
            stats.wins += 1
        else:
            stats.losses += 1

    for t in trades:
        if t.r_multiple is None:
            continue

        s = by_strategy[t.strategy]
        s.strategy = t.strategy
        _add_trade(s, t)

        hour = t.entry_ts.hour
        key = (t.strategy, hour)
        sh = by_sh[key]
        sh.strategy = f"{t.strategy} @ {hour:02d}:00 ET"
        _add_trade(sh, t)

        sym = by_symbol[t.symbol]
        sym.strategy = t.symbol
        _add_trade(sym, t)

    return BacktestResult(
        trades=trades,
        by_strategy=dict(by_strategy),
        by_strategy_hour=dict(by_sh),
        by_symbol=dict(by_symbol),
    )


def run_backtest(
    symbols: list[str],
    start: date,
    end: date,
    client: AlpacaMarketDataClient | None = None,
    *,
    playbook_override: list[Strategy] | None = None,
    exit_model: TradeExitModel = DEFAULT_EXIT_MODEL,
    feed: str | None = None,
    respect_strategy_overrides: bool = True,
    exit_model_transform: object | None = None,
    collect_fire_bars: dict | None = None,
    rematch_fire_timeline: dict | None = None,
    cache_daily: bool = False,
) -> BacktestResult:
    """End-to-end: fetch → cache → replay → aggregate.

    ``playbook_override`` (optional) replaces the live PLAYBOOK for
    this run. Used by Phase-2 mining to re-include suspended
    strategies (Breaker Block) without un-suspending them in
    ``app/strategies.py``.

    ``exit_model`` controls stop/target/partial/confirmation behavior.
    Existing callers get the historical baseline by default.

    ``feed`` (v7.13a, 2026-05-01) overrides the bar feed for this
    single run — None means "use ``settings.alpaca_data_feed``".
    The SIP-vs-IEX parity test passes both ``"sip"`` and ``"iex"``
    in back-to-back calls; per-feed cache directories prevent the
    two from ever silently mixing.
    """
    logger.info(
        "backtest_start symbols=%d start=%s end=%s exit_model=%s feed=%s",
        len(symbols), start, end, exit_model.name,
        feed or settings.alpaca_data_feed,
    )
    data = fetch_history(symbols, start, end, client=client, feed=feed)

    # Daily bars for prev-day levels (Gap-and-Go / Prior-Day-Break).
    # We fetch once up-front so replay_symbol can look up each session's
    # prev-day in O(1). Failure is non-fatal — those two strategies just
    # won't fire, the other eight backtest normally.
    daily_by_symbol: dict[str, list[dict]] = {}
    try:
        daily_by_symbol = _fetch_daily_history(
            symbols, start, end, client=client, feed=feed, cache_daily=cache_daily,
        )
    except Exception:
        logger.warning("backtest_daily_history_failed", exc_info=True)

    all_trades: list[Trade] = []
    for sym, sessions in data.items():
        t0 = datetime.now()
        sym_daily = daily_by_symbol.get(sym, [])
        prev_day_lookup = _build_prev_day_lookup(sym_daily)
        sym_trades = replay_symbol(
            sym, sessions,
            prev_day_lookup=prev_day_lookup,
            daily_bars=sym_daily,
            playbook_override=playbook_override,
            exit_model=exit_model,
            respect_strategy_overrides=respect_strategy_overrides,
            exit_model_transform=exit_model_transform,
            collect_fire_bars=collect_fire_bars,
            rematch_fire_timeline=rematch_fire_timeline,
        )
        logger.info(
            "backtest_symbol_done symbol=%s sessions=%d trades=%d elapsed=%.1fs prev_day_days=%d",
            sym, len(sessions), len(sym_trades),
            (datetime.now() - t0).total_seconds(),
            len(prev_day_lookup),
        )
        all_trades.extend(sym_trades)

    result = _aggregate(all_trades)
    logger.info(
        "backtest_done total_trades=%d strategies=%d",
        len(all_trades), len(result.by_strategy),
    )
    return result


def run_exit_model_matrix(
    symbols: list[str],
    start: date,
    end: date,
    client: AlpacaMarketDataClient | None = None,
    *,
    playbook_override: list[Strategy] | None = None,
    exit_models: list[TradeExitModel] | None = None,
) -> dict[str, BacktestResult]:
    """Run one signal replay and score multiple exit models side by side."""

    exit_models = list(exit_models or default_exit_model_matrix())
    model_names = [m.name for m in exit_models]
    if len(model_names) != len(set(model_names)):
        raise ValueError("exit model names must be unique")

    logger.info(
        "exit_model_matrix_start symbols=%d start=%s end=%s models=%d",
        len(symbols), start, end, len(exit_models),
    )
    data = fetch_history(symbols, start, end, client=client)

    daily_by_symbol: dict[str, list[dict]] = {}
    try:
        daily_by_symbol = _fetch_daily_history(symbols, start, end, client=client)
    except Exception:
        logger.warning("backtest_daily_history_failed", exc_info=True)

    all_trades_by_model: dict[str, list[Trade]] = {m.name: [] for m in exit_models}
    for sym, sessions in data.items():
        t0 = datetime.now()
        sym_daily = daily_by_symbol.get(sym, [])
        prev_day_lookup = _build_prev_day_lookup(sym_daily)
        sym_matrix = replay_symbol_exit_models(
            sym,
            sessions,
            prev_day_lookup=prev_day_lookup,
            daily_bars=sym_daily,
            playbook_override=playbook_override,
            exit_models=exit_models,
        )
        for model_name, trades in sym_matrix.items():
            all_trades_by_model[model_name].extend(trades)
        logger.info(
            "exit_model_matrix_symbol_done symbol=%s sessions=%d elapsed=%.1fs trades_by_model=%s",
            sym,
            len(sessions),
            (datetime.now() - t0).total_seconds(),
            {name: len(trades) for name, trades in sym_matrix.items()},
        )

    results = {
        model_name: _aggregate(trades)
        for model_name, trades in all_trades_by_model.items()
    }
    logger.info(
        "exit_model_matrix_done models=%d baseline_trades=%d",
        len(results),
        len(results.get(DEFAULT_EXIT_MODEL.name, BacktestResult([], {}, {}, {})).trades),
    )
    return results


# ── Reporting ──────────────────────────────────────────────────────────────

def format_report(result: BacktestResult) -> str:
    """Return a human-readable table of per-strategy stats, sorted by expectancy.

    Losers float to the bottom — that's the cull list.
    """
    lines: list[str] = []
    width = 132

    lines.append("=" * width)
    lines.append("PER-STRATEGY RESULTS (sorted by expectancy — losers at the bottom)")
    lines.append("=" * width)
    lines.append(
        f"{'Strategy':<40} {'Trades':>7} {'Wins':>5} {'Win%':>6} "
        f"{'AvgR':>7} {'PF':>6} {'MaxDDR':>8} {'TotalR':>8} "
        f"{'MAE':>7} {'MFE':>7}"
    )
    lines.append("-" * width)

    rows = sorted(result.by_strategy.values(), key=lambda s: s.expectancy, reverse=True)
    if not rows:
        lines.append("  (no trades — widen the date range or check data cache)")
    for s in rows:
        pf = s.profit_factor
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        lines.append(
            f"{s.strategy[:40]:<40} {s.trades:>7d} {s.wins:>5d} "
            f"{s.win_rate * 100:>5.1f}% {s.avg_r:>7.3f} {pf_str:>6} "
            f"{s.max_drawdown_r:>8.2f} {s.total_r:>8.2f} "
            f"{s.avg_mae_r:>7.2f} {s.avg_mfe_r:>7.2f}"
        )

    lines.append("")
    lines.append("=" * width)
    lines.append("PER-SYMBOL RESULTS (weakest first)")
    lines.append("=" * width)
    lines.append(
        f"{'Symbol':<10} {'Trades':>7} {'Win%':>6} {'AvgR':>7} "
        f"{'TotalR':>8} {'MAE':>7} {'MFE':>7}"
    )
    lines.append("-" * width)

    rows_symbol = sorted(result.by_symbol.values(), key=lambda s: s.avg_r)
    for s in rows_symbol:
        lines.append(
            f"{s.strategy:<10} {s.trades:>7d} {s.win_rate * 100:>5.1f}% "
            f"{s.avg_r:>7.3f} {s.total_r:>8.2f} "
            f"{s.avg_mae_r:>7.2f} {s.avg_mfe_r:>7.2f}"
        )

    lines.append("")
    lines.append("=" * width)
    lines.append("PER-STRATEGY × HOUR-OF-DAY (ET, regular session)")
    lines.append("=" * width)
    lines.append(
        f"{'Strategy @ hour':<48} {'Trades':>7} {'Win%':>6} {'AvgR':>7} {'TotalR':>8}"
    )
    lines.append("-" * width)

    rows_h = sorted(
        result.by_strategy_hour.items(),
        key=lambda kv: (kv[0][0], kv[0][1]),
    )
    for _, s in rows_h:
        lines.append(
            f"{s.strategy[:48]:<48} {s.trades:>7d} "
            f"{s.win_rate * 100:>5.1f}% {s.avg_r:>7.3f} {s.total_r:>8.2f}"
        )

    lines.append("")
    return "\n".join(lines)


def format_exit_model_matrix_report(results: dict[str, BacktestResult]) -> str:
    """Compact scoreboard for stop/target/partial/confirmation experiments."""

    lines: list[str] = []
    width = 128
    lines.append("=" * width)
    lines.append("PHASE-2 STOP / TARGET / CONFIRMATION MATRIX")
    lines.append("=" * width)
    lines.append(
        f"{'Exit model':<34} {'Trades':>7} {'Win%':>7} {'AvgR':>8} "
        f"{'PF':>6} {'MaxDDR':>8} {'TotalR':>10} {'MAE':>7} {'MFE':>7} "
        f"{'Target%':>8} {'Stop%':>7} {'Partial%':>9}"
    )
    lines.append("-" * width)

    rows = []
    for model_name, result in results.items():
        stats = StrategyStats(strategy=model_name)
        for trade in result.trades:
            if trade.r_multiple is None:
                continue
            stats.trades += 1
            stats.total_r += trade.r_multiple
            stats.total_pnl_pct += trade.pnl_pct or 0.0
            stats.total_mae_r += trade.mae_r or 0.0
            stats.total_mfe_r += trade.mfe_r or 0.0
            stats.r_values.append(trade.r_multiple)
            if trade.r_multiple > 0:
                stats.wins += 1
            else:
                stats.losses += 1
        rows.append((stats.avg_r, model_name, result, stats))

    for _, model_name, result, stats in sorted(rows, key=lambda item: item[0], reverse=True):
        total = stats.trades or 1
        target_pct = 100.0 * sum(1 for t in result.trades if t.exit_reason == "target") / total
        stop_pct = 100.0 * sum(1 for t in result.trades if t.exit_reason == "stop") / total
        partial_pct = 100.0 * sum(1 for t in result.trades if t.partial_taken) / total
        pf = stats.profit_factor
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        lines.append(
            f"{model_name[:34]:<34} {stats.trades:>7d} "
            f"{stats.win_rate * 100:>6.1f}% {stats.avg_r:>8.3f} {pf_str:>6} "
            f"{stats.max_drawdown_r:>8.2f} {stats.total_r:>10.2f} "
            f"{stats.avg_mae_r:>7.2f} {stats.avg_mfe_r:>7.2f} "
            f"{target_pct:>7.1f}% {stop_pct:>6.1f}% {partial_pct:>8.1f}%"
        )

    lines.append("")
    return "\n".join(lines)


def save_trades_csv(result: BacktestResult, path: Path) -> None:
    """Dump every simulated trade to CSV for spreadsheet inspection.

    Outcome columns come first (stable schema for old readers); the
    Phase-2 entry-feature columns follow in ``ENTRY_FEATURE_COLUMNS``
    order. Trades captured before Phase-2 instrumentation
    (``entry_features=None``) emit empty cells in the new columns.
    """
    if not result.trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    def _round(value: object, digits: int = 4) -> object:
        # Numeric → rounded; non-numeric (str / bool / None) pass through.
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(value, digits)
        return value if value is not None else ""

    rows = []
    for t in result.trades:
        row = {
            "strategy": t.strategy,
            "symbol": t.symbol,
            "direction": t.direction,
            "exit_model": t.exit_model,
            "entry_ts_et": t.entry_ts.isoformat(),
            "entry_price": round(t.entry_price, 4),
            "stop_price": round(t.stop_price, 4),
            "target_price": round(t.target_price, 4),
            "exit_ts_et": t.exit_ts.isoformat() if t.exit_ts else "",
            "exit_price": round(t.exit_price, 4) if t.exit_price is not None else "",
            "exit_reason": t.exit_reason or "",
            "r_multiple": round(t.r_multiple, 4) if t.r_multiple is not None else "",
            "pnl_pct": round(t.pnl_pct, 4) if t.pnl_pct is not None else "",
            "risk_per_share": round(t.risk_per_share, 4) if t.risk_per_share is not None else "",
            "mae_r": round(t.mae_r, 4) if t.mae_r is not None else "",
            "mfe_r": round(t.mfe_r, 4) if t.mfe_r is not None else "",
            "max_adverse_price": round(t.max_adverse_price, 4) if t.max_adverse_price is not None else "",
            "max_favorable_price": round(t.max_favorable_price, 4) if t.max_favorable_price is not None else "",
            "partial_taken": t.partial_taken,
            "partial_exit_ts_et": t.partial_exit_ts.isoformat() if t.partial_exit_ts else "",
            "partial_exit_price": round(t.partial_exit_price, 4) if t.partial_exit_price is not None else "",
            "partial_fraction": round(t.partial_fraction, 4) if t.partial_fraction is not None else "",
            "entry_delay_bars": t.entry_delay_bars,
            "confirmation_bars": t.confirmation_bars,
            # Structural-stop telemetry (v7.7). ``structural_stop_used``
            # is the auditable boolean — silent ATR fallback would have
            # been invisible without it. ``_distance_r`` is the RAW
            # structural distance in R units; if < 1.0 the
            # ``stop_atr_floor`` was the binding constraint, not the
            # anchor.
            "structural_stop_used": t.structural_stop_used,
            "structural_stop_price": (
                round(t.structural_stop_price, 4)
                if t.structural_stop_price is not None else ""
            ),
            "structural_stop_distance_r": (
                round(t.structural_stop_distance_r, 4)
                if t.structural_stop_distance_r is not None else ""
            ),
        }
        feats = t.entry_features or {}
        for col in ENTRY_FEATURE_COLUMNS:
            row[col] = _round(feats.get(col))
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)


def save_exit_model_matrix_csv(results: dict[str, BacktestResult], path: Path) -> None:
    """Write all matrix trades into one CSV with ``exit_model`` as a column."""

    combined: list[Trade] = []
    for result in results.values():
        combined.extend(result.trades)
    save_trades_csv(_aggregate(combined), path)
