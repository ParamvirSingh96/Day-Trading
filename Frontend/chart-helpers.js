// ── Chart helpers — TradingView-inspired styling ─────────────────────────────
// Used by main.js (chart modal, perf chart) and analysis.js (analysis chart).

// ── Math helpers ─────────────────────────────────────────────────────────────

export function computeEMA(bars, period) {
  if (!bars || bars.length < period) return [];
  const k = 2 / (period + 1);
  const result = [];
  let sum = 0;
  for (let i = 0; i < period; i++) sum += bars[i].close;
  let prev = sum / period;
  result.push({ time: bars[period - 1].time, value: Math.round(prev * 100) / 100 });
  for (let i = period; i < bars.length; i++) {
    prev = bars[i].close * k + prev * (1 - k);
    result.push({ time: bars[i].time, value: Math.round(prev * 100) / 100 });
  }
  return result;
}

export function fmtVol(v) {
  if (v == null || isNaN(v)) return "—";
  if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toFixed(0);
}

// Lightweight Charts formats Unix timestamps as UTC by default. The rest of
// the app uses the browser's timezone, so explicitly format chart labels the
// same way (usually America/New_York on the trading machine).
const USER_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;

const _timeFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: USER_TIME_ZONE,
  hour: "numeric",
  minute: "2-digit",
});

const _dateFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: USER_TIME_ZONE,
  month: "short",
  day: "numeric",
});

const _dateTimeFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: USER_TIME_ZONE,
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

const _tzFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: USER_TIME_ZONE,
  timeZoneName: "short",
});

function _timeToDate(time) {
  if (typeof time === "number") return new Date(time * 1000);
  if (typeof time === "string") return new Date(time);
  if (time && typeof time === "object" && "year" in time) {
    return new Date(Date.UTC(time.year, (time.month || 1) - 1, time.day || 1));
  }
  return new Date();
}

function _tzDateKey(date) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: USER_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

export function getChartTimeZoneLabel() {
  const part = _tzFmt.formatToParts(new Date()).find((p) => p.type === "timeZoneName");
  return part?.value || USER_TIME_ZONE || "local";
}

export function formatChartCursorTime(time) {
  const date = _timeToDate(time);
  return `${_dateTimeFmt.format(date)} ${getChartTimeZoneLabel()}`;
}

function formatChartTick(time, tickMarkType) {
  const date = _timeToDate(time);
  // Lightweight Charts passes numeric tick types: 3/4 are intraday time ticks.
  // For non-time ticks, prefer a compact date label.
  if (tickMarkType === 3 || tickMarkType === 4 || tickMarkType === "Time" || tickMarkType === "TimeWithSeconds") {
    return _timeFmt.format(date);
  }
  const todayKey = _tzDateKey(new Date());
  return _tzDateKey(date) === todayKey ? _timeFmt.format(date) : _dateFmt.format(date);
}

// ── TradingView palette ─────────────────────────────────────────────────────

const TV = {
  bg:           "#131722",
  panelBg:      "#1e222d",
  grid:         "rgba(42, 46, 57, 0.6)",
  gridFine:     "rgba(42, 46, 57, 0.3)",
  text:         "#d1d4dc",
  textSecondary:"#787b86",
  textMuted:    "#545960",
  crosshair:    "#758696",
  crosshairLabel:"#131722",
  border:       "#2a2e39",
  scaleBorder:  "#2a2e39",
};

export const CANDLE_UP        = "#00e676";
export const CANDLE_DOWN      = "#ff0000";
export const CANDLE_UP_WICK   = "#00e676";
export const CANDLE_DOWN_WICK = "#ff0000";
export const VOL_UP           = "rgba(0, 230, 118, 0.20)";
export const VOL_DOWN         = "rgba(255, 0, 0, 0.20)";
export const EMA_FAST_COLOR   = "#2962ff";   // TradingView blue
export const EMA_SLOW_COLOR   = "#ff6d00";   // TradingView orange

// ── Chart factory ────────────────────────────────────────────────────────────

