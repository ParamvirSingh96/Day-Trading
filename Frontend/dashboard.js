import { API_BASE_URL } from "./config.js";
import { CANDLE_UP, CANDLE_DOWN } from "./chart-helpers.js";

/** Shared options for small dashboard charts. */
function dashChartOpts(w, h) {
  return {
    width: w, height: h,
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
    rightPriceScale: { borderColor: "rgba(111,143,176,0.10)" },
    timeScale:       { borderColor: "rgba(111,143,176,0.10)" },
    localization:    { priceFormatter: (p) => (p >= 0 ? "+$" : "-$") + Math.abs(p).toFixed(2) },
    handleScroll: true,
    handleScale: true,
  };
}

// ── DOM refs ─────────────────────────────────────────────────────────────────
const periodBtnGroup = document.getElementById("period-btn-group");
const dashEyebrow    = document.getElementById("dash-eyebrow");
const dashRefreshBtn = document.getElementById("dash-refresh-btn");

const dsTotalPnl     = document.getElementById("ds-total-pnl");
const dsWinRate      = document.getElementById("ds-win-rate");
const dsTrades       = document.getElementById("ds-trades");
const dsProfitFactor = document.getElementById("ds-profit-factor");
const dsAvgR         = document.getElementById("ds-avg-r");
const dsMaxDd        = document.getElementById("ds-max-dd");
const dsAvgWin       = document.getElementById("ds-avg-win");
const dsAvgLoss      = document.getElementById("ds-avg-loss");

const pnlChartWrap   = document.getElementById("pnl-chart-wrap");
const pnlChartStatus = document.getElementById("pnl-chart-status");
const dailyChartWrap = document.getElementById("daily-chart-wrap");
const dailyChartStatus = document.getElementById("daily-chart-status");

const bySymbolWrap   = document.getElementById("by-symbol-wrap");
const bySetupWrap    = document.getElementById("by-setup-wrap");
const byHourWrap     = document.getElementById("by-hour-wrap");
const bestWorstWrap  = document.getElementById("best-worst-wrap");
const tradeLogWrap    = document.getElementById("trade-log-wrap");
const tradeLogToggle  = document.getElementById("trade-log-toggle");
const lessonsWrap     = document.getElementById("lessons-wrap");

let currentDays = 30;
let pnlChart = null;
let dailyChart = null;

// Recent-trades pagination. Default shows the most recent 50 to keep the
// initial dashboard render snappy; click "Show all" to pull up to 5000
// (effectively unlimited for a single-user account). The toggle button
// sits next to the "Recent Trades" heading and stays hidden until we see
// evidence there are more rows than we're currently rendering.
const TRADE_LOG_PREVIEW_LIMIT = 50;
const TRADE_LOG_FULL_LIMIT    = 5000;
let tradeLogLimit = TRADE_LOG_PREVIEW_LIMIT;

