// ── Notifications, toasts, sounds, browser alerts ──────────────────────────
// Self-contained module: owns the notif-toggle button, audio context, toast
// container, and the "fire a notification for this event" dispatch logic.
//
// main.js drives it via:
//   initNotifications()              — wire up the toggle button
//   notifyTradeEvent(eventData)      — fire a notification for a trade event
//   notifyMonitorEvent(eventData)    — fire a notification for a monitor event

import { esc } from "./utils.js";

const notifToggle    = document.getElementById("notif-toggle");
const notifIcon      = document.getElementById("notif-icon");
const toastContainer = document.getElementById("toast-container");

let notificationsEnabled = localStorage.getItem("tradehack_notif") !== "off";

// v7.16.x (2026-05-21) — autonomous-mode mirror. main.js calls
// setAutonomousMode() from renderAutotradeStatus on every status update,
// so this stays current. The "approval needed" alert (5× bell + toast)
// is suppressed when autonomous mode is ON: in that mode the trade
// auto-approves immediately, so an "approval needed" alert would be
// wrong — the operator wants silence.
let _autonomousMode = false;
export function setAutonomousMode(on) {
  _autonomousMode = !!on;
}

function updateNotifToggle() {
  if (!notifToggle) return;
  notifToggle.classList.toggle("active", notificationsEnabled);
  if (notifIcon) notifIcon.textContent = notificationsEnabled ? "🔔" : "🔕";
}

export function initNotifications() {
  updateNotifToggle();
  if (notifToggle) {
    notifToggle.addEventListener("click", async () => {
      notificationsEnabled = !notificationsEnabled;
      localStorage.setItem("tradehack_notif", notificationsEnabled ? "on" : "off");
      updateNotifToggle();
      if (notificationsEnabled && "Notification" in window && Notification.permission === "default") {
        await Notification.requestPermission();
      }
    });
  }
}

// ── Web Audio beep synthesis (no external files) ───────────────────────────
let _audioCtx = null;
function getAudioCtx() {
  if (!_audioCtx) {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  return _audioCtx;
}

/**
 * Play a short synthesized tone.
 * @param {"success"|"warning"|"danger"|"info"} type
 */
export function playSound(type = "info") {
  if (!notificationsEnabled) return;
  try {
    const ctx = getAudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);

    const now = ctx.currentTime;
    gain.gain.setValueAtTime(0.15, now);

    switch (type) {
      case "success":
        osc.type = "sine";
        osc.frequency.setValueAtTime(660, now);
        osc.frequency.setValueAtTime(880, now + 0.12);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
        osc.start(now);
        osc.stop(now + 0.35);
        break;
      case "warning":
        osc.type = "triangle";
        osc.frequency.setValueAtTime(520, now);
        osc.frequency.setValueAtTime(440, now + 0.15);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.4);
        osc.start(now);
        osc.stop(now + 0.4);
        break;
      case "danger":
        osc.type = "sawtooth";
        osc.frequency.setValueAtTime(220, now);
        gain.gain.setValueAtTime(0.12, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.5);
        osc.start(now);
        osc.stop(now + 0.5);
        break;
      default:
        osc.type = "sine";
        osc.frequency.setValueAtTime(740, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.2);
        osc.start(now);
        osc.stop(now + 0.2);
    }
  } catch {
    // AudioContext may be blocked until user interaction — silently ignore
  }
}

/**
 * Play a bell-like ring `times` times, spaced ~0.62s apart (v7.16.x).
 * Used for the manual-mode "approval needed" alert — longer + more
 * insistent than the single playSound() beep so a card that needs the
 * operator's action isn't missed. ~3 seconds total for the default 5.
 * @param {number} times
 */
