"""
TradeHack Strategy Playbook — live snapshot pre-screen.
========================================================
Each strategy defines:
  • name / direction / description          — what the setup is
  • entry_rules / stop_rules / target_rules — how to trade it
  • matches(indicators) → bool              — fast pre-screen (Tier 1)

The scanner runs every strategy's `matches()` against live indicator data.
When one or more strategies match, Claude is called with the matching
strategy context so it knows *exactly* what setup to evaluate.

This engine is a *snapshot pre-screen* — it consumes a flat indicator dict
describing the current instant (no time series) and returns a boolean.
Its sister file ``setups.py`` is the historical DataFrame engine used by
the CSV-upload scanner (ORB / VWAP reclaim / EMA trend pullback as
DataFrame detectors). The two engines are now decoupled — the live
playbook is ICT-only, the upload path still runs the classic detectors.

Predicate names here carry a ``_prescreen`` suffix to make it obvious in
logs / debugging that this is the snapshot filter, not the historical
detector in setups.py.

Add new strategies by appending to PLAYBOOK at the bottom.

## Playbook history

v4 (2025-10 backtest) — pruned 8 of 15 losers using AvgR < −0.25 cut.
v5 (2026-04-21) — added two survivors (Failed Breakout Short, Opening
    Range Retest Long) and dropped three failed additions.
v6 (2026-04-24) — **pared to ICT-only.** The nine non-ICT strategies
    (Gap-and-Go L/S, Gap Fill S, Oversold Bounce, Overbought Fade, ORB
    Breakdown, Prior Day Low Break, Failed Breakout S, Opening Range
    Retest L) were removed from the playbook after the v5 Tier-2 lift
    sample showed only ICT Sweep+MSS setups cleared Sonnet's 0.70
    confidence gate. Their predicates and Strategy entries were deleted
    from this file — recover from git history if needed. The three ICT
    setups remain as the entire live-scanner playbook going forward.

    The legacy CSV-upload scanner and its classic detectors (ORB / VWAP
    reclaim / EMA pullback) were deprecated in v7.12.2 and deleted in the
    pre-publication cleanup. The live ICT scanner (audited under
    v7.10/v7.11/v7.12/v7.12.1) is the only signal source.

v7 (2026-04-24) — **expanded the ICT catalog to cover the full classic
    playbook.** Added 12 strategies built on the infrastructure shipped
    with the RTH-split + liquidity-level rebuild:

      • Judas Swing (L/S)         — first 30-min sweep of a pre-market
                                     extreme then reversal MSS.
      • Turtle Soup (L/S)         — PDH/PDL-specific sweep + MSS.
      • Asian-Range Breakout (L/S)— RTH break of overnight 18:00→04:00 ET range.
      • Power of 3 (L/S)          — opening-range sweep → MSS → expansion beyond OR.
      • Breaker Block (L/S)       — retest of a mitigated, MSS-flipped OB.
      • Multi-day PD (L/S)        — OTE retrace of the 20-day daily range.

    Two canonical ICT setups are deliberately deferred:
      • **SMT Divergence (ES/NQ)** — requires paired instrument feeds
        (e.g. SPY vs QQQ, or ES vs NQ) in the Tier-1 bar pipeline. We
        don't carry cross-instrument synchronization yet, so a predicate
        would be false-confident. See the `_ict_smt_not_implemented`
        stub at the bottom for the flag to flip when the feed is
        wired.
      • **London Killzone** — 02:00–05:00 ET is outside US equity RTH.
        The label ships via ``killzone()`` for when we extend to futures
        or FX, but no standalone predicate fires during it.

    These ship cold with no in-sample backtest. Regime weights default
    to 1.0 everywhere and hour weights seed the classic killzone bias
    (boost killzones, damp lunch) — retune after the first month of
    data lands.

v7.1 (2026-04-25, morning) — **all time-of-day filtering removed.**
    Three classes of clock logic were stripped:

      * Predicate-level time gates (Silver Bullet killzone, Judas
        first-30-min, Asian-Range Breakout RTH-only, Power-of-3
        RTH-only) — predicates now fired purely on structural
        criteria.
      * Hour-of-day multiplier (``hour_weights.py``) — neutralized
        to a no-op that always returns 1.0.
      * Regime-weight time-of-day cells unchanged (regime is a
        market-state input, not a time input).

    Side effect: ``ICT Silver Bullet (Long)`` became structurally
    identical to ``ICT Sweep + MSS (Long)`` — only difference was
    the higher ``min_confidence=0.60`` floor.

v7.2 (2026-04-25, afternoon) — **partial revert + duplicate removed.**
    After running v7.1 we walked back the time-gate removals on
    three predicates that need their session context to make sense:

      * Judas Swing (L/S) — time gate restored to first 30 min of
        RTH. The whole thesis is "they faked the open in the wrong
        direction"; firing it at 14:00 ET is just a sweep-and-MSS,
        not a Judas.
      * Asian-Range Breakout (L/S) — RTH-only gate restored. The
        setup is by definition an RTH break of the overnight range.
      * Power of 3 (L/S) — RTH-only gate restored. The thesis is
        "manipulation→expansion within the RTH session" — outside
        RTH there's no opening range to break.

    Kept from v7.1:
      * Hour-of-day multiplier stays a 1.0 no-op.
      * Silver Bullet killzone stays REMOVED — the long-side setup
        runs all session.

    Duplicate removal: with the Silver Bullet killzone gone, the
    long-side ``ICT Sweep + MSS (Long)`` predicate was structurally
    identical to ``ICT Silver Bullet (Long)``. We deleted the
    Sweep+MSS Long entry (and its ``_ict_sweep_mss_long`` predicate)
    and kept the higher-confidence ``min_confidence=0.60`` Silver
    Bullet variant — playbook count went 15 → 14. The short side
    has no Silver Bullet counterpart, so ``ICT Sweep + MSS (Short)``
    survives unchanged.

v7.3 (2026-04-25 evening) — **Tier-1 win-rate fixes from the
    post-mortem.** Four cumulative changes:

      1. Volume-confirmation floor on every ICT predicate.
         ``_ict_volume_ok`` requires ``vol_ratio >= 0.8`` (current
         bar vs 20-bar mean). Cheapest single fix — the lowest-
         conviction Tier-1 matches were heavily clustered on dead-
         tape bars.
      2. Sweep-magnitude floor inside ``_ict_recent``. A "sweep"
         whose wick excursion past the level is < 0.25× ATR is
         rejected as noise, not a real stop-run. The ICT
         ``SweepEvent`` was extended with ``wick_extreme`` so
         the magnitude is exposed at predicate time.
      3. Hard regime VETO (not multiplier) on direction-vs-regime
         conflict. Long trades when ``regime == trending_down``
         (and short trades when ``trending_up``) are now rejected
         outright before any further scoring — see
         ``regime.is_regime_vetoed``. The soft multiplier remains
         for choppy/unknown regimes where it does the right thing.
      4. ``ICT Silver Bullet (Long)`` SUSPENDED from the live
         PLAYBOOK pending an investigation into a 23.8% win rate
         on n=80 trades clustered at 10:00 ET. The predicate
         function lives on; re-enabling it is a one-line PLAYBOOK
         edit. Playbook count went 14 → 13.

v7.4 (2026-04-26, morning) — **edge-detection on breaker-zone
    predicates.** ``_nearest_breaker`` (in ``app/ict``) now takes
    ``prev_price`` and returns a ``fresh_entry: bool`` flag — True
    only when the prior bar's close was OUTSIDE the breaker zone
    and the current bar's close is INSIDE. Both
    ``_ict_breaker_block_long`` and ``_ict_breaker_block_short``
    gate on ``fresh_entry``. Intent: stop the predicate from
    re-firing every bar while price sits inside a wide breaker.

v7.5 (2026-04-26, afternoon) — **backtester scope expansion** so
    the previously zero-trade strategies (Judas, Asian-Range,
    Multi-Day PD) could actually pre-screen:

      * WARMUP_BARS lowered 60 → 15 in ``strategy_backtest.py``
        (matches the live scanner / ``compute_ict_features``
        floor). At 60 bars the first replay tick was 10:30 ET
        on a 1-min feed — past the 10:00 ET Judas cutoff.
      * Backtester session fetch widened to 18:00 prev day →
        16:00 ET (22 hours, includes the 18:00→04:00 ET Asian
        range). Cache dir bumped ``bars_cache`` →
        ``bars_cache_ext`` to avoid silently loading old RTH-
        only files under the new schema.
      * 20-day daily-bar window threaded through ``ict_seed``
        as ``20d_high`` / ``20d_low`` so ``_multi_day_range``
        in the ICT feature layer can compute the daily PD
        zone Multi-Day PD (L/S) needs.

v7.6 (2026-04-26, evening) — **Breaker Block (L/S) suspended.**
    The v7.5 backtest showed Breaker Block accounted for 89% of
    total losses (8,790 trades / -2,705 R combined) at 31–33%
    win rates. The v7.4 ``fresh_entry`` gate only cut trades
    2.5% — price oscillates in/out of breaker zones too often
    in choppy action for one-fire-per-entry to be a structural
    fix. Suspended both PLAYBOOK entries (predicates kept);
    proper fix is a per-mitigated-zone cooldown. Playbook count
    went 13 → 11.

v7.7 (2026-04-27) — **Asian-Range Breakout (L/S) suspended;
    Failed-Asian-Range-Breakout (L/S) + Retest-ARB (L/S)
    introduced as RESEARCH-ONLY variants (NOT in live
    PLAYBOOK; promotion gated on factorial-experiment
    evidence); per-strategy exit models + structural stops
    introduced.**

    The Move-4 live-PLAYBOOK exit-model matrix
    (``scripts/run_playbook_exit_matrix.py``, 18 symbols × 13
    exit models × 6-month window) revealed two separate
    findings:

      1. No single exit model is a system-wide upgrade.
         ``confirm1_s15_t25`` wins 4 strategies; ``confirm2``
         wins 3; ``s25_t25`` / ``s20_t30`` / ``s20_t20`` /
         ``s20_t25_partial1r50_be`` each win 1. The book
         wants per-strategy stop/target configuration, not a
         global default.
      2. **ARB shows the same MAE > MFE counter-edge
         signature that flagged BB.** ARB Long: AvgMAE +1.123
         vs AvgMFE +0.979 over n=570. ARB Short: +1.100 vs
         +0.982 over n=527. After 1,097 ARB signals the
         average trade has moved further AGAINST entry than
         FOR entry. No stop variant fixes this — best
         portfolio AvgR for ARB Long is -0.216 (s25_t25),
         best for ARB Short is -0.079 (confirm1_s15_t25).
         ARB drove -257.6 R of the live book's -324.4 R
         baseline bleed.

    The framing shift (driven by the Phase-2 + Move-4
    findings on BB and ARB jointly): a counter-edge predicate
    can't be saved by stop tuning. Either the event is a
    signal in the OTHER direction, or it's only context, not
    an entry. Six structural moves follow from this.

    **(1) Suspend the bleeding predicate before optimizing
    it.** Legacy ARB Long and ARB Short are SUSPENDED
    (mirror of the BB pattern): the ``Strategy(...)`` blocks
    become a NOTE comment, the predicate functions
    (``_ict_asian_range_breakout_long`` / ``_short``) stay
    callable for ``playbook_override``-driven research. No
    further tuning of the original predicate happens until
    inversion + retest experiments rule out a structural
    fix; only then does it come back, and only with a
    redefined trigger.

    **(2) Failed-Asian-Range-Breakout (L/S) — the natural
    inversion of ARB.** When price takes asian_high / low
    and gets rejected back inside the range, the move is in
    the opposite direction of the original predicate. In
    this codebase a ``SweepEvent`` with
    ``level_name="asian_high"`` and ``direction="bearish"``
    already encodes "price closed back below asian_high
    after taking the wick above" — the failed-breakout-short
    signal. Same for ``"asian_low"``/``"bullish"`` →
    failed-breakout-long. The new
    ``_ict_failed_asian_range_breakout_long`` /
    ``_short`` predicates fire on those sweeps in RTH with
    a corresponding MSS confirming the structure flipped
    after the failure. RESEARCH-ONLY: the predicate functions
    are wired to the inversion driver via
    ``playbook_override`` but DO NOT enter the live PLAYBOOK
    until the factorial experiment matrix proves positive
    out-of-sample expectancy under the promotion gates listed
    in move (6).

    **(3) Retest-ARB (L/S) — break + pullback + hold
    continuation entry.** The legacy ARB took the break
    itself; that's "buy the breakout" energy and bled
    accordingly. Retest-ARB requires the level to first be
    BREACHED then RETESTED with a hold — i.e. price ran
    through asian_high, pulled back to it, then held the
    level (a bullish sweep of ``asian_high``'s wick: dipped
    below, close came back above). The hold is what the
    failure mode of legacy ARB was missing. Encoded as:
    bullish sweep of ``asian_high`` (wick below the level,
    close back above) AND current price still ABOVE
    ``asian_high`` (didn't fail the retest after all) AND a
    recent bullish MSS (the broader bullish structure was
    in place before the retest). Mirror short: bearish sweep
    of ``asian_low`` with current price still BELOW
    ``asian_low`` + recent bearish MSS. Stop anchor is the
    Asian-range level itself (a re-entry into the range
    invalidates the retest-hold thesis), wired via
    ``_stop_anchor_asian_range``.

    **(4) Structural stops, not ATR-only stops.** The
    counter-edge signature (MAE > MFE on average) means
    the AVERAGE trade has moved further against entry than
    for it before any stop fires — but that's an average
    over many trade lengths. A subset of trades with a
    structural-stop placement (stop ABOVE the swept wick on
    a Failed-ARB short, BELOW the asian_high reclaim on a
    Retest-ARB long) gets invalidated faster and at a more
    meaningful price than ``entry − 1.5×ATR``. ATR stops
    are arbitrary distances; structural stops are at a
    price the market has already "decided about". Wired via
    a new ``structural_stop_fn: object | None`` field on
    ``Strategy`` (default None → ATR-only behavior, exactly
    as before). When set, the backtester reads the
    structural anchor price from ``ind`` at entry time and
    uses ``max(structural_distance, atr × stop_atr_floor)``
    so a too-tight structural anchor still gets bumped out
    to a sane distance. ``stop_atr_floor`` is a new
    TradeExitModel field; default 0.5 (half the baseline
    1.5×ATR) keeps structural stops from being so tight
    they get clipped on the entry bar's noise. Used in
    research only this round (Failed-ARB / Retest-ARB are
    not yet live); the inversion-vs-retest factorial
    experiment runs the structural-stop variants alongside
    the ATR-only variants so the per-trade telemetry
    (``structural_stop_used``, ``structural_stop_distance_r``
    on ``Trade``) lets us attribute lift to the stop model
    independently from the predicate / direction.

    **(5) Per-strategy exit models.** Move-4 said no global
    exit model wins; different strategies want different
    stop/target multiples. Implemented via a new
    ``exit_model_name: str | None`` field on ``Strategy``
    (string, not ``TradeExitModel``, to avoid a circular
    import with ``strategy_backtest.py``). The backtester
    resolves names against ``default_exit_model_matrix()``
    at replay time. None → use the matrix's baseline as
    today. Move-4 winners assigned per strategy where
    n ≥ ~100 has signal (live PLAYBOOK only — Failed-ARB
    and Retest-ARB are research-only this round):

        Sweep+MSS (Short)         → confirm1_s15_t25  [SUSPENDED v7.9]
        Judas (Long/Short)        → confirm1_s15_t25
        Turtle Soup (Long/Short)  → confirm1_s15_t25
        Power of 3 (Long)         → s20_t25_partial1r50_be
        Power of 3 (Short)        → confirm1_s15_t25
        Multi-Day PD (Long)       → s20_t30
        Multi-Day PD (Short)      → confirm2_s15_t25  [SUSPENDED v7.9]

    Notes: lower-n strategies inherit the portfolio-best
    ``confirm1_s15_t25`` rather than chase a noisy
    per-strategy winner. The factorial inversion driver
    uses ``confirm1_s15_t25`` uniformly across research
    cells so predicate / direction / stop comparisons aren't
    confounded by exit-model differences. Once a research
    variant clears the promotion gates and enters the live
    PLAYBOOK, an exit-model matrix re-run will pin its
    per-strategy override.

    **(6) Factorial inversion experiments — research only
    via playbook_override.** New driver
    ``scripts/run_arb_inversion_experiments.py`` runs every
    candidate predicate × direction × stop cell side by
    side on the same bar window so the lift attribution is
    unconfounded:

      ARB events (asian_high / asian_low sweeps):
        • Legacy ARB (L)   × {ATR-only, structural}
        • Legacy ARB (S)   × {ATR-only, structural}
        • Failed-ARB (L)   × {ATR-only, structural}
        • Failed-ARB (S)   × {ATR-only, structural}
        • Retest-ARB (L)   × {ATR-only, structural}
        • Retest-ARB (S)   × {ATR-only, structural}

      BB events (frozen — ``playbook_override`` only):
        • Legacy BB (L→L)  × {ATR-only, structural}
        • Legacy BB (S→S)  × {ATR-only, structural}
        • Inverted BB (L→S) × {ATR-only, structural}
        • Inverted BB (S→L) × {ATR-only, structural}

    All cells share ``confirm1_s15_t25`` as the exit model
    so cross-cell comparisons aren't confounded by stop /
    target multiple differences. Per-trade CSV exposes
    ``structural_stop_used`` (boolean — silent ATR fallback
    happens when the anchor is missing or on the wrong side
    of entry), ``structural_stop_price``, and
    ``structural_stop_distance_r`` for ex-post diagnosis.

    **Promotion gates** — a research cell must clear ALL of
    these before being lifted into the live PLAYBOOK:

      a. Sample size n ≥ 100 trades on the experiment window
         (smaller samples are dominated by noise).
      b. AvgMFE > AvgMAE on the cell (mirror of the
         counter-edge predicate diagnostic that flagged BB
         and ARB; a cell that fails this is not an entry
         signal regardless of how the win-rate looks).
      c. Positive AvgR after slippage assumptions baked in
         by ``_entry_fill_price`` / ``_exit_fill_price``.
      d. Walk-forward consistency: split the window in half
         on entry_ts; both halves' AvgR must be positive
         (not just the aggregate). One-half-only edges are
         regime artifacts.
      e. Structural-attribution gate (STRUCTURAL CELLS
         ONLY): ``structural_used_pct`` ≥ 50%. The factorial
         driver flags every trade with ``structural_stop_used``
         in the per-trade CSV. A cell labeled "structural"
         that silently fell back to ATR for most trades
         (anchor None, anchor on wrong side of entry, or no
         anchor defined) would print numbers identical to
         its ATR twin and would falsely earn the structural
         ← PROMOTE label. This gate makes the structural
         label cost-bearing: at least half the trades must
         actually use the structural stop. Vacuously True
         for ATR-only cells. This gate is enforced
         experiment-locally inside ``run_arb_inversion_experiments``
         (variable ``_GATE_MIN_STRUCTURAL_USED_PCT``); it has
         no analogue in non-structural research and is
         therefore not duplicated in the
         ``RESEARCH_PROMOTION_GATES`` constant below for
         reuse — only for documentation completeness here.

    If a candidate clears (a)–(e), it enters the live
    PLAYBOOK with its winning stop variant and an
    exit-model matrix re-run pins its per-strategy override.
    If none of the cells clear (a)–(e), the underlying
    event (asian_high/low sweep, breaker block) is treated
    as CONTEXT only — surfaced to the Tier-2 prompt as a
    structural note but never used as a Tier-1 entry
    trigger.

    Playbook count: was 11 (post-BB-freeze). Suspended 2
    (legacy ARB L+S) → 9. Failed-ARB / Retest-ARB stay OUT
    of the live PLAYBOOK pending the factorial-experiment
    promotion gate above. Reactivation is a manual edit
    in this file after the data lands.

    **(7) Factorial-experiment outcome — 2026-04-27.**
    The factorial inversion driver was run on the full
    18-symbol training universe over the
    2025-10-27 → 2026-04-24 window (last completed session
    at the time of the run). Inputs: 32 cells = 8
    predicates (legacy ARB L/S, Failed-ARB L/S, Retest-ARB
    L/S, BB L/S) × 2 directions × 2 stops, sharing
    ``confirm1_s15_t25`` as the exit model. Pinned outputs:
    ``data/research/arb_inversion_summary_2026-04-27.csv``
    and ``arb_inversion_trades_2026-04-27.csv``.

    Result: **0 / 32 cells cleared the NMAWS gates.** No
    promotion. Highlights (full table in the pinned CSV):

      * Legacy ARB at scale (n=395-497 per cell): every
        natural and inverted (direction × stop) cell still
        bleeds. AvgR range −0.090 to −0.198. Move-4's
        suspension call holds at ~5× the original sample.
      * Failed-ARB and Retest-ARB: predicates fire too
        rarely (n=22-35 per cell over 6 months × 18
        symbols). Even the four cells that posted positive
        AvgR (retest_long_INV ATR/structural at +0.11/+0.13;
        retest_short_INV structural at +0.21; failed_long
        natural structural at −0.02) are 1-3-symbol-driven
        on per-symbol decomposition: e.g.
        retest_long_short_INV is +14 R from F+MS+JPM+GOOGL
        and −10 R from the rest of the book. Walk-forward
        also fails on those cells. None are promotable.
      * Breaker Block at scale (n=2,636-2,732 per cell;
        ~10,750 trades total): unambiguously counter-edge
        in BOTH natural and inverted directions. Combined
        drag −3,336 R across the four ATR cells. The S
        gate also exposed the second BB issue: all four
        BB structural cells silently fell back to ATR
        (Struct% = 0.0%) and produced byte-identical
        numbers to their ATR twins, because BB has no
        ``structural_anchor_fn`` defined yet. Without the
        S gate this would have been read as "structural
        didn't help BB"; with it, the cells are correctly
        labeled as ATR results wearing a structural label.

    Decision per the (a)-(e) fallback rule: ARB and BB
    events stay CONTEXT only — surfaced to the Tier-2
    prompt as generic structural narrative (sweep / MSS /
    OB events without naming the asian_high / asian_low /
    breaker-block predicates), never as Tier-1 entry
    triggers. The live PLAYBOOK count remains 9 at v7.7
    close (Multi-Day PD (Short) and Sweep+MSS (Short) were
    later suspended in v7.9 → live count is now 7). Legacy
    ARB / Failed-ARB / Retest-ARB stay research-only and
    callable from this module for any future research
    round, but a code edit alone cannot promote them — a
    fresh factorial round must clear (a)-(e) first.

    Future research notes pinned by this experiment:
      * BB has no defined structural anchor. Any future
        BB structural-stop research must wire a
        ``_stop_anchor_breaker_block_*_event`` helper
        before the structural cells can carry meaning;
        the current S=False on every BB structural cell
        is a known wiring gap, not a signal.
      * Failed-ARB / Retest-ARB are too sparse to pass
        the N gate in a 6-month / 18-symbol window. To
        re-litigate these would require either a longer
        historical window (≥ 18 months) or a broader
        symbol set (≥ 50 names) — and even then the
        per-symbol concentration check would need to be
        applied before promotion.

v7.8 (2026-04-28) — **Power-of-3 hour-of-day filter; book
    flips from net-negative to net-positive on the same
    sample.**

    With ARB and BB suspended (v7.6 / v7.7) and the
    factorial round closed, the live 9-strategy PLAYBOOK
    was re-baselined on the same 2025-10-27 → 2026-04-24
    18-symbol window:
    ``data/research/playbook_baseline_2025-10-27_2026-04-24_*``.
    Result: 366 trades, **TotalR = -29.06 R**, win-rate
    43%. Power of 3 was 81% of the sample (Long n=175
    AvgR -0.086 -15.02 R; Short n=123 AvgR -0.116
    -14.24 R) and accounted for essentially all the
    bleed.

    Per-(strategy × hour) decomposition with walk-forward
    halves and per-symbol concentration checks isolated the
    P3 bleed to two contiguous hour windows:

      * P3 Long @ 10:00-10:59 ET — n=101, AvgR -0.160,
        TotalR -16.18 R, both walk-forward halves
        negative (H1 -0.121, H2 -0.198), bleed spread
        across GOOGL/JPM/MS (no single-symbol cause).
        AvgMFE ≈ AvgMAE ≈ 0.80 — classic noise pattern.
      * P3 Short @ 09:30-10:59 ET — combined n=100,
        TotalR -20.48 R. Hour 9 (n=18 AvgR -0.475) and
        hour 10 (n=82 AvgR -0.146) both bleeding with
        stable walk-forward.

    The one P3 cell that earned was P3 Short @ 11:00 ET
    (n=16, AvgR +0.547, TotalR +8.76, both halves
    positive). The fix was a session-window block of
    those bleeding hours rather than a stop-tuning round
    or a wholesale P3 suspension. ``_PO3_LONG_BLOCK_WINDOWS``
    started with ``((30, 90),)`` (10:00-10:59 ET; later extended
    in v7.10) and
    ``_PO3_SHORT_BLOCK_WINDOW = (0, 90)`` (09:30-10:59 ET)
    are checked at the top of the two P3 predicates;
    rejected trades never construct an entry.

    Re-baseline outcome on the same 18-symbol / 6-month
    window (``playbook_baseline_p3hourfilter_*``):

      * Total: -29.06 R → **+8.15 R** (+37.2 R lift),
        366 trades → 167 trades.
      * P3 Long: 175 → 75, AvgR -0.086 → +0.001,
        TotalR -15.02 → +0.09 — bleed nulled exactly
        as predicted from M-edge ≈ 0.
      * P3 Short: 123 → 24, AvgR -0.116 → +0.328,
        TotalR -14.24 → +7.86 — strongly profitable
        subset, AvgMFE 1.21 vs AvgMAE 0.80 (clear
        target-side M-edge). Almost entirely 11:00 ET
        (n=17 AvgR +0.611 TotalR +10.38).

    Two new follow-up signals surfaced in the post-filter
    table — recorded here, not actioned:

      * Sweep+MSS (Short) bleeds -6.76 R on n=18,
        concentrated 09:00-11:00 ET (-1.60 / -2.75 /
        -2.21). Same hour-filter pattern as P3.
      * Multi-Day PD (Long) is +2.39 R overall but
        bleeds at 11:00 and 13:00 ET (-3.17 / -2.17)
        inside an otherwise-strong morning profile
        (09:00 ET +5.75 R, 10:00 ET +3.34 R).

    Promotion gates pass on the post-filter book (visual
    only — no automated NMAWS test was run because the
    book is the live PLAYBOOK, not a research cell).
    Walk-forward halves on the new total were not
    enforced for this lift; the v8 plan is to re-baseline
    with ``--train-end`` so future per-hour cuts go
    through the same gate the inversion driver uses.

    Playbook count unchanged at 9. The two P3 entries
    remain in the live ``PLAYBOOK`` list — only the
    predicate ``_ict_power_of_3_long`` /
    ``_ict_power_of_3_short`` short-circuits inside the
    blocked hours.

    **(3b follow-up — matrix cooldown bug fix.)** While
    investigating an exit-model matrix sweep on the
    post-filter book, ``run_exit_model_matrix`` reported
    ICT Power of 3 (Short) × ``confirm1_s15_t25`` at
    n=14, but the same model running standalone in the
    live PLAYBOOK ``run_backtest`` reported n=24 on the
    same window / symbols / predicate. 14 of the 14
    matrix trades were a strict subset of the 24 live
    trades; all 10 missing trades had an earlier same-day
    ``baseline_s15_t25`` (no-confirmation) entry 2-9
    minutes prior — well inside the 15-bar
    ``COOLDOWN_BARS``.

    Root cause: the cooldown clock in
    ``replay_symbol_exit_models`` was keyed by
    ``strat.name`` only, shared across every exit model
    in matrix mode. A permissive model entering on the
    predicate-fire bar locked stricter models (with
    confirmation) out of the next predicate-fire bar
    they would otherwise have caught — a per-model bias
    that systematically undercounted confirmation-model
    trades and inflated the matrix's apparent
    no-confirmation lead. Single-model replays (the live
    PLAYBOOK path via ``replay_symbol`` →
    ``respect_strategy_overrides=True``) were unaffected
    because ``models_for_this_signal`` is always a
    one-element list there.

    Fix: cooldown is now keyed by ``(strat_name,
    exit_model_name)``. Each model has its own clock per
    strategy; only the model that actually entered locks
    itself out. Per-strategy single-model replays
    collapse to the old behavior automatically (one key
    per strategy). Pinned in
    ``app/strategy_backtest.replay_symbol_exit_models``.

v7.9 (2026-04-28) — **Multi-Day PD (Short) and Sweep+MSS
    (Short) suspended; live PLAYBOOK count 9 → 7. Prune
    bleeders before tuning the survivors.**

    The post-cooldown-fix matrix (v7.8 §3b) reconciled the
    ``confirm1_s15_t25`` Power-of-3 (Short) cell to its live
    baseline (n=24, AvgR +0.328, TotalR +7.86 R — exact
    match), which then made the live 9-strategy book's other
    bleeders directly readable from the same per-strategy
    table without the per-model bias. Two structural-loser
    cells stood out on the 18-symbol / 2024-10-27 →
    2026-04-24 18-month re-baseline
    (``data/research/playbook_baseline_2024-10-27_2026-04-24_*``,
    1107 trades / -83 R total):

      * ``ICT Multi-Day PD (Short)`` — n=62, AvgR -0.295,
        TotalR -18.30 R, win-rate 33.9 %. AvgMAE +1.018 vs
        AvgMFE +0.832 (counter-edge MAE > MFE diagnostic,
        same as BB and ARB before suspension). Walk-forward:
        H1 -0.452 R/trade, H2 -0.117 R/trade. H1 is the
        clean bleed; H2 is closer to noise but still
        negative — no half is positive, no clear regime
        explanation. The asymmetry against the Long side
        (Multi-Day PD (Long): n=63, AvgR +0.146, TotalR
        +9.20 R) means the multi-day-PD framework only
        works on the long side over this window, suggesting
        a directional drift in the underlying universe more
        than a structural property of the predicate.
      * ``ICT Sweep + MSS (Short)`` — n=69, AvgR -0.228,
        TotalR -15.73 R, win-rate 36.2 %. AvgMAE +1.043 vs
        AvgMFE +0.876 (same counter-edge signature). Walk-
        forward: H1 -0.291 R/trade (n=38), H2 -0.143 R/trade
        (n=31) — bleeding in both halves, no regime out.
        Hour decomposition concentrates the bleed around
        09:00-11:00 ET, but unlike P3 (which had a clean
        post-11:00 ET subset), no hour cell on Sweep+MSS
        Short clears positive AvgR with n ≥ 15. Cleaner to
        suspend the directly-named entry than to chase a
        per-hour fragment that's also low-n.

    Skipped suspension candidates (kept):
      * ``ICT Turtle Soup (Short)`` — n=13, TotalR -1.93 R.
        Within ~1 σ of zero on the sample; not a structural
        loser. Re-evaluate at n ≥ 50.
      * Long-side variants of both predicates remain in the
        live book — Multi-Day PD (Long) is the strongest
        AvgR per-trade strategy in the post-filter book and
        Sweep+MSS Long was already removed back in v7.2 (it
        was the duplicate of Silver Bullet (Long), which is
        itself separately suspended in v7.3).

    Mechanics — same pattern as BB (v7.6) and ARB (v7.7):
      * The two ``Strategy(...)`` blocks become NOTE comments
        in this file (not deleted from history — flip the
        block back in if a future research round produces a
        passing factorial cell).
      * The predicate functions ``_ict_multi_day_pd_short``
        and ``_ict_sweep_mss_short`` STAY callable. They are
        wired into ``app/regime.py`` for the regime-weight
        table and used by ``playbook_override``-driven
        research, identical to how legacy ARB / BB
        predicates were preserved.
      * Raw ICT structural context (Sweep, MSS, FVG/OB,
        intraday + multi-day PD zone) still flows to the
        Tier-2 prompt via ``analyzer._build_ict_section``,
        which reads from ``ind`` directly — independent of
        whether any named ``Strategy`` block matched. So the
        AI layer continues to see "premium PD + bearish
        sweep + MSS" as context; it just can't be told "this
        is named MD-PD (Short)" by the Tier-1 layer.
      * Live PLAYBOOK count: 9 → 7. Survivors: Sweep+MSS
        (Long) is no longer in the book post-v7.2; what
        remains is Judas (L/S), Turtle Soup (L/S),
        Power of 3 (L/S), Multi-Day PD (Long).

    Promotion-gate posture going forward: per the v7.7
    fallback rule, suspended predicates only return through
    a fresh factorial round that clears the (a)-(e) gates.
    Any tuning of the live book (per-strategy exit-model
    refresh, hour-of-day filters, etc.) should re-baseline
    AFTER this prune so the lift is attributable to the
    tuning rather than to also-removing-a-bleeder.

v7.10 (2026-04-29) — **Scanner-quality filters and context-only
    setup notes.**

    The validated v7.9 book is finally positive but still thin:
    404 trades / +12.01 R on the 18-symbol / 18-month replay and
    139 trades / +15.82 R on the recent 6-month replay. The next
    problem is not "add more predicates" — it is making the scanner
    spend attention on the names / hours where the current predicates
    have historically behaved.

    Three small-touch changes land here:

      * ``Strategy.symbol_blacklist`` lets a predicate remain live
        while blocking empirically toxic symbol cells. The first use
        is Power of 3: Long blocks MS/JPM/BAC/PLTR and Short blocks
        JPM. These are strategy-specific filters, not global universe
        deletions; the same symbols can still surface under other
        strategies if those strategies have earned them.
      * ``_PO3_LONG_BLOCK_WINDOWS`` extends the existing P3 Long
        10:00-10:59 ET block with an afternoon block from
        14:00-17:59 ET. The 18-month replay showed that late-day
        P3 Long pocket as a stable walk-forward bleed, while the
        recent-6-month-only filter did not see it.
      * ``CONTEXT_PLAYBOOK`` reuses suspended-but-callable predicates
        as named confluence notes for Tier-2 only. It never feeds the
        live scanner's ``matched_strategies`` output and therefore
        cannot create an auto-trade by itself. Raw ICT context already
        flowed through ``analyzer._build_ict_section``; this adds a
        short label when a known suspended structure is present.

    Promotion posture is unchanged: context-only predicates still
    require a fresh research pass through the v7.7 gates before they
    can re-enter the live ``PLAYBOOK``.

v7.11 (2026-04-29) — **Trade Quality Score + tiered scanner; metadata
    only — backtest trade count unchanged.**

    The v7.10.5 realistic-execution audit
    (``scripts/run_execution_audit.py``) on the v7.10 18-month replay
    revealed three things the +40 R headline did not show:

      * Friction kills most of the edge. At 0.10R per-trade friction
        (a realistic estimate for tight-spread liquid US equities)
        the book drops from +40.10 R to **+6.60 R**. Each trade is
        small-edge; multiplying 335 trades × 0.05R extra friction =
        16.75 R lost.
      * Edge is concentrated. Of 7 live strategies only 3 are
        positive after 0.10R friction:
          - Judas Long:        n=19,  +0.928 R/trade  → +17.63 R
          - Judas Short:       n=21,  +0.136 R/trade  →  +2.85 R
          - Turtle Soup Long:  n=18,  +0.060 R/trade  →  +1.08 R
          - Power of 3 (Long): n=149, -0.024 R/trade  →  -3.63 R
          - Multi-Day PD (Long): n=61, -0.056 R/trade →  -3.40 R
          - Power of 3 (Short): n=54, -0.087 R/trade  →  -4.71 R
          - Turtle Soup (Short): n=13, -0.248 R/trade →  -3.23 R
        Judas Long alone carries 84% of the post-friction TotalR.
      * A daily cap with quality selection beats no-cap under
        friction. At 0.10R friction, "best 1 per day by strategy
        AvgR" earns +19.74 R vs +6.60 R for no-cap. The ~+5 R lift
        from picking-the-best vs first-N is the upper bound on what
        a quality-score system can deliver.

    The decision (Fork C from the audit summary): **tier the live
    PLAYBOOK rather than trim it**. Keep all 7 strategies firing for
    trade-flow visibility, but classify each match A/B/Context based
    on post-friction expectancy. The frontend renders the tier badge
    so the user can act on A-grade with confidence and B-grade with
    extra discretion. Suspended-but-callable predicates (the 10
    ``CONTEXT_PLAYBOOK`` entries from v7.6/v7.7/v7.9) automatically
    map to ``tier="context"``.

    **Crucially this is metadata-only: the set of trades the backtest
    fires is identical to v7.10.** Adding the tier field does not
    gate ``Strategy.matches()``. The scoring layer (in
    ``app/trade_quality.py``) reads the tier as the BASE classification
    and may demote per-(symbol, hour) cell, but it is the analyzer
    + frontend that surface the result; the predicates themselves
    are unchanged.

    Tier assignments:

      * Judas Long             → A (primary; carries 84% of edge)
      * Judas Short            → A (positive after friction, n=21)
      * Turtle Soup Long       → A (modest but positive post-friction)
      * Power of 3 (Long)      → B (high flow, post-friction marginal)
      * Power of 3 (Short)     → B (regime-fragile; weight recent half)
      * Multi-Day PD (Long)    → B (same pattern as P3 Long)
      * Turtle Soup (Short)    → context (only live strategy that's
                                 negative even raw on 18m; n=13 is
                                 noise but pointing the wrong way —
                                 keep firing for backtest continuity
                                 but demote in the UI / Sonnet prompt)
      * All CONTEXT_PLAYBOOK   → context (10 strategies)

    Autotrade is OFF on the user's setup. v7.11 is purely scanner
    advisory layer (output schema + AI prompt + frontend display).
    The autotrade gating change ("only A-tier auto-fires") is
    deferred to v7.12 or whenever the user enables autotrade.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Strategy:
    name: str
    direction: str            # "long" | "short"
    description: str
    entry_rules: str
    stop_rules: str
    target_rules: str
    min_confidence: float = 0.60   # Claude should only recommend if above this

    # ── override in subclass or set via _check callable ──
    _check: object = field(default=None, repr=False)

    # Per-strategy exit-model override (v7.7, 2026-04-27).
    # Names a TradeExitModel from
    # ``app.strategy_backtest.default_exit_model_matrix()``. The
    # backtester resolves the name to a TradeExitModel at replay
    # time; the live scanner ignores it (Tier-1 doesn't simulate
    # exits — Claude does that at Tier-2 from ``stop_rules`` /
    # ``target_rules``). String, not a TradeExitModel reference, to
    # avoid a circular import: ``strategy_backtest.py`` imports
    # PLAYBOOK from this module. None → use the backtester's default
    # exit model (current global behavior).
    exit_model_name: str | None = None

    # Per-strategy structural-stop callable (v7.7, 2026-04-27).
    # Signature: ``fn(ind: dict, direction: str) -> float | None``.
    # Returns the structural reference price for the stop (e.g. the
    # swept wick high for a Failed-ARB short, the asian_low for a
    # Retest-ARB long). The backtester places the stop at
    # ``max(structural_distance, atr × stop_atr_floor)`` so a
    # too-tight structural anchor still gets a sane minimum
    # distance. None → ATR-only stop (current global behavior).
    structural_stop_fn: object = field(default=None, repr=False)

    # Per-strategy symbol blacklist (v7.10, 2026-04-29).
    # This is deliberately strategy-local: a symbol can be toxic for
    # one predicate (e.g. P3 Long) but still valid for another. The
    # live scanner, analyzer, and backtester stamp ``ind["symbol"]``
    # before calling ``matches`` so this gate is shared everywhere.
    symbol_blacklist: frozenset[str] | None = None

    # Quality tier for the v7.11 tiered scanner / Trade Quality Score
    # layer (assigned from the v7.10.5 execution audit's per-strategy
    # post-friction AvgR). PURE METADATA — does NOT gate ``matches()``,
    # does NOT change the backtest trade count. Read by the analyzer
    # to decorate ``matched_strategies`` output, by the AI prompt to
    # tell Sonnet "this is A-grade" vs "B-grade — friction-fragile",
    # and by the frontend to render tier badges. See module docstring
    # v7.11 entry for the assignment rationale and the score-function
    # demotion rules in ``app/trade_quality.py``.
    #   "A"       — positive AvgR after 0.10R realistic friction
    #   "B"       — raw AvgR positive but friction-fragile
    #   "context" — surface as confluence note only, not as a primary
    #               alert. Backtest still includes these trades; only
    #               the UI / autotrade gating treats them differently.
    #   "suppress"— hard reject in score function (no current uses).
    # Default is "B" — the conservative read on a strategy that hasn't
    # been audited under friction yet. Override per Strategy(...) in
    # the PLAYBOOK / CONTEXT_PLAYBOOK below.
    tier: str = "B"

    # Strategy family (v7.15, 2026-05-18). Tags WHICH scanner / playbook
    # a strategy belongs to. PURE METADATA — does NOT gate ``matches()``.
    # Used by the EOD email to render per-family win rates inside the
    # experimental scanner's section, and (later) by the
    # ``scripts/audit_universe_sources.py`` audit to UNION + segment
    # across both PLAYBOOK and EXPERIMENTAL_PLAYBOOK rows.
    #   "ict"      — current ICT predicates (PLAYBOOK + CONTEXT_PLAYBOOK)
    #   "classic"  — ORB / VWAP reclaim / EMA pullback (experimental)
    #   "momentum" — gap+RVOL breakout / failed-breakdown (experimental)
    # Default "ict" so every existing Strategy row keeps its audit lane
    # without needing an explicit family= override.
    family: str = "ict"

    # Per-strategy partial-state callable (v7.12, 2026-04-30).
    # Signature: ``fn(ind: dict) -> dict | None``.
    # Returns either None (no setup forming) or a dict with shape:
    #     {
    #         "stage": "sweep_only" | "sweep_and_mss" | "fully_formed",
    #         "missing": ["mss", "fvg_touch", ...],   # remaining gates
    #         "structural_notes": ["Bullish sweep of asian_low 4 bars ago", ...],
    #     }
    # PURE METADATA. Does NOT gate ``matches()``. Does NOT change the
    # backtest trade count. Mirrors the ``structural_stop_fn`` pattern
    # — predicates that don't define one have the wrapper return None
    # and the scanner produces the same output as today. Wired only on
    # live-PLAYBOOK strategies; CONTEXT_PLAYBOOK predicates are context-
    # only by definition and don't need partial-state visibility.
    # Used by ``app/scanner.py``'s revised short-circuit logic to
    # surface "Watching" panel entries to the analyzer / frontend.
    _partial_check: object = field(default=None, repr=False)

    def matches(self, ind: dict) -> bool:
        """Return True if the current indicators pass this strategy's pre-screen."""
        if self._check is None:
            return False
        if self.symbol_blacklist:
            sym = ind.get("symbol")
            if sym and str(sym).upper() in self.symbol_blacklist:
                return False
        try:
            return bool(self._check(ind))
        except Exception:
            return False

    def structural_stop(self, ind: dict) -> float | None:
        """Return the structural-stop reference price, or None.

        Wraps the per-strategy ``structural_stop_fn`` callable with a
        try/except so a single broken anchor never kills a backtest run
        — fall back to ATR stop instead, which is the conservative
        choice (the trade still gets simulated, just without the
        structural placement).
        """
        if self.structural_stop_fn is None:
            return None
        try:
            value = self.structural_stop_fn(ind, self.direction)
        except Exception:
            return None
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def partial_match(self, ind: dict) -> dict | None:
        """Return the forming-setup state for this strategy, or None.

        Wraps ``_partial_check`` with a try/except so a single broken
        partial-state callable never breaks the live scanner — fall
        back to None (no forming setup) silently.

        Returns:
            None when no partial-state callable is wired OR when the
            callable raised OR when the callable returned None (no
            setup forming).
            Otherwise the dict returned by the callable, with
            documented shape (stage / missing / structural_notes).

        v7.12, 2026-04-30. PURE METADATA — does NOT gate ``matches()``,
        does NOT change the backtest. Used only by the live scanner
        + analyzer to surface "Watching" panel entries to the frontend.
        """
        if self._partial_check is None:
            return None
        try:
            value = self._partial_check(ind)
        except Exception:
            return None
        if value is None:
            return None
        if not isinstance(value, dict):
            return None
        return value

    def prompt_block(self) -> str:
        """Formatted text block injected into Claude's prompt."""
        return (
            f"MATCHING STRATEGY: {self.name} ({self.direction.upper()})\n"
            f"  Setup: {self.description}\n"
            f"  Entry: {self.entry_rules}\n"
            f"  Stop:  {self.stop_rules}\n"
            f"  Target: {self.target_rules}\n"
        )


