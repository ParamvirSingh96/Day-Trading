// Setup Watch panel (v7.15a, 2026-05-04)
// =============================================================================
// Renders /market/setup-watch as a per-symbol diagnostic table.
// Read-only: this panel never triggers trades, applies, or autotrade
// state changes. It just surfaces structural state so the operator
// has visibility on quiet mornings ("Setups: 0" → "QCOM: gap skipped
// prior high, no sweep possible yet").
//
// The backend endpoint caches for 30s; this module polls every 30s.
// Manual ↻ Refresh button bypasses the local timing only — the
// server cache still applies.
// =============================================================================

import { API_BASE_URL } from "./config.js";

const POLL_INTERVAL_MS = 30_000;
let _poll_handle = null;

const els = {
  section:    document.getElementById("setup-watch-section"),
  meta:       document.getElementById("setup-watch-meta"),
  summary:    document.getElementById("setup-watch-summary"),
  list:       document.getElementById("setup-watch-list"),
  empty:      document.getElementById("setup-watch-empty"),
  refreshBtn: document.getElementById("setup-watch-refresh-btn"),
};

// Code → display label + chip color tier. Mirrors the priority order
// the backend's _CODE_PRIORITY uses; rendered codes appear in this
// order within each row.
const CODE_META = {
  setup_forming: {
    label: "Sweep + MSS",
    tier:  "fresh",
    description: "Broad sweep + MSS alignment. Research/watch state only — not predicate-ready by itself.",
  },
  swept_no_mss: {
    label: "Swept, no MSS",
    tier:  "fresh",
    description: "Sweep happened — waiting for market-structure shift",
  },
  extended_from_vwap: {
    label: "Extended from VWAP",
    tier:  "neutral",
    description: "Mean-reversion window may produce setup later",
  },
  asian_range_intact: {
    label: "Asian range intact",
    tier:  "neutral",
    description: "Price still inside the asian range — no sweep attempted",
  },
  no_sweep_yet: {
    label: "No sweep yet",
    tier:  "neutral",
    description: "No tracked level swept this session",
  },
  low_volume: {
    label: "Low volume",
    tier:  "muted",
    description: "Below the volume-confirmation threshold",
  },
  outside_killzone: {
    label: "Outside killzone",
    tier:  "muted",
    description: "Outside ICT killzone window — gated predicates won't fire",
  },
  level_skipped_by_gap: {
    label: "Level skipped by gap",
    tier:  "warn",
    description: "Gap took price past the prior-day extreme — sweep impossible from intraday",
  },
  trend_day_candidate: {
    label: "Trend-day candidate",
    tier:  "warn",
    description: "Persistent directional move; current sweep-based scanner unlikely to fire today",
  },
};

// ── Network ─────────────────────────────────────────────────────────────

async function fetchSetupWatch() {
  const r = await fetch(`${API_BASE_URL}/market/setup-watch`, {
    credentials: "include",
  });
  if (!r.ok) throw new Error(`GET /market/setup-watch failed: ${r.status}`);
  return r.json();
}

// ── Rendering ───────────────────────────────────────────────────────────

function fmtAge(secondsAgo) {
  if (secondsAgo == null) return "—";
  if (secondsAgo < 60)   return `${Math.round(secondsAgo)}s ago`;
  return `${Math.floor(secondsAgo / 60)}m ago`;
}

function renderHeader(payload) {
  const counts = payload.summary_counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const scannedAt = payload.scanned_at ? new Date(payload.scanned_at) : null;

  // Meta line: "33 symbols · scanned 09:48 ET"
  const parts = [];
  parts.push(`${payload.active_universe_size} symbols`);
  if (scannedAt) {
    const hh = String(scannedAt.getHours()).padStart(2, "0");
    const mm = String(scannedAt.getMinutes()).padStart(2, "0");
    parts.push(`scanned ${hh}:${mm} ET`);
  }
  els.meta.textContent = parts.join(" · ");

  // Summary line: "1 forming · 4 swept_no_mss · 12 asian_range_intact ..."
  // Sort by priority (use the order from CODE_META keys)
  const ordered = Object.keys(CODE_META).filter((c) => counts[c] > 0);
  if (!ordered.length) {
    els.summary.textContent =
      "Per-symbol structural state. No diagnostic codes triggered yet for this scan — symbols are quiet.";
  } else {
    const summaryParts = ordered.map((c) => {
      const m = CODE_META[c];
      return `${counts[c]} ${m.label.toLowerCase()}`;
    });
    els.summary.textContent = `Today's distribution: ${summaryParts.join(" · ")}.`;
  }
}

