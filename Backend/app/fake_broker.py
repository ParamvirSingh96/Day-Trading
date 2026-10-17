"""
fake_broker.py — in-process fake of the Alpaca endpoints TradeHack uses.
===========================================================================
Served through ``httpx.MockTransport`` so the real ``AlpacaTradingClient`` /
``AlpacaMarketDataClient`` code paths run unchanged against it. Two uses:

  * tests — every broker-boundary test (paper-only enforcement, order
    validation, error translation, demo workflow) talks to this backend and
    never touches the network;
  * ``DEMO_MODE=true`` — a clean checkout boots with synthetic minute bars and
    a $100k paper-style account so the dashboard can be explored without any
    broker credentials.

Behaviour is deliberately simple and deterministic: market orders fill
immediately at the synthetic price, limit/stop orders rest as ``accepted``,
positions are tracked in memory, and prices follow a seeded random walk so
runs are reproducible.
"""
from __future__ import annotations

import json
import math
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx

_DEFAULT_PRICES = {
    "AAPL": 230.0, "NVDA": 140.0, "AMD": 160.0, "TSLA": 250.0, "AMZN": 190.0,
    "MSFT": 420.0, "GOOGL": 170.0, "JPM": 210.0, "MS": 110.0, "INTC": 24.0,
    "SOFI": 9.0, "BAC": 41.0, "F": 11.0, "PLTR": 38.0, "SOXL": 35.0,
    "DKNG": 40.0, "XLF": 45.0, "XLE": 92.0, "ADBE": 520.0, "CRM": 270.0,
    "HD": 390.0, "UNH": 560.0, "XLV": 150.0, "SPY": 560.0, "QQQ": 480.0,
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeAlpacaBackend:
    """State + request handler. One instance can back both clients."""

    def __init__(self, seed: int = 7, starting_cash: float = 100_000.0) -> None:
        self._lock = threading.RLock()
        self._rng = random.Random(seed)
        self.cash = starting_cash
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []        # recorded for tests
        self._price_cache: dict[str, float] = dict(_DEFAULT_PRICES)
        # Optional fault injection for tests: ("POST", "/v2/orders") -> (status, body) | Exception
        self.faults: dict[tuple[str, str], Any] = {}

    # ── public helpers ─────────────────────────────────────────────────────
    def price(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol not in self._price_cache:
            self._price_cache[symbol] = 50.0 + (sum(map(ord, symbol)) % 400)
        return round(self._price_cache[symbol], 2)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def reset(self) -> None:
        with self._lock:
            self.orders.clear()
            self.positions.clear()
            self.requests.clear()
            self.faults.clear()
            self.cash = 100_000.0

    # ── request dispatch ───────────────────────────────────────────────────
    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            path = request.url.path
            method = request.method.upper()
            fault = self.faults.get((method, path))
            if fault is not None:
                if isinstance(fault, Exception):
                    raise fault
                status, body = fault
                return httpx.Response(status, json=body, request=request)
            try:
                return self._route(method, path, request)
            except KeyError as exc:
                return httpx.Response(404, json={"message": f"not found: {exc}"}, request=request)

    def _route(self, method: str, path: str, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        # ── market data ──
        if path.endswith("/stocks/bars") and method == "GET":
            return httpx.Response(200, json=self._bars(q), request=request)
        if path.endswith("/stocks/snapshots") and method == "GET":
            return httpx.Response(200, json=self._snapshots(q), request=request)
        if path.endswith("/v1beta1/news") and method == "GET":
            return httpx.Response(200, json={"news": [], "next_page_token": None}, request=request)
        # ── trading ──
        if path == "/v2/account" and method == "GET":
            return httpx.Response(200, json=self._account(), request=request)
        if path == "/v2/clock" and method == "GET":
            now = datetime.now(timezone.utc)
            return httpx.Response(200, json={"timestamp": _iso(now), "is_open": True,
                                             "next_open": _iso(now), "next_close": _iso(now)},
                                  request=request)
        if path == "/v2/calendar" and method == "GET":
            return httpx.Response(200, json=self._calendar(q), request=request)
        if path == "/v2/orders" and method == "POST":
            body = json.loads(request.content or b"{}")
            return self._submit(body, request)
        if path == "/v2/orders" and method == "GET":
            want = q.get("status", "open")
            rows = [o for o in self.orders.values()
                    if want == "all" or (want == "open") == (o["status"] in ("new", "accepted"))]
            return httpx.Response(200, json=rows, request=request)
        if path == "/v2/orders" and method == "DELETE":
            for o in self.orders.values():
                if o["status"] in ("new", "accepted"):
                    o["status"] = "canceled"
            return httpx.Response(207, json=[], request=request)
        if path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            o = self.orders[oid]
            if method == "GET":
                return httpx.Response(200, json=o, request=request)
            if method == "DELETE":
                if o["status"] not in ("new", "accepted"):
                    return httpx.Response(422, json={"message": "order is not cancelable"},
                                          request=request)
                o["status"] = "canceled"
                return httpx.Response(204, request=request)
        if path == "/v2/positions" and method == "GET":
            return httpx.Response(200, json=list(self.positions.values()), request=request)
        if path == "/v2/positions" and method == "DELETE":
            closed = [self._close(sym) for sym in list(self.positions)]
            return httpx.Response(207, json=closed, request=request)
        if path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[1].upper()
            if method == "GET":
                if sym not in self.positions:
                    return httpx.Response(404, json={"message": "position does not exist"},
                                          request=request)
                return httpx.Response(200, json=self.positions[sym], request=request)
            if method == "DELETE":
                if sym not in self.positions:
                    return httpx.Response(404, json={"message": "position does not exist"},
                                          request=request)
                return httpx.Response(200, json=self._close(sym), request=request)
        return httpx.Response(404, json={"message": f"unhandled {method} {path}"}, request=request)

    # ── market data generators ─────────────────────────────────────────────
    def _bars(self, q: dict[str, str]) -> dict[str, Any]:
        symbols = [s for s in q.get("symbols", "").split(",") if s]
        tf = q.get("timeframe", "1Min")
        minutes = {"1Min": 1, "5Min": 5, "15Min": 15, "1Day": 60 * 24}.get(tf, 1)
        end = datetime.fromisoformat(q["end"].replace("Z", "+00:00")) if q.get("end") \
            else datetime.now(timezone.utc)
        start = datetime.fromisoformat(q["start"].replace("Z", "+00:00")) if q.get("start") \
            else end - timedelta(hours=8)
        n = max(2, min(int((end - start).total_seconds() // (60 * minutes)), 3000))
        out: dict[str, list[dict[str, Any]]] = {}
        for sym in symbols:
            rng = random.Random(f"{sym}:{start.date()}:{tf}")
            px = self.price(sym) * (1 - 0.01)
            bars = []
            t = start
            for _ in range(n):
                drift = rng.gauss(0, 0.0008) * px
                o = px
                c = max(1.0, px + drift)
                h = max(o, c) + abs(rng.gauss(0, 0.0004)) * px
                l = min(o, c) - abs(rng.gauss(0, 0.0004)) * px
                v = int(abs(rng.gauss(20000, 8000)) + 500)
                bars.append({"t": _iso(t), "o": round(o, 2), "h": round(h, 2),
                             "l": round(l, 2), "c": round(c, 2), "v": v, "n": v // 10,
                             "vw": round((h + l + c) / 3, 2)})
                px = c
                t += timedelta(minutes=minutes)
            self._price_cache[sym.upper()] = px
            out[sym] = bars
        return {"bars": out, "next_page_token": None}

    def _snapshots(self, q: dict[str, str]) -> dict[str, Any]:
        symbols = [s for s in q.get("symbols", "").split(",") if s]
        now = datetime.now(timezone.utc)
        snaps: dict[str, Any] = {}
        for sym in symbols:
            p = self.price(sym)
            bar = {"t": _iso(now), "o": p, "h": round(p * 1.002, 2), "l": round(p * 0.998, 2),
                   "c": p, "v": 12000, "n": 900, "vw": p}
            snaps[sym.upper()] = {
                "latestTrade": {"t": _iso(now), "p": p, "s": 100},
                "latestQuote": {"t": _iso(now), "bp": round(p - 0.01, 2), "ap": round(p + 0.01, 2),
                                "bs": 5, "as": 5},
                "minuteBar": bar,
                "dailyBar": {**bar, "o": round(p * 0.99, 2), "v": 2_000_000},
                "prevDailyBar": {**bar, "o": round(p * 0.985, 2), "c": round(p * 0.99, 2),
                                 "v": 2_100_000},
            }
        return snaps

    def _calendar(self, q: dict[str, str]) -> list[dict[str, str]]:
        start = datetime.fromisoformat(q.get("start", datetime.now(timezone.utc).date().isoformat()))
        end = datetime.fromisoformat(q.get("end", start.date().isoformat()))
        rows, d = [], start.date()
        while d <= end.date():
            if d.weekday() < 5:
                rows.append({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
            d += timedelta(days=1)
        return rows

    # ── trading state ──────────────────────────────────────────────────────
    def _account(self) -> dict[str, Any]:
        pos_value = sum(float(p["market_value"]) for p in self.positions.values())
        equity = self.cash + pos_value
        return {
            "id": "demo-account", "account_number": "DEMO000001", "status": "ACTIVE",
            "currency": "USD", "cash": f"{self.cash:.2f}", "buying_power": f"{self.cash:.2f}",
            "non_marginable_buying_power": f"{self.cash:.2f}", "portfolio_value": f"{equity:.2f}",
            "equity": f"{equity:.2f}", "last_equity": f"{equity:.2f}", "multiplier": "1",
            "pattern_day_trader": False, "daytrade_count": 0, "trading_blocked": False,
            "account_blocked": False, "shorting_enabled": True,
        }

    def _submit(self, body: dict[str, Any], request: httpx.Request) -> httpx.Response:
        symbol = str(body.get("symbol", "")).upper()
        side = body.get("side")
        otype = body.get("type", "market")
        try:
            qty = float(body.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0.0
        if not symbol or side not in ("buy", "sell") or qty <= 0:
            return httpx.Response(422, json={"code": 40010001, "message": "invalid order"},
                                  request=request)
        now = datetime.now(timezone.utc)
        oid = str(uuid.uuid4())
        order = {
            "id": oid, "client_order_id": oid[:8], "symbol": symbol, "side": side,
            "type": otype, "order_type": otype, "qty": str(int(qty)), "filled_qty": "0",
            "filled_avg_price": None, "status": "accepted", "time_in_force": body.get("time_in_force", "day"),
            "limit_price": body.get("limit_price"), "stop_price": body.get("stop_price"),
            "order_class": body.get("order_class", "simple"), "legs": None,
            "submitted_at": _iso(now), "created_at": _iso(now), "updated_at": _iso(now),
            "filled_at": None, "extended_hours": bool(body.get("extended_hours", False)),
        }
        if otype == "market":
            px = self.price(symbol)
            cost = px * qty
            if side == "buy" and cost > self.cash:
                return httpx.Response(403, json={"code": 40310000, "message": "insufficient buying power"},
                                      request=request)
            self._apply_fill(symbol, side, qty, px)
            order.update({"status": "filled", "filled_qty": str(int(qty)),
                          "filled_avg_price": f"{px:.2f}", "filled_at": _iso(now)})
        self.orders[oid] = order
        return httpx.Response(200, json=order, request=request)

    def _apply_fill(self, symbol: str, side: str, qty: float, px: float) -> None:
        pos = self.positions.get(symbol)
        signed = qty if side == "buy" else -qty
        self.cash -= signed * px
        if pos is None:
            self.positions[symbol] = self._position(symbol, signed, px)
            return
        cur = float(pos["qty"])
        new_qty = cur + signed
        if abs(new_qty) < 1e-9:
            del self.positions[symbol]
            return
        avg = float(pos["avg_entry_price"])
        if (cur > 0) == (signed > 0):
            avg = (avg * abs(cur) + px * abs(signed)) / (abs(cur) + abs(signed))
        self.positions[symbol] = self._position(symbol, new_qty, avg)

    def _position(self, symbol: str, qty: float, avg: float) -> dict[str, Any]:
        px = self.price(symbol)
        mv = px * qty
        pl = (px - avg) * qty
        return {
            "symbol": symbol, "qty": str(int(qty)), "avg_entry_price": f"{avg:.2f}",
            "current_price": f"{px:.2f}", "market_value": f"{mv:.2f}",
            "cost_basis": f"{avg * qty:.2f}", "unrealized_pl": f"{pl:.2f}",
            "unrealized_plpc": f"{(pl / (avg * abs(qty))) if avg and qty else 0:.4f}",
            "side": "long" if qty > 0 else "short", "asset_class": "us_equity", "exchange": "NASDAQ",
        }

    def _close(self, symbol: str) -> dict[str, Any]:
        pos = self.positions[symbol]
        qty = float(pos["qty"])
        side = "sell" if qty > 0 else "buy"
        px = self.price(symbol)
        self._apply_fill(symbol, side, abs(qty), px)
        now = datetime.now(timezone.utc)
        oid = str(uuid.uuid4())
        order = {"id": oid, "client_order_id": oid[:8], "symbol": symbol, "side": side,
                 "type": "market", "order_type": "market", "qty": str(int(abs(qty))),
                 "filled_qty": str(int(abs(qty))), "filled_avg_price": f"{px:.2f}",
                 "status": "filled", "time_in_force": "day", "limit_price": None,
                 "stop_price": None, "order_class": "simple", "legs": None,
                 "submitted_at": _iso(now), "created_at": _iso(now), "updated_at": _iso(now),
                 "filled_at": _iso(now), "extended_hours": False}
        self.orders[oid] = order
        return order


_SHARED: FakeAlpacaBackend | None = None
_SHARED_LOCK = threading.Lock()


def shared_backend() -> FakeAlpacaBackend:
    """Process-wide backend used by DEMO_MODE so both clients and the
    autotrader see one consistent account."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = FakeAlpacaBackend()
        return _SHARED


def reset_shared_backend() -> None:
    global _SHARED
    with _SHARED_LOCK:
        _SHARED = None
