"""
WebSocket hub — pushes real-time events to all connected browser clients.
==========================================================================
Event types:
  autotrade_status   Full autotrade status snapshot (replaces 5s polling)
  market_pulse       Market pulse update (replaces 60s polling)
  trade_event        Individual trade lifecycle events (approval, execution, etc.)
  monitor_event      Position monitor updates (trailing stop moved, position closed)
  wallet_update      Account/position change signal (tells frontend to refresh)

Connection flow:
  1. Client opens ws://.../ws
  2. Server authenticates via the session cookie sent during upgrade
  3. Server adds client to the broadcast set
  4. Backend emits events via broadcast() — all clients receive them
  5. Client can send {"type": "ping"} to keep alive; server responds {"type": "pong"}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .db import SessionLocal
from .models import UserSession
from .settings import settings

logger = logging.getLogger("tradehack.ws")


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self):
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        logger.info("ws_connected clients=%d", self.client_count)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._connections.discard(ws)
        logger.info("ws_disconnected clients=%d", self.client_count)

    async def broadcast(self, event_type: str, data: Any = None):
        """Send an event to ALL connected clients — in parallel."""
        if not self._connections:
            return

        payload = json.dumps(
            {"type": event_type, "data": data, "ts": datetime.now(timezone.utc).isoformat()},
            default=str,
        )

        # Snapshot the socket set under the lock, then fire sends concurrently
        # outside the lock so one slow client can't stall the rest.
        async with self._lock:
            targets = list(self._connections)

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
                    self._connections.discard(ws)
            logger.debug("ws_broadcast_pruned_dead count=%d", len(dead))

    async def send_personal(self, ws: WebSocket, event_type: str, data: Any = None):
        """Send an event to a single client."""
        payload = json.dumps(
            {"type": event_type, "data": data, "ts": datetime.now(timezone.utc).isoformat()},
            default=str,
        )
        await ws.send_text(payload)


# Global singleton
manager = ConnectionManager()


def _authenticate_ws(ws: WebSocket) -> bool:
    """Validate the session cookie from the WebSocket upgrade request."""
    cookie_header = ws.headers.get("cookie", "")
    if not cookie_header:
        return False

    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except Exception:
        return False

    morsel = cookies.get(settings.session_cookie_name)
    if not morsel:
        return False

    raw_token = morsel.value
    if not raw_token:
        return False

    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    db = SessionLocal()
    try:
        session = db.scalar(
            select(UserSession).where(UserSession.token_hash == token_hash)
        )
        if not session:
            return False

        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return False

        return True
    except Exception:
        return False
    finally:
        db.close()


async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket handler — authenticate, accept, listen for pings."""
    # Authenticate before accepting
    if not _authenticate_ws(ws):
        await ws.close(code=4001, reason="Authentication required")
        return

    await manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "ping":
                await manager.send_personal(ws, "pong")
            # Future: handle client-initiated messages here
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
