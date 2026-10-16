// ── ICT overlay — zone / liquidity / sweep visuals on TradingView charts ─────
//
// Consumes `/market/ict/{symbol}` and paints the geometry on top of an
// existing Lightweight Charts instance. Two concerns are separated:
//
//   fetchICTZones()    — pure data fetch. Returns the response dict or null.
//   renderICTOverlay() — draws zone lines + sweep/MSS markers on a
//                        candle series. Returns a destroy() cleanup.
//   renderICTPills()   — paints the at-a-glance header pills (killzone,
//                        premium/discount, OTE flags) into a DOM node.
//
// Every render is idempotent: callers should invoke destroy() before
// re-rendering with fresh data so stale price lines don't accumulate.
// The module is deliberately stateless — all handles live in the
// returned object so the caller owns the lifecycle.

import { API_BASE_URL } from "./config.js";

const LS = LightweightCharts.LineStyle;

// ── Palette — transparent so candles stay readable underneath ───────────────
// Keep these keyed by concept so tweaks are obvious at a glance.
const COL = {
  bullZone:   "rgba(0, 217, 126, 0.45)",
  bullFill:   "rgba(0, 217, 126, 0.08)",
  bearZone:   "rgba(255, 77, 90, 0.45)",
  bearFill:   "rgba(255, 77, 90, 0.08)",
  liquidity:  "rgba(167, 139, 250, 0.55)",  // muted purple — matches VWAP hue
  sweepBull:  "#00d97e",
  sweepBear:  "#ff4d5a",
  mssBull:    "#22c55e",
  mssBear:    "#ef4444",
};

// ── Caps — keep the overlay legible on a busy chart ─────────────────────────
// Limits apply per-direction. Backend already caps zones to ~20 items
// per list; we take a tighter subset to avoid visual noise.
const CAPS = {
  fvgPerDir:  4,
  obPerDir:   4,
  sweeps:     6,
  mssEvents:  4,
};


// ── Fetch ────────────────────────────────────────────────────────────────────

/**
 * Hit the ICT endpoint and return its JSON, or null on any failure.
 * Silent on error — the analysis page should keep working even when
 * the ICT layer can't load (e.g. backend restart).
 */
