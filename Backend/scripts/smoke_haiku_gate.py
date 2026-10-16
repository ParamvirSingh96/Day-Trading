"""
smoke_haiku_gate.py — Verify the Haiku Tier-1.5 gate observability + the
2026-04-25 prompt/payload changes:

  1. Per-cycle counters (``haiku_calls`` / ``haiku_none_count`` /
     ``haiku_reject_count``) live on ``AutoTradeState`` and are exposed
     via ``_haiku_gate_status_block`` with the right shape.

     * ``calls == 0`` → both rates are ``None`` (cold-cycle neutral).
     * ``none_rate > 0.05`` flips ``infra_alert``.
     * ``reject_rate < 0.50`` (with calls ≥ 4) flips ``leniency_alert``.

  2. ``haiku_screener._build_prompt`` accepts and renders the four new
     structural fields (``vol_ratio`` / ``ict_killzone`` / ``regime`` /
     ``rs_zscore``) — including the missing/None paths.

  3. The "Default to NOT VALID" wording is in the prompt template and
     the old "lean VALID" instruction is gone.

  4. ``_parse_verdict`` still round-trips a basic JSON response.

The script does NOT make any network calls — it only exercises the
in-process helpers. Mirror the structure of ``smoke_ict_v7_playbook.py``:
PASS / FAIL lines per assertion, exit 0 only when every assertion
passes.
"""

from __future__ import annotations

import sys
import traceback


# Each ``check`` invocation increments this counter; ``done`` exits with a
# non-zero code if any failed.
_FAILS: list[str] = []


