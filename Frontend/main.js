import { API_BASE_URL } from "./config.js";
import {
  createEnhancedChart, addCandleSeries, addVolumeSeries,
  addEMAOverlays, addCurrentPriceLine, setupLegend, setupZoomControls,
  setupFreePan, getChartTimeZoneLabel,
} from "./chart-helpers.js?v=2";
import { esc, fmtUSD, fmtTime, normalizeSetup } from "./utils.js";
import {
  initNotifications,
  notifyTradeEvent, notifyMonitorEvent,
  showToast,
  setAutonomousMode,
} from "./notifications.js";
import { initWebSocket, isWsConnected } from "./ws.js";
import { initLessons, loadLessons, setRecentTrades } from "./lessons.js";
import { initStudy, loadStudyInsights } from "./study.js";
import { initReadinessGuard } from "./readiness.js";
import { initStalenessMonitor } from "./staleness.js";

// ── Sound & Browser Notification System ─────────────────────────────────────
initNotifications();

// ── Event handlers (called from WebSocket message router) ───────────────────
function handleTradeEvent(data) {
  const event = data?.event;
  // Always refresh status for approval/circuit events
  if (event === "approval_requested" || event === "circuit_breaker") {
    fetchAutotradeStatus();
  }
  // Fire notification
  notifyTradeEvent(data);
}

function handleMonitorEvent(data) {
  const event = data?.event;
  if (event === "position_closed" || event === "close_failed") {
    loadWallet();
  }
  // Fire notification
  notifyMonitorEvent(data);
}

const _SESSION_LABELS = {
  open: "● Market Open",
  pre_market: "● Pre-Market",
  after_hours: "● After-Hours",
  closed: "● Market Closed",
};
const _SESSION_CLASS = {
  open: "open",
  pre_market: "extended",
  after_hours: "extended",
  closed: "closed",
};

function renderMarketPulse(data) {
  if (!data) return;
  if (marketBadge) {
    const session = data.market_session || (data.market_open ? "open" : "closed");
    marketBadge.textContent = _SESSION_LABELS[session] || "● Market Closed";
    marketBadge.className = `market-badge ${_SESSION_CLASS[session] || "closed"}`;
  }
  if (pulseUpdated) {
    pulseUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }
  if (data.pulses && data.pulses.length > 0) {
    // v7.16.9 (2026-05-12): sort alphabetically by symbol for consistent
    // ordering across refreshes. Server returns universe-order which can
    // shift if the applied universe is rebuilt mid-session.
    const sorted = [...data.pulses].sort(
      (a, b) => (a.symbol || "").localeCompare(b.symbol || ""),
    );
    pulseStrip.innerHTML = sorted.map(renderPulseCard).join("");
    requestAnimationFrame(drawPulseSparklines);
  }
}

// ── Market Pulse ──────────────────────────────────────────────────────────────
const pulseStrip      = document.getElementById("pulse-strip");
const marketBadge     = document.getElementById("market-badge");
const pulseUpdated    = document.getElementById("pulse-updated");
const pulseRefreshBtn = document.getElementById("pulse-refresh-btn");
let pulseInterval     = null;

function renderPulseCard(p) {
  const sym = esc(p.symbol);
  // No data placeholder (market closed, new symbol, etc.)
  if (p.has_data === false) {
    return `
    <div class="pulse-card pulse-card-nodata" data-symbol="${sym}" role="button" tabindex="0" title="${sym} — no recent data">
      <div class="pulse-card-symbol">${sym}</div>
      <div class="pulse-card-price muted-copy">—</div>
      <div class="pulse-card-change muted-copy">No data</div>
    </div>`;
  }
  const sign      = p.change_pct >= 0 ? "+" : "";
  const colorCls  = p.direction === "up" ? "positive" : p.direction === "down" ? "negative" : "muted-copy";
  const arrow     = p.direction === "up" ? "▲" : p.direction === "down" ? "▼" : "●";
  // JSON-stringified sparkline is stuffed into a single-quoted attribute — esc()
  // neutralizes any stray ' or < that might appear if the server ever returns
  // non-numeric values in the array.
  const sparkAttr = esc(JSON.stringify(p.sparkline || []));
  return `
    <div class="pulse-card" data-symbol="${sym}" role="button" tabindex="0" title="Click to view ${sym} chart">
      <div class="pulse-card-symbol">${sym}</div>
      <div class="pulse-card-price">$${p.price.toFixed(2)}</div>
      <div class="pulse-card-change ${colorCls}">${arrow} ${sign}${p.change_pct.toFixed(2)}%</div>
      <div class="pulse-card-vol muted-copy">${p.relative_volume.toFixed(1)}x vol</div>
      <canvas class="pulse-sparkline" data-points='${sparkAttr}' data-direction="${esc(p.direction)}"></canvas>
    </div>`;
}

/**
 * Draw sparkline area charts on all pulse card canvases.
 * Called after innerHTML is set so the canvas elements exist in the DOM.
 */
