/**
 * staleness.js — nudges the operator to reload long-running tabs.
 *
 * Why this exists: the dashboard accumulates chart data, WebSocket
 * buffers, and forming-setup history while open. After ~24h the
 * browser content process can OOM and crash the tab — observed on
 * Firefox 2026-05-28 with the symptom "page won't reload, not even
 * with hard refresh." This banner appears once the page has been
 * open >24h, suggesting a reload before the browser crashes.
 *
 * Zero risk to the trading loop: this is purely a frontend nudge,
 * no fetch calls, no backend interaction. Dismissing hides it for
 * the rest of the session; the next reload starts the timer fresh.
 */

const STALENESS_THRESHOLD_MS = 24 * 60 * 60 * 1000;  // 24h
const CHECK_INTERVAL_MS      = 30 * 60 * 1000;       // every 30 min

const PAGE_LOADED_AT = Date.now();
let _dismissed = false;

function showBanner() {
  if (_dismissed) return;
  const el = document.getElementById("staleness-banner");
  if (!el) return;
  const hoursOpen = Math.floor((Date.now() - PAGE_LOADED_AT) / (60 * 60 * 1000));
  const textEl = document.getElementById("staleness-banner-text");
  if (textEl) {
    textEl.innerHTML =
      `Tab open for <strong>${hoursOpen}+ hours</strong> — long-running browser tabs ` +
      `can crash on memory. Reload to keep things snappy.`;
  }
  el.classList.remove("readiness-banner-hidden");
}

function hideBanner() {
  const el = document.getElementById("staleness-banner");
  if (el) el.classList.add("readiness-banner-hidden");
}

export function initStalenessMonitor() {
  // Wire button handlers once. Safe if the banner div is missing
  // (e.g. on login.html) — every getElementById returns null and
  // the addEventListener calls are skipped.
  const reloadBtn  = document.getElementById("staleness-banner-reload");
  const dismissBtn = document.getElementById("staleness-banner-dismiss");
  if (reloadBtn) {
    reloadBtn.addEventListener("click", () => window.location.reload());
  }
  if (dismissBtn) {
    dismissBtn.addEventListener("click", () => {
      _dismissed = true;
      hideBanner();
    });
  }

  // Check on a slow timer — 30 min is plenty for a 24h trigger and
  // doesn't burn CPU. First check fires after the interval (not
  // immediately), so a freshly-reloaded tab is never nagged.
  setInterval(() => {
    if (Date.now() - PAGE_LOADED_AT >= STALENESS_THRESHOLD_MS) {
      showBanner();
    }
  }, CHECK_INTERVAL_MS);
}
