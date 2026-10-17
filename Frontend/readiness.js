// Morning Readiness Guard (v7.16, 2026-05-07)
// =============================================================================
// Two-layer guard against the "backend hung / autotrade off / universe
// not applied" failure modes that have repeatedly cost trades:
//
//  Layer 1 — Heartbeat watchdog (THIS module, frontend)
//    Every 30s, fetch GET /health with a 5-sec timeout.
//      - Timeout / network error → red banner: "Backend unavailable"
//        (this layer fires REGARDLESS of what the backend says, so a
//         hung uvicorn no longer hides behind cached panels)
//      - HTTP 200 → proceed to layer 2
//
//  Layer 2 — Readiness state (backend + frontend)
//    GET /trading/readiness returns a labelled state:
//      - severity: ok | info | warn | urgent | blocking
//      - message + actions + notify
//    Frontend renders a tier-colored banner above Dynamic Stocks.
//    Browser notification + sound only for urgent + blocking.
//
// Audit: dismissals + alert acknowledgements POST to
// /trading/readiness/event so we can review whether the guard
// changed operator behavior over a 2-week window.
//
// READS-ONLY GUARANTEE: this module never starts/stops autotrade,
// never modifies any predicate, never fires orders. It only renders
// a banner and posts dismissal events. Operationally safe.
// =============================================================================

import { API_BASE_URL } from "./config.js";
import { esc } from "./utils.js";
import { playSound, sendBrowserNotification } from "./notifications.js";

const POLL_INTERVAL_MS = 30_000;     // every 30s
const HEALTH_TIMEOUT_MS = 5_000;     // /health must answer within 5s
const STORAGE_KEY = "tradehack_readiness_dismissed";

// ── Module state ───────────────────────────────────────────────────────────

let _poll_handle = null;
let _last_severity = null;
let _last_stage = null;
let _last_notify_sent_at = 0;        // browser-notification debounce

const els = {
  banner:       null,
  icon:         null,
  text:         null,
  actions:      null,
  dismissBtn:   null,
};

// ── Public API ─────────────────────────────────────────────────────────────

export function initReadinessGuard() {
  els.banner     = document.getElementById("readiness-banner");
  els.icon       = document.getElementById("readiness-banner-icon");
  els.text       = document.getElementById("readiness-banner-text");
  els.actions    = document.getElementById("readiness-banner-actions");
  els.dismissBtn = document.getElementById("readiness-banner-dismiss");
  if (!els.banner) return;

  els.dismissBtn.addEventListener("click", () => {
    const stage = els.banner.dataset.stage || "unknown";
    const severity = els.banner.dataset.severity || "info";
    const message = els.text.textContent || "";
    _persistDismissal(stage);
    _hideBanner();
    _emitEvent({ severity, stage, action: "user_dismissed", message });
  });

  // v7.16.1 (2026-05-07 fix): notifications + sound now go through the
  // existing notifications.js infrastructure (`sendBrowserNotification`,
  // `playSound`) so they respect the app-level 🔔 toggle. We do NOT
  // request permission here — initNotifications() in main.js handles
  // that path on the toggle click. If the user has notifications OFF,
  // urgent/blocking readiness states still surface via the banner +
  // page-title pulse — they just don't fire OS-level notifs/beeps.

  pollOnce();
  if (_poll_handle == null) {
    _poll_handle = setInterval(pollOnce, POLL_INTERVAL_MS);
  }
}

// ── Polling cycle ──────────────────────────────────────────────────────────

async function pollOnce() {
  // Layer 1: heartbeat. If /health doesn't answer in 5s, the banner
  // goes red regardless of any cached state.
  const healthOk = await _checkHealth();
  if (!healthOk) {
    _renderBackendDown();
    return;
  }

  // Layer 2: full readiness state.
  try {
    const res = await fetch(`${API_BASE_URL}/trading/readiness`, {
      credentials: "include",
    });
    if (!res.ok) {
      // /trading/readiness should never fail with 4xx/5xx since it's
      // unauth and idempotent. If it does, treat as a soft warning.
      _renderSoftError(`Readiness endpoint returned HTTP ${res.status}`);
      return;
    }
    const state = await res.json();
    _renderState(state);
  } catch (err) {
    _renderSoftError(`Readiness fetch failed: ${err.message || err}`);
  }
}

