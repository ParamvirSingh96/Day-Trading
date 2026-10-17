"""
WebSocket hub — pushes real-time events to connected operator browsers.
==========================================================================
Event types:
  autotrade_status   Full autotrade status snapshot (replaces 5s polling)
  market_pulse       Market pulse update (replaces 60s polling)
  trade_event        Individual trade lifecycle events (approval, execution, etc.)
  monitor_event      Position monitor updates (trailing stop moved, position closed)
  wallet_update      Account/position change signal (tells frontend to refresh)

Connection flow (2026-10 hardening):
  1. Client opens ws://.../ws — the Origin header must be one of the
     configured CORS origins (close 4003 otherwise).
  2. Server validates the session cookie: it must belong to an OPERATOR with
     an unexpired session (close 4001 otherwise).
  3. The socket is registered against its session token hash + user id.
  4. The session is re-validated on every inbound message and at least every
     WS_REVALIDATE_SECONDS; logout / re-login close the bound sockets
     immediately (``revoke_session_sockets`` / ``revoke_user_sockets``).
  5. Client can send {"type": "ping"}; server responds {"type": "pong"}.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .db import SessionLocal
from .models import User, UserSession
from .settings import settings

logger = logging.getLogger("tradehack.ws")

WS_REVALIDATE_SECONDS = 30.0
CLOSE_AUTH_REQUIRED = 4001
CLOSE_ORIGIN_DENIED = 4003


@dataclass
class _Conn:
    ws: WebSocket
    user_id: int
    token_hash: str


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self):
        self._conns: dict[WebSocket, _Conn] = {}
        self._lock = asyncio.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def client_count(self) -> int:
        return len(self._conns)

    async def connect(self, ws: WebSocket, user_id: int, token_hash: str):
        await ws.accept()
        self.loop = asyncio.get_running_loop()
        async with self._lock:
            self._conns[ws] = _Conn(ws=ws, user_id=user_id, token_hash=token_hash)
        logger.info("ws_connected clients=%d", self.client_count)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._conns.pop(ws, None)
        logger.info("ws_disconnected clients=%d", self.client_count)

    async def broadcast(self, event_type: str, data: Any = None):
        """Send an event to ALL connected clients — in parallel."""
        if not self._conns:
            return

        payload = json.dumps(
            {"type": event_type, "data": data, "ts": datetime.now(timezone.utc).isoformat()},
            default=str,
        )

        async with self._lock:
            targets = list(self._conns)
        if not targets:
            return

        results = await asyncio.gather(
            *(ws.send_text(payload) for ws in targets),
            return_exceptions=True,
        )
        dead = [ws for ws, r in zip(targets, results) if isinstance(r, BaseException)]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._conns.pop(ws, None)
            logger.debug("ws_broadcast_pruned_dead count=%d", len(dead))

    async def send_personal(self, ws: WebSocket, event_type: str, data: Any = None):
        payload = json.dumps(
            {"type": event_type, "data": data, "ts": datetime.now(timezone.utc).isoformat()},
            default=str,
        )
        await ws.send_text(payload)

    async def close_matching(self, *, token_hash: str | None = None,
                             user_id: int | None = None,
                             code: int = CLOSE_AUTH_REQUIRED,
                             reason: str = "Session revoked") -> int:
        """Close every socket bound to the given session or user."""
        async with self._lock:
            victims = [c for c in self._conns.values()
                       if (token_hash is not None and c.token_hash == token_hash)
                       or (user_id is not None and c.user_id == user_id)]
            for c in victims:
                self._conns.pop(c.ws, None)
        for c in victims:
            try:
                await c.ws.close(code=code, reason=reason)
            except Exception:
                pass
        if victims:
            logger.info("ws_revoked count=%d reason=%s", len(victims), reason)
        return len(victims)


# Global singleton
manager = ConnectionManager()


def _run_on_loop(coro) -> None:
    """Schedule a coroutine on the hub's loop from sync route handlers
    (which FastAPI runs in a worker thread) or from async code."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None and running is manager.loop:
        running.create_task(coro)
        return
    if manager.loop is not None and manager.loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, manager.loop)
        return
    coro.close()  # no hub loop yet (tests / no clients) — nothing to revoke


