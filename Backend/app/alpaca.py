from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import httpx

from . import trading_mode
from .settings import settings

logger = logging.getLogger("tradehack.alpaca")

# ── Rate-limit / retry settings ─────────────────────────────────────────────

_MAX_RETRIES = 4           # up to 4 retries (5 total attempts)
_BASE_BACKOFF = 0.5        # first retry waits 0.5 s

# ── Snapshot cache ─────────────────────────────────────────────────────────
# fetch_snapshots gets hammered inside a scan cycle (pre-screen, sizing,
# slippage guard, monitor, etc). A short TTL cache cuts API calls roughly
# 5× at peak without hurting freshness — 30 s is well under the autotrader's
# 1-min bar cadence.
_SNAPSHOT_TTL_SECONDS = 30.0
_snapshot_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_snapshot_cache_lock = threading.Lock()
_snapshot_cache_hits = 0
_snapshot_cache_misses = 0


def _cache_get_snapshots(symbols: list[str], feed: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return (cached_results, missing_symbols) from the snapshot cache."""
    global _snapshot_cache_hits, _snapshot_cache_misses
    now = time.monotonic()
    cached: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    with _snapshot_cache_lock:
        for sym in symbols:
            key = (sym.upper(), feed)
            entry = _snapshot_cache.get(key)
            if entry and (now - entry[0]) < _SNAPSHOT_TTL_SECONDS:
                cached[sym.upper()] = entry[1]
                _snapshot_cache_hits += 1
            else:
                missing.append(sym)
                _snapshot_cache_misses += 1
    return cached, missing


def _cache_put_snapshots(results: dict[str, dict[str, Any]], feed: str) -> None:
    """Store fresh snapshot results into the cache."""
    now = time.monotonic()
    with _snapshot_cache_lock:
        for sym, snap in results.items():
            _snapshot_cache[(sym.upper(), feed)] = (now, snap)
        # Bound cache size; keep most-recent entries.
        if len(_snapshot_cache) > 2000:
            # Drop the oldest 25% in one pass.
            by_age = sorted(_snapshot_cache.items(), key=lambda kv: kv[1][0])
            for key, _ in by_age[: len(by_age) // 4]:
                _snapshot_cache.pop(key, None)


def get_snapshot_cache_stats() -> dict[str, int]:
    """Expose hit/miss counts for diagnostics."""
    with _snapshot_cache_lock:
        return {
            "hits": _snapshot_cache_hits,
            "misses": _snapshot_cache_misses,
            "size": len(_snapshot_cache),
            "ttl_seconds": int(_SNAPSHOT_TTL_SECONDS),
        }
_MAX_BACKOFF = 16.0        # cap exponential backoff at 16 s
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}


def _retry_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP request with exponential-backoff retry on 429 / 5xx.

    Respects the ``Retry-After`` header when present (Alpaca sends this on
    429 responses).  Falls back to exponential backoff: 0.5 → 1 → 2 → 4 s
    (capped at ``_MAX_BACKOFF``).

    Non-retryable errors (4xx other than 429) are raised immediately.

    MUTATIONS ARE NEVER AUTO-RETRIED (2026-07-03): a POST /v2/orders or
    DELETE /v2/positions/... that timed out or drew a 5xx may STILL have been
    accepted broker-side; blindly resending it can place a duplicate order or
    double-close a position — the same failure class as the 2026-06-15 XLF
    duplicate incident, at a layer the app-level cooldown guard cannot see.
    Non-idempotent methods get exactly one attempt; recovery belongs to the
    caller's failure boundaries (e.g. autotrade's close_failed → next-cycle
    re-check of broker state before any resubmit).
    """
    if method.upper() not in _IDEMPOTENT_METHODS:
        max_retries = 0
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.request(method, url, **kwargs)

            if response.status_code not in _RETRYABLE_STATUS:
                return response

            # Retryable status — compute wait time
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(float(retry_after), _MAX_BACKOFF)
                except ValueError:
                    wait = min(_BASE_BACKOFF * (2 ** attempt), _MAX_BACKOFF)
            else:
                wait = min(_BASE_BACKOFF * (2 ** attempt), _MAX_BACKOFF)

            if attempt < max_retries:
                logger.warning(
                    "alpaca_rate_limit status=%d attempt=%d/%d wait=%.1fs url=%s",
                    response.status_code, attempt + 1, max_retries + 1, wait, url,
                )
                time.sleep(wait)
            else:
                # Exhausted retries — return the last response so caller can
                # raise_for_status() with the real status code
                logger.error(
                    "alpaca_retries_exhausted status=%d attempts=%d url=%s",
                    response.status_code, max_retries + 1, url,
                )
                return response

        except httpx.RequestError as exc:
            last_exc = exc
            wait = min(_BASE_BACKOFF * (2 ** attempt), _MAX_BACKOFF)
            if attempt < max_retries:
                logger.warning(
                    "alpaca_request_error attempt=%d/%d wait=%.1fs error=%s url=%s",
                    attempt + 1, max_retries + 1, wait, exc, url,
                )
                time.sleep(wait)
            else:
                logger.error("alpaca_request_failed attempts=%d url=%s error=%s", max_retries + 1, url, exc)
                raise

    # Should never reach here, but just in case
    if last_exc:
        raise last_exc
    raise RuntimeError("Unexpected retry loop exit")


def convert_alpaca_bars(raw_bars: list[dict]) -> list[dict]:
    """Convert Alpaca bar dicts to the internal format used by indicators/analyzer.

    Each returned dict has keys: time (unix seconds), open, high, low, close, volume.
    """
    return [
        {
            "time":   int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()),
            "open":   float(b["o"]),
            "high":   float(b["h"]),
            "low":    float(b["l"]),
            "close":  float(b["c"]),
            "volume": float(b["v"]),
        }
        for b in raw_bars
    ]