async function _checkHealth() {
  // Native AbortController — no library needed.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT_MS);
  try {
    const r = await fetch(`${API_BASE_URL}/health`, {
      method: "GET",
      signal: ctrl.signal,
      // Don't send credentials — /health is unauthenticated and we
      // want it to be the cheapest possible probe.
    });
    return r.ok;
  } catch (e) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// ── Render: Backend down (Layer-1 alert) ───────────────────────────────────

function _renderBackendDown() {
  if (!els.banner) return;
  const stage = "backend_unreachable";
  const severity = "blocking";

  els.banner.dataset.severity = severity;
  els.banner.dataset.stage = stage;
  _setClasses("blocking");
  els.banner.classList.remove("readiness-banner-hidden");

  els.icon.textContent = "🚨";
  els.text.innerHTML =
    "<strong>Backend unavailable.</strong> The trading server isn't responding to /health within 5 seconds. " +
    "Setup Watch / Dynamic Stocks may be showing stale data. " +
    "Restart uvicorn to recover.";
  els.actions.innerHTML = "";

  if (_last_stage !== stage) {
    _emitEvent({
      severity, stage, action: "frontend_alert",
      message: "/health timed out from frontend heartbeat.",
    });
    _maybeNotify(severity, "Backend unavailable", "Trading server not responding to /health.");
  }
  _last_severity = severity;
  _last_stage = stage;
}

function _renderSoftError(msg) {
  if (!els.banner) return;
  els.banner.dataset.severity = "warn";
  els.banner.dataset.stage = "frontend_soft_error";
  _setClasses("warn");
  els.banner.classList.remove("readiness-banner-hidden");
  els.icon.textContent = "⚠";
  els.text.innerHTML = `<strong>Readiness check error:</strong> ${esc(msg)}`;
  els.actions.innerHTML = "";
}

// ── Render: Layer-2 readiness state ────────────────────────────────────────

function _renderState(state) {
  if (!els.banner) return;
  const { severity, stage, message, actions = [], notify } = state;

  // OK = hide. The user has nothing actionable; we don't burn screen
  // real estate on green chrome.
  if (severity === "ok") {
    _hideBanner();
    _last_severity = "ok";
    _last_stage = stage;
    return;
  }

  // Has the user already dismissed this stage today? Don't re-surface
  // (unless severity climbs).
  if (_isDismissed(stage) && _severityRank(severity) <= _severityRank(_last_severity)) {
    _hideBanner();
    return;
  }

  els.banner.dataset.severity = severity;
  els.banner.dataset.stage = stage;
  _setClasses(severity);
  els.banner.classList.remove("readiness-banner-hidden");

  els.icon.textContent = _iconFor(severity);
  els.text.innerHTML = esc(message);

  // CTA buttons
  els.actions.innerHTML = "";
  for (const action of actions) {
    if (action === "dismiss") continue;   // dismiss is its own button
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "readiness-banner-cta";
    btn.textContent = _ctaLabel(action);
    btn.addEventListener("click", () => _handleCta(action, state));
    els.actions.appendChild(btn);
  }

  // Browser notification (debounced) for urgent / blocking only when
  // the severity has actually climbed since the last alert.
  if (notify && _severityRank(severity) > _severityRank(_last_severity)) {
    _maybeNotify(severity, _titleFor(severity), message);
  }

  // Audit-log every severity transition.
  if (_last_stage !== stage || _last_severity !== severity) {
    _emitEvent({ severity, stage, action: "frontend_alert", message });
  }

  _last_severity = severity;
  _last_stage = stage;
}

function _hideBanner() {
  if (!els.banner) return;
  els.banner.classList.add("readiness-banner-hidden");
}

function _setClasses(severity) {
  els.banner.classList.remove(
    "readiness-banner-info",
    "readiness-banner-warn",
    "readiness-banner-urgent",
    "readiness-banner-blocking",
  );
  els.banner.classList.add(`readiness-banner-${severity}`);
}

// ── CTA dispatch ───────────────────────────────────────────────────────────

function _handleCta(action, state) {
  if (action === "start_autotrade") {
    // Defer to existing autotrade controls — don't duplicate state.
    const toggle = document.getElementById("autotrade-toggle");
    if (toggle) {
      toggle.click();
      _emitEvent({
        severity: state.severity, stage: state.stage,
        action: "user_acted",
        message: "Clicked Start AI Trading from readiness banner.",
      });
    }
  } else if (action === "apply_morning_universe") {
    const apply = document.getElementById("morning-apply-btn");
    if (apply) {
      apply.click();
      _emitEvent({
        severity: state.severity, stage: state.stage,
        action: "user_acted",
        message: "Clicked Apply Universe from readiness banner.",
      });
    }
  }
}

function _ctaLabel(action) {
  switch (action) {
    case "start_autotrade":        return "▶ Start AI Trading";
    case "apply_morning_universe": return "Apply universe";
    default:                       return action;
  }
}

// ── Browser notification ───────────────────────────────────────────────────

function _maybeNotify(severity, title, body) {
  // 2-min debounce so a poll cycle that fluctuates between urgent/ok
  // doesn't spam the user.
  const now = Date.now();
  if (now - _last_notify_sent_at < 120_000) return;
  _last_notify_sent_at = now;

  // Both helpers below short-circuit when the app-level 🔔 toggle is
  // OFF (`localStorage.tradehack_notif === "off"`), so this respects
  // the user's existing notification preference. The visual banner
  // is independent of the toggle and always shows.
  sendBrowserNotification(title, body);
  if (severity === "blocking") {
    playSound("danger");
  } else if (severity === "urgent") {
    playSound("warning");
  }
}

// ── Audit event POST ───────────────────────────────────────────────────────

async function _emitEvent({ severity, stage, action, message }) {
  try {
    await fetch(`${API_BASE_URL}/trading/readiness/event`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ severity, stage, action, message: message || "" }),
    });
  } catch (e) {
    // Auth failures pre-login are expected. Silent fallback.
  }
}

// ── Per-day dismissal persistence ──────────────────────────────────────────

function _persistDismissal(stage) {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const raw = localStorage.getItem(STORAGE_KEY);
    let data = {};
    try { data = JSON.parse(raw) || {}; } catch (e) {}
    if (data.date !== today) data = { date: today, stages: [] };
    if (!data.stages.includes(stage)) data.stages.push(stage);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch (e) {}
}

function _isDismissed(stage) {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return false;
    const data = JSON.parse(raw);
    if (data.date !== today) return false;
    return (data.stages || []).includes(stage);
  } catch (e) {
    return false;
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

function _severityRank(s) {
  return { ok: 0, info: 1, warn: 2, urgent: 3, blocking: 4 }[s || ""] || 0;
}

function _iconFor(s) {
  return { info: "ℹ", warn: "⚠", urgent: "⚠", blocking: "🚨" }[s] || "●";
}

function _titleFor(s) {
  return s === "blocking" ? "AI Trading is OFF" : "Trading window approaching";
}