function drawPulseSparklines() {
  document.querySelectorAll(".pulse-sparkline").forEach((canvas) => {
    const points = JSON.parse(canvas.dataset.points || "[]");
    if (points.length < 2) { canvas.style.display = "none"; return; }

    const dir = canvas.dataset.direction;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width  = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const min = Math.min(...points);
    const max = Math.max(...points);
    const range = max - min || 1;

    // Pick color based on direction
    const lineColor = dir === "up" ? "#00d97e" : dir === "down" ? "#ff4d5a" : "#636d80";
    const fillColor = dir === "up" ? "rgba(0,217,126,0.10)" : dir === "down" ? "rgba(255,77,90,0.10)" : "rgba(99,109,128,0.06)";

    const stepX = w / (points.length - 1);
    const pad = 2; // vertical padding
    const plotH = h - pad * 2;

    // Draw area fill
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let i = 0; i < points.length; i++) {
      const x = i * stepX;
      const y = pad + plotH - ((points[i] - min) / range) * plotH;
      if (i === 0) ctx.lineTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();

    // Draw line
    ctx.beginPath();
    for (let i = 0; i < points.length; i++) {
      const x = i * stepX;
      const y = pad + plotH - ((points[i] - min) / range) * plotH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });
}

async function loadMarketPulse() {
  if (pulseRefreshBtn) pulseRefreshBtn.disabled = true;
  try {
    const res = await fetch(`${API_BASE_URL}/market/pulse`, { credentials: "include" });

    if (res.status === 503) {
      pulseStrip.innerHTML = `
        <div class="pulse-unconfigured">
          <strong>Alpaca not configured</strong>
          <p class="muted-copy">Add your API key to the backend <code>.env</code> file to enable live market data.</p>
        </div>`;
      if (marketBadge) { marketBadge.textContent = "No API key"; marketBadge.className = "market-badge closed"; }
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = await res.json();

    if (marketBadge) {
      const session = data.market_session || (data.market_open ? "open" : "closed");
      marketBadge.textContent = _SESSION_LABELS[session] || "● Market Closed";
      marketBadge.className   = `market-badge ${_SESSION_CLASS[session] || "closed"}`;
    }
    if (pulseUpdated) {
      pulseUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    if (!data.pulses.length) {
      pulseStrip.innerHTML = `<div class="pulse-loading muted-copy">No market data returned. The market may be closed.</div>`;
      return;
    }

    // v7.16.9 (2026-05-12): sort alphabetically by symbol — same as the
    // dashboard-load path above. Keeps card order stable across refreshes.
    const sortedPulses = [...data.pulses].sort(
      (a, b) => (a.symbol || "").localeCompare(b.symbol || ""),
    );
    pulseStrip.innerHTML = sortedPulses.map(renderPulseCard).join("");
    requestAnimationFrame(drawPulseSparklines);
  } catch (err) {
    pulseStrip.innerHTML = `<div class="pulse-loading muted-copy">Could not load market data — ${esc(err.message)}</div>`;
  } finally {
    if (pulseRefreshBtn) pulseRefreshBtn.disabled = false;
  }
}

if (pulseRefreshBtn) {
  pulseRefreshBtn.addEventListener("click", loadMarketPulse);
}

const logoutButton    = document.getElementById("logout-btn");
const sessionUserName  = document.getElementById("session-user-name");
const sessionUserEmail = document.getElementById("session-user-email");

async function fetchSession() {
  const response = await fetch(`${API_BASE_URL}/auth/me`, { credentials: "include" });
  if (!response.ok) throw new Error("Unauthenticated");
  return response.json();
}

async function renderSession() {
  try {
    const session = await fetchSession();
    sessionUserName.textContent  = session.user.name;
    sessionUserEmail.textContent = session.user.is_operator === false
      ? `${session.user.email} · view-only (not an operator)`
      : session.user.email;
    return session;
  } catch {
    window.location.href = "./login.html";
    return null;
  }
}

if (logoutButton) {
  logoutButton.addEventListener("click", async () => {
    await fetch(`${API_BASE_URL}/auth/logout`, { method: "POST", credentials: "include" });
    window.location.href = "./index.html";
  });
}

// v7.16.11 (2026-05-12): wall-clock-aligned refresh — fire at 12:00:00,
// 12:01:00, 12:02:00 etc. rather than drifting from page-load time.
// Aligning to bar boundaries also means fresher data: each fetch happens
// right after a 1-min Alpaca bar closes, not somewhere mid-bar.
function scheduleMinuteAligned(fn, opts = {}) {
  const offsetMs = opts.offsetMs || 0;
  const tick = () => { try { fn(); } catch (e) { console.error(e); } };
  const msUntilNext = 60_000 - (Date.now() % 60_000) + offsetMs;
  const handles = { timeoutId: null, intervalId: null };
  handles.timeoutId = setTimeout(() => {
    handles.timeoutId = null;
    tick();
    handles.intervalId = setInterval(tick, 60_000);
  }, msUntilNext);
  handles.cancel = () => {
    if (handles.timeoutId) clearTimeout(handles.timeoutId);
    if (handles.intervalId) clearInterval(handles.intervalId);
    handles.timeoutId = null;
    handles.intervalId = null;
  };
  return handles;
}

renderSession().then(() => {
  loadMarketPulse();
  // Aligns to wall-clock minute boundaries: 12:00:00, 12:01:00, ...
  // The handle stays in pulseInterval-shaped form so the existing
  // clearInterval(pulseInterval) call site (mode-flip flow) still works
  // by calling the .cancel() method below.
  pulseInterval = scheduleMinuteAligned(loadMarketPulse);
  loadWallet();
  initAutotrade();
});

// ── Candlestick Chart Modal ───────────────────────────────────────────────────
const chartModal         = document.getElementById("chart-modal");
const chartModalSymbol   = document.getElementById("chart-modal-symbol");
const chartModalEyebrow  = document.getElementById("chart-modal-eyebrow");
const chartStatus        = document.getElementById("chart-status");
const chartContainer     = document.getElementById("chart-container");
const chartCloseBtn      = document.getElementById("chart-close-btn");
const modalTfBtnGroup    = document.getElementById("modal-tf-btn-group");
let chartInstance        = null;
let chartResizeObserver  = null;
let currentChartSymbol   = null;
let modalTimeframe       = "1Min";

const _MODAL_TF_META = {
  "1Min":  { label: "1min" },
  "5Min":  { label: "5min" },
  "15Min": { label: "15min" },
};

function updateModalEyebrow() {
  const m = _MODAL_TF_META[modalTimeframe] || _MODAL_TF_META["1Min"];
  if (chartModalEyebrow) chartModalEyebrow.textContent = `${m.label} · ${getChartTimeZoneLabel()}`;
}

function openChartModal(symbol) {
  currentChartSymbol = symbol;
  modalTimeframe = "1Min";
  chartModalSymbol.textContent = symbol;
  chartStatus.textContent = "Loading chart data…";
  chartContainer.innerHTML = "";
  chartModal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  analyzeBtn.disabled = true;
  updateModalEyebrow();

  // Reset TF button active state
  if (modalTfBtnGroup) {
    modalTfBtnGroup.querySelectorAll(".tf-btn").forEach((b) => {
      b.classList.toggle("tf-btn-active", b.dataset.tf === "1Min");
    });
  }

  if (chartInstance) {
    chartInstance.remove();
    chartInstance = null;
  }
  if (chartResizeObserver) {
    chartResizeObserver.disconnect();
    chartResizeObserver = null;
  }

  // Double rAF: lets the browser finish laying out the (now-visible) modal so
  // chartContainer.clientWidth returns its real pixel width, not 0.
  requestAnimationFrame(() => requestAnimationFrame(() => loadChart(symbol, modalTimeframe)));
}

function closeChartModal() {
  chartModal.classList.add("hidden");
  document.body.style.overflow = "";
  currentChartSymbol = null;
  analyzeBtn.disabled = true;
  if (chartInstance) {
    chartInstance.remove();
    chartInstance = null;
  }
}

// v7.15.2 (2026-05-05): expose openChartModal on the global window so
// other modules can request a chart pop-out without duplicating the
// lightweight-charts setup. Used by:
//   * morning-universe.js — Dynamic Stocks card click → bigger chart view
//   * (future) any panel that surfaces a symbol the operator might
//     want to drill into
// The fallback path in those modules synthesizes a click on a
// .pulse-card[data-symbol] element, but that's brittle (relies on the
// pulse-strip being mounted). Direct exposure is the cleanest contract.
window.openChartModal = openChartModal;

async function loadChart(symbol, tf) {
  tf = tf || modalTimeframe;
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 15_000);
  try {
    if (typeof LightweightCharts === "undefined") {
      throw new Error("chart library did not load; check your internet connection and refresh");
    }

    const res = await fetch(`${API_BASE_URL}/market/chart/${symbol}?timeframe=${tf}`, {
      credentials: "include",
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (!data.bars.length) {
      chartStatus.textContent = "No bar data available — the market may be closed.";
      return;
    }

    chartStatus.textContent = "";

    // Use explicit pixel dimensions — by the time we reach here (after double rAF)
    // the browser has laid out the modal and clientWidth is reliable.
    const chartW = chartContainer.clientWidth || 860;
    const chartH = chartContainer.clientHeight || (window.innerWidth <= 480 ? 260 : window.innerWidth <= 760 ? 300 : 420);

    // ── Enhanced chart ──────────────────────────────────────────────────
    chartInstance = createEnhancedChart(chartContainer, symbol, chartW, chartH);

    // Candlestick + volume + EMA overlays
    const candleSeries = addCandleSeries(chartInstance, data.bars);
    const volSeries    = addVolumeSeries(chartInstance, data.bars);
    addEMAOverlays(chartInstance, data.bars);
    addCurrentPriceLine(candleSeries, data.bars);

    chartInstance.timeScale().fitContent();

    // OHLCV legend (top-left) + zoom controls (top-right) + free 2D panning
    setupLegend(chartContainer, data.bars, candleSeries, volSeries, chartInstance, symbol);
    const resetVertical = setupFreePan(chartContainer, chartInstance);
    setupZoomControls(chartContainer, chartInstance, resetVertical);

    // Enable AI analysis now that we have chart data
    analyzeBtn.disabled = false;

    // Keep the chart correctly sized if the user resizes the window
    chartResizeObserver = new ResizeObserver((entries) => {
      if (!chartInstance || !entries[0]) return;
      const { width: w, height: h } = entries[0].contentRect;
      const opts = {};
      if (w > 0) opts.width = Math.floor(w);
      if (h > 0) opts.height = Math.floor(h);
      if (opts.width || opts.height) chartInstance.applyOptions(opts);
    });
    chartResizeObserver.observe(chartContainer);

  } catch (err) {
    const msg = err?.name === "AbortError"
      ? "request timed out after 15s; refresh the page or restart the backend"
      : err.message;
    chartStatus.textContent = `Could not load chart — ${msg}`;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

// ── AI Analysis — navigate to dedicated page ─────────────────────────────────
const analyzeBtn = document.getElementById("analyze-btn");

if (analyzeBtn) {
  analyzeBtn.addEventListener("click", () => {
    if (currentChartSymbol) {
      window.location.href = `./analysis.html?symbol=${encodeURIComponent(currentChartSymbol)}`;
    }
  });
}

// ── Modal timeframe toggle ──────────────────────────────────────────────────
if (modalTfBtnGroup) {
  modalTfBtnGroup.addEventListener("click", async (e) => {
    const btn = e.target.closest(".tf-btn");
    if (!btn || btn.classList.contains("tf-btn-active") || !currentChartSymbol) return;
    const tf = btn.dataset.tf;
    if (!tf) return;

    modalTfBtnGroup.querySelectorAll(".tf-btn").forEach((b) => b.classList.remove("tf-btn-active"));
    btn.classList.add("tf-btn-active", "tf-btn-loading");

    modalTimeframe = tf;
    updateModalEyebrow();

    // Destroy and rebuild chart for new timeframe
    if (chartInstance) {
      chartInstance.remove();
      chartInstance = null;
    }
    chartStatus.textContent = "Loading chart data…";
    chartContainer.innerHTML = "";

    await loadChart(currentChartSymbol, tf);
    btn.classList.remove("tf-btn-loading");
  });
}

// Delegated click/keyboard on pulse strip
if (pulseStrip) {
  pulseStrip.addEventListener("click", (e) => {
    const card = e.target.closest(".pulse-card[data-symbol]");
    if (card) openChartModal(card.dataset.symbol);
  });
  pulseStrip.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      const card = e.target.closest(".pulse-card[data-symbol]");
      if (card) { e.preventDefault(); openChartModal(card.dataset.symbol); }
    }
  });
}

// Close via button, backdrop click, or Escape
if (chartCloseBtn) chartCloseBtn.addEventListener("click", closeChartModal);
if (chartModal) {
  chartModal.addEventListener("click", (e) => {
    if (e.target === chartModal || e.target.classList.contains("chart-modal-backdrop")) {
      closeChartModal();
    }
  });
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && chartModal && !chartModal.classList.contains("hidden")) closeChartModal();
});

// ── AI Auto-Trade ───────────────────────────────────────────────────────────
const autotradeToggle       = document.getElementById("autotrade-toggle");
const autonomousToggle      = document.getElementById("autonomous-toggle");
const autonomousLabel       = document.getElementById("autonomous-toggle-label");
const approvalBanner        = document.getElementById("approval-banner");
const approvalBannerText    = document.getElementById("approval-banner-text");
const autotradeDot          = document.getElementById("autotrade-dot");
const autotradeLabel        = document.getElementById("autotrade-toggle-label");
const autotradeStatusEl     = document.getElementById("autotrade-status");
const atScans               = document.getElementById("at-scans");
const atScansToday          = document.getElementById("at-scans-today");
const atSetups              = document.getElementById("at-setups");
const atTrades              = document.getElementById("at-trades");
const atDailyPnl            = document.getElementById("at-daily-pnl");

// ── "Today's P&L" tile (v7.16.x, 2026-06-10) ────────────────────────────────
// Two sources, equity preferred:
//   1. Alpaca equity − last_equity (ground truth, includes open positions) —
//      available once the account route passes last_equity through (staged
//      backend change; live after the next boundary restart).
//   2. Fallback: monitor-price P&L from today's closed + open positions in
//      the autotrade status payload. Approximate — the 2026-06-09 fill
//      reconciliation showed monitor prices can drift ~$20 from real fills.
let dailyPnlEquity = null;     // source 1, set by loadAccount()
let dailyPnlPositions = null;  // source 2, set by the status handler

function renderDailyPnl() {
  if (!atDailyPnl) return;
  const v = dailyPnlEquity ?? dailyPnlPositions;
  if (v === null || v === undefined || Number.isNaN(v)) {
    atDailyPnl.textContent = "—";
    atDailyPnl.classList.remove("positive", "negative");
    atDailyPnl.removeAttribute("title");
    return;
  }
  atDailyPnl.textContent = `${v >= 0 ? "+" : "-"}${fmtUSD(Math.abs(v))}`;
  atDailyPnl.classList.toggle("positive", v > 0);
  atDailyPnl.classList.toggle("negative", v < 0);
  atDailyPnl.title = dailyPnlEquity !== null
    ? "Alpaca equity vs prior close (ground truth, includes open positions)"
    : "Monitor-price P&L: today's closed trades + open positions (approximate; fills can differ slightly)";
}

function computePositionsPnl(data) {
  const etDay = (iso) =>
    new Date(iso).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const today = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  let total = 0;
  let any = false;
  for (const p of data.closed_positions || []) {
    if (!p.exit_price || !p.entry_price || !p.qty) continue;
    if (p.exited_at && etDay(p.exited_at) !== today) continue;
    const perShare = p.direction === "short"
      ? p.entry_price - p.exit_price : p.exit_price - p.entry_price;
    total += perShare * p.qty;
    any = true;
  }
  for (const p of data.monitored_positions || []) {
    if (!p.current_price || !p.entry_price || !p.qty) continue;
    const perShare = p.direction === "short"
      ? p.entry_price - p.current_price : p.current_price - p.entry_price;
    total += perShare * p.qty;
    any = true;
  }
  return any ? total : null; // null = no activity today → tile shows "—"
}
const autotradeLog          = document.getElementById("autotrade-log");
const autotradeLogList      = document.getElementById("autotrade-log-list");
const autotradeApprovals    = document.getElementById("autotrade-approvals");
const autotradeApprovalList = document.getElementById("autotrade-approvals-list");
const autotradeMonitor      = document.getElementById("autotrade-monitor");
const autotradeMonitorList  = document.getElementById("autotrade-monitor-list");
const atMonitoring          = document.getElementById("at-monitoring");
const atCache               = document.getElementById("at-cache");
let autotradePolling        = null;
// Approval IDs the user has already approved/rejected. We hide them from the
// UI until the backend finally drops them from pending_approvals, so the card
// doesn't flicker back via polling / WebSocket push.
const _dismissedApprovalIds = new Set();

// Most recent /trading/account snapshot. Populated by loadAccount() and used
// by preTradeBlock() to gate buy buttons before the user can submit an order
// that would trigger a Good Faith Violation (cash account) or Pattern Day
// Trader flag (margin account under $25k equity). The backend enforces the
// same rules; this is the UI mirror so the user never sees a click-then-fail.
let currentAccountState = null;

// Returns { blocked: bool, reason: string } for a prospective trade.
// Paper accounts and missing state are never blocked.
// `openedToday` only matters for sells — it flags a close that completes a
// day-trade (PDT concern) and, combined with unsettled cash, a potential GFV.
function preTradeBlock({ side = "buy", notional = 0, openedToday = false } = {}) {
  const a = currentAccountState;
  if (!a || a.paper) return { blocked: false, reason: "" };

  const isBuy = String(side).toLowerCase() === "buy";

  // PDT-flagged: broker won't let you open new trades. Closes still ok.
  if (a.pattern_day_trader && isBuy) {
    return { blocked: true, reason: "Account is PDT-flagged — closing only." };
  }

  // PDT at-limit: a 4th day-trade in 5 days on a margin account under $25k.
  // Buys that open a new position are a leg of a potential day-trade; sells
  // of same-day-opened positions *complete* one. Either way → block.
  const pdtAtLimit = a.pdt_applies
    && typeof a.pdt_daytrades_remaining === "number"
    && a.pdt_daytrades_remaining <= 0;
  if (pdtAtLimit) {
    if (isBuy) {
      return { blocked: true, reason: "Would trigger Pattern Day Trader flag (4th day-trade in 5 days, equity under $25k)." };
    }
    if (openedToday) {
      return { blocked: true, reason: "Selling a same-day-opened position would complete a day-trade and trigger the PDT flag." };
    }
  }

  // GFV on buy: purchasing with unsettled proceeds.
  if (isBuy && typeof a.settled_cash === "number" && notional > 0) {
    // Allow a $1 fudge factor for rounding / fills priced slightly above mid.
    if (notional > a.settled_cash + 1) {
      const unsettled = a.unsettled_today || a.unsettled_cash || 0;
      return {
        blocked: true,
        reason: `Would trigger Good Faith Violation — only $${a.settled_cash.toFixed(2)} settled${unsettled > 0 ? `, $${unsettled.toFixed(2)} unsettled until T+1` : ""}.`,
      };
    }
  }

  // GFV on sell: selling a same-day-opened position while there are unsettled
  // proceeds is the classic GFV pattern (it implies the position was bought
  // with those unsettled funds and you're closing before they settle).
  if (!isBuy && openedToday) {
    const unsettled = a.unsettled_today || a.unsettled_cash || 0;
    if (unsettled > 0) {
      return {
        blocked: true,
        reason: `Would likely trigger Good Faith Violation — position was opened today and $${unsettled.toFixed(2)} of today's proceeds are unsettled (T+1).`,
      };
    }
  }

  return { blocked: false, reason: "" };
}

// Toggle the Approve button + warning banner for a specific approval card
// based on the current account state and the given notional.
function applyGateToCard(id, notional) {
  if (!autotradeApprovalList) return;
  const card = autotradeApprovalList.querySelector(`.approval-card[data-approval-id="${id}"]`);
  if (!card) return;
  const approveBtn = card.querySelector(`.approval-btn.approve[data-id="${id}"]`);
  if (!approveBtn) return; // card has no approve button (stale + invalid)

  const side = card.querySelector(".autotrade-log-side")?.textContent?.toLowerCase() || "buy";
  const gate = preTradeBlock({ side, notional });

  const body = card.querySelector(".approval-card-body");
  let notice = card.querySelector(".approval-gate-notice");

  if (gate.blocked) {
    approveBtn.disabled = true;
    approveBtn.classList.add("blocked");
    approveBtn.title = gate.reason;
    approveBtn.textContent = "🚫 Blocked";
    if (!notice && body) {
      notice = document.createElement("div");
      notice.className = "approval-gate-notice";
      const sizing = body.querySelector(".approval-sizing");
      if (sizing && sizing.parentNode === body) {
        sizing.insertAdjacentElement("afterend", notice);
      } else {
        body.appendChild(notice);
      }
    }
    if (notice) notice.textContent = `⚠️ ${gate.reason}`;
  } else {
    approveBtn.disabled = false;
    approveBtn.classList.remove("blocked");
    approveBtn.title = "";
    approveBtn.textContent = "Approve Trade";
    if (notice) notice.remove();
  }
}

// Re-evaluate the gate on every Close button in the Wallet. Called after
// position renders and account refreshes so the Close button reflects PDT/GFV
// status without requiring a click-fail round-trip.
function refreshPositionCloseGates() {
  const walletRoot = document.getElementById("wallet-positions");
  if (!walletRoot) return;
  walletRoot.querySelectorAll(".wallet-pos-close").forEach((btn) => {
    const openedToday = btn.dataset.openedToday === "1";
    const gate = preTradeBlock({ side: "sell", openedToday });
    if (gate.blocked) {
      btn.classList.add("blocked");
      btn.title = gate.reason;
    } else {
      btn.classList.remove("blocked");
      btn.title = "";
    }
  });
}

// Re-evaluate the gate on every currently-rendered approval card. Called after
// the account snapshot refreshes so the Approve button state is always in
// sync with the latest settled cash / day-trade count.
function refreshApprovalGates() {
  if (!autotradeApprovalList) return;
  const cards = autotradeApprovalList.querySelectorAll(".approval-card");
  cards.forEach((card) => {
    const id = card.dataset.approvalId;
    const totalEl = card.querySelector(`.sizing-total[data-id="${id}"]`);
    const qtyEl = card.querySelector(`.sizing-qty[data-id="${id}"]`);
    if (!totalEl || !qtyEl) return;
    const price = parseFloat(totalEl.dataset.price || 0);
    const qty = parseInt(qtyEl.value, 10) || 0;
    applyGateToCard(id, price * qty);
  });
}

// ── AI signal-stack helpers (features #8/#9/#10) ─────────────────────────────
// These render the pieces the backend has been shipping for a while but the
// approval card wasn't surfacing: the full multiplier chain (raw × cal ×
// regime × news × brier), the P(target)/P(stop)/P(neither) distribution,
// and the per-chip evidence (regime label + vol, article count + sentiment,
// brier bucket hit-rate). Each helper returns "" when its data is absent or
// neutral so the card stays clean on setups with nothing to say.

function _fmtMult(m) {
  if (m == null) return null;
  const n = Number(m);
  if (!Number.isFinite(n)) return null;
  // ×1.00 shown as "×1" to reduce visual noise when neutral
  return `×${n.toFixed(2)}`;
}

function _multPillClass(m) {
  if (m == null) return "mult-pill neutral";
  const n = Number(m);
  if (!Number.isFinite(n)) return "mult-pill neutral";
  if (n > 1.02) return "mult-pill up";
  if (n < 0.98) return "mult-pill down";
  return "mult-pill neutral";
}

// Probability distribution bar: tri-color split showing Claude's actual
// P(target)/P(stop)/P(neither) as allocated width. This is the headline
// #10 surface — a scalar "81% confident" hides whether the other 19%
// goes to stop or to neither, which dramatically changes expected value.
function _probDistributionHtml(a) {
  const pt = a.p_target, ps = a.p_stop, pn = a.p_neither;
  if (pt == null || ps == null || pn == null) return "";
  const t = Math.max(0, Math.min(100, pt * 100));
  const s = Math.max(0, Math.min(100, ps * 100));
  const n = Math.max(0, Math.min(100, pn * 100));
  return `
    <div class="approval-prob-dist" title="Claude's structured probability distribution">
      <div class="prob-dist-bar">
        <div class="prob-seg prob-seg-target" style="width:${t}%"></div>
        <div class="prob-seg prob-seg-neither" style="width:${n}%"></div>
        <div class="prob-seg prob-seg-stop" style="width:${s}%"></div>
      </div>
      <div class="prob-dist-labels">
        <span class="prob-label prob-target">🎯 ${t.toFixed(0)}% target</span>
        <span class="prob-label prob-neither">↔ ${n.toFixed(0)}% neither</span>
        <span class="prob-label prob-stop">🛑 ${s.toFixed(0)}% stop</span>
      </div>
    </div>`;
}

// Compact multiplier chain: raw × cal × regime × rs × news × brier = final.
// Pills show their own contribution; omitted when the multiplier is null
// (e.g. news degraded-no-op with no data, brier degraded to 1.0).
function _signalStackHtml(a) {
  const raw = a.raw_confidence;
  const cal = a.calibration_multiplier;
  const reg = a.regime_multiplier;
  const rs = a.rs_multiplier;
  const news = a.news_multiplier;
  const brier = a.brier_multiplier;
  const final = a.confidence;

  // If we have no breakdown at all (all nulls), don't render — keeps legacy
  // approvals from pre-upgrade scan loops looking unchanged.
  if (raw == null && cal == null && reg == null && rs == null && news == null && brier == null) return "";

  const pill = (label, mult, title) => {
    if (mult == null) return "";
    const cls = _multPillClass(mult);
    return `<span class="${cls}" title="${esc(title)}">${esc(label)} ${_fmtMult(mult)}</span>`;
  };

  const rawPct = raw != null ? `${(raw * 100).toFixed(0)}%` : "—";
  const finalPct = final != null ? `${(final * 100).toFixed(0)}%` : "—";

  const parts = [
    `<span class="stack-base" title="Claude's raw confidence before adjustments">raw ${rawPct}</span>`,
    pill("cal", cal, "Per-(strategy, symbol) historical calibration (#6)"),
    pill("regime", reg, "Market regime multiplier (#8)"),
    pill("rs", rs, "Relative Strength vs SPY multiplier (#12)"),
    pill("news", news, "News catalyst multiplier (#9)"),
    pill("brier", brier, "Brier-decile calibration feedback (#10)"),
    `<span class="stack-arrow">=</span>`,
    `<span class="stack-final">${finalPct}</span>`,
  ].filter(Boolean);

  return `<div class="approval-signal-stack">${parts.join(" ")}</div>`;
}

// Regime chip — one-liner "Trending Up · vol 14% · ×1.00" style summary.
// Only shown if we have a regime label (#8 degrades to neutral otherwise).
function _regimeChipHtml(a) {
  const label = a.regime_label || a.regime;
  if (!label) return "";
  const mult = a.regime_multiplier;
  const detail = a.regime_detail || {};
  const vol = detail.vol_pct != null ? `vol ${Number(detail.vol_pct).toFixed(0)}%` : null;
  const multTxt = _fmtMult(mult);
  const cls = _multPillClass(mult);
  const pieces = [esc(String(label))];
  if (vol) pieces.push(esc(vol));
  if (multTxt) pieces.push(multTxt);
  return `<span class="evidence-chip regime ${cls}" title="Market regime from SPY (20d vol + trend)">🌊 ${pieces.join(" · ")}</span>`;
}

// News chip — article count + sentiment + multiplier. Suppressed when news
// is neutral with no articles (the common case when there's no catalyst).
function _newsChipHtml(a) {
  const mult = a.news_multiplier;
  const detail = a.news_detail || {};
  const articles = Array.isArray(detail.articles) ? detail.articles : [];
  const stance = a.news_stance || detail.sentiment;
  const label = a.news_label;
  const isNeutral = (mult == null || Math.abs((mult ?? 1) - 1) < 0.02) && articles.length === 0;
  if (isNeutral) return "";
  const multTxt = _fmtMult(mult);
  const cls = _multPillClass(mult);
  const pieces = [];
  if (articles.length) pieces.push(`${articles.length} article${articles.length === 1 ? "" : "s"}`);
  if (label) pieces.push(esc(String(label)));
  else if (stance) pieces.push(esc(String(stance)));
  if (multTxt) pieces.push(multTxt);
  if (!pieces.length) return "";
  return `<span class="evidence-chip news ${cls}" title="News catalyst adjustment (#9)">📰 ${pieces.join(" · ")}</span>`;
}

// Brier bucket chip — "0.8-0.9: pred 85%, hit 76% (n=18)". Only shown when
// the #10 layer actually had a bucket to score against (not degraded).
function _brierChipHtml(a) {
  const d = a.brier_detail || {};
  if (!d || d.reason !== "bucket") return "";
  const bucket = d.bucket || "?";
  const pred = d.predicted_mean != null ? `${(d.predicted_mean * 100).toFixed(0)}%` : "—";
  const emp = d.empirical_hit_rate != null ? `${(d.empirical_hit_rate * 100).toFixed(0)}%` : "—";
  const n = d.n != null ? `n=${esc(d.n)}` : "";
  const mult = a.brier_multiplier;
  const multTxt = _fmtMult(mult);
  const cls = _multPillClass(mult);
  return `<span class="evidence-chip brier ${cls}" title="Calibration feedback from last ~30 days (#10)">🎯 ${esc(bucket)}: pred ${pred}, hit ${emp}${n ? " · " + n : ""}${multTxt ? " · " + multTxt : ""}</span>`;
}

// Calibration source chip — "SOFI/Oversold Bounce n=34 backtest". Shown
// when the #6 multiplier had a cell-level or strategy-fallback hit.
function _calibrationChipHtml(a) {
  const d = a.calibration_detail || {};
  const reason = d.reason || a.calibration_reason;
  if (!reason || reason === "no_data") return "";
  const mult = a.calibration_multiplier;
  const multTxt = _fmtMult(mult);
  const cls = _multPillClass(mult);
  let label;
  if (reason === "cell") {
    const n = d.n || a.calibration_n || "?";
    const wr = d.win_rate != null ? `${(d.win_rate * 100).toFixed(0)}% wr` : "";
    label = `cell n=${n}${wr ? " · " + wr : ""}`;
  } else if (reason === "strategy_fallback") {
    const n = d.strategy_n || "?";
    label = `strategy fallback n=${n}`;
  } else {
    label = reason;
  }
  return `<span class="evidence-chip cal ${cls}" title="Historical calibration (#6)">📊 ${esc(label)}${multTxt ? " · " + multTxt : ""}</span>`;
}

// Haiku pre-screen chip (#11) — two-stage AI gate tag. Every approval
// that reaches the user has either passed Haiku or the screener was
// unreachable (fail-open). Shown so the user can see the pre-screen
// rationale at a glance and spot-check Haiku's judgment against what
// the final Opus analysis produced.
function _haikuChipHtml(a) {
  const valid = a.haiku_screen_valid;
  const reason = a.haiku_screen_reason;
  if (valid !== true) return "";   // null (degraded) or false (never shown): hide
  const tip = reason
    ? `Pre-screened by Haiku (#11) — ${reason}`
    : "Pre-screened by Haiku (#11)";
  const label = reason ? String(reason) : "pre-screened";
  return `<span class="evidence-chip haiku up" title="${esc(tip)}">⚡ Haiku ✓ · ${esc(label)}</span>`;
}

// RS chip (#12) — relative strength / weakness vs SPY. Emoji flips
// with the direction of the RS signal vs the trade: 📈 when the trade
// is aligned with leading/lagging action, 📉 when it's fighting the
// tape, 🔄 for neutral/unknown. Hidden when the classifier ran on a
// "wait" direction or the sector snapshot was missing (no useful
// information to surface).
function _rsChipHtml(a) {
  const state = a.rs_state;
  const mult = a.rs_multiplier;
  if (!state || state === "unknown") return "";
  const label = a.rs_label || state;
  const detail = a.rs_detail || {};
  const reason = detail.reason || "";
  const multTxt = _fmtMult(mult);
  const cls = _multPillClass(mult);
  // Emoji encodes "is the trade aligned with RS or fighting it?"
  const leaderStates = new Set(["strong_leader", "leader", "mild_leader"]);
  const laggerStates = new Set(["mild_lagger", "lagger", "strong_lagger"]);
  let emoji = "🔄";
  if (leaderStates.has(state)) emoji = "📈";
  else if (laggerStates.has(state)) emoji = "📉";
  const pieces = [esc(String(label))];
  if (multTxt) pieces.push(multTxt);
  const tip = reason
    ? `Relative Strength (#12) — ${reason}`
    : "Relative Strength vs SPY (#12)";
  return `<span class="evidence-chip rs ${cls}" title="${esc(tip)}">${emoji} ${pieces.join(" · ")}</span>`;
}

function _evidenceChipRowHtml(a) {
  const chips = [
    _haikuChipHtml(a),
    _regimeChipHtml(a),
    _rsChipHtml(a),
    _calibrationChipHtml(a),
    _newsChipHtml(a),
    _brierChipHtml(a),
  ].filter(Boolean);
  if (!chips.length) return "";
  return `<div class="approval-evidence-row">${chips.join("")}</div>`;
}

// v7.16.x (2026-05-21) — top-of-page "approval needed" banner. Manual
// mode only: in autonomous mode nothing waits for approval (trades
// auto-fire), so the banner stays hidden. Driven off pending_approvals
// so it auto-clears the moment the card is approved / rejected / expired.
function renderApprovalBanner(data) {
  if (!approvalBanner) return;
  const pending = Array.isArray(data.pending_approvals) ? data.pending_approvals : [];
  const autonomous = data.autonomous_mode ?? false;
  if (autonomous || pending.length === 0) {
    approvalBanner.classList.add("readiness-banner-hidden");
    return;
  }
  const n = pending.length;
  const syms = pending
    .map((a) => a.symbol || (a.trade_log && a.trade_log.symbol) || "?")
    .join(", ");
  if (approvalBannerText) {
    approvalBannerText.innerHTML =
      `<strong>${n} trade${n > 1 ? "s" : ""} waiting for your approval</strong> — ` +
      `${esc(syms)}. Review the approval card${n > 1 ? "s" : ""} below before ` +
      `${n > 1 ? "they expire" : "it expires"}.`;
  }
  approvalBanner.classList.remove("readiness-banner-hidden");
}

function renderAutotradeStatus(data) {
  const running = data.running;

  // Toggle button state
  autotradeToggle.classList.toggle("active", running);
  autotradeLabel.textContent = running ? "Stop AI Trading" : "Start AI Trading";

  // v7.16.x — Autonomous (auto-approve) toggle state. Independent of the
  // running switch. ``?? false`` keeps it OFF during the deploy window
  // before the backend restart provides the field.
  if (autonomousToggle) {
    const auto = data.autonomous_mode ?? false;
    autonomousToggle.classList.toggle("active", auto);
    if (autonomousLabel) {
      autonomousLabel.textContent = auto ? "Autonomous: ON" : "Autonomous: OFF";
    }
  }

  // v7.16.x — keep notifications.js's autonomous mirror current, so the
  // approval_requested bell stays silent when autonomous is ON.
  setAutonomousMode(data.autonomous_mode ?? false);

  // v7.16.x — approval-needed banner (manual mode only).
  renderApprovalBanner(data);

  // Status text
  if (data.circuit_breaker_tripped) {
    autotradeStatusEl.textContent = "STOPPED — Daily loss limit reached";
    autotradeStatusEl.classList.add("negative");
  } else if (running) {
    autotradeStatusEl.classList.remove("negative");
    if (data.pending_approvals && data.pending_approvals.length > 0) {
      autotradeStatusEl.textContent = `${data.pending_approvals.length} trade(s) awaiting your approval`;
    } else if (data.monitored_positions && data.monitored_positions.length > 0) {
      const monNames = data.monitored_positions.map((m) => m.symbol).join(", ");
      autotradeStatusEl.textContent = `Monitoring ${monNames}`;
    } else if (data.confirming && data.confirming.length > 0) {
      const names = data.confirming.map((c) => c.symbol).join(", ");
      autotradeStatusEl.textContent = `Watching ${names}…`;
    } else {
      const lastScan = data.last_scan
        ? `Last scan ${new Date(data.last_scan).toLocaleTimeString()}`
        : "Starting…";
      autotradeStatusEl.textContent = lastScan;
    }
  } else {
    autotradeStatusEl.classList.remove("negative");
    autotradeStatusEl.textContent = data.scan_count > 0 ? "Stopped" : "Idle";
  }

  if (data.last_error && running && !data.circuit_breaker_tripped) {
    autotradeStatusEl.textContent += ` — ${data.last_error}`;
  }

  // Stats — v7.16.8: only Scans + Trades remain in the UI. Setups,
  // Watching, and Cache tiles were removed; their data is still
  // available on `data.strategies_matched`, `data.monitored_positions`,
  // and `data.analysis_cache` if we ever bring those back.
  if (atScans)  atScans.textContent = data.scan_count;
  // v7.16.x — "Today's Scans" tile. Fall back to "—" when the backend
  // hasn't been restarted with the new field yet (graceful during the
  // deploy window: the frontend updates on browser refresh, the field
  // appears once uvicorn restarts).
  if (atScansToday) {
    atScansToday.textContent =
      data.scan_count_today ?? "—";
  }
  if (atTrades) {
    const executedCount = data.trades_today.filter(
      (t) => !["confirming", "rejected", "cancelled", "pending_approval", "expired", "failed"].includes(t.status)
    ).length;
    atTrades.textContent = executedCount;
  }
  // v7.16.x (2026-06-10) — "Today's P&L" tile, fallback source: monitor-price
  // P&L from today's closed + open positions in this payload. Overridden by
  // the equity-based number in loadAccount() once the account route exposes
  // last_equity (staged; live after the next boundary restart).
  dailyPnlPositions = computePositionsPnl(data);
  renderDailyPnl();

  // Sync universe from status config
  if (data.config && data.config.trading_universe) {
    currentUniverse = data.config.trading_universe;
    isCustomUniverse = true; // if it comes from status, it's the active set
    renderUniverseTags();
  }

  // ── Monitored positions (trailing stop + time exit) ──
  if (data.monitored_positions && data.monitored_positions.length > 0) {
    autotradeMonitor.classList.remove("hidden");

    // Remember which cards are expanded so we preserve state across re-renders
    const expanded = new Set(
      autotradeMonitorList.querySelectorAll(".monitor-card.expanded")
    );
    const expandedSymbols = new Set();
    expanded.forEach((el) => expandedSymbols.add(el.dataset.symbol));

    autotradeMonitorList.innerHTML = data.monitored_positions.map((m) => {
      const dirCls = m.direction === "long" ? "buy" : "sell";
      const holdSec = m.hold_seconds || 0;
      const holdMin = Math.floor(holdSec / 60);
      const holdRemSec = holdSec % 60;
      const holdStr = holdMin > 0 ? `${holdMin}m ${holdRemSec}s` : `${holdRemSec}s`;
      const holdPct = Math.min(100, (holdSec / (m.max_hold_minutes * 60)) * 100);
      const timeLeft = Math.max(0, m.max_hold_minutes * 60 - holdSec);
      const tlMin = Math.floor(timeLeft / 60);
      const tlSec = timeLeft % 60;
      const timeLeftStr = tlMin > 0 ? `${tlMin}m ${tlSec}s` : `${tlSec}s`;

      // P&L calculation
      const pl = m.direction === "long"
        ? (m.current_price || 0) - m.entry_price
        : m.entry_price - (m.current_price || 0);
      const plTotal = pl * m.qty;
      const plPct = m.entry_price > 0 ? (pl / m.entry_price * 100) : 0;
      const plSign = plTotal >= 0 ? "+" : "";
      const plCls = plTotal >= 0 ? "positive" : "negative";

      // How far price is from trailing stop
      const stopDist = m.trailing_stop && m.current_price
        ? (m.direction === "long"
            ? ((m.current_price - m.trailing_stop) / m.current_price * 100)
            : ((m.trailing_stop - m.current_price) / m.current_price * 100))
        : null;
      const stopDistStr = stopDist != null ? `${stopDist.toFixed(2)}%` : "—";

      const hwm = m.direction === "long" ? m.highest_price : m.lowest_price;
      const hwmLabel = m.direction === "long" ? "High" : "Low";

      const isOpen = expandedSymbols.has(m.symbol);

      // Distance to target
      const targetPrice = m.target ? parseFloat(String(m.target).replace(/[^0-9.]/g, "")) : null;
      let targetDist = "";
      if (targetPrice && m.current_price) {
        const diff = m.direction === "long"
          ? ((targetPrice - m.current_price) / m.current_price * 100)
          : ((m.current_price - targetPrice) / m.current_price * 100);
        targetDist = `${diff.toFixed(2)}% away`;
      }

      // R-multiple (reward achieved vs risk taken)
      const stopPrice = m.initial_stop || (m.stop_loss ? parseFloat(String(m.stop_loss).replace(/[^0-9.]/g, "")) : null);
      let rMultiple = "";
      if (stopPrice && m.entry_price) {
        const risk = Math.abs(m.entry_price - stopPrice);
        if (risk > 0) {
          const r = pl / risk;
          rMultiple = `${r >= 0 ? "+" : ""}${r.toFixed(2)}R`;
        }
      }

      const mSym = esc(m.symbol);
      return `
        <div class="monitor-card ${isOpen ? "expanded" : ""}" data-symbol="${mSym}">
          <div class="monitor-card-header" onclick="this.parentElement.classList.toggle('expanded')">
            <span class="autotrade-log-side ${dirCls}">${esc(String(m.direction || "").toUpperCase())}</span>
            <strong class="monitor-symbol">${mSym}</strong>
            <span class="monitor-qty">${esc(m.qty)} shares</span>
            <span class="monitor-pl ${plCls}">${plSign}$${Math.abs(plTotal).toFixed(2)} (${plSign}${plPct.toFixed(2)}%)</span>
            <span class="monitor-chevron">&#9660;</span>
          </div>
          <div class="monitor-card-expand">
            <div class="monitor-detail-row">
              <div class="monitor-detail">
                <span class="monitor-label">Entry</span>
                <span class="monitor-value">$${m.entry_price.toFixed(2)}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">Current</span>
                <span class="monitor-value">$${(m.current_price || 0).toFixed(2)}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">${hwmLabel}</span>
                <span class="monitor-value positive">$${hwm ? hwm.toFixed(2) : "—"}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">Target</span>
                <span class="monitor-value">${esc(m.target) || "—"}</span>
              </div>
            </div>
            <div class="monitor-detail-row">
              <div class="monitor-detail">
                <span class="monitor-label">Trail Stop</span>
                <span class="monitor-value negative">$${m.trailing_stop ? m.trailing_stop.toFixed(2) : "—"}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">Initial Stop</span>
                <span class="monitor-value">${esc(m.stop_loss) || "—"}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">Stop Dist.</span>
                <span class="monitor-value">${stopDistStr}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">To Target</span>
                <span class="monitor-value">${targetDist || "—"}</span>
              </div>
            </div>
            <div class="monitor-detail-row">
              <div class="monitor-detail">
                <span class="monitor-label">Confidence</span>
                <span class="monitor-value">${m.confidence ? (m.confidence * 100).toFixed(0) + "%" : "—"}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">R/R</span>
                <span class="monitor-value">${esc(m.risk_reward) || "—"}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">R-Multiple</span>
                <span class="monitor-value ${pl >= 0 ? "positive" : "negative"}">${rMultiple || "—"}</span>
              </div>
              <div class="monitor-detail">
                <span class="monitor-label">Entry Zone</span>
                <span class="monitor-value">${esc(m.entry_zone) || "—"}</span>
              </div>
            </div>
            <div class="monitor-hold-row">
              <span class="monitor-label">Hold</span>
              <div class="monitor-hold-bar">
                <div class="monitor-hold-fill ${holdPct > 80 ? "warning" : ""}" style="width:${holdPct}%"></div>
              </div>
              <span class="monitor-hold-time">${holdStr} / ${esc(m.max_hold_minutes)}m</span>
            </div>
            <div class="monitor-time-left ${holdPct > 80 ? "warning" : ""}">
              ${holdPct > 80 ? "⚠ " : ""}${timeLeftStr} remaining
            </div>
            ${m.setup ? `<div class="monitor-setup"><span class="monitor-label">Setup</span> ${esc(m.setup)}</div>` : ""}
            ${m.reasoning ? `<div class="monitor-reasoning"><span class="monitor-label">Reasoning</span> ${esc(m.reasoning)}</div>` : ""}
            <div class="monitor-actions">
              <button class="monitor-close-btn" data-symbol="${mSym}" type="button">Close Position</button>
            </div>
          </div>
        </div>`;
    }).join("");
  } else {
    autotradeMonitor.classList.add("hidden");
  }

  // ── Pending approvals (active + stale) ──
  // Filter out approvals the user just clicked (approve/reject) so polling or
  // WebSocket pushes don't re-insert the card before the backend drops it.
  const visibleApprovals = (data.pending_approvals || []).filter(
    (a) => !_dismissedApprovalIds.has(a.id)
  );
  // Clean up any dismissed IDs the backend has finally forgotten about.
  const backendIds = new Set((data.pending_approvals || []).map((a) => a.id));
  for (const id of Array.from(_dismissedApprovalIds)) {
    if (!backendIds.has(id)) _dismissedApprovalIds.delete(id);
  }

  if (visibleApprovals.length > 0) {
    autotradeApprovals.classList.remove("hidden");
    autotradeApprovalList.innerHTML = visibleApprovals.map((a) => {
      const conf = (a.confidence * 100).toFixed(0);
      const movePct = a.price_move_pct != null ? `${a.price_move_pct >= 0 ? "+" : ""}${a.price_move_pct.toFixed(3)}%` : "";

      const cardClass = "approval-card";

      // Confidence bar color: green ≥85, yellow 80-84, orange <80
      const confNum = a.confidence * 100;
      const confColor = confNum >= 85 ? "#00d97e" : confNum >= 80 ? "#f0c040" : "#ff4d5a";

      // Countdown timer — when this hits 0 the backend expires the approval
      // on the next status poll and the card disappears. Next scan may re-
      // issue if the setup still qualifies.
      const timeLeft = Math.max(0, Math.round((new Date(a.expires) - Date.now()) / 1000));
      const timeLeftStr = timeLeft > 60 ? `${Math.floor(timeLeft / 60)}m ${timeLeft % 60}s` : `${timeLeft}s`;
      const timerHtml = `<span class="approval-timer">${timeLeftStr} left</span>`;

      const confidenceHtml = `
        <div class="approval-confidence">
          <div class="approval-conf-bar">
            <div class="approval-conf-fill" style="width:${confNum}%; background:${confColor}"></div>
          </div>
          <span class="approval-conf-label" style="color:${confColor}">${conf}% confident</span>
        </div>
        ${_probDistributionHtml(a)}
        ${_signalStackHtml(a)}
        ${_evidenceChipRowHtml(a)}`;

      // Sizing breakdown (computed first so the pre-trade gate below knows the notional)
      const sz = a.sizing || {};
      const szQty = a.qty || 1;
      const szPrice = sz.price || a.prediction_price || 0;
      const szTotal = (szQty * szPrice);

      // Pre-trade gate: if this order would trigger PDT or GFV on a live
      // account, disable the Approve button client-side so the user can't even
      // attempt to submit. The backend enforces the same rules regardless.
      const gate = preTradeBlock({ side: a.side || "buy", notional: szTotal });
      const approveBtn = gate.blocked
        ? `<button class="approval-btn approve blocked" data-id="${esc(a.id)}" type="button" disabled title="${esc(gate.reason)}">🚫 Blocked</button>`
        : `<button class="approval-btn approve" data-id="${esc(a.id)}" type="button">Approve Trade</button>`;
      const gateNoticeHtml = gate.blocked
        ? `<div class="approval-gate-notice">⚠️ ${esc(gate.reason)}</div>`
        : "";

      // Action buttons — simple Approve/Reject. Expired approvals are removed
      // server-side on the next status poll; the next scan re-issues if the
      // setup still qualifies.
      const actionsHtml = `
        ${approveBtn}
        <button class="approval-btn reject" data-id="${esc(a.id)}" type="button">Reject</button>`;
      const szMethod = sz.method === "risk_based" ? "Risk-based" : "Portfolio %";
      const szMult = sz.confidence_multiplier || 1.0;
      const szMaxAffordable = sz.max_affordable || 0;
      const szSpendable = sz.spendable || sz.cash || sz.buying_power || 0;
      const maxAttr = szMaxAffordable > 0 ? `max="${esc(szMaxAffordable)}"` : "";
      const bpHint = szSpendable > 0
        ? `<span class="sizing-bp">Max ${esc(szMaxAffordable.toLocaleString("en-US"))} shares · Cash $${esc(szSpendable.toLocaleString("en-US", {maximumFractionDigits: 0}))}</span>`
        : "";

      let sizingHtml = `
        <div class="approval-sizing">
          <div class="approval-sizing-header">
            <span class="approval-label">Position Size</span>
            <span class="approval-sizing-method">${szMethod}${szMult > 1 ? ` · ${esc(szMult)}x conf boost` : ""}</span>
          </div>
          <div class="approval-sizing-controls">
            <button class="sizing-btn sizing-minus" data-id="${esc(a.id)}" type="button">−</button>
            <div class="sizing-qty-display">
              <input type="number" class="sizing-qty" data-id="${esc(a.id)}" value="${esc(szQty)}" min="1" ${maxAttr} step="1" inputmode="numeric" />
              <span class="sizing-unit">shares</span>
            </div>
            <button class="sizing-btn sizing-plus" data-id="${esc(a.id)}" type="button">+</button>
          </div>
          <div class="approval-sizing-summary">
            <span class="sizing-total" data-id="${esc(a.id)}" data-price="${esc(szPrice)}" data-max="${esc(szMaxAffordable)}">$${esc(szTotal.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}))}</span>
            <span class="sizing-label">total cost</span>
            ${bpHint}
            ${sz.risk_budget ? `<span class="sizing-risk">Risk budget: $${esc(sz.risk_budget.toLocaleString("en-US", {minimumFractionDigits: 0}))}</span>` : ""}
            ${sz.stop_distance ? `<span class="sizing-risk">Stop distance: $${sz.stop_distance.toFixed(2)}</span>` : ""}
          </div>
        </div>`;

      return `
        <div class="${cardClass}" data-approval-id="${esc(a.id)}">
          <div class="approval-card-top">
            <div class="approval-card-header">
              <span class="autotrade-log-side ${esc(a.side)}">${esc(String(a.side || "").toUpperCase())}</span>
              <strong class="approval-symbol">${esc(a.symbol)}</strong>
              ${timerHtml}
            </div>
            ${confidenceHtml}
          </div>
          <div class="approval-card-body">
            <div class="approval-detail">
              <span class="approval-label">Setup</span>
              <span class="approval-value">${esc(a.setup)}</span>
            </div>
            <div class="approval-detail-row">
              ${a.entry_zone ? `<div class="approval-detail"><span class="approval-label">Entry</span><span class="approval-value">${esc(a.entry_zone)}</span></div>` : ""}
              ${a.stop_loss ? `<div class="approval-detail"><span class="approval-label">Stop Loss</span><span class="approval-value negative">${esc(a.stop_loss)}</span></div>` : ""}
              ${a.target ? `<div class="approval-detail"><span class="approval-label">Target</span><span class="approval-value positive">${esc(a.target)}</span></div>` : ""}
              ${a.risk_reward ? `<div class="approval-detail"><span class="approval-label">R:R</span><span class="approval-value">${esc(a.risk_reward)}</span></div>` : ""}
            </div>
            ${sizingHtml}
            ${gateNoticeHtml}
            ${a.reasoning ? `
              <div class="approval-detail">
                <span class="approval-label">AI Reasoning</span>
                <span class="approval-reasoning">${esc(a.reasoning)}</span>
              </div>
            ` : ""}
          </div>
          <div class="approval-card-actions">
            ${actionsHtml}
          </div>
        </div>`;
    }).join("");
  } else {
    autotradeApprovals.classList.add("hidden");
  }

  // ── Trade log ──
  if (data.trades_today.length > 0) {
    autotradeLog.classList.remove("hidden");
    autotradeLogList.innerHTML = data.trades_today.slice().reverse().map((t) => {
      const time = new Date(t.time).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
      let confirmDetail = "";
      if (t.status === "confirming") {
        confirmDetail = `<span class="autotrade-log-confirm watching">Watching $${t.prediction_price?.toFixed(2) ?? "—"}…</span>`;
      } else if (t.confirmation_price != null) {
        const sign = t.price_move_pct >= 0 ? "+" : "";
        const cls = (t.status === "rejected") ? "wrong" : "right";
        confirmDetail = `<span class="autotrade-log-confirm ${cls}">$${t.prediction_price?.toFixed(2)} → $${t.confirmation_price.toFixed(2)} (${sign}${t.price_move_pct.toFixed(3)}%)</span>`;
      }
      const statusLabel = t.status === "pending_approval" ? "awaiting" : t.status;

      // Exit info (trailing stop / time exit)
      let exitDetail = "";
      if (t.exit_reason) {
        const exitLabels = { trailing_stop: "Trail Stop", max_hold_time: "Max Hold", eod_exit: "EOD Exit", external: "External", manual: "Manual" };
        const exitLabel = exitLabels[t.exit_reason] || t.exit_reason;
        exitDetail = `<span class="autotrade-log-exit">${esc(exitLabel)}${t.exit_price ? ` @ $${t.exit_price.toFixed(2)}` : ""}</span>`;
      }

      // Hold duration
      let holdDetail = "";
      if (t.hold_duration_seconds != null) {
        const hm = Math.floor(t.hold_duration_seconds / 60);
        const hs = t.hold_duration_seconds % 60;
        holdDetail = `<span class="autotrade-log-hold">${hm}m${hs}s</span>`;
      }

      return `
        <div class="autotrade-log-row">
          <span class="autotrade-log-time">${time}</span>
          <span class="autotrade-log-side ${esc(t.side)}">${esc(t.side)}</span>
          <span class="autotrade-log-symbol">${esc(t.symbol)}</span>
          <span class="autotrade-log-setup">${esc(t.setup)}</span>
          ${confirmDetail}
          ${exitDetail}
          ${holdDetail}
          <span class="autotrade-log-conf">${(t.confidence * 100).toFixed(0)}%</span>
          <span class="autotrade-log-status ${esc(t.status)}">${esc(statusLabel)}</span>
        </div>`;
    }).join("");
  } else {
    autotradeLog.classList.add("hidden");
  }
}

async function fetchAutotradeStatus() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/autotrade/status`, { credentials: "include" });
    if (!res.ok) return;
    const data = await res.json();
    renderAutotradeStatus(data);
  } catch { /* silent */ }
}

if (autotradeToggle) {
  autotradeToggle.addEventListener("click", async () => {
    const isRunning = autotradeToggle.classList.contains("active");
    const endpoint = isRunning ? "stop" : "start";

    autotradeToggle.disabled = true;
    autotradeLabel.textContent = isRunning ? "Stopping…" : "Starting…";

    try {
      const res = await fetch(`${API_BASE_URL}/trading/autotrade/${endpoint}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderAutotradeStatus(data);

      // Start or stop polling (only as fallback if WebSocket is not connected)
      if (data.running && !autotradePolling && !isWsConnected()) {
        autotradePolling = setInterval(fetchAutotradeStatus, 5000);
      } else if (!data.running && autotradePolling) {
        clearInterval(autotradePolling);
        autotradePolling = null;
      }
    } catch (err) {
      autotradeStatusEl.textContent = `Error: ${err.message}`;
    } finally {
      autotradeToggle.disabled = false;
    }
  });
}

