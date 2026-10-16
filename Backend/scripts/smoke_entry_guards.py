"""Regression for the entry risk-control guards (2026-06-15 XLF incident).

Proves the two pure-restrictive guards would have prevented the four XLF shorts
fired at 15:54-15:55:
  • Guard 2 — no new entries inside the EOD close window.
  • Guard 1 — same-symbol cooldown reserves a symbol the instant it's submitted
    and blocks cross-cycle re-fires for the cooldown window.
Pure logic; no Alpaca, no DB.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app import autotrade

ET = ZoneInfo("America/New_York")


def main() -> None:
    eb = autotrade._entry_blocked_eod
    # Guard 2 — eod_buffer=15 → entry cutoff 15:45 ET (Mon 2026-06-15).
    assert eb(datetime(2026, 6, 15, 15, 30, tzinfo=ET), 15) is False   # 15:30 allowed
    assert eb(datetime(2026, 6, 15, 15, 44, tzinfo=ET), 15) is False   # just before cutoff
    assert eb(datetime(2026, 6, 15, 15, 45, tzinfo=ET), 15) is True    # at cutoff
    assert eb(datetime(2026, 6, 15, 15, 54, tzinfo=ET), 15) is True    # YESTERDAY'S XLF → blocked
    assert eb(datetime(2026, 6, 15, 9, 40, tzinfo=ET), 15) is False    # morning entry allowed
    assert eb(datetime(2026, 6, 13, 15, 54, tzinfo=ET), 15) is False   # Saturday → weekend guard
    assert eb(datetime(2026, 6, 15, 15, 31, tzinfo=ET), 30) is True    # eod_buffer=30 → cutoff 15:30
    print("  Guard 2 (EOD entry cutoff): 15:54 entries blocked, mornings allowed  ✓")

    # Guard 1 — same-symbol cooldown (15 min, mirrors backtest COOLDOWN_BARS).
    autotrade._recent_entry_ts.clear()
    soc = autotrade._symbol_on_cooldown
    now = 1_000_000.0
    assert soc("XLF", now) is False                       # never traded → free
    autotrade._recent_entry_ts["XLF"] = now               # reserve at submit instant
    assert soc("XLF", now + 60) is True                   # +1 min → blocked
    assert soc("XLF", now + 14 * 60) is True              # +14 min → still blocked
    assert soc("XLF", now + 16 * 60) is False             # +16 min → expired
    assert soc("NVDA", now + 60) is False                 # other symbols unaffected
    print("  Guard 1 (same-symbol cooldown): reserves XLF 15m after submit  ✓")

    # XLF×4 reproduction — the candidate filter must let only the FIRST entry
    # through; the 15-21s-apart re-fires (yesterday's cadence) are all blocked.
    autotrade._recent_entry_ts.clear()
    hits = [{"symbol": "XLF"}]
    blocked: set = set()

    def filter_candidates(now_ts: float):
        return [h for h in hits
                if h["symbol"] not in blocked
                and not autotrade._symbol_on_cooldown(h["symbol"], now_ts)]

    t0 = 2_000_000.0
    assert [h["symbol"] for h in filter_candidates(t0)] == ["XLF"]  # cycle 1 fires
    autotrade._recent_entry_ts["XLF"] = t0                          # reserve on submit
    for dt in (15, 21, 30, 72):                                     # subsequent cycles
        assert filter_candidates(t0 + dt) == [], f"re-fire at +{dt}s not blocked"
    print("  XLF×4 scenario: only the first entry passes; all re-fires blocked  ✓")

    print("All entry-guard checks passed.")


if __name__ == "__main__":
    main()