// ── Utilities ────────────────────────────────────────────────────────────────
function fmtUSD(n) {
  if (n == null) return "—";
  const sign = n >= 0 ? "+" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPct(n) {
  if (n == null) return "—";
  return n.toFixed(1) + "%";
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

function fmtDuration(seconds) {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function colorClass(val) {
  if (val > 0) return "positive";
  if (val < 0) return "negative";
  return "";
}

// ── Stat cards ───────────────────────────────────────────────────────────────
function renderStats(d) {
  dsTotalPnl.textContent = fmtUSD(d.total_pnl);
  dsTotalPnl.className = `dash-stat-value ${colorClass(d.total_pnl)}`;

  dsWinRate.textContent = d.total_trades > 0 ? fmtPct(d.win_rate) : "—";
  dsWinRate.className = `dash-stat-value ${d.win_rate >= 50 ? "positive" : d.win_rate > 0 ? "negative" : ""}`;

  dsTrades.textContent = d.total_trades > 0 ? `${d.wins}W / ${d.losses}L (${d.total_trades})` : "—";

  dsProfitFactor.textContent = d.profit_factor != null ? d.profit_factor.toFixed(2) : "—";
  dsProfitFactor.className = `dash-stat-value ${d.profit_factor != null ? (d.profit_factor >= 1.5 ? "positive" : d.profit_factor >= 1 ? "" : "negative") : ""}`;

  dsAvgR.textContent = d.avg_r_multiple != null ? d.avg_r_multiple.toFixed(2) + "R" : "—";
  dsAvgR.className = `dash-stat-value ${d.avg_r_multiple != null ? colorClass(d.avg_r_multiple) : ""}`;

  dsMaxDd.textContent = d.max_drawdown > 0 ? "-" + fmtUSD(d.max_drawdown).replace("+", "") : "—";
  dsMaxDd.className = `dash-stat-value ${d.max_drawdown > 0 ? "negative" : ""}`;

  dsAvgWin.textContent = d.avg_win > 0 ? fmtUSD(d.avg_win) : "—";
  dsAvgWin.className = `dash-stat-value ${d.avg_win > 0 ? "positive" : ""}`;

  dsAvgLoss.textContent = d.avg_loss < 0 ? fmtUSD(d.avg_loss) : "—";
  dsAvgLoss.className = `dash-stat-value ${d.avg_loss < 0 ? "negative" : ""}`;
}

// ── Cumulative P&L chart ─────────────────────────────────────────────────────
function renderPnlChart(cumulative) {
  if (pnlChart) { pnlChart.remove(); pnlChart = null; }
  pnlChartWrap.innerHTML = "";

  if (!cumulative || cumulative.length === 0) {
    pnlChartWrap.innerHTML = `<p class="muted-copy">No trade data yet.</p>`;
    return;
  }

  const w = pnlChartWrap.clientWidth || 800;
  const h = 260;
  pnlChart = LightweightCharts.createChart(pnlChartWrap, dashChartOpts(w, h));

  const series = pnlChart.addAreaSeries({
    topColor: "rgba(38,166,154,0.25)",
    bottomColor: "rgba(38,166,154,0.01)",
    lineColor: CANDLE_UP,
    lineWidth: 2,
  });

  series.setData(cumulative.map(pt => ({
    time: pt.date,
    value: pt.pnl,
  })));

  // Color the area red if overall negative
  const lastVal = cumulative[cumulative.length - 1]?.pnl || 0;
  if (lastVal < 0) {
    series.applyOptions({
      topColor: "rgba(239,83,80,0.25)",
      bottomColor: "rgba(239,83,80,0.01)",
      lineColor: CANDLE_DOWN,
    });
  }

  pnlChart.timeScale().fitContent();

  new ResizeObserver(entries => {
    if (!pnlChart || !entries[0]) return;
    const { width: rw } = entries[0].contentRect;
    if (rw > 0) pnlChart.applyOptions({ width: Math.floor(rw) });
  }).observe(pnlChartWrap);
}

// ── Daily P&L histogram ──────────────────────────────────────────────────────
function renderDailyChart(daily) {
  if (dailyChart) { dailyChart.remove(); dailyChart = null; }
  dailyChartWrap.innerHTML = "";

  if (!daily || daily.length === 0) {
    dailyChartWrap.innerHTML = `<p class="muted-copy">No trade data yet.</p>`;
    return;
  }

  const w = dailyChartWrap.clientWidth || 800;
  const h = 200;
  dailyChart = LightweightCharts.createChart(dailyChartWrap, dashChartOpts(w, h));

  const series = dailyChart.addHistogramSeries({
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });

  series.setData(daily.map(pt => ({
    time: pt.date,
    value: pt.pnl,
    color: pt.pnl >= 0 ? "rgba(38,166,154,0.65)" : "rgba(239,83,80,0.65)",
  })));

  dailyChart.timeScale().fitContent();

  new ResizeObserver(entries => {
    if (!dailyChart || !entries[0]) return;
    const { width: rw } = entries[0].contentRect;
    if (rw > 0) dailyChart.applyOptions({ width: Math.floor(rw) });
  }).observe(dailyChartWrap);
}

// ── Performance by Symbol / Setup tables ─────────────────────────────────────
function renderBreakdownTable(wrap, data, labelHeader) {
  const entries = Object.entries(data);
  if (entries.length === 0) {
    wrap.innerHTML = `<p class="muted-copy">No data yet.</p>`;
    return;
  }

  let html = `<table class="dash-table">
    <thead><tr>
      <th>${labelHeader}</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>P&L</th>
    </tr></thead><tbody>`;

  for (const [key, s] of entries) {
    const pnlCls = colorClass(s.pnl);
    html += `<tr>
      <td class="dash-table-key">${key}</td>
      <td>${s.trades}</td>
      <td>${s.wins}</td>
      <td>${fmtPct(s.win_rate)}</td>
      <td class="${pnlCls}">${fmtUSD(s.pnl)}</td>
    </tr>`;
  }
  html += `</tbody></table>`;
  wrap.innerHTML = html;
}

// ── Best / Worst trade cards ─────────────────────────────────────────────────
function renderBestWorst(d) {
  if (!d.best_trade && !d.worst_trade) {
    bestWorstWrap.innerHTML = `<p class="muted-copy">No closed trades yet.</p>`;
    return;
  }

  function tradeCard(label, t, cls) {
    if (!t) return "";
    return `
      <div class="dash-bw-card ${cls}">
        <span class="dash-bw-label">${label}</span>
        <div class="dash-bw-symbol">${t.symbol}</div>
        <div class="dash-bw-pnl ${colorClass(t.pnl)}">${fmtUSD(t.pnl)} (${t.pnl_pct != null ? t.pnl_pct.toFixed(2) + "%" : "—"})</div>
        <div class="muted-copy">${t.setup}</div>
        <div class="muted-copy">${t.direction} · ${fmtDuration(t.hold_duration_seconds)} · ${fmtTime(t.opened_at)}</div>
        ${t.reasoning ? `<div class="dash-bw-reasoning muted-copy">"${t.reasoning.substring(0, 150)}${t.reasoning.length > 150 ? "…" : ""}"</div>` : ""}
      </div>`;
  }

  bestWorstWrap.innerHTML =
    tradeCard("Best Trade", d.best_trade, "best") +
    tradeCard("Worst Trade", d.worst_trade, "worst");
}

// ── Recent trades table ──────────────────────────────────────────────────────
function renderTradeLog(trades) {
  _updateTradeLogToggle(trades ? trades.length : 0);

  if (!trades || trades.length === 0) {
    tradeLogWrap.innerHTML = `<p class="muted-copy">No trades recorded yet.</p>`;
    return;
  }

  // Outcome-class badge mapping (#10) — surfaces the classified outcome
  // on each closed trade so it reads at a glance whether a stop-out was a
  // clean stop or a "stop-then-extreme" (favorable_extreme = price crossed
  // target at some point during the hold before exiting via stop/timeout).
  const outcomeLabel = {
    target_hit:        "✓ Target",
    stop_hit:          "✗ Stop",
    neither:           "↔ Neither",
    favorable_extreme: "⚡ Fav. extreme",
  };
  const outcomeBadge = (cls) => {
    if (!cls) return "";
    const label = outcomeLabel[cls] || cls;
    return `<span class="outcome-badge ${cls}">${label}</span>`;
  };

  // Structured probabilities (#10) — tooltip-only on the confidence cell so
  // the column stays narrow. Hover shows Claude's full P(target)/P(stop)/
  // P(neither) distribution at entry time.
  const probTitle = (t) => {
    if (t.p_target == null && t.p_stop == null && t.p_neither == null) return "";
    const pt = t.p_target != null ? (t.p_target * 100).toFixed(0) + "%" : "—";
    const ps = t.p_stop != null ? (t.p_stop * 100).toFixed(0) + "%" : "—";
    const pn = t.p_neither != null ? (t.p_neither * 100).toFixed(0) + "%" : "—";
    return `title="P(target)=${pt} · P(stop)=${ps} · P(neither)=${pn}"`;
  };

  let html = `<table class="dash-table">
    <thead><tr>
      <th>Time</th><th>Symbol</th><th>Dir</th><th>Setup</th><th>Conf</th>
      <th>Entry</th><th>Exit</th><th>P&L</th><th>R</th><th>Hold</th>
      <th>Exit Reason</th><th>Outcome</th>
    </tr></thead><tbody>`;

  for (const t of trades) {
    const pnlCls = colorClass(t.pnl);
    html += `<tr>
      <td class="muted-copy">${fmtTime(t.opened_at)}</td>
      <td class="dash-table-key">${t.symbol}</td>
      <td>${t.direction}</td>
      <td class="muted-copy" title="${t.setup || ""}">${(t.setup || "—").substring(0, 30)}</td>
      <td ${probTitle(t)}>${t.confidence != null ? (t.confidence * 100).toFixed(0) + "%" : "—"}</td>
      <td>${t.entry_price != null ? "$" + t.entry_price.toFixed(2) : "—"}</td>
      <td>${t.exit_price != null ? "$" + t.exit_price.toFixed(2) : "—"}</td>
      <td class="${pnlCls}">${t.pnl != null ? fmtUSD(t.pnl) : "—"}</td>
      <td class="${t.r_multiple != null ? colorClass(t.r_multiple) : ""}">${t.r_multiple != null ? t.r_multiple.toFixed(2) + "R" : "—"}</td>
      <td class="muted-copy">${fmtDuration(t.hold_duration_seconds)}</td>
      <td class="muted-copy">${t.exit_reason || t.status || "—"}</td>
      <td>${outcomeBadge(t.outcome_class)}</td>
    </tr>`;
  }
  html += `</tbody></table>`;
  tradeLogWrap.innerHTML = html;
}

// Toggle the "Show all" / "Show recent 50" button. Only visible once we know
// there might be more trades available: either the page is already in preview
// mode and the server returned a full 50, or we're in "show all" mode and
// should offer a way back to the lighter default view.
function _updateTradeLogToggle(renderedCount) {
  if (!tradeLogToggle) return;
  const inPreviewMode = (tradeLogLimit === TRADE_LOG_PREVIEW_LIMIT);
  const preview = TRADE_LOG_PREVIEW_LIMIT;
  if (inPreviewMode) {
    // Only surface the toggle if we hit the preview ceiling — otherwise
    // there's nothing more to show and the button would mislead.
    if (renderedCount >= preview) {
      tradeLogToggle.hidden = false;
      tradeLogToggle.textContent = `Show all`;
    } else {
      tradeLogToggle.hidden = true;
    }
  } else {
    tradeLogToggle.hidden = false;
    tradeLogToggle.textContent = `Show recent ${preview}`;
  }
}

if (tradeLogToggle) {
  tradeLogToggle.addEventListener("click", async () => {
    tradeLogLimit = (tradeLogLimit === TRADE_LOG_PREVIEW_LIMIT)
      ? TRADE_LOG_FULL_LIMIT
      : TRADE_LOG_PREVIEW_LIMIT;
    tradeLogToggle.disabled = true;
    try {
      // Just re-fetch history — the other dashboard cards don't need to
      // change when only the trade-log limit toggles.
      const res = await fetch(
        `${API_BASE_URL}/trading/history?days=${currentDays}&limit=${tradeLogLimit}`,
        { credentials: "include" },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const hist = await res.json();
      renderTradeLog(hist.trades);
    } catch (err) {
      console.error("Trade log toggle error:", err);
    } finally {
      tradeLogToggle.disabled = false;
    }
  });
}

// ── AI Lessons ───────────────────────────────────────────────────────────────
async function loadLessons() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/lessons?limit=20`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (!data.lessons || data.lessons.length === 0) {
      lessonsWrap.innerHTML = `<p class="muted-copy">No lessons yet — the AI will analyze losing trades and store lessons here.</p>`;
      return;
    }

    let html = `<table class="dash-table">
      <thead><tr>
        <th>Date</th><th>Symbol</th><th>Setup</th><th>P&L</th><th>What Went Wrong</th>
      </tr></thead><tbody>`;

    for (const l of data.lessons) {
      html += `<tr>
        <td class="muted-copy">${fmtTime(l.created_at)}</td>
        <td class="dash-table-key">${l.symbol}</td>
        <td class="muted-copy">${(l.setup || "—").substring(0, 30)}</td>
        <td class="negative">${fmtUSD(l.pnl)}</td>
        <td>${l.lesson}</td>
      </tr>`;
    }
    html += `</tbody></table>`;
    lessonsWrap.innerHTML = html;
  } catch (err) {
    lessonsWrap.innerHTML = `<p class="muted-copy">Could not load lessons — ${err.message}</p>`;
  }
}

// ── Main load ────────────────────────────────────────────────────────────────
async function loadDashboard() {
  dashEyebrow.textContent = `Performance · Last ${currentDays} days`;
  if (dashRefreshBtn) dashRefreshBtn.disabled = true;

  try {
    const [perfRes, histRes] = await Promise.all([
      fetch(`${API_BASE_URL}/trading/performance?days=${currentDays}`, { credentials: "include" }),
      fetch(`${API_BASE_URL}/trading/history?days=${currentDays}&limit=${tradeLogLimit}`, { credentials: "include" }),
    ]);

    if (!perfRes.ok) throw new Error(`Performance: HTTP ${perfRes.status}`);
    if (!histRes.ok) throw new Error(`History: HTTP ${histRes.status}`);

    const perf = await perfRes.json();
    const hist = await histRes.json();

    renderStats(perf);
    renderPnlChart(perf.cumulative_pnl);
    renderDailyChart(perf.daily_pnl);
    renderBreakdownTable(bySymbolWrap, perf.by_symbol, "Symbol");
    renderBreakdownTable(bySetupWrap, perf.by_setup, "Strategy");
    renderBreakdownTable(byHourWrap, perf.by_hour || {}, "Hour (ET)");
    renderBestWorst(perf);
    renderTradeLog(hist.trades);
  } catch (err) {
    console.error("Dashboard load error:", err);
    pnlChartWrap.innerHTML = `<p class="muted-copy">Failed to load — ${err.message}</p>`;
  } finally {
    if (dashRefreshBtn) dashRefreshBtn.disabled = false;
  }

  loadLessons();
}

// ── Period toggle ────────────────────────────────────────────────────────────
if (periodBtnGroup) {
  periodBtnGroup.addEventListener("click", (e) => {
    const btn = e.target.closest(".tf-btn");
    if (!btn) return;
    const days = parseInt(btn.dataset.days, 10);
    if (isNaN(days) || days === currentDays) return;

    periodBtnGroup.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("tf-btn-active"));
    btn.classList.add("tf-btn-active");
    currentDays = days;
    loadDashboard();
  });
}

if (dashRefreshBtn) {
  dashRefreshBtn.addEventListener("click", loadDashboard);
}

// ── CSV Export ──────────────────────────────────────────────────────────────
const dashExportBtn = document.getElementById("dash-export-btn");
if (dashExportBtn) {
  dashExportBtn.addEventListener("click", async () => {
    dashExportBtn.disabled = true;
    dashExportBtn.textContent = "Exporting…";
    try {
      const res = await fetch(`${API_BASE_URL}/trading/export/csv?days=${currentDays}`, { credentials: "include" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `tradehack_trades_${currentDays}d.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Export failed:", err);
      alert("Export failed — " + err.message);
    } finally {
      dashExportBtn.disabled = false;
      dashExportBtn.textContent = "Export CSV";
    }
  });
}