// v7.16.x — Autonomous (auto-approve) toggle. POSTs the desired state to
// /trading/autotrade/autonomous; the response is the full autotrade status,
// so renderAutotradeStatus re-paints both this toggle and everything else.
if (autonomousToggle) {
  autonomousToggle.addEventListener("click", async () => {
    const isOn = autonomousToggle.classList.contains("active");
    autonomousToggle.disabled = true;
    if (autonomousLabel) {
      autonomousLabel.textContent = isOn ? "Disabling…" : "Enabling…";
    }
    try {
      const res = await fetch(`${API_BASE_URL}/trading/autotrade/autonomous`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !isOn }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderAutotradeStatus(data);
    } catch (err) {
      autotradeStatusEl.textContent = `Error: ${err.message}`;
    } finally {
      autonomousToggle.disabled = false;
    }
  });
}

// Check auto-trade status on page load (in case it was left running)
async function initAutotrade() {
  await fetchAutotradeStatus();
  await loadUniverse();
  // If already running, start polling as fallback (WebSocket will take over)
  if (autotradeToggle.classList.contains("active") && !autotradePolling) {
    autotradePolling = setInterval(fetchAutotradeStatus, 5000);
  }
  // Connect WebSocket for real-time push updates
  initWebSocket({
    handlers: {
      autotrade_status: renderAutotradeStatus,
      market_pulse: renderMarketPulse,
      trade_event: handleTradeEvent,
      monitor_event: handleMonitorEvent,
      wallet_update: () => loadWallet(),
      // The paper↔live toggle emits this on every flip. Snapshot shape is
      // the same as GET /trading/mode's top-level fields. We pre-paint the
      // badge here so the UI updates instantly; loadWallet() (triggered by
      // the accompanying wallet_update event) confirms it from the server.
      trading_mode_changed: (snap) => {
        if (!snap || typeof snap.mode !== "string") return;
        setBadge(walletModeBadge, snap.mode);
        if (walletModeLabel) {
          walletModeLabel.textContent = snap.mode === "live"
            ? "Live Cash Account — real money"
            : "Paper Trading Account";
        }
        document.body.classList.toggle("live-trading", snap.mode === "live");
      },
    },
    onOpen: () => {
      // Stop autotrade-status polling — autotrade status IS pushed via WS
      // (`autotrade_status` event from the server's scan loop), so the
      // 5s poll is redundant once WS is connected.
      if (autotradePolling) { clearInterval(autotradePolling); autotradePolling = null; }
      // v7.16.12 (2026-05-12): DO NOT kill pulseInterval on WS open.
      // Backend pushes `market_pulse` only on cache MISSES of
      // GET /market/pulse (see routers/market.py:295). The frontend
      // poll is what drives those cache misses. Killing the poll froze
      // pulse updates after the initial 30s cache TTL elapsed. Polling
      // and WS push co-exist: poll → cache-miss → server broadcasts to
      // all clients (including this one). That gives every open tab a
      // near-simultaneous pulse update once per minute.
    },
    onClose: () => {
      // Pulse polling already running independent of WS state (see above),
      // so nothing to re-arm here. Just resume autotrade-status polling
      // if autotrade is on.
      if (autotradeToggle && autotradeToggle.classList.contains("active") && !autotradePolling) {
        autotradePolling = setInterval(fetchAutotradeStatus, 5000);
      }
    },
  });
}