export function createEnhancedChart(container, symbol, width, height) {
  const chart = LightweightCharts.createChart(container, {
    width,
    height,
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: TV.textSecondary,
      fontSize: 11,
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Trebuchet MS', Roboto, Ubuntu, sans-serif",
    },
    grid: {
      vertLines: { color: TV.gridFine, visible: true },
      horzLines: { color: TV.grid, visible: true },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: {
        color: TV.crosshair,
        width: 1,
        style: LightweightCharts.LineStyle.Dashed,
        labelBackgroundColor: TV.crosshairLabel,
        labelVisible: true,
      },
      horzLine: {
        color: TV.crosshair,
        width: 1,
        style: LightweightCharts.LineStyle.Dashed,
        labelBackgroundColor: TV.crosshairLabel,
        labelVisible: true,
      },
    },
    rightPriceScale: {
      borderColor: TV.scaleBorder,
      borderVisible: true,
      scaleMargins: { top: 0.06, bottom: 0.18 },
      entireTextOnly: true,
      textColor: TV.textSecondary,
    },
    timeScale: {
      borderColor: TV.scaleBorder,
      borderVisible: true,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,
      minBarSpacing: 5,
      fixLeftEdge: false,
      fixRightEdge: false,
      tickMarkFormatter: formatChartTick,
    },
    localization: {
      priceFormatter: (p) => p.toFixed(2),
      timeFormatter: formatChartCursorTime,
    },
    watermark: {
      visible: false,
    },
    handleScroll: {
      mouseWheel: true,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: true,
    },
    handleScale: {
      mouseWheel: true,
      pinch: true,
      axisPressedMouseMove: { time: true, price: true },
      axisDoubleClickReset: { time: true, price: true },
    },
  });

  // Remove LWC attribution
  requestAnimationFrame(() => {
    container.querySelectorAll("a").forEach((a) => {
      if (a.href?.includes("tradingview") || a.querySelector("svg")) {
        a.style.display = "none";
      }
    });
    container.querySelectorAll("div[style*='z-index']").forEach((d) => {
      if (d.querySelector("a")) d.style.display = "none";
    });
  });

  return chart;
}

// ── Series builders ──────────────────────────────────────────────────────────

export function addCandleSeries(chart, bars) {
  const series = chart.addCandlestickSeries({
    upColor:         CANDLE_UP,
    downColor:       CANDLE_DOWN,
    borderUpColor:   CANDLE_UP,
    borderDownColor: CANDLE_DOWN,
    wickUpColor:     CANDLE_UP_WICK,
    wickDownColor:   CANDLE_DOWN_WICK,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  series.setData(bars);
  return series;
}

export function addVolumeSeries(chart, bars) {
  const series = chart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "volume",
    lastValueVisible: false,
    priceLineVisible: false,
  });
  chart.priceScale("volume").applyOptions({
    scaleMargins: { top: 0.82, bottom: 0 },
  });
  series.setData(bars.map((b) => ({
    time:  b.time,
    value: b.volume,
    color: b.close >= b.open ? VOL_UP : VOL_DOWN,
  })));
  return series;
}