// ── Auth guard ───────────────────────────────────────────────────────────────
async function checkAuth() {
  try {
    const res = await fetch(`${API_BASE_URL}/auth/me`, { credentials: "include" });
    if (!res.ok) { window.location.href = "./login.html"; return; }
    const data = await res.json();
    const nameEl = document.getElementById("session-user-name");
    const emailEl = document.getElementById("session-user-email");
    if (nameEl) nameEl.textContent = data.user.name;
    if (emailEl) emailEl.textContent = data.user.email;
  } catch {
    window.location.href = "./login.html";
  }
}

const logoutBtn = document.getElementById("logout-btn");
if (logoutBtn) {
  logoutBtn.addEventListener("click", async () => {
    await fetch(`${API_BASE_URL}/auth/logout`, { method: "POST", credentials: "include" });
    window.location.href = "./login.html";
  });
}

// ── Paper Trade Simulator ────────────────────────────────────────────────────

const simPeriodBtnGroup = document.getElementById("sim-period-btn-group");
const simChartWrap      = document.getElementById("sim-chart-wrap");
const simBySymbolWrap   = document.getElementById("sim-by-symbol-wrap");
const simTradesWrap     = document.getElementById("sim-trades-wrap");
const simApprovalBreak  = document.getElementById("sim-approval-breakdown");