// ── Approval actions ────────────────────────────────────────────────────────
if (autotradeApprovalList) {
  // Recompute total cost based on the current qty value.
  // While `allowEmpty` is true (user is mid-typing), an empty / invalid field
  // is left alone — we just blank out the total so nothing shows a stale number.
  // When false (blur, +/-, approve), we clamp back to [1, max_affordable].
  function updateSizingRow(id, { allowEmpty = false } = {}) {
    const qtyEl = autotradeApprovalList.querySelector(`.sizing-qty[data-id="${id}"]`);
    const totalEl = autotradeApprovalList.querySelector(`.sizing-total[data-id="${id}"]`);
    if (!qtyEl || !totalEl) return;

    const price = parseFloat(totalEl.dataset.price || 0);
    const max = parseInt(totalEl.dataset.max || 0, 10) || 0;
    const raw = qtyEl.value;
    const parsed = parseInt(raw, 10);
    const isEmpty = raw === "" || !Number.isFinite(parsed);

    if (allowEmpty && isEmpty) {
      // Let the user finish typing — clear total, drop at-max styling
      totalEl.textContent = "—";
      totalEl.classList.remove("sizing-at-max");
      return;
    }

    let qty = parsed;
    if (!Number.isFinite(qty) || qty < 1) qty = 1;
    if (max > 0 && qty > max) qty = max;
    if (String(qty) !== qtyEl.value) qtyEl.value = qty;

    if (price > 0) {
      const total = qty * price;
      totalEl.textContent = "$" + total.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
      if (max > 0 && qty >= max) {
        totalEl.classList.add("sizing-at-max");
      } else {
        totalEl.classList.remove("sizing-at-max");
      }
      // Re-evaluate the pre-trade gate: bumping qty up past settled cash must
      // instantly block the Approve button (and vice-versa).
      applyGateToCard(id, total);
    }
  }

  // +/- qty buttons (always step by 1)
  autotradeApprovalList.addEventListener("click", (e) => {
    const plus = e.target.closest(".sizing-plus");
    const minus = e.target.closest(".sizing-minus");
    if (!plus && !minus) return;

    const id = (plus || minus).dataset.id;
    const qtyEl = autotradeApprovalList.querySelector(`.sizing-qty[data-id="${id}"]`);
    if (!qtyEl) return;

    let qty = parseInt(qtyEl.value, 10) || 1;
    qty = plus ? qty + 1 : Math.max(1, qty - 1);
    qtyEl.value = qty;
    updateSizingRow(id);
  });

  // Direct typing — update total but don't force a value while the field is empty
  autotradeApprovalList.addEventListener("input", (e) => {
    const input = e.target.closest(".sizing-qty");
    if (!input) return;
    updateSizingRow(input.dataset.id, { allowEmpty: true });
  });

  // On blur, snap back to a valid number so approve has something sane to read
  autotradeApprovalList.addEventListener("focusout", (e) => {
    const input = e.target.closest(".sizing-qty");
    if (!input) return;
    updateSizingRow(input.dataset.id);
  });

  // Select all on focus for faster replacement
  autotradeApprovalList.addEventListener("focusin", (e) => {
    const input = e.target.closest(".sizing-qty");
    if (input) input.select();
  });

  // Approve / Reject
  autotradeApprovalList.addEventListener("click", async (e) => {
    const btn = e.target.closest(".approval-btn");
    if (!btn) return;

    const approvalId = btn.dataset.id;
    const isApprove = btn.classList.contains("approve");
    const isReject = btn.classList.contains("reject");

    const card = btn.closest(".approval-card");

    // Belt-and-suspenders: re-check the gate at click time in case the account
    // snapshot changed between render and click (e.g. another trade filled).
    if (isApprove) {
      const totalEl = card.querySelector(`.sizing-total[data-id="${approvalId}"]`);
      const qtyEl = card.querySelector(`.sizing-qty[data-id="${approvalId}"]`);
      const price = parseFloat(totalEl?.dataset.price || 0);
      const qty = parseInt(qtyEl?.value, 10) || 0;
      const side = card.querySelector(".autotrade-log-side")?.textContent?.toLowerCase() || "buy";
      const gate = preTradeBlock({ side, notional: price * qty });
      if (gate.blocked) {
        applyGateToCard(approvalId, price * qty);
        alert(`Trade blocked: ${gate.reason}`);
        return;
      }
    }

    card.querySelectorAll(".approval-btn").forEach((b) => { b.disabled = true; });

    const endpoint = isApprove ? "approve" : "reject";

    // Read the user's (possibly adjusted) qty from the card BEFORE removing it
    let body = "{}";
    if (isApprove) {
      const qtyEl = card.querySelector(`.sizing-qty[data-id="${approvalId}"]`);
      if (qtyEl) {
        const userQty = parseInt(qtyEl.value, 10);
        if (userQty > 0) body = JSON.stringify({ qty: userQty });
      }
    }

    // Mark as dismissed so polling / WebSocket status pushes don't re-render
    // this card while the backend is still processing the order.
    _dismissedApprovalIds.add(approvalId);

    // Optimistically remove the card immediately so user doesn't see a stuck "Approving…"
    card.style.transition = "opacity 0.2s, transform 0.2s";
    card.style.opacity = "0";
    card.style.transform = "translateX(20px)";
    setTimeout(() => {
      card.remove();
      // If no cards left, hide the approvals section
      if (!autotradeApprovalList.children.length) {
        autotradeApprovals.classList.add("hidden");
      }
    }, 200);

    try {
      const res = await fetch(`${API_BASE_URL}/trading/autotrade/${endpoint}/${approvalId}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderAutotradeStatus(data);
    } catch (err) {
      // Card is already gone — show a toast-style error instead of restoring it
      console.error(`Failed to ${endpoint} trade:`, err.message);
    }
  });
}

// ── Monitor close action ───────────────────────────────────────────────────
if (autotradeMonitorList) {
  autotradeMonitorList.addEventListener("click", async (e) => {
    const btn = e.target.closest(".monitor-close-btn");
    if (!btn) return;
    e.stopPropagation(); // don't toggle expand/collapse

    const symbol = btn.dataset.symbol;
    // Monitored positions are autotrade-managed and by definition opened this
    // session, so openedToday is always true for the gate check.
    const gate = preTradeBlock({ side: "sell", openedToday: true });
    if (gate.blocked) {
      alert(`Cannot close ${symbol}: ${gate.reason}`);
      return;
    }
    if (!confirm(`Close your ${symbol} position at market?`)) return;

    btn.disabled = true;
    btn.textContent = "Closing…";

    try {
      const res = await fetch(`${API_BASE_URL}/trading/autotrade/close/${symbol}`, {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      renderAutotradeStatus(data);
    } catch (err) {
      alert(`Failed to close ${symbol}: ${err.message}`);
      btn.disabled = false;
      btn.textContent = "Close Position";
    }
  });
}

// ── Trading Universe Editor ─────────────────────────────────────────────────
const universeTagsEl   = document.getElementById("universe-tags");
const universeInput    = document.getElementById("universe-input");
const universeAddBtn   = document.getElementById("universe-add-btn");
const universeResetBtn = document.getElementById("universe-reset-btn");
const universeCountEl  = document.getElementById("universe-count");

let currentUniverse    = [];   // active symbols
let isCustomUniverse   = false;

function renderUniverseTags() {
  if (!universeTagsEl) return;
  universeTagsEl.innerHTML = currentUniverse.map((sym) => {
    const s = esc(sym);
    return `
    <span class="universe-tag${isCustomUniverse ? ' is-custom' : ''}">
      ${s}
      <button class="universe-tag-remove" data-symbol="${s}" title="Remove ${s}">✕</button>
    </span>`;
  }).join("");
  if (universeCountEl) {
    universeCountEl.textContent = `${currentUniverse.length} symbol${currentUniverse.length !== 1 ? "s" : ""}${isCustomUniverse ? " (custom)" : ""}`;
  }
  if (universeResetBtn) {
    universeResetBtn.style.display = isCustomUniverse ? "" : "none";
  }
}

async function loadUniverse() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/autotrade/universe`, { credentials: "include" });
    if (!res.ok) return;
    const data = await res.json();
    currentUniverse  = data.active || [];
    isCustomUniverse = data.is_custom || false;
    renderUniverseTags();
  } catch { /* silent */ }
}

async function saveUniverse(symbols) {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/autotrade/universe`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    currentUniverse  = data.active || [];
    isCustomUniverse = data.is_custom || false;
    renderUniverseTags();
    // If symbols were removed, the backend purged their study insights.
    // Refresh the Study panel so the dropped stocks disappear.
    if (data.study_insights_purged > 0) loadStudyInsights();
  } catch (err) {
    console.error("Failed to save universe:", err);
  }
}

async function resetUniverse() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/autotrade/universe`, {
      method: "DELETE",
      credentials: "include",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    currentUniverse  = data.active || [];
    isCustomUniverse = false;
    renderUniverseTags();
    if (data.study_insights_purged > 0) loadStudyInsights();
  } catch (err) {
    console.error("Failed to reset universe:", err);
  }
}

function addSymbolToUniverse() {
  if (!universeInput) return;
  const sym = universeInput.value.trim().toUpperCase();
  if (!sym || !/^[A-Z]{1,10}$/.test(sym)) return;
  if (currentUniverse.includes(sym)) {
    universeInput.value = "";
    return;
  }
  const updated = [...currentUniverse, sym];
  universeInput.value = "";
  saveUniverse(updated);
}

if (universeAddBtn) {
  universeAddBtn.addEventListener("click", addSymbolToUniverse);
}
if (universeInput) {
  universeInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addSymbolToUniverse(); }
  });
}
if (universeResetBtn) {
  universeResetBtn.addEventListener("click", resetUniverse);
}
if (universeTagsEl) {
  universeTagsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".universe-tag-remove");
    if (!btn) return;
    const sym = btn.dataset.symbol;
    const updated = currentUniverse.filter((s) => s !== sym);
    if (updated.length === 0) {
      resetUniverse();
    } else {
      saveUniverse(updated);
    }
  });
}

