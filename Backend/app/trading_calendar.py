"""
Trading-day calendar (v7.16+, 2026-05-13)
=============================================================
Fetches today's market hours from Alpaca's ``/v2/calendar`` endpoint and
normalizes the response into a frozen ``TradingDay`` value object the
scheduler can use to time auto-stop / daily-summary events around the
actual close (handles early-close days like Thanksgiving Friday, July 3,
Christmas Eve).

Fail-closed
-----------
If the Alpaca API call fails for any reason, ``get_today()`` returns a
``TradingDay`` with ``is_open=False`` and ``lookup_failed=True``. The
auto-start event (Phase 4) treats this as "do not start trading" and
emails an alert. Reason: when we can't verify whether today is a trading
day, the safer default is "don't trade."

Cache
-----
``get_today()`` caches its result for the rest of the ET day on success.
On failure, it caches for ``_FAILURE_TTL_SECONDS`` (5 minutes) so a
transient Alpaca outage doesn't permanently block the trading day — the
next call after 5 min retries the API.

Normalization
-------------
Alpaca returns open/close as ``"HH:MM"`` strings. ``_parse_time`` accepts
plain strings, ``datetime.time``, and ``datetime.datetime`` for defensive
SDK-shape changes. Output is always a ``datetime.time`` (no tz attached
to the time itself; the tz lives on ``market_close_at_et``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("tradehack.trading_calendar")

_ET = ZoneInfo("America/New_York")
_FAILURE_TTL_SECONDS = 300


@dataclass(frozen=True)
class TradingDay:
    """The market's status for a single ET date.

    ``is_open`` distinguishes weekday-holiday/weekend (``False``,
    ``lookup_failed=False``) from API failure (``False``,
    ``lookup_failed=True``). Both cause the scheduler to skip trading,
    but ``lookup_failed=True`` triggers an alert email.
    """
    date: date
    is_open: bool
    open_et: time | None
    close_et: time | None
    market_close_at_et: datetime | None     # ET-tz-aware datetime for sleep math
    is_early_close: bool                    # True if close_et < 16:00
    lookup_failed: bool                     # True if API call failed (fail-closed)


# Cache: today_et → (TradingDay, fetched_at). Successes valid until
# next ET midnight (handled implicitly by date-keyed lookup); failures
# expire after _FAILURE_TTL_SECONDS so a transient outage can clear.
_cache: dict[date, tuple[TradingDay, datetime]] = {}


def _today_et() -> date:
    return datetime.now(_ET).date()


def _parse_time(value: Any) -> time | None:
    """Normalize Alpaca's calendar time field to ``datetime.time``.

    Accepts ``"HH:MM"`` / ``"HH:MM:SS"`` strings, ``datetime.time``, and
    ``datetime.datetime``. Returns ``None`` on empty/None input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, str):
        parts = value.split(":")
        return time(
            int(parts[0]),
            int(parts[1]),
            int(parts[2]) if len(parts) > 2 else 0,
        )
    raise ValueError(f"unparseable calendar time value: {value!r}")


def _build_today_from_entry(today_et: date, entry: dict[str, Any]) -> TradingDay:
    open_t = _parse_time(entry.get("open"))
    close_t = _parse_time(entry.get("close"))
    close_dt = (
        datetime.combine(today_et, close_t, tzinfo=_ET)
        if close_t is not None
        else None
    )
    is_early = close_t is not None and close_t < time(16, 0)
    return TradingDay(
        date=today_et,
        is_open=True,
        open_et=open_t,
        close_et=close_t,
        market_close_at_et=close_dt,
        is_early_close=is_early,
        lookup_failed=False,
    )


def _fail_closed(today_et: date) -> TradingDay:
    return TradingDay(
        date=today_et,
        is_open=False,
        open_et=None,
        close_et=None,
        market_close_at_et=None,
        is_early_close=False,
        lookup_failed=True,
    )


def _fetch_today_from_alpaca(today_et: date) -> TradingDay:
    """Hit ``/v2/calendar?start=today&end=today``. Empty response list =
    market closed today (weekend or holiday). Raises on HTTP/network
    errors; the caller catches and converts to fail-closed."""
    from .alpaca import get_shared_trading_client

    client = get_shared_trading_client()
    entries = client.get_calendar(today_et, today_et)
    if not entries:
        return TradingDay(
            date=today_et,
            is_open=False,
            open_et=None,
            close_et=None,
            market_close_at_et=None,
            is_early_close=False,
            lookup_failed=False,
        )
    return _build_today_from_entry(today_et, entries[0])


def get_today() -> TradingDay:
    """Return today's ``TradingDay``. Cached for the rest of the ET day
    on success; for 5 minutes on failure (fail-closed).

    Never raises. On any underlying failure, returns a ``TradingDay``
    with ``is_open=False`` and ``lookup_failed=True`` and logs at
    WARNING with ``calendar_lookup_failed fail_closed=true``.
    """
    today_et = _today_et()

    # Cache lookup — keyed on today's ET date so the cache rolls over
    # implicitly at midnight ET.
    if today_et in _cache:
        cached_day, cached_at = _cache[today_et]
        if not cached_day.lookup_failed:
            return cached_day
        # Failure entries expire after _FAILURE_TTL_SECONDS so transient
        # Alpaca outages can clear within the same trading day.
        age = (datetime.now(_ET) - cached_at).total_seconds()
        if age < _FAILURE_TTL_SECONDS:
            return cached_day

    # Fetch
    try:
        day = _fetch_today_from_alpaca(today_et)
    except Exception as exc:
        logger.warning(
            "calendar_lookup_failed fail_closed=true err=%s",
            exc, exc_info=True,
        )
        day = _fail_closed(today_et)

    _cache[today_et] = (day, datetime.now(_ET))
    return day


def clear_cache() -> None:
    """Drop the in-memory cache. Used by tests and by manual
    ``/scheduler/refresh-calendar`` requests."""
    _cache.clear()
