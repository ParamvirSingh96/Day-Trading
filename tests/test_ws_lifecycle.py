"""WebSocket feed: origin allow-list, operator-only, and session lifecycle
(logout / expiry revoke an already-open socket)."""
from __future__ import annotations

import dataclasses
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import OPERATOR, VISITOR, register, register_and_login, session_cookie_header
from app import auth as auth_mod
from app import ws as ws_mod
from app.models import UserSession

ORIGIN = {"Origin": "http://testserver"}


def test_rejects_without_session(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers=ORIGIN):
            pass
    assert exc.value.code == ws_mod.CLOSE_AUTH_REQUIRED


def test_rejects_disallowed_origin(operator_client):
    headers = {**session_cookie_header(operator_client), "Origin": "http://evil.example"}
    with pytest.raises(WebSocketDisconnect) as exc:
        with operator_client.websocket_connect("/ws", headers=headers):
            pass
    assert exc.value.code == ws_mod.CLOSE_ORIGIN_DENIED


def test_rejects_missing_origin(operator_client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with operator_client.websocket_connect("/ws", headers=session_cookie_header(operator_client)):
            pass
    assert exc.value.code == ws_mod.CLOSE_ORIGIN_DENIED


def test_operator_connects_and_pings(operator_client):
    headers = {**session_cookie_header(operator_client), **ORIGIN}
    with operator_client.websocket_connect("/ws", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"


def test_non_operator_cannot_subscribe(client, monkeypatch):
    register(client, OPERATOR)
    monkeypatch.setattr(auth_mod, "settings",
                        dataclasses.replace(auth_mod.settings, registration_mode="open"))
    visitor = client.__class__(client.app)
    register_and_login(visitor, VISITOR)
    headers = {**session_cookie_header(visitor), **ORIGIN}
    with pytest.raises(WebSocketDisconnect) as exc:
        with visitor.websocket_connect("/ws", headers=headers):
            pass
    assert exc.value.code == ws_mod.CLOSE_AUTH_REQUIRED


def test_logout_closes_the_open_socket(operator_client):
    headers = {**session_cookie_header(operator_client), **ORIGIN}
    with operator_client.websocket_connect("/ws", headers=headers) as ws:
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"
        r = operator_client.post("/auth/logout")
        assert r.status_code == 200
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == ws_mod.CLOSE_AUTH_REQUIRED


def test_revoked_session_is_detected_on_next_message(operator_client, db):
    headers = {**session_cookie_header(operator_client), **ORIGIN}
    with operator_client.websocket_connect("/ws", headers=headers) as ws:
        # revoke out-of-band (e.g. expiry sweep / admin) without the logout hook
        db.query(UserSession).delete()
        db.commit()
        ws.send_text(json.dumps({"type": "ping"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == ws_mod.CLOSE_AUTH_REQUIRED


def test_relogin_supersedes_old_socket(operator_client):
    headers = {**session_cookie_header(operator_client), **ORIGIN}
    with operator_client.websocket_connect("/ws", headers=headers) as ws:
        r = operator_client.post("/auth/login", json={"email": OPERATOR["email"],
                                                       "password": OPERATOR["password"]})
        assert r.status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