let simDays = 7;
let simChart = null;

function renderSimStats(d) {
  const el = (id) => document.getElementById(id);

  el("sim-total-pnl").textContent = fmtUSD(d.total_pnl);
  el("sim-total-pnl").className = `dash-stat-value ${colorClass(d.total_pnl)}`;

  el("sim-win-rate").textContent = d.total_closed > 0 ? fmtPct(d.win_rate) : "—";
  el("sim-win-rate").className = `dash-stat-value ${d.win_rate >= 50 ? "positive" : d.win_rate > 0 ? "negative" : ""}`;

  el("sim-signals").textContent = d.total_recommendations > 0
    ? `${d.wins}W / ${d.losses}L (${d.total_closed} closed)`
    : "—";

  el("sim-profit-factor").textContent = d.profit_factor != null ? d.profit_factor.toFixed(2) : "—";
  el("sim-profit-factor").className = `dash-stat-value ${d.profit_factor != null ? (d.profit_factor >= 1.5 ? "positive" : d.profit_factor >= 1 ? "" : "negative") : ""}`;

  el("sim-max-dd").textContent = d.max_drawdown > 0 ? "-" + fmtUSD(d.max_drawdown).replace("+", "") : "—";
  el("sim-max-dd").className = `dash-stat-value ${d.max_drawdown > 0 ? "negative" : ""}`;

  el("sim-approved-pnl").textContent = fmtUSD(d.approved_pnl);
  el("sim-approved-pnl").className = `dash-stat-value ${colorClass(d.approved_pnl)}`;

  el("sim-missed-pnl").textContent = fmtUSD(d.missed_pnl);
  el("sim-missed-pnl").className = `dash-stat-value ${colorClass(d.missed_pnl)}`;

  el("sim-open").textContent = d.open_trades > 0 ? d.open_trades : "—";

  // Approval breakdown bar
  if (simApprovalBreak && d.total_recommendations > 0) {
    const total = d.total_recommendations;
    const pct = (n) => ((n / total) * 100).toFixed(1);
    simApprovalBreak.innerHTML = `
      <div class="sim-approval-bar">
        <div class="sim-bar-segment sim-bar-approved" style="width:${pct(d.approved_count)}%" title="Approved: ${d.approved_count}"></div>
        <div class="sim-bar-segment sim-bar-rejected" style="width:${pct(d.rejected_count)}%" title="Rejected: ${d.rejected_count}"></div>
        <div class="sim-bar-segment sim-bar-expired" style="width:${pct(d.expired_count)}%" title="Expired: ${d.expired_count}"></div>
        <div class="sim-bar-segment sim-bar-pending" style="width:${pct(d.unapproved_count + d.open_trades)}%" title="Open/Pending: ${d.unapproved_count + d.open_trades}"></div>
      </div>
      <div class="sim-approval-legend">
        <span class="sim-legend-item"><span class="sim-dot sim-bar-approved"></span> Approved ${d.approved_count}</span>
        <span class="sim-legend-item"><span class="sim-dot sim-bar-rejected"></span> Rejected ${d.rejected_count}</span>
        <span class="sim-legend-item"><span class="sim-dot sim-bar-expired"></span> Expired ${d.expired_count}</span>
        <span class="sim-legend-item"><span class="sim-dot sim-bar-pending"></span> Pending ${d.unapproved_count}</span>
      </div>`;
  }
}