// ── Wallet ──────────────────────────────────────────────────────────────────
const wPortfolio    = document.getElementById("w-portfolio");
const wCash         = document.getElementById("w-cash");
const wBuyingPower  = document.getElementById("w-buying-power");
const wUnsettledHint = document.getElementById("w-unsettled-hint");
const wPdtHint       = document.getElementById("w-pdt-hint");
const wUnrealizedPl = document.getElementById("w-unrealized-pl");
const walletPositions    = document.getElementById("wallet-positions");
const walletOrders       = document.getElementById("wallet-orders");
const walletRefreshBtn   = document.getElementById("wallet-refresh-btn");
const walletModeBadge    = document.getElementById("wallet-mode-badge");
const walletModeLabel    = document.getElementById("wallet-mode-label");
const closeAllPosBtn     = document.getElementById("close-all-positions-btn");
const orderTabs          = document.getElementById("order-tabs");
let currentOrderStatus   = "closed";

// Live 1-second polling for open positions
let positionsLiveInterval = null;
let lastRenderedSymbols   = new Set();

async function loadWallet() {
  await Promise.all([loadAccount(), loadPositions(), loadOrders(currentOrderStatus)]);
}

// ── Account summary ──
async function loadAccount() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/account`, { credentials: "include" });
    if (res.status === 503) {
      [wPortfolio, wCash, wBuyingPower, wUnrealizedPl].forEach((el) => { el.textContent = "—"; });
      walletModeBadge.textContent = "NO API KEY";
      walletModeBadge.className = "trade-mode-badge";
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const a = await res.json();
    currentAccountState = a;
    // Re-evaluate pre-trade gates on any currently-rendered approval cards
    // and wallet Close buttons so they update immediately when the account
    // snapshot changes (e.g. daytrade_count ticks up mid-session).
    refreshApprovalGates();
    refreshPositionCloseGates();

    walletModeBadge.textContent = a.paper ? "PAPER" : "LIVE";
    walletModeBadge.className = `trade-mode-badge ${a.paper ? "paper" : "live"}`;
    if (walletModeLabel) {
      walletModeLabel.textContent = a.paper ? "Paper Trading Account" : "Live Cash Account — real money";
    }
    // Warn the top bar once per page load when running live so the user can
    // see at a glance that orders will hit a real broker.
    document.body.classList.toggle("live-trading", !a.paper);

    wPortfolio.textContent = fmtUSD(a.portfolio_value);
    wCash.textContent = fmtUSD(a.cash);
    // Today's P&L tile, preferred source: equity − last_equity (ground
    // truth). last_equity arrives once the staged account-route passthrough
    // deploys (next boundary restart); until then this stays null and the
    // tile uses the status-payload fallback.
    dailyPnlEquity = (typeof a.last_equity === "number" && a.last_equity > 0
                      && typeof a.equity === "number")
      ? a.equity - a.last_equity : null;
    renderDailyPnl();
    // On a live cash account, show settled_cash — the backend computes this
    // locally (cash minus today's unsettled sell proceeds) so it's correct
    // even when Alpaca's non_marginable_buying_power reports 0. Only falls
    // back to raw buying_power on paper or if the field is missing.
    const liveSettled = !a.paper && typeof a.settled_cash === "number";
    wBuyingPower.textContent = fmtUSD(liveSettled ? a.settled_cash : a.buying_power);
    if (wUnsettledHint) {
      if (!a.paper && a.unsettled_cash > 0) {
        wUnsettledHint.textContent = `${fmtUSD(a.unsettled_cash)} unsettled (T+1)`;
        wUnsettledHint.hidden = false;
      } else {
        wUnsettledHint.textContent = "";
        wUnsettledHint.hidden = true;
      }
    }
    if (wPdtHint) {
      if (a.pattern_day_trader) {
        wPdtHint.textContent = "⚠️ PDT flagged — closing only";
        wPdtHint.classList.add("danger");
        wPdtHint.hidden = false;
      } else if (a.pdt_applies && a.pdt_at_risk) {
        wPdtHint.textContent = `⚠️ ${a.pdt_daytrades_remaining} day-trade${a.pdt_daytrades_remaining === 1 ? "" : "s"} left before PDT flag`;
        wPdtHint.classList.remove("danger");
        wPdtHint.hidden = false;
      } else if (a.pdt_applies) {
        wPdtHint.textContent = `${a.daytrade_count}/3 day-trades in last 5 days`;
        wPdtHint.classList.remove("danger");
        wPdtHint.hidden = false;
      } else {
        wPdtHint.textContent = "";
        wPdtHint.classList.remove("danger");
        wPdtHint.hidden = true;
      }
    }

    const pl = a.unrealized_pl;
    const sign = pl >= 0 ? "+" : "";
    wUnrealizedPl.textContent = `${sign}${fmtUSD(pl)}`;
    wUnrealizedPl.className = `wallet-card-value ${pl >= 0 ? "positive" : "negative"}`;
  } catch (err) {
    wPortfolio.textContent = "—";
    console.error("Wallet account error:", err);
  }
}

// ── Positions ──
async function loadPositions() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/positions`, { credentials: "include" });
    if (res.status === 503) {
      walletPositions.innerHTML = `<p class="wallet-empty">Connect your Alpaca API key to see positions.</p>`;
      stopPositionsLivePolling();
      lastRenderedSymbols = new Set();
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const positions = await res.json();

    if (!positions.length) {
      walletPositions.innerHTML = `<p class="wallet-empty">No open positions</p>`;
      closeAllPosBtn.classList.add("hidden");
      stopPositionsLivePolling();
      lastRenderedSymbols = new Set();
      return;
    }

    closeAllPosBtn.classList.remove("hidden");

    // Fast path: if the same set of symbols is still open, patch values in place
    // (no flicker, preserves expand state, avoids DOM churn on 1-second polls).
    const newSymbols = new Set(positions.map((p) => p.symbol));
    const sameShape =
      newSymbols.size === lastRenderedSymbols.size &&
      [...newSymbols].every((s) => lastRenderedSymbols.has(s));

    if (sameShape) {
      updatePositionsInPlace(positions);
      startPositionsLivePolling();
      return;
    }

    // Remember expanded positions across re-renders
    const expandedPos = new Set(
      walletPositions.querySelectorAll(".wallet-pos-card.expanded")
    );
    const expandedPosSymbols = new Set();
    expandedPos.forEach((el) => expandedPosSymbols.add(el.dataset.symbol));

    walletPositions.innerHTML = positions.map((p) => {
      const pl = p.unrealized_pl;
      const plSign = pl >= 0 ? "+" : "";
      const plCls = pl >= 0 ? "positive" : "negative";
      const plPct = (p.unrealized_plpc * 100).toFixed(2);
      const mktVal = p.market_value ? fmtUSD(p.market_value) : fmtUSD(p.current_price * p.qty);
      const costBasis = p.cost_basis ? fmtUSD(p.cost_basis) : fmtUSD(p.avg_entry_price * p.qty);
      const side = parseFloat(p.qty) > 0 ? "LONG" : "SHORT";
      const sideCls = side === "LONG" ? "buy" : "sell";
      const changePct = p.change_today != null ? (p.change_today * 100).toFixed(2) : null;
      const isOpen = expandedPosSymbols.has(p.symbol);

      // Time info from autotrade context
      let openedStr = "—";
      let holdStr = "—";
      if (p.opened_at) {
        try {
          const d = new Date(p.opened_at);
          openedStr = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
        } catch (_) {}
      }
      if (p.hold_seconds != null) {
        const hm = Math.floor(p.hold_seconds / 60);
        const hs = p.hold_seconds % 60;
        holdStr = hm > 0 ? `${hm}m ${hs}s` : `${hs}s`;
      }

      const pSym = esc(p.symbol);
      return `
        <div class="wallet-pos-card ${isOpen ? "expanded" : ""}" data-symbol="${pSym}">
          <div class="wallet-pos-row" data-symbol="${pSym}">
            <span class="wallet-pos-symbol">${pSym}</span>
            <span class="wallet-pos-qty">${Math.abs(p.qty)} shares</span>
            <span class="wallet-pos-pl ${plCls}" data-field="row-pl">${plSign}${fmtUSD(pl)} (${plSign}${plPct}%)</span>
            <button class="wallet-pos-close" data-symbol="${pSym}" data-opened-today="${p.opened_today ? "1" : "0"}" type="button">Close</button>
            <span class="wallet-pos-chevron">&#9660;</span>
          </div>
          <div class="wallet-pos-expand">
            <div class="wallet-pos-detail-grid">
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Side</span>
                <span class="autotrade-log-side ${sideCls}" style="font-size:0.7rem;padding:2px 8px;">${side}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Avg Entry</span>
                <span class="wallet-pos-val">${fmtUSD(p.avg_entry_price)}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Current</span>
                <span class="wallet-pos-val" data-field="current">${fmtUSD(p.current_price)}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Target Exit</span>
                <span class="wallet-pos-val positive">${p.target ? esc(p.target) : "—"}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Stop Loss</span>
                <span class="wallet-pos-val negative">${p.stop_loss ? esc(p.stop_loss) : "—"}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">R/R</span>
                <span class="wallet-pos-val">${p.risk_reward ? esc(p.risk_reward) : "—"}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Purchased</span>
                <span class="wallet-pos-val">${openedStr}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Held For</span>
                <span class="wallet-pos-val" data-field="held">${holdStr}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Market Value</span>
                <span class="wallet-pos-val" data-field="mkt-val">${mktVal}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Cost Basis</span>
                <span class="wallet-pos-val">${costBasis}</span>
              </div>
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Unrealized P&L</span>
                <span class="wallet-pos-val ${plCls}" data-field="detail-pl">${plSign}${fmtUSD(pl)}</span>
              </div>
              ${changePct != null ? `
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">Today</span>
                <span class="wallet-pos-val ${parseFloat(changePct) >= 0 ? "positive" : "negative"}" data-field="today">${parseFloat(changePct) >= 0 ? "+" : ""}${changePct}%</span>
              </div>` : ""}
              ${p.confidence ? `
              <div class="wallet-pos-detail">
                <span class="wallet-pos-label">AI Confidence</span>
                <span class="wallet-pos-val">${(p.confidence * 100).toFixed(0)}%</span>
              </div>` : ""}
            </div>
            ${p.setup ? `<div class="wallet-pos-extra"><span class="wallet-pos-label">Setup</span> ${esc(p.setup)}</div>` : ""}
          </div>
        </div>`;
    }).join("");

    lastRenderedSymbols = newSymbols;
    refreshPositionCloseGates();
    startPositionsLivePolling();
  } catch (err) {
    walletPositions.innerHTML = `<p class="wallet-error">Could not load positions — ${esc(err.message)}</p>`;
  }
}