# ── ICT helpers ───────────────────────────────────────────────────────────────

# Volume confirmation floor. Every ICT predicate requires ``vol_ratio``
# (current bar / 20-bar mean volume) to clear this gate before firing.
# 0.8 is roughly the bottom of the "session-normal" band — anything
# under that is dead-tape activity, where structural-only sweeps and
# MSS prints are mostly noise. Added 2026-04-25 as the cheapest single
# fix for the low Tier-1 win rate: the post-mortem showed the lowest-
# conviction matches were almost all on sub-0.8 vol_ratio bars.
_MIN_VOL_RATIO = 0.8

# Sweep magnitude floor, expressed as a multiple of ATR. A "sweep"
# whose wick only pokes a tick or two past the level is statistical
# noise, not a stop-run. We want the wick excursion past the level
# to be at least 0.25× ATR to count it as a real sweep. Added
# 2026-04-25 — the post-mortem of low-win-rate Tier-1 matches showed
# a heavy tail of micro-wick sweeps on otherwise-flat bars.
_MIN_SWEEP_ATR_MULT = 0.25


def _ict_volume_ok(ind: dict) -> bool:
    """Return True iff volume is at or above the ICT-volume floor.

    Reads ``vol_ratio`` from ``ind`` (current bar volume / 20-bar mean).
    Treats missing/zero/None as 0 — that fails the gate, which is the
    correct conservative behavior. The shared helper is used as the
    first line in every ICT predicate so the constant lives in exactly
    one place.
    """
    try:
        return float(ind.get("vol_ratio") or 0) >= _MIN_VOL_RATIO
    except (TypeError, ValueError):
        return False