def revoke_session_sockets(token_hash: str) -> None:
    _run_on_loop(manager.close_matching(token_hash=token_hash, reason="Logged out"))


def revoke_user_sockets(user_id: int) -> None:
    _run_on_loop(manager.close_matching(user_id=user_id, reason="Session superseded"))


# ── Handshake checks ─────────────────────────────────────────────────────────

def _origin_allowed(ws: WebSocket) -> bool:
    """Browsers always send Origin on a WebSocket upgrade; it must be one of
    the configured frontend origins (same allow-list as CORS)."""
    origin = (ws.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        return False
    allowed = {o.strip().rstrip("/") for o in settings.cors_origins}
    return origin in allowed


def _session_lookup(token_hash: str) -> tuple[int, str] | None:
    """Return ``(user_id, token_hash)`` for a valid OPERATOR session, else None."""
    db = SessionLocal()
    try:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
        if not session:
            return None
        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return None
        user = db.get(User, session.user_id)
        if not user or not user.is_operator:
            return None
        return user.id, token_hash
    except Exception as exc:
        logger.debug("ws_session_lookup_error error=%s", exc)
        return None
    finally:
        db.close()


def _token_hash_from_cookie(ws: WebSocket) -> str | None:
    cookie_header = ws.headers.get("cookie", "")
    if not cookie_header:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except Exception:
        return None
    morsel = cookies.get(settings.session_cookie_name)
    if not morsel or not morsel.value:
        return None
    return hashlib.sha256(morsel.value.encode("utf-8")).hexdigest()


async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket handler — check origin, authenticate an operator,
    accept, then keep re-validating the session for the socket's lifetime."""
    if not _origin_allowed(ws):
        await ws.close(code=CLOSE_ORIGIN_DENIED, reason="Origin not allowed")
        return

    token_hash = _token_hash_from_cookie(ws)
    ident = _session_lookup(token_hash) if token_hash else None
    if ident is None:
        await ws.close(code=CLOSE_AUTH_REQUIRED, reason="Authentication required")
        return
    user_id, token_hash = ident

    await manager.connect(ws, user_id=user_id, token_hash=token_hash)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=WS_REVALIDATE_SECONDS)
            except asyncio.TimeoutError:
                raw = None

            # Re-validate on every message AND on every idle interval: an
            # expired or logged-out session must not keep receiving the feed.
            if _session_lookup(token_hash) is None:
                await manager.disconnect(ws)
                await ws.close(code=CLOSE_AUTH_REQUIRED, reason="Session expired")
                return
            if raw is None:
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "ping":
                await manager.send_personal(ws, "pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("ws_error error=%s", exc)
    finally:
        await manager.disconnect(ws)


# ── Convenience broadcast helpers (called from autotrade.py, etc.) ──────────

def emit_autotrade_status(status_data: dict):
    """Push a full autotrade status snapshot to all clients."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast("autotrade_status", status_data))
    except RuntimeError:
        pass  # no event loop — skip (e.g. during tests)


def emit_trade_event(event_name: str, data: dict):
    """Push a single trade lifecycle event (approval created, order filled, etc.)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast("trade_event", {"event": event_name, **data}))
    except RuntimeError:
        pass


def emit_monitor_event(event_name: str, data: dict):
    """Push a position monitor event (trailing stop moved, position closed, etc.)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast("monitor_event", {"event": event_name, **data}))
    except RuntimeError:
        pass


def emit_market_pulse(pulse_data: dict):
    """Push market pulse data to all clients."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast("market_pulse", pulse_data))
    except RuntimeError:
        pass


def emit_wallet_update():
    """Signal clients to refresh wallet data (account, positions, orders)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast("wallet_update", None))
    except RuntimeError:
        pass


def emit_trading_mode_changed(snapshot: dict):
    """Push a trading-mode flip (paper↔live) to all clients so the wallet
    badge + any mode-conditional UI re-render immediately. ``snapshot`` is
    the ``trading_mode.mode_snapshot()`` dict (no credentials)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast("trading_mode_changed", snapshot))
    except RuntimeError:
        pass