function renderSimChart(cumulative) {
  if (simChart) { simChart.remove(); simChart = null; }
  if (!simChartWrap) return;
  simChartWrap.innerHTML = "";

  if (!cumulative || cumulative.length === 0) {
    simChartWrap.innerHTML = `<p class="muted-copy">No simulated trade data yet — start the AI trader to begin tracking.</p>`;
    return;
  }

  const w = simChartWrap.clientWidth || 800;
  const h = 220;
  simChart = LightweightCharts.createChart(simChartWrap, dashChartOpts(w, h));

  const series = simChart.addAreaSeries({
    topColor: "rgba(108,92,231,0.25)",
    bottomColor: "rgba(108,92,231,0.01)",
    lineColor: "#6c5ce7",
    lineWidth: 2,
  });

  series.setData(cumulative.map(pt => ({ time: pt.date, value: pt.pnl })));

  const lastVal = cumulative[cumulative.length - 1]?.pnl || 0;
  if (lastVal < 0) {
    series.applyOptions({
      topColor: "rgba(239,83,80,0.25)",
      bottomColor: "rgba(239,83,80,0.01)",
      lineColor: CANDLE_DOWN,
    });
  }

  simChart.timeScale().fitContent();

  new ResizeObserver(entries => {
    if (!simChart || !entries[0]) return;
    const { width: rw } = entries[0].contentRect;
    if (rw > 0) simChart.applyOptions({ width: Math.floor(rw) });
  }).observe(simChartWrap);
}

