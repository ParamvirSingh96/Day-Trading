import { API_BASE_URL } from "./config.js";
import { esc } from "./utils.js";
import {
  createEnhancedChart, addCandleSeries, addVolumeSeries,
  addEMAOverlays, addCurrentPriceLine, setupLegend, setupZoomControls,
  setupFreePan, getChartTimeZoneLabel, VOL_UP, VOL_DOWN,
} from "./chart-helpers.js?v=2";
import {
  fetchICTZones, renderICTOverlay, renderICTPills,
} from "./ict-overlay.js";

// ── Symbol from URL ───────────────────────────────────────────────────────────
const params = new URLSearchParams(window.location.search);
const symbol = (params.get("symbol") || "").toUpperCase().replace(/[^A-Z]/g, "");

if (!symbol) window.location.href = "./app.html";

// ── Session ───────────────────────────────────────────────────────────────────
(async () => {
  try {
    const res = await fetch(`${API_BASE_URL}/auth/me`, { credentials: "include" });
    if (!res.ok) { window.location.href = "./login.html"; return; }
    const { user } = await res.json();
    document.getElementById("session-user-name").textContent  = user.name;
    document.getElementById("session-user-email").textContent = user.email;
  } catch {
    window.location.href = "./login.html";
  }
})();

const logoutBtn = document.getElementById("logout-btn");
if (logoutBtn) {
  logoutBtn.addEventListener("click", async () => {
    await fetch(`${API_BASE_URL}/auth/logout`, { method: "POST", credentials: "include" });
    window.location.href = "./login.html";
  });
}

// ── DOM refs ──────────────────────────────────────────────────────────────────
const pageSymbol             = document.getElementById("page-symbol");
const chartStatusEl          = document.getElementById("chart-status");
const chartContainer         = document.getElementById("chart-container");
const analysisLoadingSection = document.getElementById("analysis-loading-section");
const analysisStatusEl       = document.getElementById("analysis-status");
const analysisSection        = document.getElementById("analysis-section");
const reanalyzeBtn           = document.getElementById("reanalyze-btn");
const pnlCard                = document.getElementById("pnl-card");
const pnlSharesInput         = document.getElementById("pnl-shares");

// ── Timeframe state ──────────────────────────────────────────────────────────
const _TF_META = {
  "1Min":  { label: "1-min bars",  window: "Last 5 days · incl. extended hours" },
  "5Min":  { label: "5-min bars",  window: "Last 5 days" },
  "15Min": { label: "15-min bars", window: "Last 7 days" },
};
let currentTimeframe = "1Min";
const tfBtnGroup = document.getElementById("tf-btn-group");
const pageEyebrow = document.getElementById("page-eyebrow");

function updateTfEyebrow() {
  const m = _TF_META[currentTimeframe] || _TF_META["1Min"];
  if (pageEyebrow) pageEyebrow.textContent = `AI Chart Analysis · ${m.label} · ${m.window} · ${getChartTimeZoneLabel()}`;
}
updateTfEyebrow();

// ── Chart state ───────────────────────────────────────────────────────────────
let chartInstance   = null;
let candleSeriesRef = null;
let volSeriesRef    = null;
let chartPriceLines = [];
let ictOverlayHandle = null;   // { destroy } from renderICTOverlay — tear down before re-render
let ictToggleOn      = true;   // user-togglable; persisted to localStorage below
try {
  const saved = localStorage.getItem("ictToggleOn");
  if (saved !== null) ictToggleOn = saved === "true";
} catch (_) { /* storage blocked — default stays true */ }
const LS = LightweightCharts.LineStyle;

// ── Price-line helpers ────────────────────────────────────────────────────────
function parsePriceStr(str) {
  const s = String(str).replace(/\$/g, "").trim();
  if (s.includes(" - ")) {
    const parts = s.split(" - ").map((p) => parseFloat(p.trim()));
    return (parts[0] + parts[1]) / 2;
  }
  return parseFloat(s);
}

function clearChartMarkers() {
  if (candleSeriesRef && chartPriceLines.length) {
    chartPriceLines.forEach((pl) => { try { candleSeriesRef.removePriceLine(pl); } catch (_) {} });
  }
  chartPriceLines = [];
}