# ── Trading enums ─────────────────────────────────────────────────────────────

class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT  = "limit"
    STOP   = "stop"
    STOP_LIMIT = "stop_limit"

class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


def _default_data_feed() -> str:
    """Resolve the active Alpaca bars feed.

    Reads ``settings.alpaca_data_feed`` (env-driven, see settings.py).
    Defined as a module-level helper rather than inlined in the
    dataclass default so the resolution happens lazily — important
    because settings is imported at module load time and we want the
    value cached at request-construction time, not import time.
    """
    return settings.alpaca_data_feed or "sip"


@dataclass(frozen=True)
class AlpacaBarsRequest:
    symbols: list[str]
    timeframe: str
    start: datetime
    end: datetime
    adjustment: str = "split"
    # v7.13a (2026-05-01): default sourced from settings.alpaca_data_feed
    # (env-driven via ALPACA_DATA_FEED in .env). Callers can still
    # override per-request — the parity test passes feed= explicitly
    # to compare both. Frozen-dataclass + mutable-default would be a
    # bug; ``field(default_factory=...)`` keeps it immutable AND
    # responsive to env changes between tests.
    feed: str = field(default_factory=_default_data_feed)
    limit: int = 10_000


class AlpacaMarketDataClient:
    def __init__(self) -> None:
        # Market-data keys follow the active trading mode so a user
        # with different data subscriptions per account (paper free-tier
        # vs. live Algo+) gets the feed that matches their current
        # selection. Falls back to legacy ALPACA_API_KEY via
        # ``trading_mode.get_credentials``.
        api_key, secret_key, _ = trading_mode.get_credentials()
        if not api_key or not secret_key:
            active_mode = trading_mode.get_mode()
            raise ValueError(
                f"Missing Alpaca credentials for {active_mode!r} mode. Set "
                f"ALPACA_{active_mode.upper()}_API_KEY + "
                f"ALPACA_{active_mode.upper()}_SECRET_KEY in .env."
            )

        self._client = httpx.Client(
            base_url=settings.alpaca_market_data_url,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=30.0,
        )

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP request with automatic retry on 429 / 5xx."""
        return _retry_request(self._client, method, url, **kwargs)

    def fetch_news(
        self,
        symbols: list[str],
        limit: int = 10,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent news articles for the given symbols.

        Uses the Alpaca News API (v1beta1).  Returns a list of article dicts
        with keys: id, headline, summary, source, created_at, symbols, url.
        """
        params: dict[str, str] = {
            "symbols": ",".join(symbols),
            "limit": str(min(limit, 50)),
            "sort": "desc",
            "include_content": "false",
        }
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        # News endpoint is on v1beta1, not v2
        url = self._client.base_url.copy_with(path="/v1beta1/news")
        r = self._request("GET", str(url), params=params)
        r.raise_for_status()
        return r.json().get("news", [])

    def validate_credentials(self) -> None:
        """Make a minimal request to confirm the API key and secret are accepted.

        Uses a single-bar SPY request as a lightweight probe.  Raises ValueError
        with a human-readable message on auth failure so callers can surface it
        directly to the user rather than letting it surface as an opaque 403 mid-ingestion.
        """
        probe_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        probe_start = probe_end - timedelta(minutes=10)
        try:
            response = self._request(
                "GET", "/stocks/bars",
                params={
                    "symbols": "SPY",
                    "timeframe": "1Min",
                    "start": probe_start.isoformat(),
                    "end": probe_end.isoformat(),
                    "limit": "1",
                    "feed": "iex",
                },
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Could not reach Alpaca API: {exc}") from exc

        if response.status_code in (401, 403):
            logger.warning("alpaca_auth_failed status=%d", response.status_code)
            raise ValueError(
                "Alpaca API credentials were rejected (HTTP %d). "
                "Check ALPACA_API_KEY and ALPACA_SECRET_KEY." % response.status_code
            )
        response.raise_for_status()
        logger.debug("alpaca_credentials_valid")

    def fetch_snapshots(self, symbols: list[str], feed: str = "sip") -> dict[str, dict[str, Any]]:
        """Fetch latest snapshots for multiple symbols.

        Each snapshot includes: latest trade, latest quote (bid/ask), minute bar, daily bar.
        Returns {symbol: snapshot_dict}. Served from a 30s in-process cache when
        possible; only the missing symbols hit the wire.
        """
        if not symbols:
            return {}

        cached, missing = _cache_get_snapshots(symbols, feed)

        if not missing:
            return cached

        fresh: dict[str, dict[str, Any]] = {}
        # Alpaca limits to ~200 symbols per request
        for i in range(0, len(missing), 200):
            chunk = missing[i:i + 200]
            r = self._request("GET", "/stocks/snapshots", params={
                "symbols": ",".join(chunk),
                "feed": feed,
            })
            r.raise_for_status()
            fresh.update(r.json())

        if fresh:
            _cache_put_snapshots(fresh, feed)

        # Return callers' requested casing alongside cached (upper) keys —
        # merge so both the fresh and cached maps are visible. Upstream code
        # already normalizes with .upper() before lookup.
        return {**cached, **fresh}

    def fetch_stock_bars(self, request: AlpacaBarsRequest) -> dict[str, list[dict[str, Any]]]:
        bars_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in request.symbols}
        next_page_token: str | None = None

        while True:
            params = {
                "symbols": ",".join(request.symbols),
                "timeframe": request.timeframe,
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "adjustment": request.adjustment,
                "feed": request.feed,
                "limit": str(request.limit),
            }
            if next_page_token:
                params["page_token"] = next_page_token

            response = self._request("GET", "/stocks/bars", params=params)
            response.raise_for_status()
            payload = response.json()

            for symbol, symbol_bars in payload.get("bars", {}).items():
                bars_by_symbol.setdefault(symbol, []).extend(symbol_bars)

            next_page_token = payload.get("next_page_token")
            if not next_page_token:
                break

        return bars_by_symbol