export function playRepeatingBell(times = 5) {
  if (!notificationsEnabled) return;
  try {
    const ctx = getAudioCtx();
    // AudioContext starts suspended until a user gesture. The operator
    // has clicked buttons by the time a match fires, so resume() should
    // succeed — best-effort, ignored if it can't.
    if (ctx.state === "suspended") ctx.resume();
    const interval = 0.62;  // seconds between rings
    for (let i = 0; i < times; i++) {
      const t0 = ctx.currentTime + i * interval;
      // Bell timbre: a fundamental + a higher partial, sharp attack,
      // exponential decay. Two oscillators scheduled per ring.
      [[880, 0.28], [1760, 0.10]].forEach(([freq, peak]) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, t0);
        gain.gain.setValueAtTime(0.0001, t0);
        gain.gain.exponentialRampToValueAtTime(peak, t0 + 0.008);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.55);
        osc.start(t0);
        osc.stop(t0 + 0.6);
      });
    }
  } catch {
    // AudioContext blocked / unavailable — silently ignore.
  }
}

// ── Toast in-app notifications ─────────────────────────────────────────────
/**
 * Show a toast notification in the top-right corner.
 * @param {Object} opts
 * @param {string} opts.title
 * @param {string} [opts.message]
 * @param {"success"|"warning"|"danger"|"info"} [opts.variant]
 * @param {string} [opts.icon]
 * @param {number} [opts.duration]  ms before auto-dismiss (default 5000)
 */
export function showToast({ title, message = "", variant = "info", icon = "ℹ️", duration = 5000 }) {
  if (!toastContainer) return;
  const el = document.createElement("div");
  el.className = `toast toast-${variant}`;
  el.innerHTML = `
    <span class="toast-icon">${esc(icon)}</span>
    <div class="toast-body">
      <div class="toast-title">${esc(title)}</div>
      ${message ? `<div class="toast-msg">${esc(message)}</div>` : ""}
    </div>
    <button class="toast-close" type="button">✕</button>`;
  toastContainer.appendChild(el);

  const dismiss = () => {
    el.classList.add("toast-exit");
    el.addEventListener("animationend", () => el.remove());
  };
  el.querySelector(".toast-close").addEventListener("click", dismiss);
  if (duration > 0) setTimeout(dismiss, duration);

  while (toastContainer.children.length > 5) {
    toastContainer.firstElementChild.remove();
  }
}

// ── Browser (OS-level) notification ────────────────────────────────────────
export function sendBrowserNotification(title, body) {
  if (!notificationsEnabled) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    new Notification(title, {
      body,
      icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><text y='24' font-size='24'>📈</text></svg>",
      tag: "tradehack-" + Date.now(),
      silent: true,
    });
  } catch { /* silent */ }
}