// Patch only the live-changing values into existing position cards.
// Used on 1-second polls to avoid DOM rebuild (no flicker, no focus loss).
function updatePositionsInPlace(positions) {
  for (const p of positions) {
    const card = walletPositions.querySelector(`.wallet-pos-card[data-symbol="${p.symbol}"]`);
    if (!card) continue;

    const pl     = p.unrealized_pl;
    const plSign = pl >= 0 ? "+" : "";
    const plCls  = pl >= 0 ? "positive" : "negative";
    const plPct  = (p.unrealized_plpc * 100).toFixed(2);
    const mktVal = p.market_value ? fmtUSD(p.market_value) : fmtUSD(p.current_price * p.qty);

    const rowPl = card.querySelector('[data-field="row-pl"]');
    if (rowPl) {
      rowPl.textContent = `${plSign}${fmtUSD(pl)} (${plSign}${plPct}%)`;
      rowPl.classList.toggle("positive", pl >= 0);
      rowPl.classList.toggle("negative", pl < 0);
    }

    const setText = (field, val) => {
      const el = card.querySelector(`[data-field="${field}"]`);
      if (el && el.textContent !== val) el.textContent = val;
    };
    setText("current", fmtUSD(p.current_price));
    setText("mkt-val", mktVal);

    const detailPl = card.querySelector('[data-field="detail-pl"]');
    if (detailPl) {
      detailPl.textContent = `${plSign}${fmtUSD(pl)}`;
      detailPl.classList.toggle("positive", pl >= 0);
      detailPl.classList.toggle("negative", pl < 0);
    }

    if (p.change_today != null) {
      const todayPct = (p.change_today * 100).toFixed(2);
      const todayEl = card.querySelector('[data-field="today"]');
      if (todayEl) {
        todayEl.textContent = `${parseFloat(todayPct) >= 0 ? "+" : ""}${todayPct}%`;
        todayEl.classList.toggle("positive", parseFloat(todayPct) >= 0);
        todayEl.classList.toggle("negative", parseFloat(todayPct) < 0);
      }
    }

    if (p.hold_seconds != null) {
      const hm = Math.floor(p.hold_seconds / 60);
      const hs = p.hold_seconds % 60;
      setText("held", hm > 0 ? `${hm}m ${hs}s` : `${hs}s`);
    }
  }
}

