"""Regression for the 30%-of-equity per-position notional cap (2026-06-15).

Proves:
  • an oversized entry (tight stop → huge risk-based qty, the XLF case) is
    capped so its notional ≤ max_position_equity_pct of equity,
  • a risk-limited small position is NOT inflated up to the cap,
  • leaves room for ~3 concurrent positions.
Mocks the Alpaca account; places no orders, touches no DB.
"""
from __future__ import annotations

import asyncio

from app import autotrade


class FakeClient:
    def __init__(self, equity=99_000.0):
        self.equity = equity

    def get_account(self):
        return {
            "portfolio_value": str(self.equity), "equity": str(self.equity),
            "buying_power": str(self.equity * 4),      # 4x margin (paper)
            "cash": str(self.equity),
            "non_marginable_buying_power": str(self.equity),
        }


async def run() -> None:
    autotrade.get_shared_trading_client = lambda: FakeClient()
    autotrade.trading_mode.is_paper = lambda: True
    EQUITY = 99_000.0
    cap_pct = autotrade.state.max_position_equity_pct
    assert cap_pct == 0.30, cap_pct

    # 1. XLF-style: $53.50, tight 0.7% stop → risk-based qty is huge; must cap
    #    to ≤ 30% of equity (≈ 555 sh), NOT ~95% of funds (≈ 1760).
    price = 53.50
    stop = price * (1 - 0.007)  # long 0.7% stop → tiny stop distance
    r = await autotrade._compute_position_qty(price, stop_loss_price=stop, confidence=0.85)
    notional = r["qty"] * price
    cap_dollars = EQUITY * cap_pct
    assert notional <= cap_dollars + price, f"notional {notional:.0f} > cap {cap_dollars:.0f}"
    assert r["qty"] <= 556, f"expected ~555, got {r['qty']}"   # vs ~1760 pre-cap
    assert r["qty"] >= 540
    # Three of these fit inside equity with headroom (the whole point).
    assert 3 * notional <= EQUITY, f"3x {notional:.0f} should fit in {EQUITY:.0f}"
    print(f"  oversized entry capped: qty={r['qty']} notional=${notional:,.0f} "
          f"(≤ 30% = ${cap_dollars:,.0f}); pre-cap would have been ~1760  ✓")

    # 2. Risk-limited small position (wide stop) must NOT be inflated to the cap.
    price2 = 100.0
    stop2 = 80.0  # $20 stop distance → small risk-based qty
    r2 = await autotrade._compute_position_qty(price2, stop_loss_price=stop2, confidence=0.80)
    # risk_budget = 99000*0.05*1.0 = 4950; qty = floor(4950/20) = 247; cap = 297.
    assert r2["qty"] == 247, f"expected 247 (risk-limited, uncapped), got {r2['qty']}"
    assert r2["qty"] < int(EQUITY * cap_pct / price2)  # below the cap → untouched
    print(f"  risk-limited small position untouched: qty={r2['qty']} (cap would allow "
          f"{int(EQUITY*cap_pct/price2)})  ✓")

    print("All position-cap checks passed.")


if __name__ == "__main__":
    asyncio.run(run())