function addPriceLine(price, color, title, lineStyle = LS.Dashed, lineWidth = 1) {
  if (!candleSeriesRef || isNaN(price)) return;
  chartPriceLines.push(
    candleSeriesRef.createPriceLine({ price, color, lineWidth, lineStyle, axisLabelVisible: true, title })
  );
}

function renderChartMarkers(d) {
  clearChartMarkers();
  if (!candleSeriesRef) return;
  const { recommendation: rec, key_levels: levels } = d;
  const entry  = parsePriceStr(rec.entry_zone);
  const stop   = parsePriceStr(rec.stop_loss);
  const target = parsePriceStr(rec.target);

  addPriceLine(levels.vwap,       "#a78bfa", "VWAP",       LS.Dashed,      1);
  addPriceLine(levels.support,    "#64748b", "Support",    LS.LargeDashed, 1);
  addPriceLine(levels.resistance, "#64748b", "Resistance", LS.LargeDashed, 1);
  if (rec.direction !== "wait") {
    addPriceLine(entry,  "#f59e0b", "Entry",    LS.Dashed, 2);
    addPriceLine(stop,   "#ef5350", "Stop",     LS.Solid,  2);
    addPriceLine(target, "#26a69a", "Target",   LS.Solid,  2);
  }
}

// ── P&L Calculator ────────────────────────────────────────────────────────────
function calcPnl() {
  if (!pnlCard || pnlCard.classList.contains("hidden")) return;
  const shares         = Math.max(1, parseInt(pnlSharesInput.value) || 100);
  const profitPerShare = parseFloat(pnlCard.dataset.profitPerShare || 0);
  const lossPerShare   = parseFloat(pnlCard.dataset.lossPerShare   || 0);
  const profitPct      = parseFloat(pnlCard.dataset.profitPct      || 0);
  const lossPct        = parseFloat(pnlCard.dataset.lossPct        || 0);

  document.getElementById("pnl-win-per-share").textContent  = `+$${profitPerShare.toFixed(2)} (+${profitPct.toFixed(2)}%)`;
  document.getElementById("pnl-loss-per-share").textContent = `-$${lossPerShare.toFixed(2)} (-${lossPct.toFixed(2)}%)`;
  document.getElementById("pnl-win-total").textContent      = `+$${(profitPerShare * shares).toFixed(2)}`;
  document.getElementById("pnl-loss-total").textContent     = `-$${(lossPerShare   * shares).toFixed(2)}`;
}

function renderPnl(d) {
  const { recommendation: rec } = d;
  const entry  = parsePriceStr(rec.entry_zone);
  const stop   = parsePriceStr(rec.stop_loss);
  const target = parsePriceStr(rec.target);

  if (isNaN(entry) || isNaN(stop) || isNaN(target)) {
    pnlCard.classList.add("hidden");
    return;
  }

  const effectiveDir   = rec.direction === "wait" ? (target > entry ? "long" : "short") : rec.direction;
  const profitPerShare = effectiveDir === "long" ? target - entry : entry - target;
  const lossPerShare   = effectiveDir === "long" ? entry  - stop  : stop  - entry;
  const profitPct      = (profitPerShare / entry) * 100;
  const lossPct        = (lossPerShare   / entry) * 100;

  document.getElementById("pnl-entry").textContent  = `$${entry.toFixed(2)}`;
  document.getElementById("pnl-stop").textContent   = `$${stop.toFixed(2)}`;
  document.getElementById("pnl-target").textContent = `$${target.toFixed(2)}`;
  const dirLabel = rec.direction === "wait"
    ? `HYPOTHETICAL ${effectiveDir.toUpperCase()}`
    : rec.direction.toUpperCase();
  document.getElementById("pnl-dir").textContent = dirLabel;
  document.getElementById("pnl-dir").className   =
    `rec-direction ${effectiveDir}${rec.direction === "wait" ? " hypothetical" : ""}`;

  pnlCard.dataset.profitPerShare = profitPerShare.toFixed(4);
  pnlCard.dataset.lossPerShare   = lossPerShare.toFixed(4);
  pnlCard.dataset.profitPct      = profitPct.toFixed(2);
  pnlCard.dataset.lossPct        = lossPct.toFixed(2);

  calcPnl();
  pnlCard.classList.remove("hidden");
}

