"""
trading_mode.py — Runtime paper ↔ live trading-mode singleton.
=============================================================

Why this exists
---------------
``settings`` is loaded once from env at startup and is frozen. We want
the user to flip paper↔live from the UI WITHOUT editing ``.env`` and
restarting the process, so every Alpaca client construction is routed
through this module. ``get_credentials()`` returns the
``(api_key, secret_key, base_url)`` tuple for the *currently-selected*
mode; ``set_mode()`` flips the selection and persists the choice to
disk so it survives a process restart.

Env var shape (set in ``.env``)
-------------------------------
  ALPACA_PAPER_API_KEY
  ALPACA_PAPER_SECRET_KEY
  ALPACA_PAPER_URL          (default: https://paper-api.alpaca.markets)

  ALPACA_LIVE_API_KEY
  ALPACA_LIVE_SECRET_KEY
  ALPACA_LIVE_URL           (default: https://api.alpaca.markets)

  TRADING_MODE_DEFAULT      "paper" | "live"  (first-boot default;
                                               the persistence file
                                               wins on subsequent boots)

Backward compat
---------------
If only the legacy ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` /
``ALPACA_TRADING_URL`` are set, they populate whichever mode matches
the trading URL (paper if the URL contains ``paper-api``, otherwise
live). The other mode is "unconfigured" and any attempt to switch
into it is blocked at the API layer with a clear error message — so
rolling this out doesn't require the user to touch their .env.

Persistence
-----------
The selected mode is written to ``Backend/data/trading_mode.json``.
Format: ``{"mode": "paper"|"live", "updated_at": "<iso8601>"}``. This
file is read once on first access; subsequent ``get_mode()`` /
``set_mode()`` calls use the in-memory cache. Concurrent writes are
serialized through ``_LOCK``.

Safety discipline
-----------------
This module only handles credential + mode state. The actual
side-effects of a flip (resetting the shared Alpaca client, clearing
autotrade state, restarting the WebSocket stream) live in the
``/trading/mode`` endpoint so those couplings stay out of the
credential layer.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .settings import BASE_DIR, settings

logger = logging.getLogger("tradehack.trading_mode")

Mode = Literal["paper", "live"]


class PaperBoundaryViolation(RuntimeError):
    """Raised whenever anything would route trading traffic outside the
    approved paper destination while the build is paper-only, or to a host
    that does not match the selected mode. Mapped to HTTP 403 by the API."""


def paper_only_enabled() -> bool:
    """PAPER_ONLY=true (the default) makes live trading unreachable: the
    mode cannot be switched to live and the broker client refuses any
    non-paper destination. Tests monkeypatch this function."""
    return bool(settings.paper_only)

# ── Persistence ──────────────────────────────────────────────────────────────

_STATE_DIR  = BASE_DIR / "data"
_STATE_FILE = _STATE_DIR / "trading_mode.json"
_LOCK = threading.Lock()

# Cached mode — loaded from disk on first use, mutated by set_mode().
_mode: Mode | None = None


# ── Env-var plumbing (paper + live side by side) ─────────────────────────────

_PAPER_URL_DEFAULT = "https://paper-api.alpaca.markets"
_LIVE_URL_DEFAULT  = "https://api.alpaca.markets"
_PAPER_HOST        = "paper-api.alpaca.markets"


def _legacy_mode_hint() -> Mode:
    """Guess the mode implied by the legacy single-key env config.

    Used ONLY when the side-by-side ALPACA_PAPER_* / ALPACA_LIVE_* vars
    aren't set and we have to fall back to ALPACA_API_KEY. Looks at the
    trading URL first (authoritative), then ALPACA_PAPER as a tiebreak.
    """
    url = (settings.alpaca_trading_url or "").lower()
    if _PAPER_HOST in url:
        return "paper"
    if url and _PAPER_HOST not in url:
        return "live"
    return "paper" if settings.alpaca_paper else "live"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _credentials_for(mode: Mode) -> tuple[str, str, str]:
    """Return ``(api_key, secret, base_url)`` for the given mode.

    Preference order:
      1. Side-by-side ALPACA_<MODE>_API_KEY / SECRET / URL
      2. Legacy ALPACA_API_KEY / SECRET / TRADING_URL — only used when
         that legacy config unambiguously matches ``mode`` (per
         ``_legacy_mode_hint()``); otherwise the mode is considered
         unconfigured and empty strings are returned.
    """
    upper = mode.upper()
    key    = _env(f"ALPACA_{upper}_API_KEY")
    secret = _env(f"ALPACA_{upper}_SECRET_KEY")
    url    = _env(f"ALPACA_{upper}_URL",
                  _PAPER_URL_DEFAULT if mode == "paper" else _LIVE_URL_DEFAULT)

    if key and secret:
        return key, secret, url

    # Fall back to legacy single-key config, but ONLY when it matches
    # the mode the user is asking about. Keys from the paper account
    # are useless for live trading and vice-versa — returning them
    # would silently route traffic to the wrong broker.
    if _legacy_mode_hint() == mode:
        return (
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            settings.alpaca_trading_url or (
                _PAPER_URL_DEFAULT if mode == "paper" else _LIVE_URL_DEFAULT
            ),
        )
    return "", "", url


def is_configured(mode: Mode) -> bool:
    """Does the given mode have both API key + secret configured?"""
    key, secret, _ = _credentials_for(mode)
    return bool(key and secret)


def get_credentials_for(mode: Mode) -> tuple[str, str, str]:
    """Public version of ``_credentials_for`` — same contract."""
    return _credentials_for(mode)


# ── Mode state — read / write ────────────────────────────────────────────────

def _load_persisted_mode() -> Mode | None:
    """Read ``data/trading_mode.json`` if it exists and is valid."""
    try:
        raw = _STATE_FILE.read_text()
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("trading_mode_load_failed error=%s", exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("trading_mode_corrupt path=%s", _STATE_FILE)
        return None
    mode = data.get("mode")
    if mode not in ("paper", "live"):
        return None
    return mode


def _write_persisted_mode(mode: Mode) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode":       mode,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(_STATE_FILE)  # atomic on POSIX


def _default_mode() -> Mode:
    """First-boot default when no persistence file exists.

    Prefers TRADING_MODE_DEFAULT env var → legacy hint → paper.
    Never defaults to live unless the user has explicitly set it up.
    """
    env_default = _env("TRADING_MODE_DEFAULT").lower()
    if env_default in ("paper", "live"):
        return env_default  # type: ignore[return-value]
    return _legacy_mode_hint()


def get_mode() -> Mode:
    """Return the current trading mode, loading from disk on first call."""
    global _mode
    if _mode is not None:
        return _mode
    with _LOCK:
        if _mode is not None:
            return _mode
        loaded = _load_persisted_mode() or _default_mode()
        if loaded == "live" and paper_only_enabled():
            # A persisted or env-derived "live" cannot survive a paper-only
            # build: never trust state on disk over the configured boundary.
            logger.warning("trading_mode_forced_paper reason=PAPER_ONLY requested=%s", loaded)
            loaded = "paper"
        _mode = loaded
        logger.info("trading_mode_loaded mode=%s source=%s",
                    _mode, "persist" if _STATE_FILE.exists() else "default")
        return _mode


def set_mode(mode: Mode) -> None:
    """Persist and cache a new mode.

    Does NOT run any of the downstream side-effects (shared-client
    reset, WS stream restart, autotrade state purge). The endpoint
    that owns the flip is responsible for those — this function is
    deliberately narrow so tests can poke the mode without booting
    the full trading stack.
    """
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")
    if mode == "live" and paper_only_enabled():
        raise PaperBoundaryViolation(
            "This build is paper-only (PAPER_ONLY=true): live trading mode is "
            "not available. Set PAPER_ONLY=false, configure live credentials, "
            "and set LIVE_TRADING_ENABLED=yes to enable real-money trading."
        )
    global _mode
    with _LOCK:
        _write_persisted_mode(mode)
        _mode = mode
        logger.info("trading_mode_set mode=%s", mode)


def is_live() -> bool:
    """Shortcut — True when the active mode is ``live``."""
    return get_mode() == "live"


def is_paper() -> bool:
    """Shortcut — True when the active mode is ``paper``.

    Drop-in replacement for the legacy ``settings.alpaca_paper`` reads
    in the account / order gating code so those sites pick up the
    runtime mode flip.
    """
    return get_mode() == "paper"


def get_credentials() -> tuple[str, str, str]:
    """Return credentials for the CURRENTLY active mode."""
    return _credentials_for(get_mode())


# ── Status snapshot (for UI / API) ───────────────────────────────────────────

def mode_snapshot() -> dict:
    """JSON-safe snapshot for the ``/trading/mode`` endpoint + WS events.

    Shape:
      {
        "mode":       "paper" | "live",
        "paper":      {"configured": bool, "base_url": str},
        "live":       {"configured": bool, "base_url": str},
        "persist_path": "data/trading_mode.json",
      }

    Credentials are never leaked — we only expose whether each mode
    has keys configured so the UI can gate its switch button.
    """
    mode = get_mode()
    _, _, paper_url = _credentials_for("paper")
    _, _, live_url  = _credentials_for("live")
    return {
        "mode":         mode,
        "paper":        {"configured": is_configured("paper"), "base_url": paper_url},
        "live":         {"configured": is_configured("live"),  "base_url": live_url},
        "persist_path": str(_STATE_FILE.relative_to(BASE_DIR)) if _STATE_FILE.exists() else None,
    }
