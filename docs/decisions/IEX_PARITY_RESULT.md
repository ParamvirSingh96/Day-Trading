# IEX Parity Test — RESULT: FAIL (do not use IEX for the predicate scanner)

**TL;DR:** IEX failed parity against SIP on 2026-05-01.
**9 of 126 v7.12.1 trades** had a clean match. **Do not use IEX as the
data source for the live scanner, the backtest, or any signal-generation
path.** Keep `ALPACA_DATA_FEED=sip` in `Backend/.env`.

---

## Date and window

| Field | Value |
|---|---|
| Test date | 2026-05-01 |
| Window | 2025-10-27 → 2026-04-24 (the v7.12.1 audit window) |
| Universe | 23 symbols (the v7.12.1 audited set) |
| Script | `Backend/scripts/run_iex_parity_test.py` |
| Threshold | ≥ 95% clean trade match (per the v7.13a plan) |

## The numbers

| Metric | Value |
|---|---:|
| SIP trades | **126** ← matches the v7.12.1 baseline byte-for-byte (regression check passed) |
| IEX trades | **257** |
| Matched on `(symbol, entry_ts, direction, strategy)` | 10 |
| ↳ Clean (\|ΔR\| ≤ 0.05) | **9** |
| ↳ Diverged (\|ΔR\| > 0.20) | 1 |
| **SIP-only** (IEX missed the setup) | **116** |
| **IEX-only** (IEX hallucinated a setup) | **247** |
| **Match rate** | **7.14%** (threshold 95%) |
| Total bars (SIP) | 1,679,563 |
| Total bars (IEX) | 1,055,241 |
| IEX/SIP bar ratio | **0.628** (IEX missing ~37% of bars) |
| Days where IEX returned 0 bars but SIP > 0 | 68 |

## Why it failed

The ICT predicate family reads wick prices to decide whether a level was
swept, whether structure broke, and whether an FVG was retested. IEX is
one exchange's view of the tape; SIP is the consolidated tape across
every US exchange. With ~37% of bars missing on IEX, intermediate price
action that *prevents* a sweep on SIP simply isn't visible — so the same
high-water-mark wick that didn't actually sweep on SIP looks like a
clean sweep on IEX.

This shows up most clearly on **Multi-Day PD (Long)**, which fires on
prior-day-low touches: 32 trades on SIP, **137 on IEX (4.3×)**. Turtle
Soup (both directions) was 5–7× more active on IEX for the same reason.
The hallucination pattern dominates in the per-strategy breakdown:

| Strategy | SIP | IEX | IEX/SIP |
|---|---:|---:|---:|
| ICT Multi-Day PD (Long) | 32 | 137 | 4.281 |
| ICT Turtle Soup (Long) | 6 | 31 | 5.167 |
| ICT Turtle Soup (Short) | 3 | 21 | 7.000 |
| ICT Power of 3 (Short) | 17 | 27 | 1.588 |
| ICT Power of 3 (Long) | 58 | 34 | 0.586 |
| ICT Judas Swing (Long) | 6 | 5 | 0.833 |
| ICT Judas Swing (Short) | 4 | 2 | 0.500 |

Per-symbol bar ratios also matched intuition: high-volume mega-caps that
trade heavily on NYSE/NASDAQ have the biggest IEX gap (NVDA 0.490, SOXL
0.490, TSLA 0.491, INTC 0.522, PLTR 0.532), while sector ETFs and
lower-vol names route a higher fraction through IEX (XLV 0.916, DKNG
0.850, BAC 0.833). A "use IEX only for closer-parity symbols" hybrid was
considered and rejected — it would create a mixed-feed system that
poisons the audit trail without changing the dollar economics.

## Decision

**Stay on `ALPACA_DATA_FEED=sip` indefinitely** for the predicate
scanner, the backtest, and any signal-generation path. The ~$99/month
SIP subscription buys ~+$1,644/week of expected post-friction edge at
$100k / 1% risk per the v7.12.1 baseline — the ROI on the SIP cost is
roughly 70× per month. SIP is now a **known infrastructure cost**, not
an optimization target.

## What's preserved as a reusable harness

- `Backend/scripts/run_iex_parity_test.py` — the parity test itself.
  Drop in a new `feed=` value to compare any future Alpaca feed (or
  fork the bar-pull layer to compare Polygon, Databento, etc.).
- `Backend/data/research/iex_parity_2026-05-01.txt` — the full report.
- `Backend/data/research/iex_parity_2026-05-01_diff.csv` — per-trade
  diff. Bucket column distinguishes `matched` / `sip_only` / `iex_only`.
- Per-feed cache directories `Backend/data/bars_cache_ext_{sip,iex}/`
  — the IEX cache is harmless to keep; delete only if disk pressure
  becomes a concern (currently ~22 MB).
- Env-driven feed selector (`ALPACA_DATA_FEED` in `.env`,
  `settings.alpaca_data_feed`, `AlpacaBarsRequest.feed`,
  `run_backtest(..., feed=...)`) — already wired and validated.

## How to re-validate against a future feed (Polygon, etc.)

The current parity test is Alpaca-feed-specific (it sets `feed="iex"`
on the Alpaca API). To run against Polygon or another vendor:

1. Add a Polygon (or other) bar adapter behind the same shape as
   `AlpacaMarketDataClient.fetch_stock_bars`.
2. In `Backend/app/strategy_backtest.py::fetch_history`, swap which
   client gets called when a hypothetical
   `MARKET_DATA_PROVIDER=polygon` env is set.
3. Re-run `Backend/scripts/run_iex_parity_test.py` against the new
   provider — the bar-level + trade-level diff logic is provider-
   agnostic (it operates on the in-memory `dict[symbol, dict[date,
   list[bar]]]` shape that all our adapters return).

A second run targeting Polygon makes sense if/when SIP cost becomes a
real friction (e.g. running 5+ accounts or scaling capital meaningfully
beyond $50k–$100k). Until then, SIP is correct.

## What this result reinforces (the bigger point)

Same theme as the 2026-05-01 AI-layer replay (9.9% direction agreement
between Sonnet and the predicate scanner): **the validated Layer-1
scanner is the core asset**. Cheaper or weaker upstream/downstream
layers (a thinner data feed; an AI gate that vetoes 90% of predicate
matches) distort the audited edge in ways that matter at trade-set
granularity, even when the aggregate-level signals look reasonable.
The +42.72 R / 6m post-friction baseline is a property of the *whole*
system as audited — feed + universe + predicates + execution rules —
and substituting any one component requires this kind of trade-level
parity check before trusting it.