// v7.15.1: compact tabular row. One physical line per symbol —
// SYMBOL | GAP | VWAP-DIST | VOL | STRUCTURE | [code chips]. Per-symbol summary
// (the head code's human description) moves to the row's title
// tooltip so the line stays scannable across 33 symbols.
function renderRow(d) {
  const row = document.createElement("div");
  row.className = "setup-watch-row";
  row.dataset.symbol = d.symbol;
  if (d.summary) row.title = d.summary;

  // Symbol cell
  const sym = document.createElement("div");
  sym.className = "setup-watch-cell setup-watch-cell-symbol";
  sym.textContent = d.symbol;
  row.appendChild(sym);

  // Gap cell — green/red on direction, dash when null
  const gap = document.createElement("div");
  gap.className = "setup-watch-cell setup-watch-cell-numeric";
  if (d.gap_pct == null) {
    gap.textContent = "—";
    gap.classList.add("setup-watch-cell-muted");
  } else {
    gap.textContent = `${d.gap_pct >= 0 ? "+" : ""}${d.gap_pct.toFixed(1)}%`;
    gap.dataset.dir = d.gap_pct >= 0 ? "pos" : "neg";
  }
  row.appendChild(gap);

  // Distance-from-VWAP cell
  const dist = document.createElement("div");
  dist.className = "setup-watch-cell setup-watch-cell-numeric";
  if (d.distance_from_vwap_atr == null) {
    dist.textContent = "—";
    dist.classList.add("setup-watch-cell-muted");
  } else {
    dist.textContent = `${d.distance_from_vwap_atr.toFixed(1)}×`;
  }
  row.appendChild(dist);

  // Vol cell
  const vol = document.createElement("div");
  vol.className = "setup-watch-cell setup-watch-cell-numeric";
  if (d.vol_ratio == null) {
    vol.textContent = "—";
    vol.classList.add("setup-watch-cell-muted");
  } else {
    vol.textContent = `${d.vol_ratio.toFixed(1)}×`;
    if (d.vol_ratio < 1.0) vol.classList.add("setup-watch-cell-muted");
  }
  row.appendChild(vol);

  // Structure cell (v7.15.6) — broad diagnostic readiness percentage + bar.
  // This is NOT a trade probability. It only counts diagnostic prerequisites
  // that are visible to Setup Watch: killzone, recent sweep, MSS reversal,
  // volume, and directionally relevant FVG/OB touch. The live scanner still
  // requires strategy-specific levels/context before a trade can fire.
  // Tier (color) climbs with the percentage:
  //   0%        → muted   (nothing aligned)
  //   1–49%     → neutral (early structure)
  //   50–74%    → warm    (half the gates pass)
  //   75–99%    → fresh   (almost there — sweep + MSS + one of kz/vol)
  //   100%      → broad-ready (all 5 diagnostic gates pass; still not a trade signal)
  const match = document.createElement("div");
  match.className = "setup-watch-cell setup-watch-cell-progress";
  if (d.proximity_pct == null) {
    match.textContent = "—";
    match.classList.add("setup-watch-cell-muted");
  } else {
    const pct = d.proximity_pct;
    let tier = "muted";
    if (pct >= 100)      tier = "match";
    else if (pct >= 75)  tier = "fresh";
    else if (pct >= 50)  tier = "warm";
    else if (pct > 0)    tier = "neutral";
    match.dataset.tier = tier;

    const bar = document.createElement("span");
    bar.className = "setup-watch-progress-bar";
    const fill = document.createElement("span");
    fill.className = "setup-watch-progress-fill";
    fill.style.width = `${pct}%`;
    bar.appendChild(fill);

    const label = document.createElement("span");
    label.className = "setup-watch-progress-label";
    label.textContent = `${pct}%`;

    match.appendChild(bar);
    match.appendChild(label);

    const passed = d.proximity_passed != null ? d.proximity_passed : "?";
    const total  = d.proximity_total  != null ? d.proximity_total  : 5;
    match.title = `${passed}/${total} broad prerequisites pass — killzone, recent sweep, MSS reversal, volume, FVG/OB touch. Not a trade signal.`;
  }
  row.appendChild(match);

  // Codes cell — chips inline. Quiet symbols show a single muted
  // "—" instead of a row of chip-like blanks.
  const codesEl = document.createElement("div");
  codesEl.className = "setup-watch-cell setup-watch-cell-codes";
  if ((d.codes || []).length === 0) {
    codesEl.textContent = "—";
    codesEl.classList.add("setup-watch-cell-muted");
  } else {
    for (const code of d.codes) {
      const meta = CODE_META[code] || { label: code, tier: "neutral", description: code };
      const chip = document.createElement("span");
      chip.className = `setup-watch-code setup-watch-code-${meta.tier}`;
      chip.textContent = meta.label;
      chip.title = meta.description;
      codesEl.appendChild(chip);
    }
  }
  row.appendChild(codesEl);

  return row;
}

function renderList(payload) {
  const diags = payload.diagnostics || [];
  els.list.innerHTML = "";
  if (!diags.length) {
    const empty = document.createElement("div");
    empty.className = "setup-watch-empty muted-copy";
    empty.textContent = "No active universe — apply a morning universe or wait for autotrade to load symbols.";
    els.list.appendChild(empty);
    return;
  }
  // Header row — column labels for the table. Sticky for long lists
  // (CSS handles the sticky behavior).
  const header = document.createElement("div");
  header.className = "setup-watch-row setup-watch-row-header";
  for (const label of ["Symbol", "Gap", "VWAP", "Vol", "Structure", "Diagnostics"]) {
    const cell = document.createElement("div");
    cell.className = "setup-watch-cell setup-watch-cell-header";
    cell.textContent = label;
    header.appendChild(cell);
  }
  els.list.appendChild(header);

  for (const d of diags) {
    els.list.appendChild(renderRow(d));
  }
}

function render(payload) {
  renderHeader(payload);
  renderList(payload);
}

// ── Lifecycle ────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const payload = await fetchSetupWatch();
    render(payload);
  } catch (err) {
    if (err && /401/.test(err.message)) return;   // pre-login is expected
    els.list.innerHTML = "";
    const e = document.createElement("div");
    e.className = "setup-watch-empty muted-copy";
    e.textContent = `Error: ${err.message}`;
    e.dataset.level = "warn";
    els.list.appendChild(e);
  }
}

function init() {
  if (!els.section) return;
  els.refreshBtn.addEventListener("click", () => {
    els.refreshBtn.disabled = true;
    refresh().finally(() => { els.refreshBtn.disabled = false; });
  });

  refresh();
  if (_poll_handle == null) {
    _poll_handle = setInterval(refresh, POLL_INTERVAL_MS);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