# ── Trading Client (paper & live) ─────────────────────────────────────────────

_PAPER_HOST = "paper-api.alpaca.markets"
_LIVE_OPT_IN_ENV = "LIVE_TRADING_ENABLED"
_LIVE_OPT_IN_VALUE = "yes"


def _is_live_mode() -> bool:
    """Return True only if the active trading mode is LIVE.

    Sourced from the ``trading_mode`` singleton (runtime-mutable via
    the ``/trading/mode`` endpoint) rather than reading env directly,
    so a UI flip is reflected on the next client construction without
    a process restart.
    """
    return trading_mode.is_live()


def _live_trading_opted_in() -> bool:
    return os.getenv(_LIVE_OPT_IN_ENV, "").strip().lower() == _LIVE_OPT_IN_VALUE


# Guard so the scary "LIVE — real money is at stake" banner is only logged once
# per process, no matter how many AlpacaTradingClient instances get created.
_LIVE_BANNER_LOGGED = False


class AlpacaTradingClient:
    """Thin wrapper around Alpaca's Trading API v2.

    Defaults to paper trading (https://paper-api.alpaca.markets).
    Uses the same API key/secret as the market-data client.

    Live-trading guard (belt-and-suspenders):
      - ALPACA_PAPER=false alone is NOT enough to place real-money orders.
      - The env var LIVE_TRADING_ENABLED=yes must ALSO be set, OR the
        trading URL must be the paper host, for the client to initialize.
      - submit_order() re-checks the opt-in on every call.
    """

    def __init__(self) -> None:
        # Route through the ``trading_mode`` singleton so runtime mode
        # flips (``/trading/mode``) are reflected on the very next
        # client construction. Falls back to the legacy ``ALPACA_API_KEY``
        # / ``ALPACA_SECRET_KEY`` pair when only that single-mode config
        # is present — see ``trading_mode._credentials_for``.
        api_key, secret_key, base_url = trading_mode.get_credentials()
        active_mode = trading_mode.get_mode()
        if not api_key or not secret_key:
            raise ValueError(
                f"Missing Alpaca credentials for {active_mode!r} mode. Set "
                f"ALPACA_{active_mode.upper()}_API_KEY + "
                f"ALPACA_{active_mode.upper()}_SECRET_KEY in .env (or use the "
                "legacy ALPACA_API_KEY / ALPACA_SECRET_KEY if this is the "
                "only mode you use)."
            )

        # Refuse to construct a live-trading client unless the user has
        # explicitly opted in via env var. Belt-and-suspenders: even when
        # the UI flips to live, the opt-in must be set so an accidental
        # flip of the persistence file can't execute real orders on its
        # own.
        if _is_live_mode() and not _live_trading_opted_in():
            raise RuntimeError(
                "LIVE trading mode is active but "
                f"{_LIVE_OPT_IN_ENV}={_LIVE_OPT_IN_VALUE!r} is not set. "
                "Refusing to initialize trading client. Set "
                f"{_LIVE_OPT_IN_ENV}={_LIVE_OPT_IN_VALUE} in .env to confirm "
                "you intend to trade real money, or switch back to paper "
                "via the UI toggle."
            )

        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID":     api_key,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=15.0,
        )
        # Cache the base_url on the instance so the trade stream can
        # derive its WebSocket URL from the same credentials the client
        # was built against — avoids subtle drift if ``trading_mode``
        # flipped between __init__ and stream construction.
        self._base_url = base_url
        self._mode = active_mode
        global _LIVE_BANNER_LOGGED
        if _is_live_mode() and not _LIVE_BANNER_LOGGED:
            logger.warning(
                "LIVE_TRADING_CLIENT_INITIALIZED url=%s — real money is at stake.",
                base_url,
            )
            _LIVE_BANNER_LOGGED = True

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP request with automatic retry on 429 / 5xx."""
        return _retry_request(self._client, method, url, **kwargs)

    # v7.16 (2026-05-13): trading-day calendar fetch. Used by
    # ``app/trading_calendar.py`` for fail-closed scheduler timing
    # (auto-stop / daily-summary sleep until ``close + offset``). The
    # Alpaca v2/calendar endpoint requires only API key auth — no
    # special trading entitlement — and is read-only (no live-trading
    # opt-in concern). Days the market is closed are absent from the
    # response list rather than included with empty open/close fields.
    def get_calendar(self, start: Any, end: Any) -> list[dict[str, Any]]:
        start_str = start.isoformat() if hasattr(start, "isoformat") else str(start)
        end_str = end.isoformat() if hasattr(end, "isoformat") else str(end)
        resp = self._request("GET", "/v2/calendar", params={"start": start_str, "end": end_str})
        resp.raise_for_status()
        return resp.json()

    # ── Account ───────────────────────────────────────────────────────────

    def get_account(self) -> dict[str, Any]:
        """Return the full account object (cash, equity, buying power, etc.)."""
        r = self._request("GET", "/v2/account")
        r.raise_for_status()
        return r.json()

    # ── Orders ────────────────────────────────────────────────────────────

    def submit_order(
        self,
        symbol: str,
        qty: int | float,
        side: OrderSide,
        order_type: OrderType = OrderType.MARKET,
        time_in_force: TimeInForce = TimeInForce.DAY,
        limit_price: float | None = None,
        stop_price: float | None = None,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        extended_hours: bool = False,
    ) -> dict[str, Any]:
        """Submit a new order.  Returns the Alpaca order object."""
        body: dict[str, Any] = {
            "symbol":        symbol.upper(),
            "qty":           str(qty),
            "side":          side.value,
            "type":          order_type.value,
            "time_in_force": time_in_force.value,
        }
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        if stop_price is not None:
            body["stop_price"] = str(round(stop_price, 2))
        if extended_hours:
            body["extended_hours"] = True

        # One-sided protective legs are REJECTED (2026-07-06 P1 fix): Alpaca
        # brackets require both legs, and the old code silently DROPPED a lone
        # stop_loss or take_profit — the order went out naked with no error.
        # The autotrade path is already all-or-nothing (use_bracket requires
        # both); this protects the manual /order and /quick-trade endpoints.
        if (take_profit is None) != (stop_loss is None):
            raise ValueError(
                "submit_order: take_profit and stop_loss must be provided together "
                f"(got take_profit={take_profit!r}, stop_loss={stop_loss!r}) — "
                "a one-sided leg would be silently dropped and the order placed naked"
            )

        # Bracket order legs — requires BOTH take_profit and stop_loss
        if take_profit is not None and stop_loss is not None:
            body["order_class"] = "bracket"
            body["take_profit"] = {"limit_price": str(round(take_profit, 2))}
            body["stop_loss"] = {"stop_price": str(round(stop_loss, 2))}

        # Per-call guard — re-checks opt-in at submission time so a mid-process
        # env flip can't sneak a live order through an already-constructed client.
        if _is_live_mode() and not _live_trading_opted_in():
            logger.error(
                "order_blocked_live_guard symbol=%s side=%s qty=%s — %s=%s required",
                symbol, side.value, qty, _LIVE_OPT_IN_ENV, _LIVE_OPT_IN_VALUE,
            )
            raise RuntimeError(
                f"Refusing to submit live order: set {_LIVE_OPT_IN_ENV}="
                f"{_LIVE_OPT_IN_VALUE} to enable real-money trading."
            )

        mode = "LIVE" if _is_live_mode() else "paper"
        logger.info(
            "order_submit mode=%s symbol=%s side=%s qty=%s type=%s ext_hours=%s",
            mode, symbol, side.value, qty, order_type.value, extended_hours,
        )
        r = self._request("POST", "/v2/orders", json=body)
        if r.status_code >= 400:
            # Extract Alpaca's rejection reason before raising
            try:
                detail = r.json().get("message", r.text)
            except (json.JSONDecodeError, ValueError, AttributeError):
                detail = r.text
            logger.error("order_rejected symbol=%s status=%d reason=%s", symbol, r.status_code, detail)
            raise ValueError(f"Alpaca rejected order ({r.status_code}): {detail}")
        order = r.json()
        logger.info("order_accepted id=%s status=%s", order.get("id"), order.get("status"))
        return order

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch a single order by ID."""
        r = self._request("GET", f"/v2/orders/{order_id}")
        r.raise_for_status()
        return r.json()

    def list_orders(
        self,
        status: str = "open",
        limit: int = 50,
        direction: str = "desc",
    ) -> list[dict[str, Any]]:
        """List recent orders."""
        r = self._request("GET", "/v2/orders", params={
            "status": status, "limit": str(limit), "direction": direction,
        })
        r.raise_for_status()
        return r.json()

    def get_todays_sell_proceeds(self) -> float:
        """Sum of today's filled sell proceeds — these are unsettled until T+1.

        On a cash account, spending this money on anything you might flip before
        it settles triggers a Good Faith Violation. Alpaca's
        `non_marginable_buying_power` is supposed to track this but often
        reports 0 on fresh cash accounts, so we compute it ourselves from
        today's fills.

        Returns the total notional ($) of sells filled today in US/Eastern time.
        """
        try:
            # Midnight ET today, in UTC ISO8601 (Alpaca's `after` param is UTC).
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
            now_et = datetime.now(et)
            midnight_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            after_utc = midnight_et.astimezone(timezone.utc).isoformat()

            r = self._request("GET", "/v2/orders", params={
                "status": "closed",
                "limit": "100",
                "direction": "desc",
                "after": after_utc,
            })
            r.raise_for_status()
            orders = r.json() or []
        except Exception as exc:
            logger.warning("todays_sell_proceeds_lookup_failed error=%s", exc)
            return 0.0

        total = 0.0
        for o in orders:
            if (o.get("side") or "").lower() != "sell":
                continue
            if (o.get("status") or "").lower() != "filled":
                continue
            # filled_avg_price may be None for partial / canceled. filled_qty is string.
            try:
                price = float(o.get("filled_avg_price") or 0)
                qty = float(o.get("filled_qty") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0 and qty > 0:
                total += price * qty
        return round(total, 2)

    def get_todays_buy_symbols(self) -> set[str]:
        """Return the set of symbols with a filled BUY order today (US/Eastern).

        Used to flag positions as opened-today, which matters for PDT/GFV
        gating: selling a same-day-opened position closes a day-trade, and
        selling one that was bought with unsettled funds triggers GFV.
        """
        try:
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
            now_et = datetime.now(et)
            midnight_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            after_utc = midnight_et.astimezone(timezone.utc).isoformat()

            r = self._request("GET", "/v2/orders", params={
                "status": "closed",
                "limit": "100",
                "direction": "desc",
                "after": after_utc,
            })
            r.raise_for_status()
            orders = r.json() or []
        except Exception as exc:
            logger.warning("todays_buy_symbols_lookup_failed error=%s", exc)
            return set()

        syms: set[str] = set()
        for o in orders:
            if (o.get("side") or "").lower() != "buy":
                continue
            if (o.get("status") or "").lower() != "filled":
                continue
            sym = (o.get("symbol") or "").upper()
            if sym:
                syms.add(sym)
        return syms

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order."""
        r = self._request("DELETE", f"/v2/orders/{order_id}")
        r.raise_for_status()
        logger.info("order_cancelled id=%s", order_id)

    def cancel_all_orders(self) -> None:
        """Cancel every open order."""
        r = self._request("DELETE", "/v2/orders")
        r.raise_for_status()
        logger.info("all_orders_cancelled")

    # ── Positions ─────────────────────────────────────────────────────────

    def list_positions(self) -> list[dict[str, Any]]:
        """Return all open positions."""
        r = self._request("GET", "/v2/positions")
        r.raise_for_status()
        return r.json()

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        """Get a single position by symbol.  Returns None if no position."""
        r = self._request("GET", f"/v2/positions/{symbol.upper()}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def close_position(self, symbol: str) -> dict[str, Any]:
        """Close an entire position in a symbol (market sell/cover)."""
        r = self._request("DELETE", f"/v2/positions/{symbol.upper()}")
        r.raise_for_status()
        logger.info("position_closed symbol=%s", symbol)
        return r.json()

    def close_all_positions(self) -> list[dict[str, Any]]:
        """Liquidate all positions."""
        r = self._request("DELETE", "/v2/positions")
        r.raise_for_status()
        logger.info("all_positions_closed")
        return r.json()


# ── Shared-client singleton ───────────────────────────────────────────────────
# AlpacaTradingClient holds an httpx.Client with a connection pool. Constructing
# a fresh one on every request wastes the pool and spams logs. Most request
# paths can safely share a single module-level client; autotrade's internal
# worker threads can still build their own via the AlpacaTradingClient() ctor.
_SHARED_CLIENT: AlpacaTradingClient | None = None
_SHARED_CLIENT_LOCK = threading.Lock()


def get_shared_trading_client() -> AlpacaTradingClient:
    """Return a process-wide shared AlpacaTradingClient, constructing on first use.

    httpx.Client is thread-safe for concurrent requests, so one shared client
    works across FastAPI handlers. Raises ValueError / RuntimeError the same
    way AlpacaTradingClient() does if credentials or live-mode opt-in fail.
    """
    global _SHARED_CLIENT
    if _SHARED_CLIENT is not None:
        return _SHARED_CLIENT
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is None:
            _SHARED_CLIENT = AlpacaTradingClient()
        return _SHARED_CLIENT


def reset_shared_trading_client() -> None:
    """Drop the shared client — used on process shutdown / tests."""
    global _SHARED_CLIENT
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None:
            try:
                _SHARED_CLIENT.close()
            except Exception:
                pass
        _SHARED_CLIENT = None


# ── Alpaca trade-updates WebSocket stream ─────────────────────────────────────
# Alpaca exposes a realtime stream of order events (new, fill, partial_fill,
# canceled, rejected, ...) that lets us react to fills in milliseconds instead
# of polling /v2/orders. This class is a minimal async client for that stream.
# Endpoint: wss://{api|paper-api}.alpaca.markets/stream
# Docs:     https://docs.alpaca.markets/docs/websocket-streaming

class AlpacaTradeStream:
    """Async client for Alpaca's /stream trade_updates WebSocket.

    Usage:
        stream = AlpacaTradeStream(on_trade_update=my_callback)
        task = asyncio.create_task(stream.run_forever())
        ...
        await stream.stop()
        await task
    """

    def __init__(self, on_trade_update=None) -> None:
        self._callback = on_trade_update
        self._stop = False
        self._task: Any = None

    @property
    def stream_url(self) -> str:
        # The stream host is the trading API host with scheme swapped to
        # wss://. Sourced from the ``trading_mode`` module so a runtime
        # paper↔live flip routes the reconnect loop to the correct
        # broker on its next attempt.
        _, _, base = trading_mode.get_credentials()
        base = (base or "").rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/stream"

    async def run_forever(self) -> None:
        """Connect, subscribe, and dispatch events. Reconnects on failure."""
        # Import lazily so modules that never enable the stream don't need the
        # websockets dep at import time (uvicorn[standard] ships it anyway).
        try:
            import websockets
        except ImportError:
            logger.warning("alpaca_trade_stream_disabled reason=websockets_not_installed")
            return

        # Re-read creds on every outer reconnect attempt so a mode flip
        # picks up the new key/secret the next time the loop circles
        # back — no need to kill+recreate the stream object just to
        # change accounts.
        api_key, secret_key, _ = trading_mode.get_credentials()
        if not api_key or not secret_key:
            logger.info(
                "alpaca_trade_stream_disabled reason=no_credentials mode=%s",
                trading_mode.get_mode(),
            )
            return

        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(
                    self.stream_url,
                    ping_interval=30,
                    ping_timeout=20,
                ) as ws:
                    logger.info("alpaca_trade_stream_connected url=%s", self.stream_url)
                    backoff = 1.0  # reset after a successful connect

                    # Auth — re-read credentials each connect so mode
                    # flips apply on the next reconnect cycle.
                    api_key, secret_key, _ = trading_mode.get_credentials()
                    await ws.send(json.dumps({
                        "action": "auth",
                        "key":    api_key,
                        "secret": secret_key,
                    }))
                    # Subscribe to trade_updates
                    await ws.send(json.dumps({
                        "action": "listen",
                        "data": {"streams": ["trade_updates"]},
                    }))

                    async for raw in ws:
                        if self._stop:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        stream_name = msg.get("stream")
                        data = msg.get("data") or {}
                        if stream_name == "trade_updates" and self._callback is not None:
                            try:
                                result = self._callback(data)
                                # Support both sync and async callbacks
                                if hasattr(result, "__await__"):
                                    await result
                            except Exception as exc:
                                logger.warning("alpaca_trade_stream_callback_failed error=%s", exc)
            except Exception as exc:
                if self._stop:
                    break
                logger.warning(
                    "alpaca_trade_stream_disconnected error=%s retry_in=%.1fs",
                    exc, backoff,
                )
                # Backoff + jitter, capped at 30s. Keep this loop resilient —
                # a missed fill because we gave up would be costly.
                import asyncio as _asyncio
                await _asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        logger.info("alpaca_trade_stream_stopped")

    async def stop(self) -> None:
        self._stop = True