function renderSimTrades(trades) {
  if (!simTradesWrap) return;

  if (!trades || trades.length === 0) {
    simTradesWrap.innerHTML = `<p class="muted-copy">No simulated trades yet.</p>`;
    return;
  }

  let html = `<table class="dash-table">
    <thead><tr>
      <th>Time</th><th>Symbol</th><th>Dir</th><th>Setup</th><th>Conf</th>
      <th>Entry</th><th>Exit</th><th>P&L</th><th>Result</th><th>Approved?</th>
    </tr></thead><tbody>`;

  for (const t of trades) {
    const pnlCls = colorClass(t.pnl);
    const statusBadge = t.status === "won" ? '<span class="sim-badge sim-won">WON</span>'
      : t.status === "lost" ? '<span class="sim-badge sim-lost">LOST</span>'
      : '<span class="sim-badge sim-open">OPEN</span>';
    const approvalBadge = t.was_approved === "yes" ? '<span class="sim-badge sim-yes">Yes</span>'
      : t.was_approved === "no" ? '<span class="sim-badge sim-no">No</span>'
      : t.was_approved === "expired" ? '<span class="sim-badge sim-expired">Expired</span>'
      : '<span class="muted-copy">—</span>';

    html += `<tr>
      <td class="muted-copy">${fmtTime(t.opened_at)}</td>
      <td class="dash-table-key">${t.symbol}</td>
      <td>${t.direction}</td>
      <td class="muted-copy" title="${t.setup || ""}">${(t.setup || "—").substring(0, 25)}</td>
      <td>${t.confidence != null ? (t.confidence * 100).toFixed(0) + "%" : "—"}</td>
      <td>$${t.entry_price?.toFixed(2) || "—"}</td>
      <td>${t.exit_price != null ? "$" + t.exit_price.toFixed(2) : "—"}</td>
      <td class="${pnlCls}">${t.pnl != null ? fmtUSD(t.pnl) : "—"}</td>
      <td>${statusBadge}</td>
      <td>${approvalBadge}</td>
    </tr>`;
  }
  html += `</tbody></table>`;
  simTradesWrap.innerHTML = html;
}