// ── Render analysis results ───────────────────────────────────────────────────
function renderAnalysis(d) {
  lastAnalysisData = d;
  const { recommendation: rec, indicators: ind } = d;

  // Trend
  const trendBadge = document.getElementById("analysis-trend-badge");
  trendBadge.textContent = d.trend.toUpperCase();
  trendBadge.className   = `trend-badge trend-${d.trend}`;
  document.getElementById("analysis-strength").textContent = d.trend_strength;
  document.getElementById("analysis-pattern").textContent  = d.pattern;

  // Matched strategies banner — v7.11 adds tier badge + score + tooltip.
  // Each match dict from the backend now carries:
  //   { name, direction, tier: "A"|"B"|"context", score: 0-100,
  //     reasons: string[], post_friction_avg_r: number|null }
  // We render the existing chip layout but extend it with a tier
  // badge + numeric score and surface the reasons array as a hover
  // tooltip so the user gets quality context at a glance.
  const renderStratChip = (s) => {
    const dirCls = s.direction === "long" ? "strat-long"
                : s.direction === "short" ? "strat-short" : "";
    const tier = (s.tier || "B");
    const tierCls = `strat-tier-${esc(tier.toLowerCase())}`;
    const scoreStr = (typeof s.score === "number") ? ` · ${Math.round(s.score)}` : "";
    const reasonsTitle = (s.reasons && s.reasons.length)
      ? s.reasons.join("\n")
      : "";
    const titleAttr = reasonsTitle
      ? ` title="${esc(reasonsTitle)}"`
      : "";
    return `<span class="strat-chip ${dirCls}"${titleAttr}><strong>${esc(s.name)}</strong><span class="strat-tier ${tierCls}">${esc(tier.toUpperCase())}</span>${scoreStr}</span>`;
  };

  const stratEl = document.getElementById("matched-strategies");
  if (stratEl && d.matched_strategies && d.matched_strategies.length) {
    // Sort: A-tier first, then B-tier, then context. Within tier, by
    // score desc. Lets the eye land on the strongest setup first.
    const tierRank = { A: 0, B: 1, context: 2, suppress: 3 };
    const sorted = [...d.matched_strategies].sort((a, b) => {
      const ra = tierRank[a.tier] ?? 9;
      const rb = tierRank[b.tier] ?? 9;
      if (ra !== rb) return ra - rb;
      return (b.score || 0) - (a.score || 0);
    });
    stratEl.innerHTML = sorted.map(renderStratChip).join("");
    stratEl.classList.remove("hidden");
  } else if (stratEl) {
    stratEl.classList.add("hidden");
  }

  // v7.12: "Watching" panel — setups FORMING but not yet entries.
  // CRITICAL UX: a forming setup must NEVER look like an entry alert.
  // - Header copy: explicit "Not entries yet"
  // - Visual: ⏳ icon, dashed border, muted color (via .strat-chip-forming)
  // - Each row says "Waiting for: <missing list>" to tell the user
  //   exactly what's still required.
  // - NO toasts, NO browser notifications, NO sounds (Phase 1 is
  //   visual-only; notifications deferred to v7.13+ once we know
  //   which stage transitions are predictive).
  const formingEl = document.getElementById("forming-strategies");
  if (formingEl) {
    if (d.forming_setups && d.forming_setups.length) {
      const renderForming = (s) => {
        const dirCls = s.direction === "long" ? "strat-long"
                    : s.direction === "short" ? "strat-short" : "";
        const stage = s.stage || "forming";
        const stageLabel = stage === "sweep_only" ? "sweep only"
                         : stage === "sweep_and_mss" ? "sweep + MSS"
                         : esc(stage);
        const missing = (s.missing || []).join(" + ");
        const waitingFor = missing ? `Waiting for: ${esc(missing)}` : "";
        const tooltip = (s.structural_notes || []).join("\n");
        const titleAttr = tooltip ? ` title="${esc(tooltip)}"` : "";
        return `<span class="strat-chip strat-chip-forming ${dirCls}"${titleAttr}>` +
               `<span class="forming-icon">⏳</span>` +
               `<strong>${esc(s.name)}</strong>` +
               `<span class="forming-stage">${stageLabel}</span>` +
               (waitingFor ? `<span class="forming-waiting">${waitingFor}</span>` : "") +
               `</span>`;
      };
      const headerLine =
        `<div class="forming-header">` +
        `<strong>Watching — setups forming (${d.forming_setups.length})</strong>` +
        `<span class="forming-disclaimer">Not entries yet — waiting for additional confirmation</span>` +
        `</div>`;
      const chips = `<div class="forming-chips">${d.forming_setups.map(renderForming).join("")}</div>`;
      formingEl.innerHTML = headerLine + chips;
      formingEl.classList.remove("hidden");
    } else {
      formingEl.classList.add("hidden");
    }
  }

  // Context-only structural setups (v7.11): rendered separately below
  // the named-setup row when present. These are confluence notes from
  // the suspended-but-callable predicates (BB / ARB / Sweep+MSS Short /
  // MD-PD Short etc.) — they never auto-trade and are visually demoted
  // so the user doesn't mistake them for primary alerts.
  const ctxEl = document.getElementById("context-strategies");
  if (ctxEl) {
    if (d.context_strategies && d.context_strategies.length) {
      ctxEl.innerHTML = d.context_strategies.map(renderStratChip).join("");
      ctxEl.classList.remove("hidden");
    } else {
      ctxEl.classList.add("hidden");
    }
  }

  // "Top setup today" header (v7.11): tells the user at a glance
  // whether there's an A-grade match, the best B-grade fallback, or
  // only context-only entries. Set by Fork-C tier prioritization.
  const headerEl = document.getElementById("matched-strategies-header");
  if (headerEl) {
    const matches = d.matched_strategies || [];
    const aGrade = matches.filter((s) => s.tier === "A");
    const bGrade = matches.filter((s) => s.tier === "B");
    const ctxOnly = matches.filter((s) => s.tier === "context");
    if (aGrade.length > 0) {
      const top = aGrade.sort((a, b) => (b.score || 0) - (a.score || 0))[0];
      headerEl.innerHTML = `<strong>Top setup:</strong> ${esc(top.name)} <span class="strat-tier strat-tier-a">A</span> · score ${Math.round(top.score || 0)}`;
      headerEl.classList.remove("hidden");
    } else if (bGrade.length > 0) {
      const top = bGrade.sort((a, b) => (b.score || 0) - (a.score || 0))[0];
      headerEl.innerHTML = `<em>No A-grade setup. Best available is <strong>${esc(top.name)}</strong> <span class="strat-tier strat-tier-b">B</span> — trade with extra confirmation.</em>`;
      headerEl.classList.remove("hidden");
    } else if (ctxOnly.length > 0) {
      headerEl.innerHTML = `<em>Context-only structure detected. Wait for a primary signal.</em>`;
      headerEl.classList.remove("hidden");
    } else {
      headerEl.classList.add("hidden");
    }
  }

  // Indicator chips
  const rsiZone  = ind.rsi  > 70 ? "overbought" : ind.rsi < 30 ? "oversold" : "neutral";
  const macdSide = ind.macd > ind.macd_signal ? "bullish" : "bearish";
  document.getElementById("analysis-indicators").innerHTML = `
    <div class="ind-chip">EMA9 <strong>$${esc(ind.ema9)}</strong></div>
    <div class="ind-chip">EMA20 <strong>$${esc(ind.ema20)}</strong></div>
    <div class="ind-chip rsi-${rsiZone}">RSI <strong>${esc(ind.rsi)}</strong></div>
    <div class="ind-chip">VWAP <strong>$${esc(ind.vwap)}</strong></div>
    <div class="ind-chip ind-chip-${macdSide}">MACD <strong>${ind.macd > 0 ? "+" : ""}${Number(ind.macd).toFixed(3)}</strong></div>
    <div class="ind-chip">ATR <strong>$${esc(ind.atr)}</strong></div>
    <div class="ind-chip">High <strong>$${esc(ind.session_high)}</strong></div>
    <div class="ind-chip">Low <strong>$${esc(ind.session_low)}</strong></div>`;

  // Key levels
  document.getElementById("analysis-levels").innerHTML = `
    <div class="level-row"><span class="muted-copy">Support</span>    <strong>$${esc(d.key_levels.support)}</strong></div>
    <div class="level-row"><span class="muted-copy">Resistance</span> <strong>$${esc(d.key_levels.resistance)}</strong></div>
    <div class="level-row"><span class="muted-copy">VWAP</span>       <strong>$${esc(d.key_levels.vwap)}</strong></div>`;

  // Reasoning & risks
  document.getElementById("analysis-reasoning").textContent = d.reasoning;
  document.getElementById("analysis-risks").textContent     = d.risks;

  // Recommendation card
  const dirClass = rec.direction === "long" ? "long" : rec.direction === "short" ? "short" : "wait";
  const confPct  = Math.round(rec.confidence * 100);
  document.getElementById("analysis-rec").innerHTML = `
    <div class="rec-direction ${dirClass}">${esc(rec.direction.toUpperCase())}</div>
    <p class="rec-setup">${esc(rec.setup)}</p>
    <div class="rec-grid">
      <div class="rec-item"><span class="muted-copy">Entry</span><strong>${esc(rec.entry_zone)}</strong></div>
      <div class="rec-item"><span class="muted-copy">Stop</span><strong class="negative">${esc(rec.stop_loss)}</strong></div>
      <div class="rec-item"><span class="muted-copy">Target</span><strong class="positive">${esc(rec.target)}</strong></div>
      <div class="rec-item"><span class="muted-copy">R:R</span><strong>${esc(rec.risk_reward)}</strong></div>
    </div>
    <div class="conf-row">
      <span class="muted-copy">Confidence</span>
      <div class="conf-bar-track"><div class="conf-bar-fill" style="width:${confPct}%"></div></div>
      <span>${confPct}%</span>
    </div>`;

  // Price lines on chart + P&L card
  renderChartMarkers(d);
  renderPnl(d);

  // Show trade panel
  showTradePanel(d);

  // Show results, hide spinner
  analysisLoadingSection.classList.add("hidden");
  analysisSection.classList.remove("hidden");
}

