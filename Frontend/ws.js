// ── WebSocket connection manager ───────────────────────────────────────────
// Real-time push channel with auto-reconnect and keep-alive ping.
// main.js wires this up by calling initWebSocket({ handlers, onOpen, onClose }).
// Handlers is a map { msgType -> (data) => void }. onOpen/onClose let main.js
// swap between WS-mode and polling-mode for status & market pulse.

import { WS_URL } from "./config.js";

let ws = null;
let wsReconnectTimer = null;
let wsReconnectDelay = 1000;        // starts at 1s, doubles on each failure
const WS_MAX_RECONNECT_DELAY = 30_000;
const WS_PING_INTERVAL = 25_000;    // send ping every 25s to keep alive
let wsPingTimer = null;
let wsConnected = false;

export function isWsConnected() {
  return wsConnected;
}

const wsIndicator = document.getElementById("ws-indicator");

function updateWsIndicator(connected) {
  wsConnected = connected;
  if (wsIndicator) {
    wsIndicator.textContent = connected ? "● Connected" : "● Reconnecting…";
    wsIndicator.className = `ws-indicator ${connected ? "connected" : "disconnected"}`;
  }
}

function scheduleReconnect(opts) {
  if (wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    connectWebSocket(opts);
  }, wsReconnectDelay);
  wsReconnectDelay = Math.min(wsReconnectDelay * 2, WS_MAX_RECONNECT_DELAY);
}

function connectWebSocket(opts) {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  try {
    ws = new WebSocket(WS_URL);
  } catch {
    scheduleReconnect(opts);
    return;
  }

  ws.onopen = () => {
    wsReconnectDelay = 1000;
    updateWsIndicator(true);

    opts.onOpen?.();

    // Keep-alive ping
    if (wsPingTimer) clearInterval(wsPingTimer);
    wsPingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
      }
    }, WS_PING_INTERVAL);
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "pong") return;
    const handler = opts.handlers?.[msg.type];
    if (handler) handler(msg.data);
  };

  ws.onclose = () => {
    updateWsIndicator(false);
    if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
    scheduleReconnect(opts);
    opts.onClose?.();
  };

  ws.onerror = () => {
    // onclose will fire after this
  };
}

export function initWebSocket(opts) {
  connectWebSocket(opts);
}