def _ict_recent(
    event: dict | None,
    max_bars_ago: int,
    *,
    ind: dict | None = None,
    min_atr_mult: float | None = None,
) -> bool:
    """Common guard for ICT events: present AND recent enough to still be live.

    ``ict_last_*`` features serialize with ``bars_ago``. A sweep or
    MSS that fired hours ago isn't an actionable setup — we want the
    structure to still be the dominant recent event. Thresholds were
    picked at 1-minute resolution: 30 bars ≈ 30 min, long enough for
    a typical sweep → MSS → retrace sequence to complete, short
    enough to avoid re-triggering on stale geometry.

    When ``min_atr_mult`` is provided (sweep events only — MSS events
    don't have a magnitude), we additionally require the sweep's wick
    excursion past its level to be at least ``min_atr_mult × ATR``.
    The ATR comes from ``ind["atr"]``; if it's missing or non-positive
    we fail the gate (conservative: a sweep we can't size is a sweep
    we shouldn't trade). The magnitude itself comes from the sweep
    event's serialized ``magnitude`` field (populated by
    ``SweepEvent.to_dict``); for older bar frames where the sweep
    detector hasn't been re-run the field will be missing — same
    conservative fail.
    """
    if not event:
        return False
    if event.get("bars_ago", 10**9) > max_bars_ago:
        return False

    if min_atr_mult is not None:
        # Sweep-magnitude gate. Only meaningful when the caller passed
        # ``ind`` (so we can read ATR) AND the event carries magnitude.
        if ind is None:
            return False
        try:
            atr = float(ind.get("atr") or 0)
        except (TypeError, ValueError):
            atr = 0.0
        if atr <= 0:
            return False
        magnitude = event.get("magnitude")
        if magnitude is None:
            # Backfill: derive from level_price + wick_extreme if both
            # are present. Older serializations of SweepEvent without
            # the wick_extreme field will fall through to the no-mag
            # fail below.
            lp = event.get("level_price")
            we = event.get("wick_extreme")
            if lp is not None and we is not None:
                try:
                    magnitude = abs(float(we) - float(lp))
                except (TypeError, ValueError):
                    magnitude = None
        if magnitude is None:
            return False
        try:
            if float(magnitude) < min_atr_mult * atr:
                return False
        except (TypeError, ValueError):
            return False

    return True