function startPositionsLivePolling() {
  if (positionsLiveInterval) return;
  // Wallet + position updates are primarily push-driven via WebSocket
  // (see ws.js `wallet_update` handler). This 15s interval is a safety net
  // for: (a) missed events / transient WS drop, (b) live P&L ticking while
  // positions are open (Alpaca trade_updates only fires on fills, not on
  // intraday price moves). 15s keeps us well under Alpaca's rate limits.
  positionsLiveInterval = setInterval(() => {
    loadPositions();
    loadAccount();
  }, 15_000);
}

function stopPositionsLivePolling() {
  if (!positionsLiveInterval) return;
  clearInterval(positionsLiveInterval);
  positionsLiveInterval = null;
}

// ── Orders ──
async function loadOrders(status) {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/orders?order_status=${status}`, { credentials: "include" });
    if (res.status === 503) {
      walletOrders.innerHTML = `<p class="wallet-empty">Connect your Alpaca API key to see orders.</p>`;
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const orders = await res.json();

    if (!orders.length) {
      const label = status === "closed" ? "filled" : status === "open" ? "open" : "";
      walletOrders.innerHTML = `<p class="wallet-empty">No ${label} orders</p>`;
      return;
    }

    walletOrders.innerHTML = orders.slice(0, 50).map((o) => {
      const price = o.filled_avg_price
        ? fmtUSD(o.filled_avg_price)
        : o.limit_price ? `Limit ${fmtUSD(o.limit_price)}` : "Market";
      const statusCls = esc((o.status || "").toLowerCase().replace(/ /g, "_"));
      const time = fmtTime(o.filled_at || o.submitted_at);
      return `
        <div class="wallet-order-row">
          <span class="wallet-order-side ${esc(o.side)}">${esc(o.side)}</span>
          <span class="wallet-order-symbol">${esc(o.symbol)}</span>
          <span class="wallet-order-detail">${esc(o.qty)} @ ${price}</span>
          <span class="wallet-order-time">${time}</span>
          <span class="wallet-order-status ${statusCls}">${esc(o.status)}</span>
        </div>`;
    }).join("");
  } catch (err) {
    walletOrders.innerHTML = `<p class="wallet-error">Could not load orders — ${esc(err.message)}</p>`;
  }
}

// ── Wallet event listeners ──
if (walletRefreshBtn) {
  walletRefreshBtn.addEventListener("click", () => {
    walletRefreshBtn.disabled = true;
    Promise.all([loadWallet(), fetchAutotradeStatus()])
      .finally(() => { walletRefreshBtn.disabled = false; });
  });
}

// ── Paper ↔ live trading-mode switcher ──────────────────────────────────────
// The wallet mode badge doubles as a click-to-switch button. Clicking opens a
// confirmation modal that:
//   • fetches GET /trading/mode to see preflight state (configured flags,
//     autotrade_running, open_positions, pending_approvals)
//   • populates the target mode + any hard blockers / soft warnings
//   • disables the Confirm button when a hard blocker is present
// On Confirm we POST /trading/mode. The backend broadcasts a
// `trading_mode_changed` WS event + `wallet_update`, which naturally refreshes
// the account view and re-renders the badge via loadAccount().
const modeModal         = document.getElementById("mode-modal");
const modeModalTitle    = document.getElementById("mode-modal-title");
const modeModalClose    = document.getElementById("mode-modal-close");
const modeModalCancel   = document.getElementById("mode-modal-cancel");
const modeModalConfirm  = document.getElementById("mode-modal-confirm");
const modeModalCopy     = document.getElementById("mode-modal-copy");
const modeModalHint     = document.getElementById("mode-modal-hint");
const modeFlowCurrent   = document.getElementById("mode-flow-current");
const modeFlowTarget    = document.getElementById("mode-flow-target");
const modeBlockers      = document.getElementById("mode-blockers");
const modeBlockersList  = document.getElementById("mode-blockers-list");
const modeForceStopWrap = document.getElementById("mode-force-stop-wrap");
const modeForceStopCb   = document.getElementById("mode-force-stop-cb");
const modeForceStopLbl  = document.getElementById("mode-force-stop-label");

let modeModalTarget = null;  // "paper" | "live" — the mode we're proposing to switch to

function setBadge(el, mode) {
  if (!el) return;
  el.textContent = mode === "live" ? "LIVE" : "PAPER";
  el.className = `trade-mode-badge ${mode === "live" ? "live" : "paper"}`;
}

function closeModeModal() {
  if (!modeModal) return;
  modeModal.classList.add("hidden");
  modeModalTarget = null;
  if (modeForceStopCb) modeForceStopCb.checked = false;
}

async function openModeModal() {
  if (!modeModal) return;
  // Reset UI to a neutral state while we fetch
  modeModalConfirm.disabled = true;
  modeModalConfirm.textContent = "Switch";
  modeBlockers.classList.add("hidden");
  modeBlockersList.innerHTML = "";
  modeForceStopWrap.classList.add("hidden");
  if (modeForceStopCb) modeForceStopCb.checked = false;
  modeModalCopy.textContent = "Loading…";
  modeModalCopy.classList.remove("danger");
  modeModalHint.textContent = "";
  modeModal.classList.remove("hidden");

  let info;
  try {
    const res = await fetch(`${API_BASE_URL}/trading/mode`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    info = await res.json();
  } catch (err) {
    modeModalCopy.textContent = `Could not load mode info: ${err.message}`;
    modeModalCopy.classList.add("danger");
    return;
  }

  const current = info.mode;                       // "paper" | "live"
  const target  = current === "paper" ? "live" : "paper";
  modeModalTarget = target;

  modeModalTitle.textContent = `Switch to ${target === "live" ? "LIVE" : "Paper"} Trading`;
  setBadge(modeFlowCurrent, current);
  setBadge(modeFlowTarget, target);

  // Body copy — make the real-money flip extra obvious
  if (target === "live") {
    modeModalCopy.innerHTML =
      "You are about to switch to <strong>LIVE trading</strong>. All subsequent " +
      "orders will use real money on your funded Alpaca account. Make sure your " +
      "position sizing and autotrade guards are calibrated for live markets.";
    modeModalCopy.classList.add("danger");
    modeModalConfirm.textContent = "Switch to LIVE";
    modeModalConfirm.dataset.target = "live";
  } else {
    modeModalCopy.textContent =
      "Switch back to the paper account. All new orders will use simulated " +
      "funds; open live positions will remain on the live account untouched.";
    modeModalCopy.classList.remove("danger");
    modeModalConfirm.textContent = "Switch to Paper";
    modeModalConfirm.dataset.target = "paper";
  }

  // Hard blockers: target unconfigured, open monitored positions
  const blockers = [];
  const targetConfigured = info[target]?.configured;
  if (!targetConfigured) {
    const upper = target.toUpperCase();
    blockers.push(
      `No credentials configured for ${target}. Set ALPACA_${upper}_API_KEY ` +
      `and ALPACA_${upper}_SECRET_KEY in .env and restart the backend.`
    );
  }
  const openPositions = Array.isArray(info.open_positions) ? info.open_positions : [];
  if (openPositions.length) {
    blockers.push(
      `Close all monitored autotrade positions first: ${openPositions.join(", ")}`
    );
  }
  if (blockers.length) {
    modeBlockersList.innerHTML = blockers.map((b) => `<li>${esc(b)}</li>`).join("");
    modeBlockers.classList.remove("hidden");
  }

  // Soft warning: running autotrade or pending approvals → offer force-stop
  const running  = !!info.autotrade_running;
  const pending  = info.pending_approvals | 0;
  if (running || pending > 0) {
    const parts = [];
    if (running) parts.push("the auto-trader is running");
    if (pending) parts.push(`${pending} pending approval${pending === 1 ? "" : "s"}`);
    modeForceStopLbl.textContent =
      `Stop ${parts.join(" and reject ")} before switching`;
    modeForceStopWrap.classList.remove("hidden");
  }

  // Credential hint (handy when the toggle is greyed out)
  const paperOK = info.paper?.configured ? "✓" : "✗";
  const liveOK  = info.live?.configured  ? "✓" : "✗";
  modeModalHint.textContent =
    `Paper keys ${paperOK}   •   Live keys ${liveOK}   •   persist: ${info.persist_path || "in-memory default"}`;

  // Enable Confirm only when there are no hard blockers
  modeModalConfirm.disabled = blockers.length > 0;
}

async function submitModeSwitch() {
  if (!modeModalTarget) return;
  const target = modeModalTarget;
  const forceStop = !!(modeForceStopCb && modeForceStopCb.checked);

  modeModalConfirm.disabled = true;
  modeModalConfirm.textContent = "Switching…";

  try {
    const res = await fetch(`${API_BASE_URL}/trading/mode`, {
      method:      "POST",
      credentials: "include",
      headers:     { "Content-Type": "application/json" },
      body:        JSON.stringify({ mode: target, force_stop: forceStop }),
    });
    const body = await res.json().catch(() => ({}));

    if (!res.ok) {
      // 409 preflight block: detail = {message, reasons: [...]}. Surface
      // every reason so the user can fix them in one pass instead of
      // whack-a-mole-ing through them one at a time.
      const detail  = body?.detail;
      let  message  = `HTTP ${res.status}`;
      if (detail && typeof detail === "object") {
        const reasons = Array.isArray(detail.reasons) ? detail.reasons : [];
        message = reasons.length ? reasons.join("\n• ") : (detail.message || message);
        if (reasons.length) message = "• " + message;
      } else if (typeof detail === "string") {
        message = detail;
      }
      showToast({
        title:    "Mode switch blocked",
        message,
        variant:  "warning",
        icon:     "⚠️",
        duration: 7000,
      });
      modeModalConfirm.disabled = false;
      modeModalConfirm.textContent = target === "live" ? "Switch to LIVE" : "Switch to Paper";
      return;
    }

    // Success — the backend has already broadcast `trading_mode_changed` +
    // `wallet_update`, so the badge will re-render on its own. We just close
    // the modal and toast.
    closeModeModal();
    const newMode = body?.mode_snapshot?.mode || target;
    showToast({
      title:   `Now trading in ${newMode === "live" ? "LIVE" : "paper"} mode`,
      message: newMode === "live"
        ? "Orders will use real money."
        : "Orders will use the Alpaca paper account.",
      variant: newMode === "live" ? "danger" : "success",
      icon:    newMode === "live" ? "🔴" : "📝",
    });
    // Belt-and-suspenders: refresh wallet immediately (the WS event should
    // also cover this, but we don't want a blank badge if the event is lost).
    loadWallet();
    fetchAutotradeStatus();
  } catch (err) {
    showToast({
      title:   "Mode switch failed",
      message: err.message,
      variant: "danger",
      icon:    "⚠️",
    });
    modeModalConfirm.disabled = false;
    modeModalConfirm.textContent = target === "live" ? "Switch to LIVE" : "Switch to Paper";
  }
}

// Wire up the badge, modal dismissal handlers, and Confirm.
if (walletModeBadge) {
  walletModeBadge.addEventListener("click", () => { openModeModal(); });
}
if (modeModalClose)  modeModalClose.addEventListener("click",  closeModeModal);
if (modeModalCancel) modeModalCancel.addEventListener("click", closeModeModal);
if (modeModal) {
  modeModal.addEventListener("click", (e) => {
    // Dismiss on backdrop click (but NOT on clicks inside the content).
    if (e.target instanceof HTMLElement && e.target.dataset.modeDismiss === "1") {
      closeModeModal();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modeModal.classList.contains("hidden")) {
      closeModeModal();
    }
  });
}
if (modeModalConfirm) {
  modeModalConfirm.addEventListener("click", submitModeSwitch);
}

// Order tab switching
if (orderTabs) {
  orderTabs.addEventListener("click", (e) => {
    const tab = e.target.closest(".wallet-tab");
    if (!tab) return;
    orderTabs.querySelectorAll(".wallet-tab").forEach((t) => t.classList.remove("wallet-tab-active"));
    tab.classList.add("wallet-tab-active");
    currentOrderStatus = tab.dataset.status;
    loadOrders(currentOrderStatus);
  });
}

// Close single position
if (walletPositions) {
  walletPositions.addEventListener("click", async (e) => {
    const btn = e.target.closest(".wallet-pos-close");
    if (!btn) return;
    e.stopPropagation();
    const symbol = btn.dataset.symbol;
    const openedToday = btn.dataset.openedToday === "1";
    // Pre-trade gate: closing a same-day-opened position on a live account
    // may complete a PDT day-trade or trigger a GFV. Block rather than submit.
    const gate = preTradeBlock({ side: "sell", openedToday });
    if (gate.blocked) {
      alert(`Cannot close ${symbol}: ${gate.reason}`);
      return;
    }
    if (!confirm(`Close entire ${symbol} position at market?`)) return;
    btn.disabled = true;
    btn.textContent = "Closing…";
    try {
      const res = await fetch(`${API_BASE_URL}/trading/positions/${symbol}`, {
        method: "DELETE", credentials: "include",
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch {}
        throw new Error(detail);
      }
      await loadWallet();
    } catch (err) {
      alert(`Failed to close ${symbol}: ${err.message}`);
      btn.disabled = false;
      btn.textContent = "Close";
    }
  });

  // Click a position row → expand/collapse detail panel
  walletPositions.addEventListener("click", (e) => {
    if (e.target.closest(".wallet-pos-close")) return;
    const row = e.target.closest(".wallet-pos-row");
    if (!row) return;
    const card = row.closest(".wallet-pos-card");
    if (card) card.classList.toggle("expanded");
  });
}

// Close all positions
if (closeAllPosBtn) {
  closeAllPosBtn.addEventListener("click", async () => {
    // Pre-trade gate: if any same-day-opened position would trigger PDT or GFV
    // on close, block the entire bulk action — safer than partially closing and
    // letting the broker reject the rest mid-stream.
    const closeButtons = walletPositions
      ? [...walletPositions.querySelectorAll(".wallet-pos-close")]
      : [];
    const risky = closeButtons
      .map((b) => ({
        symbol: b.dataset.symbol,
        gate: preTradeBlock({ side: "sell", openedToday: b.dataset.openedToday === "1" }),
      }))
      .filter((x) => x.gate.blocked);
    if (risky.length) {
      const syms = risky.map((x) => x.symbol).join(", ");
      alert(
        `Cannot Close All: ${syms} would trigger PDT/GFV.\n\n` +
        `${risky[0].gate.reason}\n\n` +
        `Close positions held overnight individually, or wait until proceeds settle.`
      );
      return;
    }
    if (!confirm("Close ALL open positions at market?")) return;
    closeAllPosBtn.disabled = true;
    closeAllPosBtn.textContent = "Closing…";
    try {
      const res = await fetch(`${API_BASE_URL}/trading/positions`, {
        method: "DELETE", credentials: "include",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadWallet();
    } catch (err) {
      alert(`Failed to close positions: ${err.message}`);
    } finally {
      closeAllPosBtn.disabled = false;
      closeAllPosBtn.textContent = "Close All";
    }
  });
}

// ── Performance Dashboard ──────────────────────────────────────────────────
const perfDashboard    = document.getElementById("perf-dashboard");
const perfStatsGrid    = document.getElementById("perf-stats-grid");
const perfChartWrap    = document.getElementById("perf-chart-wrap");
const perfBreakdowns   = document.getElementById("perf-breakdowns");
const perfHistoryList  = document.getElementById("perf-history-list");
const perfPeriodSelect = document.getElementById("perf-period");
const perfRefreshBtn   = document.getElementById("perf-refresh-btn");

let perfChart = null;

async function loadPerformance() {
  const days = perfPeriodSelect ? perfPeriodSelect.value : 7;
  try {
    const [perfRes, histRes] = await Promise.all([
      fetch(`${API_BASE_URL}/trading/performance?days=${days}`, { credentials: "include" }),
      fetch(`${API_BASE_URL}/trading/history?days=${days}&limit=50`, { credentials: "include" }),
    ]);
    if (!perfRes.ok || !histRes.ok) return;
    const perf = await perfRes.json();
    const hist = await histRes.json();

    if (perf.total_trades === 0 && hist.count === 0) {
      perfDashboard.classList.add("hidden");
      return;
    }

    perfDashboard.classList.remove("hidden");
    renderPerfStats(perf);
    renderPerfChart(perf);
    renderPerfBreakdowns(perf);
    renderPerfHistory(hist.trades);
    loadLessons();
  } catch (err) {
    console.error("Performance load failed:", err);
  }
}

function renderPerfStats(p) {
  const plCls = p.total_pnl >= 0 ? "positive" : "negative";
  const plSign = p.total_pnl >= 0 ? "+" : "";
  const avgHold = p.avg_hold_seconds > 0
    ? `${Math.floor(p.avg_hold_seconds / 60)}m`
    : "—";

  perfStatsGrid.innerHTML = `
    <div class="perf-stat">
      <span class="perf-stat-value ${plCls}">${plSign}${fmtUSD(p.total_pnl)}</span>
      <span class="perf-stat-label">Total P&L</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value">${esc(p.total_trades)}</span>
      <span class="perf-stat-label">Trades</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value ${p.win_rate >= 50 ? "positive" : "negative"}">${esc(p.win_rate)}%</span>
      <span class="perf-stat-label">Win Rate</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value positive">${fmtUSD(p.avg_win)}</span>
      <span class="perf-stat-label">Avg Win</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value negative">${fmtUSD(p.avg_loss)}</span>
      <span class="perf-stat-label">Avg Loss</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value">${p.profit_factor != null ? p.profit_factor.toFixed(2) : "—"}</span>
      <span class="perf-stat-label">Profit Factor</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value">${p.avg_r_multiple != null ? p.avg_r_multiple.toFixed(2) + "R" : "—"}</span>
      <span class="perf-stat-label">Avg R</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value">${avgHold}</span>
      <span class="perf-stat-label">Avg Hold</span>
    </div>
    <div class="perf-stat">
      <span class="perf-stat-value negative">-${fmtUSD(p.max_drawdown)}</span>
      <span class="perf-stat-label">Max Drawdown</span>
    </div>
  `;
}

function renderPerfChart(p) {
  if (!p.cumulative_pnl || p.cumulative_pnl.length < 2) {
    perfChartWrap.innerHTML = "";
    return;
  }

  // Use Lightweight Charts if available (already loaded for candlestick)
  if (typeof LightweightCharts === "undefined") {
    perfChartWrap.innerHTML = "";
    return;
  }

  perfChartWrap.innerHTML = "";
  if (perfChart) { perfChart.remove(); perfChart = null; }

  const chartW = perfChartWrap.clientWidth || 600;
  perfChart = LightweightCharts.createChart(perfChartWrap, {
    width: chartW,
    height: 180,
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: "#8a9bb5",
      fontSize: 11,
      fontFamily: "'SF Mono', 'Fira Code', 'Cascadia Code', monospace",
    },
    grid: {
      vertLines: { color: "rgba(111,143,176,0.03)" },
      horzLines: { color: "rgba(111,143,176,0.06)" },
    },
    rightPriceScale: {
      borderColor: "rgba(111,143,176,0.10)",
    },
    timeScale: {
      borderColor: "rgba(111,143,176,0.10)",
    },
    localization: {
      priceFormatter: (p) => (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2),
    },
    handleScroll: false,
    handleScale: false,
  });

  const areaSeries = perfChart.addAreaSeries({
    lineColor: "rgba(38,166,154,0.8)",
    topColor: "rgba(38,166,154,0.22)",
    bottomColor: "rgba(38,166,154,0.01)",
    lineWidth: 2,
  });

  const data = p.cumulative_pnl.map((d) => ({ time: d.date, value: d.pnl }));
  areaSeries.setData(data);
  perfChart.timeScale().fitContent();
}

function renderPerfBreakdowns(p) {
  let html = "";

  // By Symbol
  const symbols = Object.entries(p.by_symbol || {});
  if (symbols.length > 0) {
    html += `<div class="perf-breakdown"><h4>By Symbol</h4><div class="perf-breakdown-list">`;
    for (const [sym, s] of symbols.slice(0, 8)) {
      const cls = s.pnl >= 0 ? "positive" : "negative";
      html += `
        <div class="perf-breakdown-row">
          <strong>${esc(sym)}</strong>
          <span>${esc(s.trades)} trades</span>
          <span>${esc(s.win_rate)}% win</span>
          <span class="${cls}">${s.pnl >= 0 ? "+" : ""}${fmtUSD(s.pnl)}</span>
        </div>`;
    }
    html += `</div></div>`;
  }

  // By Setup
  const setups = Object.entries(p.by_setup || {});
  if (setups.length > 0) {
    html += `<div class="perf-breakdown"><h4>By Setup</h4><div class="perf-breakdown-list">`;
    for (const [setup, s] of setups.slice(0, 8)) {
      const cls = s.pnl >= 0 ? "positive" : "negative";
      html += `
        <div class="perf-breakdown-row">
          <strong>${esc(setup)}</strong>
          <span>${esc(s.trades)} trades</span>
          <span>${esc(s.win_rate)}% win</span>
          <span class="${cls}">${s.pnl >= 0 ? "+" : ""}${fmtUSD(s.pnl)}</span>
        </div>`;
    }
    html += `</div></div>`;
  }

  perfBreakdowns.innerHTML = html;
}

function renderPerfHistory(trades) {
  // Cache for the Lessons section to reuse
  setRecentTrades(trades || []);

  if (!trades || !trades.length) {
    perfHistoryList.innerHTML = `<p class="muted-copy">No closed trades yet.</p>`;
    return;
  }

  perfHistoryList.innerHTML = trades.map((t) => {
    const time = new Date(t.opened_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })
      + " " + new Date(t.opened_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    const plCls = (t.pnl || 0) >= 0 ? "positive" : "negative";
    const plSign = (t.pnl || 0) >= 0 ? "+" : "";
    const holdMin = t.hold_duration_seconds ? Math.floor(t.hold_duration_seconds / 60) : null;
    const holdStr = holdMin != null ? `${holdMin}m` : "";
    const exitLabels = { trailing_stop: "Trail", max_hold_time: "Time", eod_exit: "EOD", external: "Ext" };
    const exitTag = t.exit_reason ? (exitLabels[t.exit_reason] || t.exit_reason) : "";
    const rStr = t.r_multiple != null ? `${t.r_multiple > 0 ? "+" : ""}${t.r_multiple.toFixed(1)}R` : "";
    const isClosedLoss = t.pnl != null && t.pnl < 0 && t.closed_at;
    const reviewBtn = isClosedLoss
      ? `<button class="perf-trade-review" data-trade-id="${esc(t.id)}" title="Have the AI review this trade and add a lesson">Review</button>`
      : `<span class="perf-trade-review-spacer"></span>`;

    return `
      <div class="perf-trade-row" data-trade-id="${esc(t.id)}">
        <span class="perf-trade-time">${time}</span>
        <span class="autotrade-log-side ${esc(t.side)}">${esc(t.side)}</span>
        <span class="perf-trade-symbol">${esc(t.symbol)}</span>
        <span class="perf-trade-setup">${esc(t.setup)}</span>
        <span class="perf-trade-entry">$${(t.entry_price || 0).toFixed(2)}</span>
        <span class="perf-trade-exit">${t.exit_price ? "$" + t.exit_price.toFixed(2) : "—"}</span>
        <span class="perf-trade-pnl ${plCls}">${t.pnl != null ? plSign + fmtUSD(t.pnl) : "open"}</span>
        <span class="perf-trade-r ${plCls}">${rStr}</span>
        <span class="perf-trade-hold">${holdStr}</span>
        <span class="perf-trade-exit-reason">${esc(exitTag)}</span>
        ${reviewBtn}
      </div>`;
  }).join("");
}

// ── AI Lessons ──────────────────────────────────────────────────────────────
// Ownership moved to lessons.js — main.js only calls loadLessons() and
// setRecentTrades(). The click/refresh handlers are wired by initLessons().
initLessons();

// Generic collapsible toggle — any element with .collapsible-toggle toggles its
// parent .collapsible-panel's data-open attribute. Used by both the lessons
// and study panels.
document.addEventListener("click", (e) => {
  // Ignore clicks on buttons (review, refresh, run-study) inside the header
  if (e.target.closest("button") && !e.target.closest(".collapsible-chevron")) return;
  const toggle = e.target.closest(".collapsible-toggle");
  if (!toggle) return;
  const panel = toggle.closest(".collapsible-panel");
  if (!panel) return;
  const open = panel.getAttribute("data-open") === "true";
  panel.setAttribute("data-open", open ? "false" : "true");
});

// ── After-Hours AI Study ──────────────────────────────────────────────────
// Ownership moved to study.js — main.js only calls initStudy() and
// loadStudyInsights() on page load.
initStudy();

// v7.16 (2026-05-07): readiness guard — heartbeat watchdog + multi-stage
// banner. Polls /health every 30s with 5-sec timeout; renders red banner
// if backend hangs. Otherwise reads /trading/readiness for the stage.
initReadinessGuard();

// 2026-05-28: nudge the operator to reload after 24h+ uptime. Long-running
// browser tabs with live charts + WebSockets can OOM the content process.
// See staleness.js for full rationale.
initStalenessMonitor();

// Load study insights on page load
loadStudyInsights();

// Load performance on page load and when period changes
if (perfPeriodSelect) {
  perfPeriodSelect.addEventListener("change", loadPerformance);
}
if (perfRefreshBtn) {
  perfRefreshBtn.addEventListener("click", loadPerformance);
}

// Load on init (after session is established)
renderSession().then(() => {}).catch(() => {});
// We hook into the existing renderSession chain — performance loads after wallet
const origLoadWallet = loadWallet;
loadWallet = async function () {
  await origLoadWallet();
  await loadPerformance();
};
