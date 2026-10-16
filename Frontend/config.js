// Derive the API base URL from the current environment.
// - In local/LAN development the backend runs on port 8000 on the same host.
// - In production (HTTPS behind a reverse proxy), API is on the same origin.
const isProd = window.location.protocol === "https:";
export const API_BASE_URL = isProd
  ? window.location.origin
  : `http://${window.location.hostname}:8000`;

// WebSocket URL — same host, ws:// (dev/LAN) or wss:// (prod)
const wsProtocol = isProd ? "wss:" : "ws:";
const wsHost = isProd ? window.location.host : `${window.location.hostname}:8000`;
export const WS_URL = `${wsProtocol}//${wsHost}/ws`;