def _ict_sweep_mss_short(ind: dict) -> bool:
    """Bearish ICT confluence: sweep a high → bearish MSS → premium
    retrace into a fresh bearish FVG/OB.

    Signal order matters: the MSS must come AFTER the sweep (the
    "reversal confirmation" shows up only once liquidity has been
    taken). We check that via bars_ago — the MSS must be more recent
    than the sweep.

    Note: the long-side counterpart (``_ict_sweep_mss_long``) was
    removed 2026-04-25 because it duplicated ``_ict_silver_bullet_long``
    after the killzone gate came off Silver Bullet. The Short side has
    no Silver Bullet duplicate, so it remains in the playbook on its
    own.
    """
    if not _ict_volume_ok(ind):
        return False
    sweep = ind.get("ict_last_bear_sweep")
    mss   = ind.get("ict_last_bear_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if ind.get("ict_pd_zone") not in ("premium", "equilibrium"):
        return False
    if not ind.get("ict_in_bear_zone"):
        return False
    return True


def _ict_silver_bullet_long(ind: dict) -> bool:
    """Silver Bullet (long) — bullish Sweep+MSS confluence.

    History: originally hard-gated to the 10:00–11:00 ET "Silver
    Bullet" killzone. The killzone gate was removed 2026-04-25; the
    predicate now fires whenever the structural pattern presents,
    regardless of hour. After that change the structure became
    identical to the legacy ``_ict_sweep_mss_long`` (which used to be
    the all-day variant at ``min_confidence=0.55``). On 2026-04-25
    the legacy long predicate + its PLAYBOOK entry were deleted; this
    predicate (with ``min_confidence=0.60``) is now the sole bullish
    Sweep+MSS Long in the playbook.

    Gate:
      * recent bullish sweep (< 30 bars) — liquidity raid
      * recent bullish MSS   (< 15 bars) — reversal confirmation
      * MSS more recent than sweep (ordering matters)
      * currently touching a fresh bullish zone (FVG/OB retrace)
      * PD zone discount or equilibrium (no premium chases)

    NOTE (2026-04-25 evening): Silver Bullet (Long) was suspended
    from the live PLAYBOOK pending an investigation into its
    23.8%-win-rate cluster at 10:00 ET on n=80 trades. The predicate
    function lives on so re-enabling it later is a one-line PLAYBOOK
    edit, but ``screen()`` no longer calls it.
    """
    if not _ict_volume_ok(ind):
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss   = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    # MSS must be after sweep (more recent, so smaller bars_ago).
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if ind.get("ict_pd_zone") not in ("discount", "equilibrium"):
        return False
    if not ind.get("ict_in_bull_zone"):
        return False
    return True


# ── v7 additions (2026-04-24) — classic ICT catalog ─────────────────────────
#
# All twelve predicates below follow the same shape: a small set of
# cheap boolean checks over pre-computed ``ind`` keys that ICT feature
# builder populates. None of them re-run detectors — the feature layer
# owns that once per cycle. A predicate returning True means Tier-2
# (Sonnet) should look at the setup; it does not mean "take the trade".

# How long after a sweep can we still call the setup alive?
# Judas = tight (30 bars ≈ 30 min) because the window itself is only
# 30 min. Everything else uses the 30-bar cap Sweep+MSS Long landed on.
_JUDAS_WINDOW_END = 30           # RTH minutes from 09:30 — ends at 10:00 ET
_JUDAS_MAX_BARS_AGO = 15         # sweep/MSS must be very fresh inside the window

# Breaker-block staleness cap. A breaker that mitigated two hours ago
# isn't an active retest — it's old news. Tighter than the FVG cap
# because breakers need the liquidity context that fades faster.
_BREAKER_MAX_BARS_AGO = 60

# Power-of-3 session-window filter (added 2026-04-27, v7.7 baseline).
#
# The 6-month live-PLAYBOOK baseline (data/research/playbook_baseline_
# 2025-10-27_2026-04-24_*) showed Power of 3 was the workhorse strategy
# (298 of 366 trades = 81% of the entire book) and was bleeding -29.3 R
# combined across both legs. Per-hour decomposition revealed the bleed
# was essentially confined to two (strategy, hour) cells:
#
#   P3 Long @ 10:00-10:59 ET   n=101  TotalR -16.18  H1 -0.121, H2 -0.198
#       Largest single bleeder in the book. Both walk-forward halves
#       negative; bleed spread across GOOGL/JPM/MS (not single-name).
#       AvgMFE 0.80 ≈ AvgMAE 0.80 — classic noise pattern at this hour.
#
#   P3 Short @ 09:30-10:59 ET  n=100  TotalR -20.48  (sum of hour 9 + 10)
#       hour 9: n=18, AvgR -0.475, H1 -0.468 / H2 -0.482  (very stable bleed)
#       hour 10: n=82, AvgR -0.146, H1 -0.262 / H2 -0.029 (H2 improving
#         but still slightly negative; included in the block to match
#         the 09:00 ET treatment until a future re-baseline argues
#         otherwise).
#
# The one P3 hour that earns is P3 Short @ 11:00 ET (n=16, AvgR +0.547,
# TotalR +8.76, both halves positive). It survives the filter below.
#
# Constants name (rth_min_low, rth_min_high) — BLOCKED windows in
# minutes since RTH open at 09:30 ET, half-open [low, high). A trade
# whose ict_rth_minute falls inside any window is rejected.
#
# Estimated impact (computed on the same 366-trade baseline): book
# moves from -29.06 R to roughly +7.4 R, almost entirely by removing
# the two cells above. Re-baseline next to verify.
# v7.10 extends P3 Long with a stable 18-month afternoon bleed:
# 14:00-17:59 ET (rth_min 270-509). This is intentionally Long-only;
# P3 Short needs a separate walk-forward round before more filtering.
_PO3_LONG_BLOCK_WINDOWS = (
    (30, 90),    # block 10:00-10:59 ET
    (270, 510),  # block 14:00-17:59 ET
)
_PO3_SHORT_BLOCK_WINDOW = (0, 90)    # block 09:30-10:59 ET (rth_min 0-89)


def _ict_judas_swing_long(ind: dict) -> bool:
    """Judas Swing (long) — the 09:30-10:00 ET fakeout pattern.

    ICT thesis: the first 30 minutes of RTH often prints a "Judas"
    false move — price sweeps an overnight low (PML / Asian low /
    prior session low) to trap sellers, then reverses and runs the
    real direction. We take the long side when:

      * ``ict_rth_minute < 30`` (still in 09:30-10:00 ET)
      * recent bullish sweep of PML / asian_low / pdl / session_low
      * recent bullish MSS after that sweep
      * currently in a fresh bullish FVG/OB retrace
    """
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None or rth_min >= _JUDAS_WINDOW_END:
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss   = ind.get("ict_last_bull_mss")
    if not _ict_recent(
        sweep, _JUDAS_MAX_BARS_AGO,
        ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT,
    ):
        return False
    if not _ict_recent(mss, _JUDAS_MAX_BARS_AGO):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False  # MSS must come after sweep
    # The swept level should be an overnight/early-session pool — PML,
    # asian_low, PDL or session_low. Any other level (an FVG boundary,
    # a recent swing) doesn't match the Judas pattern.
    if sweep.get("level_name") not in ("pml", "asian_low", "pdl", "session_low"):
        return False
    if not ind.get("ict_in_bull_zone"):
        return False
    return True


def _ict_judas_swing_short(ind: dict) -> bool:
    """Mirror of ``_ict_judas_swing_long``. First 30 min sweep of
    PMH/asian_high/PDH/session_high + bearish MSS + bear-zone touch."""
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None or rth_min >= _JUDAS_WINDOW_END:
        return False
    sweep = ind.get("ict_last_bear_sweep")
    mss   = ind.get("ict_last_bear_mss")
    if not _ict_recent(
        sweep, _JUDAS_MAX_BARS_AGO,
        ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT,
    ):
        return False
    if not _ict_recent(mss, _JUDAS_MAX_BARS_AGO):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") not in ("pmh", "asian_high", "pdh", "session_high"):
        return False
    if not ind.get("ict_in_bear_zone"):
        return False
    return True


# ── Judas Long expansion variants (v7.11 research; NOT in PLAYBOOK) ─────────
#
# Research-only predicates for the Judas Expansion Experiment
# (``scripts/run_judas_expansion_experiments.py``). The goal is to find a
# Judas Long variant that clears the v7.7 promotion gates AND posts a
# positive AvgR after 0.10R realistic friction, so we can grow the live
# A-tier set beyond the single Judas Long predicate (n=19, +0.928 friction
# AvgR but only ~1 trade/month).
#
# All variants are research-only via ``playbook_override``. None are
# registered in ``PLAYBOOK``; promotion to live requires clearing the
# gates and a paper-validation period (n=50-99 → candidate; n≥100 →
# promotable). The driver short-circuits if any variant collides
# accidentally with the live Judas Long predicate via name overlap, so
# names use the ``judas_le_*`` prefix to stay distinct.
#
# Pre-declared parameter values (per ChatGPT review of the matrix):
#   * JE2 "stronger MSS" operationalized as MSS ≤ 5 bars old (fresh
#     MSS as proxy for displacement; MSSEvent does not expose
#     displacement directly so a recency cap is a defensible substitute).
#   * No threshold sweeps. Each variant is a single pre-declared spec;
#     promotion gates apply uniformly. This is data-mining hygiene.
#   * Window split JE1a (09:30-09:59) vs JE1b (10:00-10:29) is intentional;
#     the standalone tests prevent a weak later window from hiding inside
#     the strong original window.

# Constants for the variant predicates. Mirror the live Judas constants
# so the control variant is a faithful reproduction; the variants vary
# one parameter at a time.
_JUDAS_EXP_WINDOW_LATE_END = 60      # JE1b: 09:30-10:29 ET (rth_min < 60)
_JUDAS_EXP_FRESH_MSS_BARS = 5         # JE2: MSS must be ≤ 5 bars old


def _ict_judas_le_baseline_long(ind: dict) -> bool:
    """JE0 — control variant. Faithful reproduction of the live
    ``_ict_judas_swing_long`` predicate. Used to verify the research
    driver matches the production trade set on the same window. If
    JE0's n / TotalR diverges from the live Judas Long backtest
    numbers, the research driver has a wiring bug — not a research
    finding."""
    return _ict_judas_swing_long(ind)


def _ict_judas_le_window_early_long(ind: dict) -> bool:
    """JE1a — Judas Long, 09:30-09:59 ET only (control window, isolated).

    Identical to JE0 since the live predicate already enforces
    rth_min < 30. Kept as an explicit cell so the experiment matrix
    surfaces it next to JE1b for direct comparison. If JE1a ≠ JE0
    that's a wiring bug.
    """
    return _ict_judas_swing_long(ind)


def _ict_judas_le_window_late_long(ind: dict) -> bool:
    """JE1b — Judas Long, 10:00-10:29 ET only (late-window standalone test).

    Hypothesis: the canonical 09:30-10:00 window is too restrictive;
    a Judas-style fakeout into 10:00-10:30 is also tradeable. Walking
    forward from 09:30, the structure (sweep + MSS + FVG/OB) often
    completes 30-45 min in, not always under 30 min.

    Tested in isolation so a weak late window doesn't hide inside the
    strong original window. Promotion requires JE1b to clear gates on
    its own; if JE1b passes, a separate v7.12 round can decide whether
    to combine the windows.
    """
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    # Late window = [_JUDAS_WINDOW_END, _JUDAS_EXP_WINDOW_LATE_END) =
    # rth_min in [30, 60) = 10:00:00 - 10:29:59 ET.
    if rth_min is None or not (_JUDAS_WINDOW_END <= rth_min < _JUDAS_EXP_WINDOW_LATE_END):
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, _JUDAS_MAX_BARS_AGO,
                       ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, _JUDAS_MAX_BARS_AGO):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") not in ("pml", "asian_low", "pdl", "session_low"):
        return False
    if not ind.get("ict_in_bull_zone"):
        return False
    return True


def _ict_judas_le_no_fvgob_long(ind: dict) -> bool:
    """JE2 — Judas Long without the FVG/OB touch requirement, but with
    a stricter MSS recency floor.

    Hypothesis: the FVG/OB retrace requirement is window dressing; the
    real edge is "sweep + structural reversal confirmed by a sharp,
    recent MSS". The stricter MSS-recency cap (≤ 5 bars old vs the
    baseline's 15) is the substitute confluence — a fresh MSS is
    stronger than a stale one.

    Note: MSSEvent does not expose displacement directly, so we use
    recency as the strength proxy here. A future round can swap in
    a true displacement gate once the detector exposes it.
    """
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None or rth_min >= _JUDAS_WINDOW_END:
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, _JUDAS_MAX_BARS_AGO,
                       ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    # Stricter MSS recency cap — substitute for displacement strength.
    if not _ict_recent(mss, _JUDAS_EXP_FRESH_MSS_BARS):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") not in ("pml", "asian_low", "pdl", "session_low"):
        return False
    # NO FVG/OB touch requirement (the variant under test).
    return True


def _ict_judas_le_pdl_only_long(ind: dict) -> bool:
    """JE3a — Judas Long, but the swept level MUST be PDL.

    Hypothesis: PDL is the cleanest overnight liquidity pool; PML /
    asian_low / session_low dilute the signal. Tighter level filter
    should raise win rate at the cost of n.
    """
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None or rth_min >= _JUDAS_WINDOW_END:
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, _JUDAS_MAX_BARS_AGO,
                       ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, _JUDAS_MAX_BARS_AGO):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") != "pdl":
        return False
    if not ind.get("ict_in_bull_zone"):
        return False
    return True


def _ict_judas_le_asian_low_only_long(ind: dict) -> bool:
    """JE3b — Judas Long, but the swept level MUST be asian_low.

    Mirror hypothesis to JE3a: asian_low is the tightest liquidity
    pool because it formed during low-vol overnight tape and the
    break is unambiguous in the morning RTH session.
    """
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None or rth_min >= _JUDAS_WINDOW_END:
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, _JUDAS_MAX_BARS_AGO,
                       ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, _JUDAS_MAX_BARS_AGO):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") != "asian_low":
        return False
    if not ind.get("ict_in_bull_zone"):
        return False
    return True


def _ict_judas_le_vwap_reclaim_long(ind: dict) -> bool:
    """JE4 — Judas Long with VWAP reclaim confirmation.

    Hypothesis: adding a "current price > VWAP" filter on top of the
    baseline keeps only the Judas setups where the broader session
    bias has flipped bullish, filtering out fakeouts that print the
    structure but never recover above the session-mean reference.
    """
    if not _ict_judas_swing_long(ind):
        return False
    # Confluence layer: price must be above VWAP at entry.
    current_price = ind.get("current_price")
    vwap = ind.get("vwap")
    if current_price is None or vwap is None:
        return False
    if float(current_price) <= float(vwap):
        return False
    return True


def _ict_judas_le_or_reclaim_long(ind: dict) -> bool:
    """JE5 — Judas Long with opening-range high reclaim confirmation.

    Hypothesis: the cleanest Judas setups are ones where the post-MSS
    expansion has already retaken the opening range — i.e. the bullish
    case is being confirmed by the session's price action, not just by
    structural events.

    OVERLAP RISK: this variant may converge with Power of 3 (Long),
    which fires when price is above opening_range_high after a sweep+MSS.
    The promotion gate requires JE5 trade-level overlap with active
    P3 Long signals to be < 30%; the driver computes this on the
    pinned trades CSV.
    """
    if not _ict_judas_swing_long(ind):
        return False
    opening = ind.get("ict_opening_range") or {}
    or_high = opening.get("high")
    current_price = ind.get("current_price")
    if or_high is None or current_price is None:
        return False
    if float(current_price) <= float(or_high):
        return False
    return True


def _ict_turtle_soup_long(ind: dict) -> bool:
    """Turtle Soup (long) — the classic false-breakout-of-PDL reversal.

    Identical structure to Sweep+MSS Long, but the sweep level is
    REQUIRED to be the previous-day low (pdl). Tighter than the parent
    setup because PDL is the most-watched bearish pool in the session;
    wicking through it and closing back up is historically a cleaner
    signal than wicking some arbitrary session low.

    No killzone restriction — turtle soup can fire any time the PDL
    gets probed. In practice it clusters in NY AM + the post-lunch
    reversal window, but we let the data sort that out via hour
    weights rather than hard-gating in the predicate.
    """
    if not _ict_volume_ok(ind):
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss   = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") != "pdl":
        return False
    if ind.get("ict_pd_zone") not in ("discount", "equilibrium"):
        return False
    if not ind.get("ict_in_bull_zone"):
        return False
    return True


def _ict_turtle_soup_short(ind: dict) -> bool:
    """Mirror of ``_ict_turtle_soup_long``. Sweep level must be PDH."""
    if not _ict_volume_ok(ind):
        return False
    sweep = ind.get("ict_last_bear_sweep")
    mss   = ind.get("ict_last_bear_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") != "pdh":
        return False
    if ind.get("ict_pd_zone") not in ("premium", "equilibrium"):
        return False
    if not ind.get("ict_in_bear_zone"):
        return False
    return True


def _ict_asian_range_breakout_long(ind: dict) -> bool:
    """Asian-Range Breakout (long).

    ICT thesis: the 18:00 prev → 04:00 ET overnight range is where
    Asian-session liquidity accumulates. When NY opens and price closes
    above ``asian_high`` during RTH with a bullish MSS already printed,
    the buy-side liquidity has been claimed and continuation often
    follows. This is the "expansion" leg of the session.

    Predicate:
      * currently in RTH (``ict_rth_minute`` is not None)
      * ``ict_levels.asian_high`` exists (overnight data populated)
      * current price > asian_high (break confirmed)
      * recent bullish MSS (displacement, not just a wick)
      * bullish sweep present (price took the asian_high liquidity)
    """
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_rth_minute") is None:
        return False
    levels = ind.get("ict_levels") or {}
    asian_high = levels.get("asian_high")
    if asian_high is None:
        return False
    price = ind.get("current_price") or ind.get("price")
    # Fall back to session_high if the bar-level price isn't threaded
    # through — most callers do provide it in ``ind``. Without a price
    # we can't confirm the break, so we bail conservatively.
    if price is None:
        return False
    if float(price) <= float(asian_high):
        return False
    mss   = ind.get("ict_last_bull_mss")
    sweep = ind.get("ict_last_bull_sweep")
    if not _ict_recent(mss, 20):
        return False
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    # Prefer (but don't strictly require) that the swept level IS the
    # asian_high. If it isn't, the break above asian_high is still
    # valid as long as ANY recent bullish sweep confirmed momentum.
    return True


def _ict_asian_range_breakout_short(ind: dict) -> bool:
    """Mirror of ``_ict_asian_range_breakout_long``. Price must break
    BELOW ``asian_low`` with a recent bearish MSS + sweep."""
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_rth_minute") is None:
        return False
    levels = ind.get("ict_levels") or {}
    asian_low = levels.get("asian_low")
    if asian_low is None:
        return False
    price = ind.get("current_price") or ind.get("price")
    if price is None:
        return False
    if float(price) >= float(asian_low):
        return False
    mss   = ind.get("ict_last_bear_mss")
    sweep = ind.get("ict_last_bear_sweep")
    if not _ict_recent(mss, 20):
        return False
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    return True


# ── v7.7 additions (2026-04-27) — Failed-ARB / Retest-ARB ──────────────────
#
# After Move-4 flagged ARB Long/Short with the same MAE > MFE counter-edge
# signature that froze BB, the next experimental cohort tests two
# alternative reads of the same Asian-range break event:
#
#   * Failed-ARB (L/S) — the breakout FAILED (price went past the level
#     and got rejected back inside the range). Long if asian_low was the
#     swept level (failed bear breakdown → buyers); short if asian_high
#     (failed bull breakout → sellers). Direction-inverted relative to
#     legacy ARB.
#   * Retest-ARB (L/S) — same direction as legacy ARB, but only enters
#     after a successful retest hold of the broken level. The thesis
#     fix is "the break is real, but buy/sell the retest, not the
#     breakout candle". Same direction as legacy ARB.
#
# Both pairs reuse the existing ``SweepEvent`` infrastructure; no new
# detector code in ``app/ict/`` was needed. See v7.7 docstring.


def _ict_failed_asian_range_breakout_long(ind: dict) -> bool:
    """Failed Asian-Range Breakout (long) — fade a failed bear breakdown.

    Legacy ``_ict_asian_range_breakout_short`` was suspended in v7.7
    because the average ARB Short trade moved against entry (MAE > MFE
    over n=527). The natural inversion: when price takes ``asian_low``
    and gets REJECTED — i.e. the breakdown fails and price closes back
    inside the Asian range — that's a buyer step-in, and the move is
    to the upside.

    In this codebase a ``SweepEvent`` with ``level_name="asian_low"``
    and ``direction="bullish"`` already encodes "wick dipped below
    asian_low, close came back above" — exactly the failed-breakdown
    structure. A bullish MSS more recent than the sweep confirms the
    structure flipped from bearish to bullish; that's when we take the
    long.

    Predicate:
      * RTH only (``ict_rth_minute is not None``)
      * recent bullish sweep of ``asian_low`` (≤ 30 bars, ATR-mag floor)
      * recent bullish MSS, more recent than the sweep (≤ 15 bars)
      * volume floor (avoid dead-tape sweeps)

    Stop anchor (via ``structural_stop_fn=_stop_anchor_sweep_wick``):
    1 tick below the sweep's ``wick_extreme`` — the deepest point of
    the failed breakdown. If price re-takes that wick low, the
    failed-breakdown thesis is wrong (it wasn't failed after all).
    """
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_rth_minute") is None:
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss   = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False  # MSS must come AFTER the sweep
    if sweep.get("level_name") != "asian_low":
        return False
    return True