def check(label: str, ok: bool, *, detail: str = "") -> None:
    if ok:
        print(f"  [PASS] {label}")
    else:
        suffix = f" — {detail}" if detail else ""
        print(f"  [FAIL] {label}{suffix}")
        _FAILS.append(label)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    # ── Import targets ──
    from app.autotrade import (
        AutoTradeState,
        _haiku_gate_status_block,
        state as _autotrade_state,
    )
    from app.haiku_screener import (
        _build_prompt,
        _format_bar_lines,
        _parse_verdict,
        _PROMPT,
        _RECENT_BARS_1MIN,
        _RECENT_BARS_5MIN,
        _resample_to_5min,
        screen_setup,
    )

    # ── 1. AutoTradeState carries the new counter fields ──
    section("AutoTradeState haiku counters")
    s = AutoTradeState()
    check("haiku_calls defaults to 0",        s.haiku_calls == 0)
    check("haiku_none_count defaults to 0",   s.haiku_none_count == 0)
    check("haiku_reject_count defaults to 0", s.haiku_reject_count == 0)

    # ── 2. _haiku_gate_status_block shape ──
    section("_haiku_gate_status_block shape + alerts")

    # Snapshot + restore the live state so this smoke script never
    # leaks counter mutations into a follow-up scan.
    saved = (
        _autotrade_state.haiku_calls,
        _autotrade_state.haiku_none_count,
        _autotrade_state.haiku_reject_count,
    )
    try:
        # Cold cycle: 0 calls → rates should be None and alerts False.
        _autotrade_state.haiku_calls = 0
        _autotrade_state.haiku_none_count = 0
        _autotrade_state.haiku_reject_count = 0
        cold = _haiku_gate_status_block()
        check("cold cycle calls == 0",          cold["calls"] == 0)
        check("cold cycle none_rate is None",   cold["none_rate"] is None)
        check("cold cycle reject_rate is None", cold["reject_rate"] is None)
        check("cold cycle infra_alert False",   cold["infra_alert"] is False)
        check("cold cycle leniency_alert False", cold["leniency_alert"] is False)

        # Healthy mid-cycle: 10 calls, 0 none, 7 rejects = 70% reject.
        _autotrade_state.haiku_calls = 10
        _autotrade_state.haiku_none_count = 0
        _autotrade_state.haiku_reject_count = 7
        healthy = _haiku_gate_status_block()
        check("healthy reject_rate == 0.7",     healthy["reject_rate"] == 0.7)
        check("healthy none_rate == 0.0",       healthy["none_rate"] == 0.0)
        check("healthy infra_alert False",      healthy["infra_alert"] is False)
        check("healthy leniency_alert False",   healthy["leniency_alert"] is False)

        # Infra rot: 10 calls, 2 none = 20% > 5% threshold.
        _autotrade_state.haiku_calls = 10
        _autotrade_state.haiku_none_count = 2
        _autotrade_state.haiku_reject_count = 5
        infra = _haiku_gate_status_block()
        check("infra none_rate == 0.2",         infra["none_rate"] == 0.2)
        check("infra_alert True at 20% none",   infra["infra_alert"] is True)

        # Boundary: 10 calls, exactly 0.5 (5%) none → should NOT alert
        # (the threshold is strictly > 5%, not >=).
        _autotrade_state.haiku_calls = 100
        _autotrade_state.haiku_none_count = 5
        _autotrade_state.haiku_reject_count = 60
        boundary = _haiku_gate_status_block()
        check(
            "infra_alert False at exactly 5% none (strict > threshold)",
            boundary["infra_alert"] is False,
            detail=f"none_rate={boundary['none_rate']}",
        )

        # Lenient gate: 10 calls, 4 rejects = 40% < 50%.
        _autotrade_state.haiku_calls = 10
        _autotrade_state.haiku_none_count = 0
        _autotrade_state.haiku_reject_count = 4
        lenient = _haiku_gate_status_block()
        check("lenient reject_rate == 0.4",     lenient["reject_rate"] == 0.4)
        check("leniency_alert True at 40%",     lenient["leniency_alert"] is True)

        # Sub-min sample (calls < 4) should NEVER trip leniency_alert
        # even if reject_rate looks bad — too noisy.
        _autotrade_state.haiku_calls = 2
        _autotrade_state.haiku_none_count = 0
        _autotrade_state.haiku_reject_count = 0
        small_n = _haiku_gate_status_block()
        check("leniency_alert False when calls < 4",
              small_n["leniency_alert"] is False)
    finally:
        (_autotrade_state.haiku_calls,
         _autotrade_state.haiku_none_count,
         _autotrade_state.haiku_reject_count) = saved

    # ── 3. Prompt template includes the new wording ──
    section("Prompt template wording")
    check(
        "prompt contains 'Default to NOT VALID' (default-NOT-VALID rewrite)",
        "Default to NOT VALID" in _PROMPT,
    )
    check(
        "prompt names two-fact requirement",
        "two specific recent-bar facts" in _PROMPT,
    )
    check(
        "prompt no longer says 'lean VALID' (old wording removed)",
        "lean VALID" not in _PROMPT,
    )
    check(
        "prompt includes structural context placeholder",
        "{structural}" in _PROMPT,
    )
    check(
        "prompt mentions vol_ratio rejection criterion",
        "vol_ratio < 0.8" in _PROMPT,
    )
    check(
        "prompt mentions regime veto criterion",
        "regime=trending_down" in _PROMPT,
    )
    check(
        "prompt mentions rs_zscore criterion",
        "rs_zscore" in _PROMPT,
    )
    check(
        "prompt mentions ict_killzone None case",
        "ict_killzone is None" in _PROMPT,
    )
    # 1-min + 5-min block placeholders + section labels.
    check("prompt has separate 1-min bar block placeholder",
          "{bars_1min}" in _PROMPT)
    check("prompt has separate 5-min bar block placeholder",
          "{bars_5min}" in _PROMPT)
    check("prompt labels the 1-min section",
          "Recent 1-min price action" in _PROMPT)
    check("prompt labels the 5-min section",
          "Recent 5-min price action" in _PROMPT)
    check("old single-block placeholder removed",
          "{bars}" not in _PROMPT.replace("{bars_1min}", "").replace("{bars_5min}", ""))
    check("constants split into 1-min + 5-min caps",
          _RECENT_BARS_1MIN == 50 and _RECENT_BARS_5MIN == 50)

    # ── 4. _resample_to_5min behavior ──
    section("_resample_to_5min behavior")

    # Empty input → empty output.
    check("empty bars → empty resample",
          _resample_to_5min([]) == [])

    # Bars without timestamps → skipped (can't bucket).
    timeless = [{"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]
    check("timestamp-less bar → skipped (empty result)",
          _resample_to_5min(timeless) == [])

    # Five aligned 1-min bars (00:00..00:04) → one 5-min bucket.
    # Bucket O = first.O, H = max, L = min, C = last.C, V = sum, time = 0.
    aligned5 = [
        {"time": 0,   "open": 100, "high": 101, "low": 99,  "close": 100.5, "volume": 1000},
        {"time": 60,  "open": 100.5, "high": 102, "low": 100, "close": 101, "volume": 1500},
        {"time": 120, "open": 101, "high": 103, "low": 100.5, "close": 102, "volume": 800},
        {"time": 180, "open": 102, "high": 102.5, "low": 101, "close": 101.5, "volume": 2000},
        {"time": 240, "open": 101.5, "high": 102, "low": 100, "close": 101, "volume": 1200},
    ]
    out5 = _resample_to_5min(aligned5)
    check("5 aligned 1-min bars → 1 5-min bar",
          len(out5) == 1)
    check("5-min open == first 1-min open",     out5[0]["open"]  == 100)
    check("5-min high == max of 1-min highs",   out5[0]["high"]  == 103)
    check("5-min low  == min of 1-min lows",    out5[0]["low"]   == 99)
    check("5-min close == last 1-min close",    out5[0]["close"] == 101)
    check("5-min volume == sum of 1-min vols",  out5[0]["volume"] == 6500)
    check("5-min bucket time == bucket start (0)",
          out5[0]["time"] == 0)

    # Six 1-min bars spanning two buckets (last bar at 00:05 starts bucket 2).
    spanning = aligned5 + [
        {"time": 300, "open": 101, "high": 101.5, "low": 100.5, "close": 101.2, "volume": 700},
    ]
    out_span = _resample_to_5min(spanning)
    check("6 1-min bars spanning two buckets → 2 5-min bars",
          len(out_span) == 2)
    check("second bucket starts at 300",
          out_span[1]["time"] == 300)
    check("second bucket carries the trailing bar",
          out_span[1]["open"] == 101 and out_span[1]["close"] == 101.2)

    # Gap handling: bars at 0, 60, 600 → buckets [0, 600], no zero-fill at 300.
    gappy = [
        {"time": 0,   "open": 100, "high": 100, "low": 100, "close": 100, "volume": 100},
        {"time": 60,  "open": 100, "high": 100, "low": 100, "close": 100, "volume": 100},
        {"time": 600, "open": 110, "high": 110, "low": 110, "close": 110, "volume": 100},
    ]
    out_gap = _resample_to_5min(gappy)
    check("gap of one full bucket → no zero-fill",
          len(out_gap) == 2)
    check("gap result preserves bucket starts (0 and 600)",
          [b["time"] for b in out_gap] == [0, 600])

    # Partial last bucket: 7 bars (5 in bucket 0 + 2 in bucket 1) — both kept.
    partial = aligned5 + [
        {"time": 300, "open": 101, "high": 102, "low": 101, "close": 101.5, "volume": 500},
        {"time": 360, "open": 101.5, "high": 102, "low": 101.4, "close": 101.8, "volume": 400},
    ]
    out_partial = _resample_to_5min(partial)
    check("partial last bucket is kept (length 2)",
          len(out_partial) == 2)
    check("partial bucket close == last partial bar close",
          out_partial[1]["close"] == 101.8)
    check("partial bucket volume sums only its own bars",
          out_partial[1]["volume"] == 900)

    # Output is chronologically sorted regardless of input order.
    shuffled = [aligned5[2], aligned5[0], aligned5[4], aligned5[1], aligned5[3]]
    out_shuffled = _resample_to_5min(shuffled)
    # Note: open/close are technically order-dependent on the input list,
    # but we still want bucket time correct + ohlc(v) values reasonable.
    check("shuffled input → still 1 bucket at time 0",
          len(out_shuffled) == 1 and out_shuffled[0]["time"] == 0)
    check("shuffled input → same volume sum (order-invariant)",
          out_shuffled[0]["volume"] == 6500)
    check("shuffled input → same high/low extremes",
          out_shuffled[0]["high"] == 103 and out_shuffled[0]["low"] == 99)

    # Raw Alpaca shape (`o`/`h`/`l`/`c`/`v` + `t` ISO string) is accepted.
    raw = [
        {"t": "2026-04-25T13:30:00Z", "o": 50, "h": 51, "l": 49, "c": 50.5, "v": 100},
        {"t": "2026-04-25T13:31:00Z", "o": 50.5, "h": 51.5, "l": 50, "c": 51, "v": 200},
    ]
    out_raw = _resample_to_5min(raw)
    check("raw Alpaca shape (o/h/l/c/v + ISO t) → 1 bucket",
          len(out_raw) == 1)
    check("raw Alpaca high carried through correctly",
          out_raw[0]["high"] == 51.5)

    # Bars missing OHLC (only 'v') → skipped, not coerced to 0.
    bad = [{"time": 0, "v": 1000}]
    check("bar missing OHLC → skipped",
          _resample_to_5min(bad) == [])

    # ── 5. _format_bar_lines + dual-block prompt rendering ──
    section("_build_prompt dual bar blocks")

    # _format_bar_lines smoke: empty → sentinel; well-formed → indented OHLCV.
    check("_format_bar_lines([]) → '(no bar data)'",
          _format_bar_lines([]) == "  (no bar data)")
    one_line = _format_bar_lines([
        {"open": 100.12, "high": 101.34, "low": 99.5, "close": 100.7, "volume": 12345}
    ])
    check("_format_bar_lines renders OHLCV with 2dp + comma volume",
          "O=100.12 H=101.34 L=99.50 C=100.70 V=12,345" in one_line)

    # Build a 30-bar 1-min stream covering exactly 6 aligned 5-min buckets.
    # Anchor ``base`` to a 5-min boundary so the count test isn't sensitive
    # to the offset within a bucket. The unaligned case is exercised
    # separately below to lock in the partial-bucket behavior.
    base_unaligned = 1_700_000_000
    base = base_unaligned - (base_unaligned % 300)  # align to 5-min UTC boundary
    long_bars = []
    for i in range(30):
        long_bars.append({
            "time":   base + i * 60,
            "open":   100 + i * 0.1,
            "high":   100 + i * 0.1 + 0.2,
            "low":    100 + i * 0.1 - 0.2,
            "close":  100 + i * 0.1 + 0.05,
            "volume": 1000 + i,
        })
    common = dict(
        symbol="NVDA",
        price=120.0,
        direction="long",
        matched_strategies=[{"name": "ICT Judas Swing", "direction": "long"}],
        bars=long_bars,
        daily_bars=[],
    )

    rendered_full = _build_prompt(
        **common,
        vol_ratio=1.25,
        ict_killzone="ny_am",
        regime="trending_up",
        rs_zscore=0.85,
    )
    # Both block headings are present
    check("prompt body contains the 1-min heading",
          "Recent 1-min price action" in rendered_full)
    check("prompt body contains the 5-min heading",
          "Recent 5-min price action" in rendered_full)

    # Count OHLCV lines under each heading. Split on the heading prefix
    # so we can count "O=" occurrences within each region.
    h1 = rendered_full.index("Recent 1-min price action")
    h5 = rendered_full.index("Recent 5-min price action")
    section_1min = rendered_full[h1:h5]
    section_5min = rendered_full[h5:]
    n_1min_lines = section_1min.count("O=")
    n_5min_lines = section_5min.count("O=")
    # 30 1-min bars → 30 in the 1-min block (cap is 50 so all fit)
    check(f"1-min block renders all 30 input bars (got {n_1min_lines})",
          n_1min_lines == 30)
    # 30 1-min bars at 60-second cadence on an aligned base → 6 5-min buckets
    check(f"aligned base: 5-min block renders 6 resampled bars (got {n_5min_lines})",
          n_5min_lines == 6)

    # Unaligned base — the more realistic intraday case, since Alpaca bar
    # timestamps don't necessarily land on a 5-min UTC boundary. 30 1-min
    # bars starting 200s into a bucket → 7 buckets (partial-leading +
    # 5 full + partial-trailing). Both partials must be kept so the
    # freshest context survives.
    long_bars_unaligned = []
    for i in range(30):
        long_bars_unaligned.append({
            "time":   base_unaligned + i * 60,
            "open":   100, "high": 100.5, "low": 99.5, "close": 100.1,
            "volume": 1,
        })
    out_unaligned = _resample_to_5min(long_bars_unaligned)
    check(
        f"unaligned base: 30 1-min bars → 7 buckets (got {len(out_unaligned)})",
        len(out_unaligned) == 7,
    )
    # Total volume across buckets must equal sum of input volumes (no leakage).
    check("unaligned resample preserves total volume",
          sum(b["volume"] for b in out_unaligned) == 30)

    # Backwards-compat path: bars with no timestamps still render the
    # 1-min block but produce an empty 5-min block ("(no bar data)").
    timeless_common = dict(
        symbol="NVDA",
        price=120.0,
        direction="long",
        matched_strategies=[{"name": "ICT Judas Swing", "direction": "long"}],
        bars=[{"o": 119, "h": 121, "l": 118, "c": 120, "v": 1_500_000}],
        daily_bars=[],
    )
    rendered_timeless = _build_prompt(**timeless_common)
    h5_t = rendered_timeless.index("Recent 5-min price action")
    section_5min_t = rendered_timeless[h5_t:]
    check("timeless bars → 5-min block falls back to '(no bar data)'",
          "(no bar data)" in section_5min_t)
    check("timeless bars → 1-min block still renders",
          "O=119.00 H=121.00 L=118.00 C=120.00 V=1,500,000" in rendered_timeless)

    # 1-min cap: feeding 80 bars renders only the most recent _RECENT_BARS_1MIN.
    many = []
    for i in range(80):
        many.append({
            "time":   base + i * 60,
            "open":   100,
            "high":   100.5,
            "low":    99.5,
            "close":  100.1,
            "volume": 1,
        })
    rendered_many = _build_prompt(
        symbol="NVDA",
        price=120.0,
        direction="long",
        matched_strategies=[{"name": "X", "direction": "long"}],
        bars=many,
        daily_bars=[],
    )
    h1m = rendered_many.index("Recent 1-min price action")
    h5m = rendered_many.index("Recent 5-min price action")
    section_1min_m = rendered_many[h1m:h5m]
    n_capped = section_1min_m.count("O=")
    check(f"1-min block capped at _RECENT_BARS_1MIN (got {n_capped})",
          n_capped == _RECENT_BARS_1MIN)

    # ── 6. _build_prompt structural block rendering (existing v7.3 checks) ──
    section("_build_prompt structural block rendering")
    common = dict(
        symbol="NVDA",
        price=120.0,
        direction="long",
        matched_strategies=[{"name": "ICT Judas Swing", "direction": "long"}],
        bars=[{"o": 119, "h": 121, "l": 118, "c": 120, "v": 1_500_000}],
        daily_bars=[],
    )
    rendered_full = _build_prompt(
        **common,
        vol_ratio=1.25,
        ict_killzone="ny_am",
        regime="trending_up",
        rs_zscore=0.85,
    )
    check("renders vol_ratio formatted to 2dp",
          "vol_ratio: 1.25" in rendered_full)
    check("renders ict_killzone label",
          "ict_killzone: ny_am" in rendered_full)
    check("renders regime label",
          "regime: trending_up" in rendered_full)
    check("renders rs_zscore with sign + pp",
          "rs_zscore: +0.85 pp" in rendered_full)
    check("renders structural section header",
          "Structural context:" in rendered_full)

    # Negative rs_zscore should keep the sign.
    rendered_neg = _build_prompt(
        **common,
        vol_ratio=0.55,
        ict_killzone="silver_bullet",
        regime="trending_down",
        rs_zscore=-1.40,
    )
    check("renders negative rs_zscore",
          "rs_zscore: -1.40 pp" in rendered_neg)
    check("renders sub-floor vol_ratio (0.55)",
          "vol_ratio: 0.55" in rendered_neg)

    # All-None path: every field should still render with a fallback.
    rendered_none = _build_prompt(
        **common,
        vol_ratio=None,
        ict_killzone=None,
        regime=None,
        rs_zscore=None,
    )
    check("None vol_ratio renders 'n/a'",
          "vol_ratio: n/a" in rendered_none)
    check("None ict_killzone renders 'none'",
          "ict_killzone: none" in rendered_none)
    check("None regime renders 'unknown'",
          "regime: unknown" in rendered_none)
    check("None rs_zscore renders 'n/a'",
          "rs_zscore: n/a" in rendered_none)

    # Backwards-compat: caller may omit the 4 new kwargs entirely (old
    # call sites). Should default cleanly to the 'n/a' / 'none' /
    # 'unknown' rendering.
    rendered_default = _build_prompt(**common)
    check("default kwargs render 'n/a' for vol_ratio",
          "vol_ratio: n/a" in rendered_default)
    check("default kwargs render 'unknown' for regime",
          "regime: unknown" in rendered_default)

    # ── 5. screen_setup smoke (no API call — just the early returns) ──
    section("screen_setup early-return guards")
    # Empty matched_strategies → return None without calling the API.
    v_empty = screen_setup(
        "TEST",
        price=10.0,
        direction="long",
        matched_strategies=[],
        bars=[],
        daily_bars=[],
        vol_ratio=1.2,
        ict_killzone="ny_am",
        regime="trending_up",
        rs_zscore=0.5,
    )
    check("screen_setup returns None on empty matched_strategies",
          v_empty is None)

    # Bad direction → return None.
    v_bad_dir = screen_setup(
        "TEST",
        price=10.0,
        direction="wait",
        matched_strategies=[{"name": "X", "direction": "long"}],
        bars=[],
        daily_bars=[],
    )
    check("screen_setup returns None on direction='wait'",
          v_bad_dir is None)

    # ── 6. _parse_verdict still parses a clean JSON response ──
    section("_parse_verdict round-trip")
    p_yes = _parse_verdict('{"valid": true,  "reason": "fresh sweep + vol 1.4"}',
                           symbol="X")
    p_no  = _parse_verdict('{"valid": false, "reason": "ext-hours, no rth confirm"}',
                           symbol="X")
    check("parse_verdict valid=True",  p_yes is not None and p_yes.valid is True)
    check("parse_verdict valid=False", p_no  is not None and p_no.valid  is False)
    p_garbage = _parse_verdict('not json at all', symbol="X")
    check("parse_verdict garbage → None", p_garbage is None)

    # ── Done ──
    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} assertion(s) failed.")
        return 1
    print("All Haiku-gate smoke checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