async function loadSimulator() {
  try {
    const [perfRes, tradesRes] = await Promise.all([
      fetch(`${API_BASE_URL}/trading/simulator/performance?days=${simDays}`, { credentials: "include" }),
      fetch(`${API_BASE_URL}/trading/simulator/trades?days=${simDays}&limit=50`, { credentials: "include" }),
    ]);

    if (!perfRes.ok) throw new Error(`Sim perf: HTTP ${perfRes.status}`);
    if (!tradesRes.ok) throw new Error(`Sim trades: HTTP ${tradesRes.status}`);

    const perf = await perfRes.json();
    const trades = await tradesRes.json();

    renderSimStats(perf);
    renderSimChart(perf.cumulative_pnl);
    renderBreakdownTable(simBySymbolWrap, perf.by_symbol, "Symbol");
    renderSimTrades(trades.trades);
  } catch (err) {
    console.error("Simulator load error:", err);
    if (simChartWrap) simChartWrap.innerHTML = `<p class="muted-copy">Could not load simulator data — ${err.message}</p>`;
  }
}

// Sim period toggle
if (simPeriodBtnGroup) {
  simPeriodBtnGroup.addEventListener("click", (e) => {
    const btn = e.target.closest(".tf-btn");
    if (!btn) return;
    const days = parseInt(btn.dataset.days, 10);
    if (isNaN(days) || days === simDays) return;

    simPeriodBtnGroup.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("tf-btn-active"));
    btn.classList.add("tf-btn-active");
    simDays = days;
    loadSimulator();
  });
}

checkAuth().then(() => {
  loadDashboard();
  loadSimulator();
});