def _ict_failed_asian_range_breakout_short(ind: dict) -> bool:
    """Mirror of ``_ict_failed_asian_range_breakout_long``.

    Bearish sweep of ``asian_high`` (wick poked above, close came back
    below) + bearish MSS more recent than the sweep. Stop anchor: the
    swept wick high.
    """
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_rth_minute") is None:
        return False
    sweep = ind.get("ict_last_bear_sweep")
    mss   = ind.get("ict_last_bear_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if sweep.get("level_name") != "asian_high":
        return False
    return True


def _ict_retest_arb_long(ind: dict) -> bool:
    """Retest-ARB (long) — break + pullback + hold continuation.

    Where Failed-ARB Long fades the failed BREAKDOWN of asian_low,
    Retest-ARB Long takes the SAME direction as legacy ARB but enters
    only after a successful retest hold of the broken level. The fix
    for legacy ARB's MAE > MFE: maybe the break IS a real signal, but
    the entry timing was wrong (buying the breakout candle instead of
    the retest).

    A bullish sweep of ``asian_high`` (wick dipped below, close came
    back above) AFTER price has already broken above asian_high is the
    "retest held" signature — price pulled back into the level, didn't
    lose it on close, and the level now acts as support. The
    ``current_price > asian_high`` gate ensures we're still above the
    level at signal time (didn't fail the retest after all). A recent
    bullish MSS confirms the broader bullish structure was already in
    place before the retest.

    Predicate:
      * RTH only
      * recent bullish sweep of ``asian_high`` (≤ 30 bars, ATR-mag floor)
      * current price still ABOVE asian_high
      * recent bullish MSS (≤ 30 bars; we don't require it after the
        sweep — the structure existed before the retest)
      * volume floor

    Stop anchor (via ``structural_stop_fn=_stop_anchor_asian_range``):
    asian_high itself. If price closes back into the Asian range, the
    retest-hold thesis is wrong.
    """
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_rth_minute") is None:
        return False
    levels = ind.get("ict_levels") or {}
    asian_high = levels.get("asian_high")
    if asian_high is None:
        return False
    price = ind.get("current_price") or ind.get("price")
    if price is None or float(price) <= float(asian_high):
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss   = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if sweep.get("level_name") != "asian_high":
        return False
    if not _ict_recent(mss, 30):
        return False
    return True


def _ict_retest_arb_short(ind: dict) -> bool:
    """Mirror of ``_ict_retest_arb_long``.

    Bearish sweep of ``asian_low`` (wick poked below, close came back
    above) AND current price still BELOW asian_low + recent bearish
    MSS. Stop anchor: asian_low itself.
    """
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_rth_minute") is None:
        return False
    levels = ind.get("ict_levels") or {}
    asian_low = levels.get("asian_low")
    if asian_low is None:
        return False
    price = ind.get("current_price") or ind.get("price")
    if price is None or float(price) >= float(asian_low):
        return False
    sweep = ind.get("ict_last_bear_sweep")
    mss   = ind.get("ict_last_bear_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if sweep.get("level_name") != "asian_low":
        return False
    if not _ict_recent(mss, 30):
        return False
    return True


# ── Structural-stop anchors (v7.7, 2026-04-27) ─────────────────────────────
#
# Each helper returns a price that the backtester uses to anchor a
# Strategy's stop, or None to fall back to the ATR-only stop (current
# global behavior). They live here, alongside the predicates, so the
# stop-placement logic is co-located with the entry-trigger logic.
# Strategies bind to one of these via ``structural_stop_fn=...``.
# Signature is fixed: ``(ind: dict, direction: str) -> float | None``.

def _stop_anchor_sweep_wick(ind: dict, direction: str) -> float | None:
    """Stop anchor = ``wick_extreme`` of the most recent same-side sweep.

    Used by Failed-ARB. For a long trade we read the ``wick_extreme``
    of the bull sweep (the lowest tick of the failed bear-breakdown);
    the stop will be placed at or just below that level. For a short
    we read the bear sweep's ``wick_extreme`` (highest tick of the
    failed bull breakout); stop placed at or above. None when no
    same-side sweep is in view.
    """
    key = "ict_last_bull_sweep" if direction == "long" else "ict_last_bear_sweep"
    sweep = ind.get(key) or {}
    val = sweep.get("wick_extreme")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stop_anchor_asian_range(ind: dict, direction: str) -> float | None:
    """Stop anchor = ``asian_high`` (long) or ``asian_low`` (short).

    Used by Retest-ARB: we already broke the level and held the retest,
    so the level itself becomes our stop reference. Price re-entering
    the original Asian range = thesis broken.
    """
    levels = ind.get("ict_levels") or {}
    val = levels.get("asian_high" if direction == "long" else "asian_low")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ── Event-tied anchors (v7.7 amendment, 2026-04-27) ────────────────────────
#
# The direction-driven anchors above (``_stop_anchor_sweep_wick``,
# ``_stop_anchor_asian_range``) pick which sweep / level to read based
# on the TRADE direction. That works for natural-direction strategies
# but is wrong for the factorial inversion experiment: an inverted
# Failed-ARB cell (e.g. ``failed_long`` predicate × short trade) would
# read the bear sweep wick because direction="short", but the predicate
# itself fired on a BULL sweep — totally unrelated event.
#
# These four event-tied helpers ignore the ``direction`` argument and
# always read the sweep / level the predicate ACTUALLY fired on. For an
# inverted-direction cell the returned anchor will be on the
# predicate-natural side of price, which is the wrong side of entry
# for the inverted trade, so the simulator's wrong-side check
# (``_simulate_trade``'s ``use_structural`` gate) honestly falls back
# to ATR. ``Trade.structural_stop_used=False`` records the fallback.
#
# Tying anchors to the predicate event rather than the trade direction
# is the correct attribution model: it prevents a structural-stop cell
# from quietly succeeding because it landed on a coincidentally-helpful
# unrelated event.


def _stop_anchor_failed_arb_long_event(ind: dict, direction: str) -> float | None:  # noqa: ARG001
    """Anchor for the Failed-ARB *long*-event (bullish sweep of asian_low).

    Always reads ``ict_last_bull_sweep`` regardless of the supplied
    ``direction`` — the helper is tied to the EVENT, not the trade. For
    a long trade the wick_extreme is below entry (valid anchor). For an
    inverted short the same wick is below entry → wrong side → simulator
    falls back to ATR (``structural_stop_used=False``), which is the
    correct attribution: an inverted Failed-ARB short can't borrow the
    bull sweep wick as a stop reference.
    """
    sweep = ind.get("ict_last_bull_sweep") or {}
    val = sweep.get("wick_extreme")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stop_anchor_failed_arb_short_event(ind: dict, direction: str) -> float | None:  # noqa: ARG001
    """Anchor for the Failed-ARB *short*-event (bearish sweep of asian_high).

    Mirror of ``_stop_anchor_failed_arb_long_event``: always reads
    ``ict_last_bear_sweep`` regardless of trade direction. For a natural
    short the wick_extreme is above entry (valid). For an inverted long
    the same wick is above entry → wrong side → ATR fallback.
    """
    sweep = ind.get("ict_last_bear_sweep") or {}
    val = sweep.get("wick_extreme")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stop_anchor_retest_arb_long_event(ind: dict, direction: str) -> float | None:  # noqa: ARG001
    """Anchor for the Retest-ARB *long*-event (bull sweep of asian_high).

    Always returns ``asian_high`` — the level the predicate fired on.
    For a natural long, current_price > asian_high so this is below
    entry (valid anchor). For an inverted short, asian_high is below
    entry → wrong side → ATR fallback.
    """
    levels = ind.get("ict_levels") or {}
    val = levels.get("asian_high")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stop_anchor_retest_arb_short_event(ind: dict, direction: str) -> float | None:  # noqa: ARG001
    """Anchor for the Retest-ARB *short*-event (bear sweep of asian_low).

    Always returns ``asian_low``. For a natural short, current_price <
    asian_low so this is above entry (valid). For an inverted long,
    asian_low is above entry → wrong side → ATR fallback.
    """
    levels = ind.get("ict_levels") or {}
    val = levels.get("asian_low")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stop_anchor_legacy_arb_long_event(ind: dict, direction: str) -> float | None:  # noqa: ARG001
    """Anchor for the legacy ARB *long*-event (bullish breakout of asian_high).

    Always returns ``asian_high`` — the level we just broke above.
    Natural long: asian_high is below entry → valid (we re-enter range
    if the breakout fails). Inverted short: asian_high is below entry →
    wrong side → ATR fallback.
    """
    levels = ind.get("ict_levels") or {}
    val = levels.get("asian_high")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stop_anchor_legacy_arb_short_event(ind: dict, direction: str) -> float | None:  # noqa: ARG001
    """Anchor for the legacy ARB *short*-event (bearish breakdown of asian_low).

    Always returns ``asian_low``. Natural short: asian_low is above
    entry → valid. Inverted long: asian_low is above entry → wrong side →
    ATR fallback.
    """
    levels = ind.get("ict_levels") or {}
    val = levels.get("asian_low")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _ict_power_of_3_long(ind: dict) -> bool:
    """Power of 3 (long) — Accumulation → Manipulation → Expansion.

    ICT frames the day as three legs: (A) overnight accumulation, (M)
    early RTH manipulation sweep of a level, (E) expansion in the
    opposite direction. For a long PO3 we look for:

      * the MANIPULATION: a bullish sweep of the OPENING RANGE LOW
        (09:30-09:45 ET first 15 min) or any overnight pool (PML,
        asian_low, PDL). This is the fakeout move that traps sellers.
      * the EXPANSION: a bullish MSS that followed the sweep, and
        current price ABOVE the opening_range_high. If price has
        re-taken the upper bound of the opening range after a lower
        sweep + MSS, we're in the expansion leg.
      * we must be in RTH — PO3 doesn't fire pre-open.

    Unlike Sweep+MSS Long this one does NOT require a fresh FVG/OB
    touch. The expansion leg is often already running; the read is
    "we're in the move, not retracing to it".

    Hour-of-day filter (``_PO3_LONG_BLOCK_WINDOWS``): the 6-month live
    baseline (2025-10-27 → 2026-04-24) showed P3-long bleeds -16.2 R
    almost entirely inside 10:00-10:59 ET (n=101, both walk-forward
    halves negative). The 18-month v7.9 replay then exposed an
    additional stable late-day bleed from 14:00-17:59 ET. Trades
    whose ``ict_rth_minute`` falls in either blocked window are
    rejected here.
    """
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None:
        return False
    for lo, hi in _PO3_LONG_BLOCK_WINDOWS:
        if lo <= rth_min < hi:
            return False
    opening = ind.get("ict_opening_range") or {}
    or_high = opening.get("high")
    if or_high is None:
        return False
    price = ind.get("current_price") or ind.get("price")
    if price is None or float(price) <= float(or_high):
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss   = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, 60, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 30):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    # Swept level must be one of the accumulation/opening pools — we
    # don't call a break-of-OR-high "PO3" if the sweep was some random
    # 5-min low twenty bars back.
    allowed = {"opening_low", "pml", "asian_low", "pdl", "session_low"}
    if sweep.get("level_name") not in allowed:
        return False
    return True


def _ict_power_of_3_short(ind: dict) -> bool:
    """Mirror of ``_ict_power_of_3_long``. Bullish sweep of an upper
    pool (opening_high / pmh / asian_high / pdh / session_high) then
    bearish MSS, with current price BELOW opening_range_low.

    Hour-of-day filter (``_PO3_SHORT_BLOCK_WINDOW``): the 6-month
    baseline showed P3-short bled -20.5 R across 09:30-10:59 ET
    (hour 9 n=18 AvgR -0.475, hour 10 n=82 AvgR -0.146; both halves
    negative on hour 9, H1 -0.262 on hour 10). The one earning
    bucket is 11:00 ET (n=16 AvgR +0.547, both halves positive),
    which survives the filter.
    """
    if not _ict_volume_ok(ind):
        return False
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None:
        return False
    lo, hi = _PO3_SHORT_BLOCK_WINDOW
    if lo <= rth_min < hi:
        return False
    opening = ind.get("ict_opening_range") or {}
    or_low = opening.get("low")
    if or_low is None:
        return False
    price = ind.get("current_price") or ind.get("price")
    if price is None or float(price) >= float(or_low):
        return False
    sweep = ind.get("ict_last_bear_sweep")
    mss   = ind.get("ict_last_bear_mss")
    if not _ict_recent(sweep, 60, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 30):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    allowed = {"opening_high", "pmh", "asian_high", "pdh", "session_high"}
    if sweep.get("level_name") not in allowed:
        return False
    return True


def _ict_breaker_block_long(ind: dict) -> bool:
    """Breaker Block (long) — retest of a mitigated bearish OB after a
    bullish MSS flipped its polarity.

    The feature builder already does the heavy lifting: it pairs each
    mitigated OB with the opposing MSS that broke it and surfaces
    ``ict_in_bull_breaker`` + ``ict_nearest_bull_breaker``. Our job
    here is just to gate on freshness (don't retest a breaker that
    mitigated 3 hours ago) and to require the dealing range to still
    be in discount/equilibrium — a breaker retest in full premium
    usually means we're late.

    Edge-detection (added 2026-04-25): ``fresh_entry`` must be True,
    meaning price just crossed INTO the breaker zone on this bar.
    Without this gate the v7.3 backtest fired 4,377 long trades for
    -1,463 R because ``ict_in_bull_breaker`` is sticky (True every bar
    price sits inside the zone). The classic ICT teaching is that the
    retest is a single event — you fire on the touch, not on the
    state. The v7.3 backtest CSV showed clusters of 8-10 trades on
    the same symbol/direction/day (e.g. MS|long|2026-01-14 = 10
    trades), all stopping out at ~-1.25R as price oscillated within a
    stale zone. Edge detection turns this back into the one-shot
    signal the strategy was built around.
    """
    if not _ict_volume_ok(ind):
        return False
    if not ind.get("ict_in_bull_breaker"):
        return False
    brk = ind.get("ict_nearest_bull_breaker") or {}
    if brk.get("bars_ago_mitigated", 10**9) > _BREAKER_MAX_BARS_AGO:
        return False
    # Fresh-entry gate: price must have just crossed INTO the zone on
    # this bar (prev close outside, current close inside). The feature
    # layer sets fresh_entry=True on cold-start (1-bar window) so a
    # brand-new symbol isn't permanently locked out.
    if not brk.get("fresh_entry", False):
        return False
    if ind.get("ict_pd_zone") not in ("discount", "equilibrium"):
        return False
    # Require the recent-MSS context to confirm the flip is structural,
    # not a single-bar fluke. The breaker derivation already pairs with
    # an MSS, but we also want that MSS (or a fresh one) still "in view".
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(mss, 60):
        return False
    return True


def _ict_breaker_block_short(ind: dict) -> bool:
    """Mirror of ``_ict_breaker_block_long``. Same fresh-entry gate —
    see the long predicate docstring for the v7.3 post-mortem."""
    if not _ict_volume_ok(ind):
        return False
    if not ind.get("ict_in_bear_breaker"):
        return False
    brk = ind.get("ict_nearest_bear_breaker") or {}
    if brk.get("bars_ago_mitigated", 10**9) > _BREAKER_MAX_BARS_AGO:
        return False
    if not brk.get("fresh_entry", False):
        return False
    if ind.get("ict_pd_zone") not in ("premium", "equilibrium"):
        return False
    mss = ind.get("ict_last_bear_mss")
    if not _ict_recent(mss, 60):
        return False
    return True


def _ict_multi_day_pd_long(ind: dict) -> bool:
    """Multi-day PD (long) — 20-day discount retrace entry.

    Expands the PD-array concept from intraday session H/L to the
    20-day daily range (``ict_multi_day_range`` from daily_ctx). Fires
    when:

      * multi-day range is populated AND its PD zone == "discount"
      * intraday PD zone also discount/equilibrium (don't counter-trend
        the intraday leg)
      * recent bullish sweep + MSS already printed
      * currently touching a fresh bullish zone

    This is the weekly-swing overlay for the Sweep+MSS Long setup —
    same trigger, but we additionally require the bigger map to agree
    we're buying the weekly discount, not the daily premium.
    """
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_multi_day_pd_zone") != "discount":
        return False
    if ind.get("ict_pd_zone") not in ("discount", "equilibrium"):
        return False
    sweep = ind.get("ict_last_bull_sweep")
    mss   = ind.get("ict_last_bull_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if not ind.get("ict_in_bull_zone"):
        return False
    return True


def _ict_multi_day_pd_short(ind: dict) -> bool:
    """Mirror of ``_ict_multi_day_pd_long``. Multi-day premium retrace."""
    if not _ict_volume_ok(ind):
        return False
    if ind.get("ict_multi_day_pd_zone") != "premium":
        return False
    if ind.get("ict_pd_zone") not in ("premium", "equilibrium"):
        return False
    sweep = ind.get("ict_last_bear_sweep")
    mss   = ind.get("ict_last_bear_mss")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return False
    if not _ict_recent(mss, 15):
        return False
    if mss["bars_ago"] > sweep["bars_ago"]:
        return False
    if not ind.get("ict_in_bear_zone"):
        return False
    return True


def _ict_smt_not_implemented(ind: dict) -> bool:
    """SMT Divergence (ES/NQ) — DEFERRED placeholder predicate.

    SMT (Smart-Money Technique) divergence needs two correlated
    instruments and confirms when one makes a new extreme and the
    paired instrument doesn't. For ICT this is typically ES vs NQ, or
    as an equities proxy SPY vs QQQ. The Tier-1 bar pipeline today is
    single-instrument — ``compute_ict_features(bars, daily_ctx)`` has
    no hook to accept a correlated feed.

    Rather than ship a half-real predicate that always returns True
    (and pollutes Tier-2 with noise) or one that always returns False
    (and hides the gap from future maintainers), we keep this stub
    that always returns False and is NOT wired into PLAYBOOK below.
    When the paired-instrument feed lands, replace the body with:

        price_hi = ind["current_price"]
        corr_hi  = ind.get("ict_correlated_pair_high")  # new feature
        if corr_hi is None or price_hi is None: return False
        ... divergence logic ...

    and add the Strategy entry to PLAYBOOK.
    """
    return False


# ── Research → live promotion gates (v7.7, 2026-04-27) ─────────────────────
#
# Any predicate developed in ``scripts/run_arb_inversion_experiments.py``
# (or any other research driver going forward) must clear ALL of the
# following before being added to ``PLAYBOOK``. These are the same
# gates the v7.7 docstring section (6) lists; replicated here as a
# code-level constant so the rule is enforceable in future review and
# referenceable from the experiment driver's scoreboard.
#
# A research candidate fails-open on any single gate — i.e. one
# missing condition disqualifies promotion regardless of how the
# others look.
#
# NOTE — the factorial inversion driver enforces a fifth,
# experiment-local gate ("structural_attribution_threshold") on top
# of the four below: any cell labeled "structural" must have
# ``structural_used_pct`` ≥ 50% — i.e. the simulator must actually
# have anchored the stop to the predicate's triggering event on at
# least half the trades. That gate is documented inline below for
# reviewer awareness but is not part of the generic constant because
# only structural-stop research has an analogue. Non-structural
# research can ignore it.

RESEARCH_PROMOTION_GATES: dict[str, str] = {
    # (a) Sample size floor.
    "min_trades": (
        "n ≥ 100 trades across the experiment window. Smaller "
        "samples are dominated by noise; per-trade R variance on "
        "this book is large enough that win-rate point estimates "
        "below 100 trades have ±10pp confidence intervals."
    ),
    # (b) MFE > MAE — the counter-edge predicate diagnostic.
    "mfe_gt_mae": (
        "AvgMFE > AvgMAE. This is the same diagnostic that flagged "
        "the Breaker Block strategies in Phase-2 and the legacy ARB "
        "predicates in Move-4: when the average trade moves further "
        "AGAINST entry than FOR it after the signal fires, the "
        "predicate is not an entry signal regardless of how stops or "
        "targets are tuned."
    ),
    # (c) Positive expectancy net of slippage.
    "positive_avg_r": (
        "AvgR > 0 with slippage already baked in via "
        "``_entry_fill_price`` / ``_exit_fill_price`` in the "
        "backtester. AvgR > 0 on raw-fill data is not enough — "
        "anything with AvgR < ~0.05 R after slippage is below the "
        "live trading-cost floor."
    ),
    # (d) Walk-forward stability — both halves must be positive.
    "walk_forward_stability": (
        "Split the experiment window in half on entry_ts; AvgR must "
        "be positive on BOTH halves, not just the aggregate. "
        "One-half-only edges are regime artifacts that won't survive "
        "the next regime shift. The factorial driver implements this "
        "split as a column on the scoreboard."
    ),
    # (e) Structural-attribution gate — applies ONLY to structural
    # cells; vacuously satisfied for atr_only cells. Enforced inside
    # ``scripts/run_arb_inversion_experiments.py`` via the constant
    # ``_GATE_MIN_STRUCTURAL_USED_PCT`` (currently 50.0). Listed here
    # so any future structural-stop research driver inherits the
    # same threshold and the manual promotion review can confirm
    # the structural label was earned by actual structural-anchor
    # use, not silent ATR fallback.
    "structural_attribution_threshold": (
        "Structural cells only — ``structural_used_pct`` ≥ 50%. The "
        "simulator records a per-trade ``structural_stop_used`` flag "
        "in the per-trade CSV; the gate fails any cell labeled "
        "\"structural\" that fell back to ATR (anchor None, anchor "
        "on wrong side of entry, or no anchor defined for the "
        "predicate) on more than half its trades. Without this gate "
        "a structural cell with no defined anchor (e.g. Breaker "
        "Block) prints numbers identical to its ATR twin and could "
        "falsely earn a structural ← PROMOTE label. Vacuously True "
        "for atr_only cells (they are not claiming structural "
        "attribution). Threshold lives in the driver as "
        "``_GATE_MIN_STRUCTURAL_USED_PCT`` so it can be tuned per "
        "experiment without editing app/strategies.py."
    ),
    # Failure mode if no cell passes.
    "_fallback_if_none_pass": (
        "If NONE of the {predicate × direction × stop} cells for an "
        "event clear (a)–(e), the underlying event is treated as "
        "CONTEXT only — surfaced to the Tier-2 prompt as a "
        "structural note (e.g. 'asian_high was just swept and "
        "reclaimed') but never used as a Tier-1 entry trigger. "
        "Tier-1 entries require positive expectancy proof; Tier-2 "
        "narrative does not."
    ),
}


# ── Partial-state callables (v7.12, 2026-04-30) ─────────────────────────────
#
# These functions implement the ``_partial_check`` field on the seven
# live PLAYBOOK strategies. They surface "forming" structural states
# (sweep detected but no MSS; sweep + MSS but no FVG/OB retest; etc.)
# to the live scanner so the frontend can render a "Watching" panel
# above the matched-strategies row. PURE METADATA — these never gate
# ``matches()`` and never change the backtest trade set.
#
# Returned dict shape (None means nothing forming):
#     {
#         "stage": "sweep_only" | "sweep_and_mss" | "fully_formed",
#         "missing": ["mss", "fvg_touch", "rth_window", ...],
#         "structural_notes": ["Bullish sweep of pdl 4 bars ago", ...],
#     }
#
# Stage rules:
#   * "sweep_only" — recent qualifying sweep, but the MSS gate fails
#     (no MSS, stale MSS, or MSS came BEFORE the sweep)
#   * "sweep_and_mss" — sweep + MSS in correct order, but the
#     final entry trigger is missing (FVG/OB touch / OR reclaim /
#     multi-day PD context, depending on the strategy)
#   * "fully_formed" — every gate that ``matches()`` checks is
#     also passing. This DOESN'T guarantee ``matches()`` returns
#     True (e.g. symbol_blacklist might still kick in), but the
#     structural sequence is complete.
#
# Common pre-conditions: if ``_ict_volume_ok`` fails, return None
# (no actionable partial state on dead-tape bars). If the strategy
# has a hard time-window gate (Judas), being outside that window
# means there's no "forming" state to surface either.


def _partial_check_judas_long(ind: dict) -> dict | None:
    """Forming-state for ICT Judas Swing (Long)."""
    if not _ict_volume_ok(ind):
        return None
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None or rth_min >= _JUDAS_WINDOW_END:
        return None  # outside Judas window — no setup can form here
    sweep = ind.get("ict_last_bull_sweep")
    if not _ict_recent(sweep, _JUDAS_MAX_BARS_AGO,
                       ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return None
    if sweep.get("level_name") not in ("pml", "asian_low", "pdl", "session_low"):
        return None
    notes = [f"Bullish sweep of {sweep.get('level_name')} {sweep.get('bars_ago')} bars ago"]
    missing: list[str] = []
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(mss, _JUDAS_MAX_BARS_AGO):
        missing.append("mss")
        notes.append("Waiting for bullish MSS")
    elif mss["bars_ago"] > sweep["bars_ago"]:
        missing.append("mss")
        notes.append("MSS is older than sweep — waiting for fresh MSS")
    else:
        notes.append(f"Bullish MSS {mss['bars_ago']} bars ago (after sweep)")
    if not ind.get("ict_in_bull_zone"):
        missing.append("fvg_touch")
        notes.append("Waiting for retrace into bullish FVG/OB")
    stage = "fully_formed" if not missing else ("sweep_only" if "mss" in missing else "sweep_and_mss")
    return {"stage": stage, "missing": missing, "structural_notes": notes}


def _partial_check_judas_short(ind: dict) -> dict | None:
    """Forming-state for ICT Judas Swing (Short). Mirror of Long."""
    if not _ict_volume_ok(ind):
        return None
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None or rth_min >= _JUDAS_WINDOW_END:
        return None
    sweep = ind.get("ict_last_bear_sweep")
    if not _ict_recent(sweep, _JUDAS_MAX_BARS_AGO,
                       ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return None
    if sweep.get("level_name") not in ("pmh", "asian_high", "pdh", "session_high"):
        return None
    notes = [f"Bearish sweep of {sweep.get('level_name')} {sweep.get('bars_ago')} bars ago"]
    missing: list[str] = []
    mss = ind.get("ict_last_bear_mss")
    if not _ict_recent(mss, _JUDAS_MAX_BARS_AGO):
        missing.append("mss")
        notes.append("Waiting for bearish MSS")
    elif mss["bars_ago"] > sweep["bars_ago"]:
        missing.append("mss")
        notes.append("MSS is older than sweep — waiting for fresh MSS")
    else:
        notes.append(f"Bearish MSS {mss['bars_ago']} bars ago (after sweep)")
    if not ind.get("ict_in_bear_zone"):
        missing.append("fvg_touch")
        notes.append("Waiting for retrace into bearish FVG/OB")
    stage = "fully_formed" if not missing else ("sweep_only" if "mss" in missing else "sweep_and_mss")
    return {"stage": stage, "missing": missing, "structural_notes": notes}


def _partial_check_turtle_soup_long(ind: dict) -> dict | None:
    """Forming-state for ICT Turtle Soup (Long). PDL-only sweep."""
    if not _ict_volume_ok(ind):
        return None
    sweep = ind.get("ict_last_bull_sweep")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return None
    if sweep.get("level_name") != "pdl":
        return None
    notes = [f"Bullish sweep of PDL {sweep.get('bars_ago')} bars ago"]
    missing: list[str] = []
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(mss, 15):
        missing.append("mss")
        notes.append("Waiting for bullish MSS")
    elif mss["bars_ago"] > sweep["bars_ago"]:
        missing.append("mss")
        notes.append("MSS is older than sweep — waiting for fresh MSS")
    else:
        notes.append(f"Bullish MSS {mss['bars_ago']} bars ago (after sweep)")
    if ind.get("ict_pd_zone") not in ("discount", "equilibrium"):
        missing.append("pd_zone_discount")
        notes.append(f"PD zone is {ind.get('ict_pd_zone', 'unknown')} — waiting for discount/equilibrium")
    if not ind.get("ict_in_bull_zone"):
        missing.append("fvg_touch")
        notes.append("Waiting for retrace into bullish FVG/OB")
    stage = "fully_formed" if not missing else ("sweep_only" if "mss" in missing else "sweep_and_mss")
    return {"stage": stage, "missing": missing, "structural_notes": notes}


def _partial_check_turtle_soup_short(ind: dict) -> dict | None:
    """Forming-state for ICT Turtle Soup (Short). PDH-only sweep."""
    if not _ict_volume_ok(ind):
        return None
    sweep = ind.get("ict_last_bear_sweep")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return None
    if sweep.get("level_name") != "pdh":
        return None
    notes = [f"Bearish sweep of PDH {sweep.get('bars_ago')} bars ago"]
    missing: list[str] = []
    mss = ind.get("ict_last_bear_mss")
    if not _ict_recent(mss, 15):
        missing.append("mss")
        notes.append("Waiting for bearish MSS")
    elif mss["bars_ago"] > sweep["bars_ago"]:
        missing.append("mss")
        notes.append("MSS is older than sweep — waiting for fresh MSS")
    else:
        notes.append(f"Bearish MSS {mss['bars_ago']} bars ago (after sweep)")
    if ind.get("ict_pd_zone") not in ("premium", "equilibrium"):
        missing.append("pd_zone_premium")
        notes.append(f"PD zone is {ind.get('ict_pd_zone', 'unknown')} — waiting for premium/equilibrium")
    if not ind.get("ict_in_bear_zone"):
        missing.append("fvg_touch")
        notes.append("Waiting for retrace into bearish FVG/OB")
    stage = "fully_formed" if not missing else ("sweep_only" if "mss" in missing else "sweep_and_mss")
    return {"stage": stage, "missing": missing, "structural_notes": notes}


def _partial_check_p3_long(ind: dict) -> dict | None:
    """Forming-state for ICT Power of 3 (Long). Sweep accumulation +
    expansion above OR-high. Hour-filter blocks (10:00-10:59 ET,
    14:00-17:59 ET) suppress partial state entirely — no setup can
    form on those bars."""
    if not _ict_volume_ok(ind):
        return None
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None:
        return None
    for lo, hi in _PO3_LONG_BLOCK_WINDOWS:
        if lo <= rth_min < hi:
            return None  # blocked window — no forming state
    sweep = ind.get("ict_last_bull_sweep")
    if not _ict_recent(sweep, 60, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return None
    allowed = {"opening_low", "pml", "asian_low", "pdl", "session_low"}
    if sweep.get("level_name") not in allowed:
        return None
    notes = [f"Bullish sweep of {sweep.get('level_name')} {sweep.get('bars_ago')} bars ago"]
    missing: list[str] = []
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(mss, 30):
        missing.append("mss")
        notes.append("Waiting for bullish MSS")
    elif mss["bars_ago"] > sweep["bars_ago"]:
        missing.append("mss")
        notes.append("MSS is older than sweep — waiting for fresh MSS")
    else:
        notes.append(f"Bullish MSS {mss['bars_ago']} bars ago (after sweep)")
    opening = ind.get("ict_opening_range") or {}
    or_high = opening.get("high")
    price = ind.get("current_price") or ind.get("price")
    if or_high is None or price is None:
        missing.append("or_high_reclaim")
        notes.append("Waiting for opening-range data")
    elif float(price) <= float(or_high):
        missing.append("or_high_reclaim")
        notes.append(f"Price ${price:.2f} below OR-high ${float(or_high):.2f} — waiting for reclaim")
    else:
        notes.append(f"Price ${price:.2f} reclaimed OR-high ${float(or_high):.2f}")
    stage = "fully_formed" if not missing else ("sweep_only" if "mss" in missing else "sweep_and_mss")
    return {"stage": stage, "missing": missing, "structural_notes": notes}


def _partial_check_p3_short(ind: dict) -> dict | None:
    """Forming-state for ICT Power of 3 (Short). Mirror of Long."""
    if not _ict_volume_ok(ind):
        return None
    rth_min = ind.get("ict_rth_minute")
    if rth_min is None:
        return None
    lo, hi = _PO3_SHORT_BLOCK_WINDOW
    if lo <= rth_min < hi:
        return None
    sweep = ind.get("ict_last_bear_sweep")
    if not _ict_recent(sweep, 60, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return None
    allowed = {"opening_high", "pmh", "asian_high", "pdh", "session_high"}
    if sweep.get("level_name") not in allowed:
        return None
    notes = [f"Bearish sweep of {sweep.get('level_name')} {sweep.get('bars_ago')} bars ago"]
    missing: list[str] = []
    mss = ind.get("ict_last_bear_mss")
    if not _ict_recent(mss, 30):
        missing.append("mss")
        notes.append("Waiting for bearish MSS")
    elif mss["bars_ago"] > sweep["bars_ago"]:
        missing.append("mss")
        notes.append("MSS is older than sweep — waiting for fresh MSS")
    else:
        notes.append(f"Bearish MSS {mss['bars_ago']} bars ago (after sweep)")
    opening = ind.get("ict_opening_range") or {}
    or_low = opening.get("low")
    price = ind.get("current_price") or ind.get("price")
    if or_low is None or price is None:
        missing.append("or_low_breakdown")
        notes.append("Waiting for opening-range data")
    elif float(price) >= float(or_low):
        missing.append("or_low_breakdown")
        notes.append(f"Price ${price:.2f} above OR-low ${float(or_low):.2f} — waiting for breakdown")
    else:
        notes.append(f"Price ${price:.2f} broke OR-low ${float(or_low):.2f}")
    stage = "fully_formed" if not missing else ("sweep_only" if "mss" in missing else "sweep_and_mss")
    return {"stage": stage, "missing": missing, "structural_notes": notes}


def _partial_check_md_pd_long(ind: dict) -> dict | None:
    """Forming-state for ICT Multi-Day PD (Long). Sweep+MSS Long with
    multi-day discount overlay."""
    if not _ict_volume_ok(ind):
        return None
    sweep = ind.get("ict_last_bull_sweep")
    if not _ict_recent(sweep, 30, ind=ind, min_atr_mult=_MIN_SWEEP_ATR_MULT):
        return None
    notes = [f"Bullish sweep of {sweep.get('level_name')} {sweep.get('bars_ago')} bars ago"]
    missing: list[str] = []
    mss = ind.get("ict_last_bull_mss")
    if not _ict_recent(mss, 15):
        missing.append("mss")
        notes.append("Waiting for bullish MSS")
    elif mss["bars_ago"] > sweep["bars_ago"]:
        missing.append("mss")
        notes.append("MSS is older than sweep — waiting for fresh MSS")
    else:
        notes.append(f"Bullish MSS {mss['bars_ago']} bars ago (after sweep)")
    if ind.get("ict_multi_day_pd_zone") != "discount":
        missing.append("multi_day_discount")
        notes.append(f"Multi-day PD is {ind.get('ict_multi_day_pd_zone', 'unknown')} — waiting for discount")
    if ind.get("ict_pd_zone") not in ("discount", "equilibrium"):
        missing.append("intraday_pd")
        notes.append(f"Intraday PD is {ind.get('ict_pd_zone', 'unknown')} — waiting for discount/equilibrium")
    if not ind.get("ict_in_bull_zone"):
        missing.append("fvg_touch")
        notes.append("Waiting for retrace into bullish FVG/OB")
    stage = "fully_formed" if not missing else ("sweep_only" if "mss" in missing else "sweep_and_mss")
    return {"stage": stage, "missing": missing, "structural_notes": notes}


# ── The Playbook — ICT only (v6, 2026-04-24) ─────────────────────────────────
#
# Three composite ICT setups that sit on top of the ICT feature layer
# in ``app/ict/``. The Tier-1 predicates here are thin wrappers that
# read pre-computed zone flags; the real work — swing detection,
# FVG / OB construction, sweep + MSS events — happens before screen()
# is called, in ``compute_ict_features()``. Tier-2 Opus gets the
# full zone geometry via ``ind_snapshot["ict_zones"]`` to reason
# about exact entry price / stop / target.

PLAYBOOK: list[Strategy] = [

    # NOTE (2026-04-25): the legacy ``ICT Sweep + MSS (Long)`` entry
    # was removed here because it duplicated ``ICT Silver Bullet (Long)``
    # after the killzone gate came off Silver Bullet. Silver Bullet's
    # higher ``min_confidence=0.60`` floor is preferred, so it remains
    # as the sole bullish Sweep+MSS Long. The Short side has no Silver
    # Bullet duplicate, so ``ICT Sweep + MSS (Short)`` stays.

    # NOTE (2026-04-28, v7.9): ``ICT Sweep + MSS (Short)`` was
    # SUSPENDED from the live PLAYBOOK after the post-cooldown-fix
    # 18-month baseline (2024-10-27 → 2026-04-24, 18-symbol)
    # showed n=69 / AvgR -0.228 / TotalR -15.73 R / WR 36.2 %, with
    # the same counter-edge MAE > MFE signature (AvgMAE +1.043 vs
    # AvgMFE +0.876) that flagged BB (v7.6) and ARB (v7.7). Walk-
    # forward halves bled in BOTH halves (H1 -0.291 R/trade on n=38,
    # H2 -0.143 R/trade on n=31) — not a regime artifact. Hour
    # decomposition concentrates the bleed at 09:00-11:00 ET but
    # no surviving hour cell clears positive AvgR with n ≥ 15, so
    # an hour-of-day filter (the v7.8 P3 trick) doesn't apply
    # cleanly here. The predicate function
    # (``_ict_sweep_mss_short``) lives on for ``playbook_override``-
    # driven research and is still wired into the regime-weight
    # table in ``app/regime.py``. Raw Sweep / MSS / FVG / OB
    # structure continues to flow to Tier-2 via
    # ``analyzer._build_ict_section`` (reads from ``ind`` directly,
    # independent of named-strategy matches). Reactivation requires
    # a fresh factorial round that clears the (a)-(e) gates from
    # v7.7's promotion-gate framework.
    #
    # Companion suspension below: ``ICT Multi-Day PD (Short)``
    # (same -0.295 AvgR / counter-edge / both-halves-negative
    # signature on n=62). Long-side variants of both predicates
    # remain in the live book.

    # NOTE (2026-04-25 evening): ``ICT Silver Bullet (Long)`` was
    # SUSPENDED from the live PLAYBOOK pending an investigation. On
    # n=80 historical trades the 10:00 ET cluster posted a 23.8% win
    # rate — extreme enough that something specific is structurally
    # wrong (likely an interaction between the killzone removal and
    # the volume profile in the 10:00–10:30 ET pocket). The predicate
    # function (``_ict_silver_bullet_long``) lives on; flip the
    # ``Strategy(...)`` block back into the list here once the cause
    # is identified and the win rate verified out-of-sample.
    #
    # Side effect (post-v7.9): with ``ICT Sweep + MSS (Short)`` also
    # suspended, no directly-named "Sweep + MSS" entry remains in the
    # live playbook. The bullish Sweep + MSS thesis is still
    # represented via Turtle Soup (Long), Power of 3 (Long) and
    # Multi-Day PD (Long); the bearish thesis is now context-only,
    # surfaced to the Tier-2 prompt as raw Sweep / MSS structure
    # via ``analyzer._build_ict_section``.

    # ── v7 additions (2026-04-24) ──────────────────────────────────────────

    Strategy(
        name="ICT Judas Swing (Long)",
        direction="long",
        description=(
            "The 09:30–10:00 ET fakeout reversal. Price opens, "
            "sweeps an overnight low (pre-market low, Asian low, "
            "previous-day low) in the first 30 minutes to trap "
            "early sellers, then reverses — a bullish MSS prints "
            "and price retraces into a fresh bullish FVG/OB. ICT "
            "calls this the \"Judas\" move because the initial "
            "down-thrust betrays the real daily direction."
        ),
        entry_rules=(
            "Only fires in the first 30 minutes of RTH "
            "(09:30–10:00 ET). Requires a bullish sweep of PML / "
            "asian_low / PDL / session_low within the last 15 bars, "
            "followed by a bullish MSS (more recent than the sweep), "
            "and current price touching a fresh bullish FVG/OB."
        ),
        stop_rules=(
            "1 tick below the sweep wick low — the overnight "
            "liquidity pool that was just run. Invalidation = "
            "price revisiting that level inside the 30-min window."
        ),
        target_rules=(
            "Target 1: the 09:30 opening print (gap-fill objective). "
            "Target 2: prior-day high or session high, whichever is "
            "nearer. R:R ≥ 2 required before entry."
        ),
        min_confidence=0.60,
        # Move-4 best exit (n=68 — sub-100 sample, low signal):
        # inherit portfolio-best confirm1_s15_t25 rather than chase
        # a noisy per-strategy winner.
        exit_model_name="confirm1_s15_t25",
        # v7.11 tier (post-friction AvgR +0.928 on n=19, 18m audit):
        # A-grade primary. Carries 84% of post-friction edge in the
        # whole live PLAYBOOK. Surface prominently in the UI as
        # "Top setup today" when this fires.
        tier="A",
        _check=_ict_judas_swing_long,
        _partial_check=_partial_check_judas_long,
    ),

    Strategy(
        name="ICT Judas Swing (Short)",
        direction="short",
        description=(
            "Mirror of Judas Swing (Long). Price opens, sweeps an "
            "overnight HIGH (PMH / Asian high / PDH / session high) "
            "in the first 30 minutes to trap early buyers, then "
            "reverses with a bearish MSS and retraces into a fresh "
            "bearish FVG/OB."
        ),
        entry_rules=(
            "Only fires 09:30–10:00 ET. Bearish sweep of a named "
            "upper pool within 15 bars, bearish MSS more recent than "
            "the sweep, price touching a fresh bearish FVG/OB."
        ),
        stop_rules=(
            "1 tick above the sweep wick high. Invalidation = "
            "price revisiting that level inside the window."
        ),
        target_rules=(
            "Target 1: opening print (gap-fill objective). Target 2: "
            "PDL or session low. R:R ≥ 2."
        ),
        min_confidence=0.60,
        # Move-4: low-n sample, inherit portfolio-best confirm1_s15_t25.
        exit_model_name="confirm1_s15_t25",
        # v7.11 tier (post-friction AvgR +0.136 on n=21, 18m audit):
        # A-grade. Positive after friction, n=21 supports a real edge.
        tier="A",
        _check=_ict_judas_swing_short,
        _partial_check=_partial_check_judas_short,
    ),

    Strategy(
        name="ICT Turtle Soup (Long)",
        direction="long",
        description=(
            "The classic \"false break of the previous-day low\". "
            "PDL is the most-watched bearish pool in any session. "
            "When price wicks through it, closes back above, and "
            "prints a bullish MSS, the stop-run has failed and the "
            "market reverses. Entry is on the retrace into the "
            "fresh bullish FVG/OB created by the reversal move."
        ),
        entry_rules=(
            "The swept level MUST be the previous-day low (pdl). "
            "Recent bullish sweep of PDL (≤ 30 bars), bullish MSS "
            "more recent than the sweep, PD zone discount or "
            "equilibrium, currently in a fresh bullish FVG/OB."
        ),
        stop_rules=(
            "1 tick below the sweep wick low — i.e. below the PDL "
            "break. If price closes back below PDL after entry, "
            "the Turtle Soup thesis failed."
        ),
        target_rules=(
            "Target 1: prior-day close (gap magnet). Target 2: "
            "prior-day high. Skip the setup if the distance to "
            "target 1 is < 1× ATR."
        ),
        min_confidence=0.60,
        # Move-4: low-n sample, inherit portfolio-best confirm1_s15_t25.
        exit_model_name="confirm1_s15_t25",
        # v7.11 tier (post-friction AvgR +0.060 on n=18, 18m audit):
        # A-grade. Modest but positive post-friction edge.
        tier="A",
        _check=_ict_turtle_soup_long,
        _partial_check=_partial_check_turtle_soup_long,
    ),

    Strategy(
        name="ICT Turtle Soup (Short)",
        direction="short",
        description=(
            "Mirror of Turtle Soup (Long). False break of the "
            "previous-day HIGH. Wick through PDH, close back below, "
            "bearish MSS confirms the reversal, enter on retrace "
            "into a fresh bearish FVG/OB."
        ),
        entry_rules=(
            "Swept level must be PDH. Bearish sweep ≤ 30 bars, "
            "bearish MSS more recent, PD zone premium/equilibrium, "
            "price in a fresh bearish FVG/OB."
        ),
        stop_rules=(
            "1 tick above the swept PDH. Close back above PDH "
            "invalidates."
        ),
        target_rules=(
            "Target 1: prior-day close. Target 2: prior-day low. "
            "R:R ≥ 2; skip if T1 < 1× ATR away."
        ),
        min_confidence=0.60,
        # Move-4: low-n sample, inherit portfolio-best confirm1_s15_t25.
        exit_model_name="confirm1_s15_t25",
        # v7.11 tier (post-friction AvgR -0.248 on n=13, 18m audit):
        # CONTEXT-ONLY. The only live strategy that's negative even
        # raw on the 18m. n=13 is noise but it points the wrong way.
        # Predicate stays callable + the trade still fires in the
        # backtest (no trade-count change), but the UI demotes it to
        # confluence-only display and Sonnet sees it labeled as such.
        # Re-evaluate once n >= 50 across multiple windows.
        tier="context",
        _check=_ict_turtle_soup_short,
        _partial_check=_partial_check_turtle_soup_short,
    ),

    # NOTE (2026-04-27, v7.7): ``ICT Asian-Range Breakout (Long)`` and
    # ``ICT Asian-Range Breakout (Short)`` were SUSPENDED from the live
    # PLAYBOOK after the Move-4 exit-model matrix
    # (``scripts/run_playbook_exit_matrix.py``) showed both posting
    # AvgMAE > AvgMFE — the same counter-edge signature that froze the
    # Breaker Block strategies. ARB Long: AvgMAE +1.123 vs AvgMFE +0.979
    # over n=570. ARB Short: +1.100 vs +0.982 over n=527. Best stop
    # variant for ARB Long was ``s25_t25`` at -0.216 AvgR (still
    # bleeding); best for ARB Short was ``confirm1_s15_t25`` at -0.079.
    # ARB combined drove -257.6 R of the live book's -324.4 R baseline
    # bleed.
    #
    # The framing shift: a counter-edge predicate can't be saved by
    # stop tuning. Either the event is a signal in the OTHER direction
    # (Failed-ARB hypothesis), or the entry timing was wrong (Retest-ARB
    # hypothesis), or the asian_high/low sweep is only a context signal
    # rather than an entry trigger. Discriminating between these
    # hypotheses is the job of the factorial inversion driver
    # (``scripts/run_arb_inversion_experiments.py``), NOT the live
    # PLAYBOOK.
    #
    # RESEARCH-ONLY (NOT IN LIVE PLAYBOOK):
    #   * ``_ict_failed_asian_range_breakout_long`` / ``_short``
    #   * ``_ict_retest_arb_long`` / ``_short``
    #   * Legacy ``_ict_asian_range_breakout_long`` / ``_short``
    # All six remain callable from this module so the inversion driver
    # can sweep them via ``playbook_override``. They DO NOT enter the
    # live PLAYBOOK from a code edit alone — promotion requires ALL of
    # the gates documented as ``RESEARCH_PROMOTION_GATES`` below
    # (n ≥ 100, AvgMFE > AvgMAE, positive AvgR after slippage,
    # walk-forward consistency between both halves of the sample, and
    # — for cells labeled "structural" only — structural_used_pct ≥
    # 50% so the structural label is earned by actual structural-anchor
    # use, not silent ATR fallback) on the factorial experiment. If no
    # cell of {predicate × direction × stop} clears the gates, the
    # asian_high/low sweep stays as CONTEXT for Tier-2 prompting only
    # — never a Tier-1 entry trigger.

    Strategy(
        name="ICT Power of 3 (Long)",
        direction="long",
        description=(
            "Accumulation → Manipulation → Expansion. The "
            "overnight accumulation leg, a down-sweep manipulation "
            "of an opening/overnight low in the first hour, then "
            "expansion back above the opening-range high. We enter "
            "the expansion leg once the reversal is confirmed and "
            "price has re-taken the OR high."
        ),
        entry_rules=(
            "In RTH with current price ABOVE opening_range_high. "
            "Required earlier manipulation: bullish sweep of "
            "opening_low / PML / asian_low / PDL / session_low "
            "(≤ 60 bars) followed by a bullish MSS (≤ 30 bars, "
            "more recent than the sweep). No FVG-touch requirement "
            "— this is the expansion leg, not a retrace entry."
        ),
        stop_rules=(
            "Below opening_range_high (the level we just reclaimed). "
            "If we fall back inside the opening range the expansion "
            "thesis is wrong."
        ),
        target_rules=(
            "Target 1: opening_range_high + 1× opening_range size "
            "(measured move). Target 2: prior-day high or session "
            "high. Trail remainder at EMA9."
        ),
        min_confidence=0.60,
        # Move-4 best exit (n=175): s20_t25_partial1r50_be — wider
        # 2.0×ATR stop with partial-take-at-1R + breakeven move
        # rescues the 35.4% win rate. Different from the rest of the
        # book (which prefers confirm1) because PO3 fires inside an
        # already-extended move, so the partial-take captures the
        # initial expansion before the move stalls.
        exit_model_name="s20_t25_partial1r50_be",
        # v7.10 (2026-04-29): blacklist MS, JPM, BAC, PLTR — clean
        # bleed cells from the 18-month factorial audit.
        # v7.12.1 (2026-05-01): extend with DKNG and AMZN. Both
        # cleared the n>=5 + post-friction-negative gate on the
        # v7.12 23-symbol 6m baseline:
        #   * P3 Long × DKNG: n=7, 43% WR, post-friction -1.61 R
        #   * P3 Long × AMZN: n=5, 40% WR, post-friction -1.56 R
        # AMZN remains in the universe (it earns +1.50 R on
        # P3 Short) but P3 Long on AMZN is no longer eligible.
        # DKNG also stays for MD-PD Long (which is positive on it).
        symbol_blacklist=frozenset({"MS", "JPM", "BAC", "PLTR", "DKNG", "AMZN"}),
        # v7.11 tier (post-friction AvgR -0.024 on n=149, 18m audit):
        # B-grade. Raw edge survives (+0.076) but friction kills it.
        # High-flow strategy (149 trades / 18m); useful with extra
        # confirmation. Score function may demote to suppress on
        # specific (symbol, hour) cells where post-friction is
        # deeply negative.
        tier="B",
        _check=_ict_power_of_3_long,
        _partial_check=_partial_check_p3_long,
    ),

    Strategy(
        name="ICT Power of 3 (Short)",
        direction="short",
        description=(
            "Mirror of Power of 3 (Long). Morning manipulation "
            "wicks an upper pool (opening_high / PMH / asian_high / "
            "PDH / session_high), bearish MSS prints, price breaks "
            "BELOW opening_range_low — enter the expansion."
        ),
        entry_rules=(
            "In RTH with price below opening_range_low. Earlier "
            "bearish sweep of an upper pool (≤ 60 bars) + bearish "
            "MSS (≤ 30 bars, more recent)."
        ),
        stop_rules=(
            "Above opening_range_low. Close back inside the "
            "opening range invalidates."
        ),
        target_rules=(
            "Target 1: opening_range_low − 1× opening_range size. "
            "Target 2: PDL or session low. Trail remainder."
        ),
        min_confidence=0.60,
        # Move-4 best exit (n=165): confirm1_s15_t25. PO3 Short
        # didn't share the long side's preference for the partial-
        # take exit — short-side expansions tend to mean-revert
        # faster, so the partial often fills then stop-out at BE.
        exit_model_name="confirm1_s15_t25",
        symbol_blacklist=frozenset({"JPM"}),
        # v7.11 tier (post-friction AvgR -0.087 on n=54, 18m audit):
        # B-grade with regime caveat. Recent 6m strong (+0.798 AvgR
        # on n=14) but older 12m bleeds — H1 walk-forward -6.34 R on
        # n=34. Score function should weight recent edge heavier here.
        tier="B",
        _check=_ict_power_of_3_short,
        _partial_check=_partial_check_p3_short,
    ),

    # NOTE (2026-04-26): ``ICT Breaker Block (Long)`` and ``ICT Breaker
    # Block (Short)`` were SUSPENDED from the live PLAYBOOK after the
    # v7.5 backtest showed they accounted for 89% of total losses
    # (8,790 trades / -2,705 R combined) at 31–33% win rates across
    # every hour of the session. The ``fresh_entry`` edge-detection
    # added in v7.4 (predicate gates on prev-close-outside / current-
    # close-inside the breaker zone) only cut trade count by 2.5%
    # (9,017 → 8,790) — price genuinely oscillates in/out of the
    # breaker zone every ~40 bars in choppy action, so even one-fire-
    # per-zone-entry still produces 8–10 fires per session-symbol-
    # direction. The 15-bar per-strategy cooldown is the only thing
    # holding density there; without it the count would be much
    # higher. A proper fix needs a per-mitigated-zone cooldown (fire
    # once per breaker, ever — keyed off the OB's mitigation
    # timestamp/idx), which is a bigger structural change than a
    # predicate tweak. Suspending the two entries (mirroring the
    # Silver Bullet (Long) suspension above) takes the v7.5 book
    # from 10,359 trades / -3,026 R down to ~1,569 trades / ~-321 R
    # without touching any other strategy.
    #
    # The predicate functions ``_ict_breaker_block_long`` /
    # ``_ict_breaker_block_short`` live on (and still gate on
    # ``fresh_entry``); flip the two ``Strategy(...)`` blocks back
    # into the list once the per-mitigated-zone cooldown is
    # implemented and validated out-of-sample. Playbook count went
    # 13 → 11.
    #
    # FROZEN — DO NOT REACTIVATE without a structural rewrite.
    # Phase-2 entry-feature mining (2026-04-27) ruled out the
    # "fire once per breaker, ever" hypothesis: the
    # ``is_first_fire_on_breaker=True`` slice posts 32.0% WR,
    # statistically identical to the 31–33% baseline. None of the
    # stacked candidate gates (first-fire × intraday PD aligned ×
    # multi-day PD aligned × regime not vetoed × vol_ratio ≥ 1.0)
    # cleared 35% WR. The exit-model matrix
    # (``scripts/run_phase2_exit_experiments.py`` +
    # ``default_exit_model_matrix``) confirmed the issue is upstream
    # of the stop: every single model showed
    # MAE > MFE on average — the mathematical signature of a
    # counter-edge predicate. Best stop variant ``s25_t25`` only
    # lifted WR to 44.8 % at -0.219 AvgR (still bleeding); the
    # baseline ``s15_t25`` was at 31.9 % / -0.353. The widest stops
    # buy time, but price is genuinely going the wrong way most of
    # the time after these signals fire — by the time price
    # "reclaims" a mitigated OB, the dominant move from that level
    # is *continuation through* the OB, not rejection from it.
    # Reactivation requires either (a) inverting direction (treat
    # the predicate as a continuation signal — needs separate
    # inversion test), or (b) a fundamentally different breaker
    # definition that isn't the recently-mitigated-OB pattern. Until
    # then this stays out of the live PLAYBOOK; further mining work
    # uses ``playbook_override`` for analysis only.

    Strategy(
        name="ICT Multi-Day PD (Long)",
        direction="long",
        description=(
            "Same structural Sweep+MSS Long trigger, but qualified "
            "by the 20-day daily range: we only buy when daily PD "
            "also reads discount, i.e. we're buying the weekly "
            "discount, not the daily premium of a longer-term "
            "downtrend. Keeps us from taking long setups right "
            "into multi-week supply."
        ),
        entry_rules=(
            "``ict_multi_day_pd_zone`` == \"discount\" AND intraday "
            "PD in discount/equilibrium. Recent bullish sweep "
            "(≤ 30 bars) + MSS (≤ 15 bars, more recent). Fresh "
            "bullish FVG/OB touch."
        ),
        stop_rules=(
            "Below the sweep wick low, same as Sweep+MSS Long."
        ),
        target_rules=(
            "Target 1: intraday dealing-range high (session high). "
            "Target 2: multi-day mid (50% of 20-day range). "
            "Higher-timeframe R:R target — size smaller if ATR is "
            "tight vs. the multi-day span."
        ),
        min_confidence=0.55,
        # Move-4 best exit (n=24, low-n): s20_t30. Multi-day setups
        # take longer to play out so a wider stop + larger target
        # makes structural sense. Re-validate in the next matrix
        # run once n grows.
        exit_model_name="s20_t30",
        # v7.11 tier (post-friction AvgR -0.056 on n=61, 18m audit):
        # B-grade. Same friction-fragile pattern as P3 Long. Useful
        # flow but post-friction marginal; require extra confirmation.
        tier="B",
        # v7.12.1 (2026-05-01): SOFI on this strategy is a clean
        # bleed cell — n=5, 0% WR, raw AvgR -1.064, post-friction
        # TotalR -5.82 R on the v7.12 23-symbol 6m baseline. SOFI
        # remains in the live universe (it earns +1.88 R on
        # Power of 3 (Short)) but Multi-Day PD (Long) on SOFI is
        # blacklisted. Same surgical approach as the v7.10 P3 Long
        # blacklist on {MS, JPM, BAC, PLTR}.
        symbol_blacklist=frozenset({"SOFI"}),
        _check=_ict_multi_day_pd_long,
        _partial_check=_partial_check_md_pd_long,
    ),

    # NOTE (2026-04-28, v7.9): ``ICT Multi-Day PD (Short)`` was
    # SUSPENDED from the live PLAYBOOK alongside Sweep+MSS (Short).
    # On the 18-symbol / 18-month re-baseline (2024-10-27 →
    # 2026-04-24): n=62, AvgR -0.295, TotalR -18.30 R, WR 33.9 %.
    # Counter-edge MAE > MFE diagnostic (AvgMAE +1.018 vs AvgMFE
    # +0.832), same signature that flagged BB and ARB. Walk-forward
    # halves both negative (H1 -0.452 R/trade, H2 -0.117 R/trade);
    # H2 alone is closer to noise but neither half is positive.
    #
    # Key asymmetry vs the Long-side variant: ``ICT Multi-Day PD
    # (Long)`` posted +0.146 AvgR / +9.20 R / WR 49.2 % on n=63
    # over the same window. The multi-day-PD framework only worked
    # on the long side over this window — likely a directional
    # drift in the test universe, but either way the short-side
    # cell is the bleeder, not the framework as a whole.
    #
    # The predicate function (``_ict_multi_day_pd_short``) lives on
    # for ``playbook_override``-driven research and is still wired
    # into the regime-weight table in ``app/regime.py``. Multi-day
    # PD zone, intraday PD zone, and Sweep / MSS structure continue
    # to flow to Tier-2 via ``analyzer._build_ict_section`` —
    # surfaced as context, just no longer auto-named "Multi-Day PD
    # (Short)" by the Tier-1 layer. Reactivation requires a fresh
    # factorial round that clears the (a)-(e) gates from v7.7.

]


CONTEXT_PLAYBOOK: list[Strategy] = [
    Strategy(
        name="ICT Sweep + MSS (Short)",
        direction="short",
        description="Suspended bearish sweep-then-MSS structure; use only as short-side confluence.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_sweep_mss_short,
    ),
    Strategy(
        name="ICT Multi-Day PD (Short)",
        direction="short",
        description="Suspended short from multi-day premium; useful as higher-timeframe bearish context.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_multi_day_pd_short,
    ),
    Strategy(
        name="ICT Breaker Block (Long)",
        direction="long",
        description="Frozen breaker-block retest context after mitigation and bullish reclaim.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_breaker_block_long,
    ),
    Strategy(
        name="ICT Breaker Block (Short)",
        direction="short",
        description="Frozen breaker-block retest context after mitigation and bearish reclaim.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_breaker_block_short,
    ),
    Strategy(
        name="ICT Asian-Range Breakout (Long)",
        direction="long",
        description="Suspended asian-range upside break; use only as range/liquidity context.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_asian_range_breakout_long,
    ),
    Strategy(
        name="ICT Asian-Range Breakout (Short)",
        direction="short",
        description="Suspended asian-range downside break; use only as range/liquidity context.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_asian_range_breakout_short,
    ),
    Strategy(
        name="ICT Failed-ARB (Long)",
        direction="long",
        description="Research-only fade of failed asian-low breakdown; confluence note only.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_failed_asian_range_breakout_long,
    ),
    Strategy(
        name="ICT Failed-ARB (Short)",
        direction="short",
        description="Research-only fade of failed asian-high breakout; confluence note only.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_failed_asian_range_breakout_short,
    ),
    Strategy(
        name="ICT Retest-ARB (Long)",
        direction="long",
        description="Research-only retest of reclaimed asian high; confluence note only.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_retest_arb_long,
    ),
    Strategy(
        name="ICT Retest-ARB (Short)",
        direction="short",
        description="Research-only retest of lost asian low; confluence note only.",
        entry_rules="Context only — not a live entry trigger.",
        stop_rules="Context only.",
        target_rules="Context only.",
        tier="context",
        _check=_ict_retest_arb_short,
    ),
]


def screen(indicators: dict) -> list[Strategy]:
    """Return all strategies whose pre-screen conditions match the current indicators."""
    return [s for s in PLAYBOOK if s.matches(indicators)]


def context_screen(indicators: dict) -> list[Strategy]:
    """Return context-only structural setups. These never auto-trade."""
    return [s for s in CONTEXT_PLAYBOOK if s.matches(indicators)]


def playbook_summary() -> str:
    """Return a compact summary of all available strategies (for UI display)."""
    lines = []
    for s in PLAYBOOK:
        lines.append(f"• {s.name} ({s.direction.upper()}) — {s.description[:80]}…")
    return "\n".join(lines)


# ── EXPERIMENTAL_PLAYBOOK (v7.15, 2026-05-18) ─────────────────────────────────
#
# Second-scanner predicates: 3 classic (ORB / VWAP reclaim / EMA pullback) +
# 2 momentum (gap+RVOL breakout / failed-breakdown). These are NOT in the
# v7.12.1 audit baseline — they fire under the v7.15 experimental scanner
# with their own kill switch, daily cap, risk multiplier, and ICT-priority
# timeout (see ``app/scanner_experimental.py`` and ``_scan_and_trade``
# wiring in ``app/autotrade.py``).
#
# Snapshot predicates: every check reads from the precomputed ``ind`` dict
# that ``compute_indicators`` produces. Bars are also threaded in via the
# scanner so predicates that need bar windows (recent_low_touch for EMA
# pullback, recent-bar prev_low break for Failed Breakdown) can use them
# without recomputing indicators. ``compute_indicators`` already surfaces
# ``current_price``, ``ema9``, ``ema20``, ``vwap``, ``vol_ratio``, ``rsi``,
# ``gap_pct``, ``session_high``, and ``opening_range`` (nested dict with
# ``high`` / ``low``); ``daily_ctx`` carries ``prev_low``. No analyzer
# changes needed for v7.15.
#
# Each predicate has a doc-comment with the entry rule it's screening for.
# Stop / target are filled in downstream by ``_build_predicate_only_result``
# (existing autotrade helper) — these predicates only decide "does this
# setup fire?", they don't size the trade.

_EXP_OR_VOL_RATIO_MIN = 1.5       # ORB Long volume floor
_EXP_VWAP_VOL_RATIO_MIN = 1.2     # VWAP Reclaim volume floor
_EXP_EMA_PULLBACK_PCT = 0.002     # ±0.2% — how close prev low must be to ema20
_EXP_MOMO_GAP_MIN = 0.015         # +1.5% gap floor for Momentum Breakout
_EXP_MOMO_VOL_RATIO_MIN = 2.0     # Momentum Breakout vol_ratio floor
_EXP_MOMO_NEAR_HIGH_PCT = 0.005   # 0.5% — current price must be within this of session high
_EXP_FB_REBOUND_PCT = 0.003       # 0.3% — close must be this far above prev_low to qualify
_EXP_FB_LOOKBACK_BARS = 10        # how many bars back to look for the prev_low break


def _exp_get_opening_range_high(ind: dict) -> float | None:
    """``compute_indicators`` exposes opening_range as a nested dict at
    the top level; surface the high as a flat scalar (or None when the
    session hasn't formed an opening range yet — e.g. pre-09:30 ET, or
    when the bar history is too short)."""
    or_data = ind.get("opening_range")
    if not isinstance(or_data, dict):
        return None
    high = or_data.get("high")
    try:
        return float(high) if high is not None else None
    except (TypeError, ValueError):
        return None


def _check_exp_orb_long(ind: dict) -> bool:
    """ORB Long (experimental, classic family).

    Fires when the current bar closes above today's 30-min opening range
    high (09:30–10:00 ET) with confirming volume. Ported in snapshot form
    from ``app/setups.py::detect_opening_range_breakout`` (the original
    DataFrame-iterator detector used by the deprecated CSV-upload route).
    """
    try:
        cp = float(ind.get("current_price") or 0)
        or_high = _exp_get_opening_range_high(ind)
        vr = float(ind.get("vol_ratio") or 0)
    except (TypeError, ValueError):
        return False
    if or_high is None or cp <= 0:
        return False
    if cp <= or_high:
        return False
    if vr < _EXP_OR_VOL_RATIO_MIN:
        return False
    return True


def _check_exp_vwap_reclaim_long(ind: dict) -> bool:
    """VWAP Reclaim Long (experimental, classic family).

    Fires when the prior bar closed BELOW vwap and the current bar
    closes ABOVE vwap (the reclaim) with confirming volume. The bar
    window is read off ``ind["recent_bars"]`` when the scanner threads
    it in; falls back to a no-fire when only one bar is available
    (no prior-bar reference → no reclaim signal).
    """
    try:
        cp = float(ind.get("current_price") or 0)
        vwap = float(ind.get("vwap") or 0)
        vr = float(ind.get("vol_ratio") or 0)
    except (TypeError, ValueError):
        return False
    if cp <= 0 or vwap <= 0 or vr < _EXP_VWAP_VOL_RATIO_MIN:
        return False
    if cp <= vwap:
        return False
    bars = ind.get("recent_bars") or []
    if not isinstance(bars, list) or len(bars) < 2:
        return False
    try:
        prev_close = float(bars[-2].get("close") or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    # Prior bar must have been BELOW vwap (the setup is a "reclaim",
    # not a continuation). When the bar feed lags a tick and prev_close
    # ≈ vwap (within 1 cent), treat as no-fire — same conservative
    # posture as the existing ICT predicates' rounding-tolerance.
    if prev_close >= vwap:
        return False
    return True


def _check_exp_ema_pullback_long(ind: dict) -> bool:
    """EMA Pullback Long (experimental, classic family).

    Fires when the symbol is in an uptrend (ema9 > ema20), a recent bar
    touched the ema20 within ±0.2% (the pullback), and the current bar
    closes back above ema9 (the bounce). Ported from
    ``app/setups.py::detect_ema_trend_pullback``.
    """
    try:
        cp = float(ind.get("current_price") or 0)
        ema9 = float(ind.get("ema9") or 0)
        ema20 = float(ind.get("ema20") or 0)
    except (TypeError, ValueError):
        return False
    if cp <= 0 or ema9 <= 0 or ema20 <= 0:
        return False
    if ema9 <= ema20:           # trend filter: must be uptrending
        return False
    if cp <= ema9:              # bounce confirmation
        return False
    bars = ind.get("recent_bars") or []
    if not isinstance(bars, list) or len(bars) < 5:
        return False
    # Pullback: some bar in the last 5 had a low within ±0.2% of ema20.
    band_lo = ema20 * (1.0 - _EXP_EMA_PULLBACK_PCT)
    band_hi = ema20 * (1.0 + _EXP_EMA_PULLBACK_PCT)
    for bar in bars[-5:]:
        try:
            low = float(bar.get("low") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if band_lo <= low <= band_hi:
            return True
    return False


def _check_exp_momentum_breakout_long(ind: dict) -> bool:
    """Momentum Breakout Long (experimental, momentum family).

    Catalyst-driven entry: positive gap of ≥ 1.5% on heavy volume,
    RSI is in the trending-not-overbought 50–70 band, and current
    price is within 0.5% of today's session high. Different from the
    ORB rule — this fires whenever the gap+vol confluence holds, not
    just on the 30-min opening-range breakout.
    """
    try:
        gap = float(ind.get("gap_pct") or 0)
        vr = float(ind.get("vol_ratio") or 0)
        rsi = float(ind.get("rsi") or 0)
        cp = float(ind.get("current_price") or 0)
        sess_high = float(ind.get("session_high") or 0)
    except (TypeError, ValueError):
        return False
    if cp <= 0 or sess_high <= 0:
        return False
    if gap < _EXP_MOMO_GAP_MIN:
        return False
    if vr < _EXP_MOMO_VOL_RATIO_MIN:
        return False
    if not (50.0 <= rsi <= 70.0):
        return False
    # Distance from session high — accept when price is within 0.5%
    # of it (i.e. either at the high or pulling back shallowly).
    if cp < sess_high * (1.0 - _EXP_MOMO_NEAR_HIGH_PCT):
        return False
    return True


def _check_exp_failed_breakdown_long(ind: dict) -> bool:
    """Failed Breakdown Long (experimental, momentum family).

    Fires when price broke BELOW yesterday's low within the last 10
    bars and the current bar closes back ≥ 0.3% ABOVE yesterday's low
    — the "bear trap" reversal. ``prev_low`` comes from ``daily_ctx``
    (threaded into ``ind`` upstream by ``compute_indicators``); when
    it's missing (pre-RTH, holiday) the predicate self-skips.
    """
    try:
        cp = float(ind.get("current_price") or 0)
        prev_low = float(ind.get("prev_low") or 0)
    except (TypeError, ValueError):
        return False
    if cp <= 0 or prev_low <= 0:
        return False
    # Rebound: current close must be ≥ 0.3% above prev_low.
    if cp < prev_low * (1.0 + _EXP_FB_REBOUND_PCT):
        return False
    bars = ind.get("recent_bars") or []
    if not isinstance(bars, list):
        return False
    window = bars[-_EXP_FB_LOOKBACK_BARS:] if bars else []
    if not window:
        return False
    # Look for ANY bar in the lookback window whose low pierced below
    # prev_low (the false breakdown).
    for bar in window:
        try:
            low = float(bar.get("low") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if low > 0 and low < prev_low:
            return True
    return False


# ── v7.15.1 (2026-05-19) — EXPERIMENTAL_PLAYBOOK suspended ──────────────────
#
# The v7.15 backtest (2025-11-18 → 2026-05-18, audited 23-symbol universe)
# rejected all 5 predicates above:
#
#     Strategy                 Trades    Win%    AvgR     Raw TotalR
#     ─────────────────────────────────────────────────────────────────
#     ORB Long                    903    34.2%   -0.229   -207.1 R
#     Momentum Breakout Long      762    30.8%   -0.385   -293.6 R
#     Failed Breakdown Long     1,361    33.9%   -0.175   -237.5 R
#     VWAP Reclaim Long         1,307    31.2%   -0.339   -443.3 R
#     EMA Pullback Long        36,376    31.8%   -0.307  -11,180.3 R
#     ─────────────────────────────────────────────────────────────────
#     Total                    40,709                    -12,361.8 R
#
# Net post-friction at 0.10R/trade: ~-16,433 R over 6 months. For
# comparison, the audited ICT scanner posts +42.7 R post-friction on
# the same window. The predicates fire too often AND have negative
# AvgR even before friction — no simple global gate (hour blacklist,
# symbol blacklist, threshold tightening) rescues the broad strategies.
#
# Caveat: narrow (strategy × symbol × hour) cells with positive AvgR
# in the v2 trade CSV may still inform new hypotheses. The one
# positive cell in the entire run was Failed Breakdown Long @ 15:00
# ET (44 trades, win 47.7%, AvgR +0.165, TotalR +7.28R). It's small
# and could be variance, but it's the kind of narrow cell that would
# graduate into its own predicate after a proper out-of-sample test.
#
# The predicate functions (``_check_exp_orb_long`` etc.) are kept
# above as reference — they may inform future tightened variants,
# and their per-predicate unit smoke is intact.
#
# v7.15.1 protocol for the next round of candidates:
#   1. Hypotheses come with a MECHANISTIC story (time / liquidity /
#      regime / confirmation), not just pattern matching.
#   2. Holdout window LOCKED: 2026-04-19 → 2026-05-18 is paper-
#      validate only. Design + walk-forward train on
#      2025-11-18 → 2026-04-18. Any candidate gets ONE shot at the
#      holdout; failing there → dead, don't iterate on it.
#   3. Hypothesis budget: 5–10 candidates max per sprint. Then
#      pause for live paper observation, regardless of backtest results.
#   4. Each candidate must clear: friction-AvgR ≥ +0.30 AND firing
#      rate 1–5 trades/day across universe AND positive across at
#      least 4 of 6 monthly buckets in the design window.
#   5. Promotion path unchanged: EXPERIMENTAL_PLAYBOOK → live paper
#      (kill switch, 0.25× risk, 1 trade/day cap) → 30 days clean →
#      candidate to graduate into PLAYBOOK with a full audit.
#
# When this list is non-empty again, the live scanner_experimental
# module will pick the new candidates up automatically (no other
# code change needed — scanner_id="experimental_v1", separate audit
# lane, EOD email subsection, all wired in v7.15).
EXPERIMENTAL_PLAYBOOK: list[Strategy] = []


def experimental_screen(indicators: dict) -> list[Strategy]:
    """Return every EXPERIMENTAL_PLAYBOOK strategy whose pre-screen
    matches the current indicators. Mirrors ``screen()`` / ``context_screen()``;
    the only difference is the playbook iterated."""
    return [s for s in EXPERIMENTAL_PLAYBOOK if s.matches(indicators)]