// ── ICT overlay + pills ──────────────────────────────────────────────────────
// Kept as a separate async step so a slow or failing ICT backend
// can't block the primary chart render. Safe to call multiple times
// — destroys any existing overlay handle before painting new zones.
let lastICTData = null;

async function loadICT(tf, bars) {
  const pillsEl = document.getElementById("ict-pills");

  // Tear down any previous overlay so we never double-paint.
  if (ictOverlayHandle) {
    ictOverlayHandle.destroy();
    ictOverlayHandle = null;
  }
  if (pillsEl) {
    pillsEl.innerHTML = "";
    pillsEl.classList.add("hidden");
  }

  if (!ictToggleOn || !candleSeriesRef || !bars || !bars.length) return;

  const data = await fetchICTZones(symbol, tf || currentTimeframe);
  if (!data) return;     // silent — backend may just be down
  lastICTData = data;

  ictOverlayHandle = renderICTOverlay(candleSeriesRef, bars, data);
  renderICTPills(pillsEl, data);
}

// ── Load chart ────────────────────────────────────────────────────────────────
let chartResizeObs = null;

async function loadChart(tf) {
  tf = tf || currentTimeframe;
  try {
    const res = await fetch(`${API_BASE_URL}/market/chart/${symbol}?timeframe=${tf}`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (!data.bars.length) {
      chartStatusEl.textContent = "No bar data — the market may be closed.";
      return;
    }
    chartStatusEl.textContent = "";

    const volData = data.bars.map((b) => ({
      time:  b.time,
      value: b.volume,
      color: b.close >= b.open ? VOL_UP : VOL_DOWN,
    }));

    // If chart already exists, just swap the data (keeps zoom/scroll state for same TF)
    if (chartInstance && candleSeriesRef && volSeriesRef) {
      clearChartMarkers();
      candleSeriesRef.setData(data.bars);
      volSeriesRef.setData(volData);
      chartInstance.timeScale().fitContent();
      // Refresh the legend with new data
      setupLegend(chartContainer, data.bars, candleSeriesRef, volSeriesRef, chartInstance, symbol);
      loadICT(tf, data.bars);  // re-paint zones/markers against the new bar set
      return;
    }

    // First load — create the chart from scratch
    const chartW = chartContainer.clientWidth || 900;
    const chartH = chartContainer.clientHeight || (window.innerWidth <= 480 ? 260 : window.innerWidth <= 760 ? 320 : 500);

    // ── Enhanced chart ──────────────────────────────────────────────────
    chartInstance = createEnhancedChart(chartContainer, symbol, chartW, chartH);

    candleSeriesRef = addCandleSeries(chartInstance, data.bars);
    volSeriesRef    = addVolumeSeries(chartInstance, data.bars);
    addEMAOverlays(chartInstance, data.bars);
    addCurrentPriceLine(candleSeriesRef, data.bars);

    chartInstance.timeScale().fitContent();

    // OHLCV legend + zoom controls + free 2D panning
    setupLegend(chartContainer, data.bars, candleSeriesRef, volSeriesRef, chartInstance, symbol);
    const resetVertical = setupFreePan(chartContainer, chartInstance);
    setupZoomControls(chartContainer, chartInstance, resetVertical);

    // Responsive resize — update width AND height on container changes
    if (chartResizeObs) chartResizeObs.disconnect();
    chartResizeObs = new ResizeObserver((entries) => {
      if (!chartInstance || !entries[0]) return;
      const { width: w, height: h } = entries[0].contentRect;
      const opts = {};
      if (w > 0) opts.width = Math.floor(w);
      if (h > 0) opts.height = Math.floor(h);
      if (opts.width || opts.height) chartInstance.applyOptions(opts);
    });
    chartResizeObs.observe(chartContainer);

    // Paint ICT zones/markers once the chart is up. Deliberately
    // awaited last so a slow/failed ICT fetch can't delay the
    // candles showing up.
    loadICT(tf, data.bars);

  } catch (err) {
    chartStatusEl.textContent = `Could not load chart — ${err.message}`;
  }
}

// ── Run AI analysis ───────────────────────────────────────────────────────────
async function runAnalysis() {
  reanalyzeBtn.disabled = true;
  analysisSection.classList.add("hidden");
  analysisLoadingSection.classList.remove("hidden");
  analysisStatusEl.textContent = "✦ Analyzing chart with AI…";

  try {
    const res = await fetch(`${API_BASE_URL}/market/analyze/${symbol}`, { credentials: "include" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    renderAnalysis(await res.json());
  } catch (err) {
    analysisStatusEl.textContent = `Analysis failed — ${err.message}`;
    analysisLoadingSection.classList.remove("hidden");
  } finally {
    reanalyzeBtn.disabled = false;
  }
}

// ── Trading ───────────────────────────────────────────────────────────────────
const tradePanel     = document.getElementById("trade-panel");
const tradeBuyBtn    = document.getElementById("trade-buy-btn");
const tradeSellBtn   = document.getElementById("trade-sell-btn");
const tradeStatus    = document.getElementById("trade-status");
const tradeModeBadge = document.getElementById("trade-mode-badge");
const tradeQtyInput  = document.getElementById("trade-qty");
const tradeTypeSelect = document.getElementById("trade-type");
const tradeBracket   = document.getElementById("trade-bracket");

let lastAnalysis = null;     // stored for quick-trade
let lastAnalysisData = null; // stored for re-applying price lines on TF switch

function showTradePanel(d) {
  lastAnalysis = d;
  tradePanel.classList.remove("hidden");
  tradeBuyBtn.disabled  = false;
  tradeSellBtn.disabled = false;
  tradeStatus.textContent = "";

  // Pre-fill labels from recommendation
  const rec = d.recommendation;
  tradeBuyBtn.textContent  = `Buy ${symbol}`;
  tradeSellBtn.textContent = `Sell ${symbol}`;
}

async function executeTrade(side) {
  if (!lastAnalysis) return;
  tradeBuyBtn.disabled = true;
  tradeSellBtn.disabled = true;

  const rec = lastAnalysis.recommendation;
  const qty = Math.max(1, parseInt(tradeQtyInput.value) || 10);
  const useBracket = tradeBracket.checked;
  const useLimit   = tradeTypeSelect.value === "limit";

  const entry  = parsePriceStr(rec.entry_zone);
  const stop   = parsePriceStr(rec.stop_loss);
  const target = parsePriceStr(rec.target);

  const body = {
    symbol,
    side,
    qty,
    entry_price:       useLimit && !isNaN(entry)  ? entry  : null,
    stop_loss_price:   useBracket && !isNaN(stop)   ? stop   : null,
    take_profit_price: useBracket && !isNaN(target) ? target : null,
  };

  tradeStatus.textContent = `Submitting ${side} order…`;
  try {
    const res = await fetch(`${API_BASE_URL}/trading/quick-trade`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const msg = typeof err.detail === "string" ? err.detail
                : err.detail?.message || JSON.stringify(err.detail) || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    const order = await res.json();
    tradeStatus.textContent =
      `✓ Order ${order.status} — ${order.side.toUpperCase()} ${order.qty} ${order.symbol}`
      + (order.filled_avg_price ? ` @ $${order.filled_avg_price}` : "");
    tradeStatus.className = "trade-status positive";
    loadAccount();   // refresh portfolio
  } catch (err) {
    tradeStatus.textContent = `✕ Order failed — ${err.message}`;
    tradeStatus.className = "trade-status negative";
  } finally {
    tradeBuyBtn.disabled = false;
    tradeSellBtn.disabled = false;
  }
}

tradeBuyBtn.addEventListener("click",  () => executeTrade("buy"));
tradeSellBtn.addEventListener("click", () => executeTrade("sell"));

// ── Account & Positions ──────────────────────────────────────────────────────
const accountSummary  = document.getElementById("account-summary");
const positionsList   = document.getElementById("positions-list");
const ordersList      = document.getElementById("orders-list");
const refreshAcctBtn  = document.getElementById("refresh-account-btn");
const acctModeLabel   = document.getElementById("account-mode-label");

async function loadAccount() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/account`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const acct = await res.json();

    if (acct.paper) {
      tradeModeBadge.textContent = "PAPER";
      tradeModeBadge.className   = "trade-mode-badge paper";
      acctModeLabel.textContent  = "Paper Trading Account";
    } else {
      tradeModeBadge.textContent = "LIVE";
      tradeModeBadge.className   = "trade-mode-badge live";
      acctModeLabel.textContent  = "Live Trading Account";
    }

    const plClass = acct.unrealized_pl >= 0 ? "positive" : "negative";
    const plSign  = acct.unrealized_pl >= 0 ? "+" : "";
    accountSummary.innerHTML = `
      <div class="acct-grid">
        <div class="acct-item"><span class="muted-copy">Cash</span><strong>$${esc(acct.cash.toLocaleString(undefined, {minimumFractionDigits:2}))}</strong></div>
        <div class="acct-item"><span class="muted-copy">Portfolio Value</span><strong>$${esc(acct.portfolio_value.toLocaleString(undefined, {minimumFractionDigits:2}))}</strong></div>
        <div class="acct-item"><span class="muted-copy">Buying Power</span><strong>$${esc(acct.buying_power.toLocaleString(undefined, {minimumFractionDigits:2}))}</strong></div>
        <div class="acct-item"><span class="muted-copy">Unrealized P&L</span><strong class="${plClass}">${plSign}$${acct.unrealized_pl.toFixed(2)}</strong></div>
        <div class="acct-item"><span class="muted-copy">Day Trades Used</span><strong>${esc(acct.daytrade_count)} / 3</strong></div>
      </div>`;
  } catch {
    accountSummary.innerHTML = `<p class="muted-copy">Could not load account.</p>`;
  }

  // Load positions
  try {
    const res = await fetch(`${API_BASE_URL}/trading/positions`, { credentials: "include" });
    if (!res.ok) throw new Error();
    const positions = await res.json();
    if (!positions.length) {
      positionsList.innerHTML = `<p class="muted-copy">No open positions.</p>`;
    } else {
      positionsList.innerHTML = `
        <p class="eyebrow" style="margin-bottom:8px;">Open Positions</p>
        ${positions.map((p) => {
          const plCls = p.unrealized_pl >= 0 ? "positive" : "negative";
          const plS   = p.unrealized_pl >= 0 ? "+" : "";
          return `
            <div class="position-row">
              <strong>${esc(p.symbol)}</strong>
              <span>${esc(p.qty)} shares @ $${p.avg_entry_price.toFixed(2)}</span>
              <span>Now $${p.current_price.toFixed(2)}</span>
              <span class="${plCls}">${plS}$${p.unrealized_pl.toFixed(2)} (${plS}${(p.unrealized_plpc * 100).toFixed(2)}%)</span>
              <button class="ghost-button close-pos-btn" data-symbol="${esc(p.symbol)}" type="button">Close</button>
            </div>`;
        }).join("")}`;
    }
  } catch {
    positionsList.innerHTML = "";
  }

  // Load recent orders
  try {
    const res = await fetch(`${API_BASE_URL}/trading/orders?order_status=all`, { credentials: "include" });
    if (!res.ok) throw new Error();
    const orders = await res.json();
    if (!orders.length) {
      ordersList.innerHTML = "";
    } else {
      ordersList.innerHTML = `
        <p class="eyebrow" style="margin-bottom:8px;">Recent Orders</p>
        ${orders.slice(0, 10).map((o) => {
          const statusCls = o.status === "filled" ? "positive" : o.status === "canceled" ? "negative" : "muted-copy";
          return `
            <div class="order-row">
              <span class="order-side ${esc(o.side)}">${esc(o.side.toUpperCase())}</span>
              <strong>${esc(o.symbol)}</strong>
              <span>${esc(o.qty)} shares</span>
              <span>${esc(o.type)}</span>
              <span class="${statusCls}">${esc(o.status)}</span>
              ${o.filled_avg_price ? `<span>@ $${esc(o.filled_avg_price)}</span>` : ""}
            </div>`;
        }).join("")}`;
    }
  } catch {
    ordersList.innerHTML = "";
  }
}

// Close position button
positionsList.addEventListener("click", async (e) => {
  const btn = e.target.closest(".close-pos-btn");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Closing…";
  try {
    const res = await fetch(`${API_BASE_URL}/trading/positions/${btn.dataset.symbol}`, {
      method: "DELETE", credentials: "include",
    });
    if (!res.ok) throw new Error();
    loadAccount();
  } catch {
    btn.textContent = "Failed";
  }
});

refreshAcctBtn.addEventListener("click", loadAccount);

// ── Timeframe toggle ─────────────────────────────────────────────────────────
if (tfBtnGroup) {
  tfBtnGroup.addEventListener("click", async (e) => {
    const btn = e.target.closest(".tf-btn");
    if (!btn || btn.classList.contains("tf-btn-active")) return;
    const tf = btn.dataset.tf;
    if (!tf) return;

    // Update active state
    tfBtnGroup.querySelectorAll(".tf-btn").forEach((b) => b.classList.remove("tf-btn-active"));
    btn.classList.add("tf-btn-active", "tf-btn-loading");

    currentTimeframe = tf;
    updateTfEyebrow();

    // Destroy existing chart so it rebuilds with fresh series
    clearChartMarkers();
    if (chartInstance) {
      chartInstance.remove();
      chartInstance = null;
      candleSeriesRef = null;
      volSeriesRef = null;
    }
    chartStatusEl.textContent = "Loading chart data…";

    await loadChart(tf);
    btn.classList.remove("tf-btn-loading");

    // Re-apply price lines if analysis was already rendered
    if (lastAnalysisData) {
      renderChartMarkers(lastAnalysisData);
    }
  });
}

// ── ICT toggle ───────────────────────────────────────────────────────────────
// Reflect persisted state into the checkbox and re-render zones on flip.
// Flipping OFF tears down the overlay immediately; flipping ON re-fetches
// with the current timeframe so we get the latest zone geometry.
const ictToggleEl = document.getElementById("ict-toggle");
if (ictToggleEl) {
  ictToggleEl.checked = ictToggleOn;
  ictToggleEl.addEventListener("change", async () => {
    ictToggleOn = ictToggleEl.checked;
    try { localStorage.setItem("ictToggleOn", String(ictToggleOn)); } catch (_) {}
    if (!ictToggleOn) {
      // Hide — tear down any active overlay and clear the pill row.
      if (ictOverlayHandle) {
        ictOverlayHandle.destroy();
        ictOverlayHandle = null;
      }
      const pillsEl = document.getElementById("ict-pills");
      if (pillsEl) { pillsEl.innerHTML = ""; pillsEl.classList.add("hidden"); }
      return;
    }
    // Show — refetch + re-render against the current bars. We don't
    // have the last bar list in scope, so the simplest correct path
    // is to kick loadChart which will call loadICT at the end.
    // (loadChart re-uses the existing chart instance, so no flicker.)
    loadChart();
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────
pageSymbol.textContent = symbol;
document.title = `${symbol} — AI Analysis · TradeHack`;

// Fire both fetches simultaneously; chart renders fast, analysis ~5 s
loadChart();
runAnalysis();
loadAccount();

reanalyzeBtn.addEventListener("click", runAnalysis);
if (pnlSharesInput) pnlSharesInput.addEventListener("input", calcPnl);