export function addEMAOverlays(chart, bars) {
  const ema9Data  = computeEMA(bars, 9);
  const ema20Data = computeEMA(bars, 20);

  const ema9 = chart.addLineSeries({
    color: EMA_FAST_COLOR,
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  if (ema9Data.length) ema9.setData(ema9Data);

  const ema20 = chart.addLineSeries({
    color: EMA_SLOW_COLOR,
    lineWidth: 1.5,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  if (ema20Data.length) ema20.setData(ema20Data);

  return { ema9, ema20, ema9Data, ema20Data };
}

export function addCurrentPriceLine(candleSeries, bars) {
  if (!bars || !bars.length) return;
  const last = bars[bars.length - 1];
  const isUp = last.close >= last.open;
  candleSeries.createPriceLine({
    price:            last.close,
    color:            isUp ? CANDLE_UP : CANDLE_DOWN,
    lineWidth:        1,
    lineStyle:        LightweightCharts.LineStyle.Dotted,
    axisLabelVisible: true,
    title:            "",
  });
}

// ── OHLCV Legend overlay (TradingView-style) ────────────────────────────────

function updateLegendContent(el, bar, vol, symbol) {
  if (!bar) return;
  const isUp = bar.close >= bar.open;
  const cls  = isUp ? "leg-up" : "leg-dn";
  const chg  = bar.close - bar.open;
  const pct  = ((chg / bar.open) * 100);
  const sign = pct >= 0 ? "+" : "";

  el.innerHTML = `
    <div class="leg-row leg-row-main">
      <span class="leg-symbol">${symbol || ""}</span>
      <span class="leg-indicator"><span class="leg-dot" style="background:${EMA_FAST_COLOR}"></span>EMA 9</span>
      <span class="leg-indicator"><span class="leg-dot" style="background:${EMA_SLOW_COLOR}"></span>EMA 20</span>
    </div>
    <div class="leg-row">
      <span class="leg-lbl">O</span><span class="leg-val ${cls}">${bar.open.toFixed(2)}</span>
      <span class="leg-lbl">H</span><span class="leg-val ${cls}">${bar.high.toFixed(2)}</span>
      <span class="leg-lbl">L</span><span class="leg-val ${cls}">${bar.low.toFixed(2)}</span>
      <span class="leg-lbl">C</span><span class="leg-val ${cls}">${bar.close.toFixed(2)}</span>
      <span class="leg-chg ${cls}">${sign}${pct.toFixed(2)}%</span>
      <span class="leg-sep"></span>
      <span class="leg-lbl">Vol</span><span class="leg-val leg-vol">${fmtVol(vol)}</span>
    </div>
  `;
}

export function setupLegend(container, bars, candleSeries, volSeries, chart, symbol) {
  container.querySelector(".chart-legend")?.remove();

  const legend = document.createElement("div");
  legend.className = "chart-legend";
  container.appendChild(legend);

  const lastBar = bars[bars.length - 1];
  updateLegendContent(legend, lastBar, lastBar.volume, symbol);

  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.seriesData) {
      updateLegendContent(legend, lastBar, lastBar.volume, symbol);
      return;
    }
    const bar = param.seriesData.get(candleSeries);
    const vol = param.seriesData.get(volSeries);
    if (bar) {
      updateLegendContent(legend, bar, vol?.value ?? 0, symbol);
    }
  });
}

// ── Zoom controls ───────────────────────────────────────────────────────────

export function setupFreePan(container, chart) {
  let panning = false;
  let lastY = 0;
  const INITIAL_TOP    = 0.06;
  const INITIAL_BOTTOM = 0.18;

  container.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(".chart-zoom-ctrls, .chart-legend")) return;
    panning = true;
    lastY = e.clientY;
  });

  window.addEventListener("pointermove", (e) => {
    if (!panning) return;
    const dy = e.clientY - lastY;
    if (Math.abs(dy) < 1) return;
    lastY = e.clientY;
    const ps = chart.priceScale("right");
    ps.applyOptions({ autoScale: false });
    const h = container.clientHeight || 460;
    const shift = dy / h;
    const opts = ps.options();
    const top    = opts.scaleMargins?.top    ?? INITIAL_TOP;
    const bottom = opts.scaleMargins?.bottom ?? INITIAL_BOTTOM;
    ps.applyOptions({
      scaleMargins: { top: top + shift, bottom: bottom - shift },
    });
  });

  window.addEventListener("pointerup", () => { panning = false; });

  return function resetVertical() {
    chart.priceScale("right").applyOptions({
      autoScale: true,
      scaleMargins: { top: INITIAL_TOP, bottom: INITIAL_BOTTOM },
    });
  };
}

export function setupZoomControls(container, chart, resetVerticalFn) {
  container.querySelector(".chart-zoom-ctrls")?.remove();

  const wrap = document.createElement("div");
  wrap.className = "chart-zoom-ctrls";
  wrap.innerHTML = `
    <button class="czoom-btn" data-action="in" title="Zoom in">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2v10M2 7h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
    </button>
    <button class="czoom-btn" data-action="out" title="Zoom out">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 7h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
    </button>
    <button class="czoom-btn" data-action="fit" title="Reset">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 5V1h4M13 5V1H9M1 9v4h4M13 9v4H9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
  `;

  wrap.addEventListener("click", (e) => {
    const btn = e.target.closest(".czoom-btn");
    if (!btn) return;
    e.stopPropagation();
    const action = btn.dataset.action;
    const ts = chart.timeScale();
    if (action === "in") {
      const range = ts.getVisibleLogicalRange();
      if (range) {
        const span = range.to - range.from;
        const shrink = span * 0.25;
        ts.setVisibleLogicalRange({ from: range.from + shrink, to: range.to });
      }
    } else if (action === "out") {
      const range = ts.getVisibleLogicalRange();
      if (range) {
        const span = range.to - range.from;
        const expand = span * 0.5;
        ts.setVisibleLogicalRange({ from: range.from - expand, to: range.to });
      }
    } else if (action === "fit") {
      ts.fitContent();
      if (resetVerticalFn) resetVerticalFn();
    }
  });

  container.appendChild(wrap);
}