export async function fetchICTZones(symbol, timeframe = "1Min") {
  try {
    const res = await fetch(
      `${API_BASE_URL}/market/ict/${symbol}?timeframe=${timeframe}`,
      { credentials: "include" },
    );
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}


// ── Overlay rendering ────────────────────────────────────────────────────────

/**
 * Paint ICT zones + liquidity + event markers on top of an existing
 * candle series. Returns `{ destroy }` so callers can tear the overlay
 * down before re-rendering.
 *
 * @param {object} candleSeries  LightweightCharts candle series handle
 * @param {object[]} bars        Bars used by the chart (for idx→time lookup)
 * @param {object} data          Response body from /market/ict/{symbol}
 * @param {object} [opts]
 * @param {boolean} [opts.showZones=true]
 * @param {boolean} [opts.showLiquidity=true]
 * @param {boolean} [opts.showMarkers=true]
 */
export function renderICTOverlay(candleSeries, bars, data, opts = {}) {
  const {
    showZones     = true,
    showLiquidity = true,
    showMarkers   = true,
  } = opts;

  if (!candleSeries || !data || !data.zones) {
    return { destroy: () => {} };
  }

  const priceLines = [];
  let markersApplied = false;

  const addLine = (price, color, title, style = LS.Dashed, width = 1) => {
    if (price == null || isNaN(price)) return;
    try {
      priceLines.push(candleSeries.createPriceLine({
        price, color, lineWidth: width, lineStyle: style,
        axisLabelVisible: true, title,
      }));
    } catch (_) { /* series may have been disposed */ }
  };

  // ── Liquidity levels ──────────────────────────────────────────────
  // Named-level horizontal lines. Skip session_high/low because the
  // user already sees them as the chart's visible extremes; we keep
  // the chart uncluttered by focusing on LEVELS FROM PRIOR SESSIONS
  // (PDH/PDL/prev_close) which the tape can't show on its own.
  if (showLiquidity && data.zones.liquidity) {
    const liq = data.zones.liquidity;
    if (liq.pdh        != null) addLine(liq.pdh,        COL.liquidity, "PDH",      LS.LargeDashed);
    if (liq.pdl        != null) addLine(liq.pdl,        COL.liquidity, "PDL",      LS.LargeDashed);
    if (liq.prev_close != null) addLine(liq.prev_close, COL.liquidity, "Prev Cl.", LS.Dotted);
  }

  // ── FVGs + OBs: top-N most recent per direction, unmitigated only ──
  // We show BOTH boundaries of each zone (low + high) so the user can
  // read the zone width visually. Mitigated zones are intentionally
  // excluded — they're historical and muddy the live read.
  if (showZones) {
    const fvgs = data.zones.fvgs || [];
    const obs  = data.zones.obs  || [];

    const freshBull = (items) => items
      .filter((z) => z.direction === "bullish" && z.mitigated_idx == null)
      .sort((a, b) => b.created_idx - a.created_idx);
    const freshBear = (items) => items
      .filter((z) => z.direction === "bearish" && z.mitigated_idx == null)
      .sort((a, b) => b.created_idx - a.created_idx);

    const bullFVGs = freshBull(fvgs).slice(0, CAPS.fvgPerDir);
    const bearFVGs = freshBear(fvgs).slice(0, CAPS.fvgPerDir);
    const bullOBs  = freshBull(obs).slice(0, CAPS.obPerDir);
    const bearOBs  = freshBear(obs).slice(0, CAPS.obPerDir);

    bullFVGs.forEach((z, i) => {
      addLine(z.high, COL.bullZone, i === 0 ? "FVG+" : "", LS.Dotted);
      addLine(z.low,  COL.bullZone, "",                    LS.Dotted);
    });
    bearFVGs.forEach((z, i) => {
      addLine(z.high, COL.bearZone, "",                    LS.Dotted);
      addLine(z.low,  COL.bearZone, i === 0 ? "FVG−" : "", LS.Dotted);
    });
    bullOBs.forEach((z, i) => {
      addLine(z.high, COL.bullZone, i === 0 ? "OB+" : "", LS.Dashed);
      addLine(z.low,  COL.bullZone, "",                   LS.Dashed);
    });
    bearOBs.forEach((z, i) => {
      addLine(z.high, COL.bearZone, "",                   LS.Dashed);
      addLine(z.low,  COL.bearZone, i === 0 ? "OB−" : "", LS.Dashed);
    });
  }

  // ── Sweep + MSS markers ──────────────────────────────────────────
  // Markers sit at the exact bar time of the event. Bull events
  // (sweep that closed back up, bull MSS) render as up-triangles BELOW
  // the bar; bear events mirror above the bar. This matches the way
  // ICT annotations read on paper.
  if (showMarkers) {
    const markers = [];
    const sweeps     = (data.zones.sweeps     || []).slice(-CAPS.sweeps);
    const mssEvents  = (data.zones.mss_events || []).slice(-CAPS.mssEvents);

    const idxTime = (idx) => {
      if (idx == null || idx < 0 || idx >= bars.length) return null;
      return bars[idx].time;
    };

    for (const s of sweeps) {
      const t = idxTime(s.idx);
      if (t == null) continue;
      const bull = s.direction === "bullish";
      markers.push({
        time:     t,
        position: bull ? "belowBar" : "aboveBar",
        shape:    bull ? "arrowUp"  : "arrowDown",
        color:    bull ? COL.sweepBull : COL.sweepBear,
        text:     `Sweep · ${s.level_name}`,
      });
    }
    for (const m of mssEvents) {
      const t = idxTime(m.idx);
      if (t == null) continue;
      const bull = m.direction === "bullish";
      markers.push({
        time:     t,
        position: bull ? "belowBar" : "aboveBar",
        shape:    "circle",
        color:    bull ? COL.mssBull : COL.mssBear,
        text:     bull ? "MSS↑" : "MSS↓",
      });
    }

    markers.sort((a, b) => a.time - b.time);
    if (markers.length) {
      try {
        candleSeries.setMarkers(markers);
        markersApplied = true;
      } catch (_) { /* series may be disposed */ }
    }
  }

  return {
    destroy() {
      priceLines.forEach((pl) => {
        try { candleSeries.removePriceLine(pl); } catch (_) {}
      });
      priceLines.length = 0;
      if (markersApplied) {
        try { candleSeries.setMarkers([]); } catch (_) {}
      }
    },
  };
}


// ── Pills (killzone, PD, OTE flags) ──────────────────────────────────────────

/**
 * Paint a compact pill row describing the at-a-glance ICT context
 * — killzone, premium/discount, OTE status, current-zone touches.
 * The pills are meant to live above the chart. Re-rendering replaces
 * the previous content so this is safe to call on every ICT refresh.
 */
export function renderICTPills(container, data) {
  if (!container) return;
  if (!data) {
    container.innerHTML = "";
    container.classList.add("hidden");
    return;
  }

  const pills = [];

  // Killzone pill — most prominent when in-session.
  const kzLabel = {
    ny_am:         "NY AM",
    silver_bullet: "Silver Bullet",
    ny_pm:         "NY PM",
  };
  if (data.killzone && kzLabel[data.killzone]) {
    pills.push(`<span class="ict-pill ict-pill-kz">${kzLabel[data.killzone]}</span>`);
  }

  // Premium / discount
  if (data.pd_zone) {
    const cls = data.pd_zone === "discount"   ? "ict-pill-discount"
              : data.pd_zone === "premium"    ? "ict-pill-premium"
              :                                 "ict-pill-equil";
    const pct = data.pd_pct != null ? ` · ${Math.round(data.pd_pct * 100)}%` : "";
    const cap = data.pd_zone[0].toUpperCase() + data.pd_zone.slice(1);
    pills.push(`<span class="ict-pill ${cls}">${cap}${pct}</span>`);
  }

  // OTE flags — only show when active so the row doesn't carry dead weight.
  if (data.in_ote_long)  pills.push(`<span class="ict-pill ict-pill-ote">OTE Long</span>`);
  if (data.in_ote_short) pills.push(`<span class="ict-pill ict-pill-ote-short">OTE Short</span>`);

  // Zone touches
  if (data.in_bull_zone) pills.push(`<span class="ict-pill ict-pill-bullzone">In Bull Zone</span>`);
  if (data.in_bear_zone) pills.push(`<span class="ict-pill ict-pill-bearzone">In Bear Zone</span>`);

  // Recent event context — last sweep/MSS with bars_ago for sense of freshness.
  if (data.last_sweep && data.last_sweep.bars_ago != null) {
    const s = data.last_sweep;
    const age = s.bars_ago === 0 ? "now" : `${s.bars_ago}m ago`;
    const arrow = s.direction === "bullish" ? "↑" : "↓";
    pills.push(
      `<span class="ict-pill ict-pill-event">Sweep ${arrow} ${s.level_name} · ${age}</span>`
    );
  }
  if (data.last_mss && data.last_mss.bars_ago != null) {
    const m = data.last_mss;
    const age = m.bars_ago === 0 ? "now" : `${m.bars_ago}m ago`;
    const arrow = m.direction === "bullish" ? "↑" : "↓";
    pills.push(
      `<span class="ict-pill ict-pill-event">MSS ${arrow} · ${age}</span>`
    );
  }

  if (!pills.length) {
    container.innerHTML = "";
    container.classList.add("hidden");
    return;
  }

  container.innerHTML = pills.join("");
  container.classList.remove("hidden");
}