// ── Unified trade notification dispatcher ──────────────────────────────────
export function notifyTradeEvent(data) {
  if (!notificationsEnabled) return;
  const event = data?.event;
  if (!event) return;

  switch (event) {
    case "order_placed": {
      const sym = data.symbol || "???";
      const side = (data.side || "").toUpperCase();
      const qty = data.qty || "";
      playSound("success");
      showToast({
        title: `Trade Executed: ${side} ${sym}`,
        message: `${qty} share${qty !== 1 ? "s" : ""} at market`,
        variant: "success",
        icon: side === "BUY" ? "🟢" : "🔴",
      });
      sendBrowserNotification(
        `Trade Executed: ${side} ${sym}`,
        `${qty} share${qty !== 1 ? "s" : ""} submitted at market`,
      );
      break;
    }

    case "approval_requested": {
      // v7.16.x — manual-mode affordance only. When autonomous mode is
      // ON the trade auto-approves immediately, so the "approval needed"
      // alert (bell + toast) would be misleading. Stay fully silent and
      // let the subsequent order_placed event do the talking.
      if (_autonomousMode) break;
      const sym = data.symbol || "???";
      const conf = data.confidence ? `${(data.confidence * 100).toFixed(0)}%` : "";
      // v7.16.x — 5× repeating bell (was a single playSound("warning")
      // beep, which the operator reported missing). Manual mode only.
      playRepeatingBell(5);
      showToast({
        title: `Approval Needed: ${sym}`,
        message: `AI wants to trade ${sym}${conf ? ` (${conf} confidence)` : ""}. Review now!`,
        variant: "warning",
        icon: "⚡",
        duration: 10000,
      });
      sendBrowserNotification(
        `Trade Approval Needed: ${sym}`,
        `AI recommends trading ${sym}${conf ? ` with ${conf} confidence` : ""}. Open TradeHack to approve.`,
      );
      break;
    }

    case "circuit_breaker": {
      const pl = data.daily_pl != null ? `$${Math.abs(data.daily_pl).toFixed(2)}` : "";
      playSound("danger");
      showToast({
        title: "Circuit Breaker Tripped",
        message: `Daily loss limit reached${pl ? ` (${pl} loss)` : ""}. Auto-trading paused.`,
        variant: "danger",
        icon: "🛑",
        duration: 15000,
      });
      sendBrowserNotification(
        "Circuit Breaker — Trading Paused",
        `Daily loss limit reached${pl ? ` (${pl} loss)` : ""}. Auto-trading has been paused.`,
      );
      break;
    }

    case "order_failed": {
      const sym = data.symbol || "???";
      const errMsg = data.error || "";
      const slipMatch = errMsg.match(/entry zone \$?([\d.]+)[–-]\$?([\d.]+)/);
      if (slipMatch) {
        playSound("warning");
        showToast({
          title: `Trade Timed Out: ${sym}`,
          message: `Price change was above entry zone $${slipMatch[1]} to $${slipMatch[2]}`,
          variant: "warning",
          icon: "⏱️",
          duration: 10000,
        });
      } else {
        playSound("danger");
        showToast({
          title: `Order Failed: ${sym}`,
          message: errMsg || "Order could not be placed",
          variant: "danger",
          icon: "❌",
        });
      }
      break;
    }

    case "confirming": {
      const sym = data.symbol || "???";
      const dir = data.direction || "";
      playSound("info");
      showToast({
        title: `Confirming: ${sym}`,
        message: `AI detected ${dir} setup — watching for price confirmation…`,
        variant: "info",
        icon: "👁️",
        duration: 4000,
      });
      break;
    }

    case "confirmation_rejected": {
      const sym = data.symbol || "???";
      showToast({
        title: `Confirmation Failed: ${sym}`,
        message: `Price moved ${data.move_pct != null ? data.move_pct.toFixed(2) + "%" : "unfavorably"} — trade skipped`,
        variant: "info",
        icon: "⏭️",
        duration: 4000,
      });
      break;
    }

    case "started":
      playSound("info");
      showToast({ title: "AI Trading Started", message: "Auto-trader is now scanning for setups", variant: "info", icon: "🤖", duration: 3000 });
      break;

    case "stopped":
      showToast({ title: "AI Trading Stopped", variant: "info", icon: "⏹️", duration: 3000 });
      break;
  }
}

export function notifyMonitorEvent(data) {
  if (!notificationsEnabled) return;
  const event = data?.event;
  if (!event) return;

  if (event === "position_closed") {
    const sym = data.symbol || "???";
    const reason = data.reason || "unknown";
    const pnl = data.pnl;
    const pnlStr = pnl != null ? `$${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "";
    const isProfit = pnl != null && pnl >= 0;

    playSound(isProfit ? "success" : "warning");
    showToast({
      title: `Position Closed: ${sym}`,
      message: `${reason}${pnlStr ? ` — P&L: ${pnlStr}` : ""}`,
      variant: isProfit ? "success" : "warning",
      icon: isProfit ? "💰" : "📉",
    });
    sendBrowserNotification(
      `Position Closed: ${sym}`,
      `${reason}${pnlStr ? ` — P&L: ${pnlStr}` : ""}`,
    );
  } else if (event === "close_failed") {
    playSound("danger");
    showToast({
      title: `Close Failed: ${data.symbol || "???"}`,
      message: data.error || "Could not close position",
      variant: "danger",
      icon: "⚠️",
    });
  }
}
