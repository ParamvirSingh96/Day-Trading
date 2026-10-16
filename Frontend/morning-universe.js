// Morning Universe Panel (v7.13 Phase 2, 2026-05-01)
// =============================================================================
// Renders the daily screener output as a ranked-candidate panel above
// the manual universe editor in app.html. Two visual tiers — top-N
// applied (filled chips) and watchlist-only (outlined chips). Apply
// button POSTs to /market/morning-universe/apply with idempotency
// guards so a double-click can't create two valid apply snapshots
// for the same source file.
//
// API contract (see Backend/app/routers/market.py):
//   GET  /market/morning-universe → {
//     snapshot, snapshot_path, snapshot_age_seconds,
//     applied: {applied_at, applied_snapshot_path, source_snapshot_path,
//               restored_from_disk, n_base, n_dynamic, ...} | null,
//     active_universe
//   }
//   POST /market/morning-universe/apply (body: {} for defaults)
//     → {ok, applied_snapshot_path, applied_at, trading_universe,
//        n_base, n_dynamic, source_snapshot_path}
//
// Errors come back as {detail: {code, message}} with HTTP 4xx/5xx;
// we surface the code-driven message directly to the operator.
// =============================================================================

import { API_BASE_URL } from "./config.js";
import { showToast } from "./notifications.js";
import {
  createEnhancedChart,
  addCandleSeries,
  CANDLE_UP, CANDLE_DOWN,
} from "./chart-helpers.js?v=2";

const POLL_INTERVAL_MS = 30_000;   // 30s — balances freshness with API load
const CHART_REFRESH_MS = 60_000;   // 60s — mini-charts refresh once a minute
let _poll_handle = null;
let _chart_refresh_handle = null;
let _last_state = null;            // most recent payload, used for idempotency
let _rendered_chart_symbols = []; // track which symbols are currently in the grid (avoid pointless re-renders)

// Cache DOM lookups once at module load. The dashboard is single-page
// so these elements never get re-mounted.
//
// v7.15.1 (2026-05-05): the Morning Universe panel (chips, watchlist
// tier, applied banner, data-as-of header, empty-state) was removed
// from the dashboard. The Apply button + applied state now live in
// the chart-strip header. References to the deleted elements are
// gone; the surviving elements all live in the chart-strip section
// or are top-level (charts grid, etc.).
const els = {
  // Chart strip section — the operator's primary surface
  panel:            document.getElementById("morning-charts-section"),
  chartsMeta:       document.getElementById("morning-charts-meta"),
  chartsGrid:       document.getElementById("morning-charts-grid"),
  chartsEmpty:      document.getElementById("morning-charts-empty"),
  chartsRefreshBtn: document.getElementById("morning-charts-refresh-btn"),
  // Header-right buttons + applied indicator (Apply moved here in v7.15.1)
  buildBtn:        document.getElementById("morning-build-btn"),
  applyBtn:        document.getElementById("morning-apply-btn"),
  applyFeedback:   document.getElementById("morning-apply-feedback"),
  appliedBadge:    document.getElementById("morning-applied-badge"),
  appliedBadgeText:document.getElementById("morning-applied-badge-text"),
};

// ── Formatting helpers ─────────────────────────────────────────────────────

// Strip a full path down to its filename so the dashboard never shows
// server filesystem paths — the audit information is the filename.
function basename(path) {
  if (!path) return "";
  return String(path).split(/[\\/]/).pop();
}

// Render snapshot age with color-coded freshness. The dashboard panel
// hugely benefits from "is this signal still fresh?" being a glance.
//   < 60 min  → "Xm old"          (normal — still actionable)
//   < 6 h     → "Xh Ym old"        (normal — still actionable)
//   < 12 h    → "Xh old"           (caution — late-day apply)
//   ≥ 12 h    → "stale (Xh)"       (warn — likely past today's edge)
function ageLabel(seconds) {
  if (seconds == null) return { text: "—", level: "muted" };
  const mins = Math.round(seconds / 60);
  if (mins < 60) return { text: `${mins}m old`, level: "fresh" };
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (hours < 6)  return { text: `${hours}h ${remMins}m old`, level: "fresh" };
  if (hours < 12) return { text: `${hours}h old`,             level: "caution" };
  return { text: `stale (${hours}h)`,                          level: "warn" };
}

// Friendly formatting of the apply timestamp ("2026-05-01T10:28:29-04:00")
// → "10:28 ET". Keeps the banner short and scannable.
function formatAppliedAt(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm} ET`;
  } catch { return iso; }
}

// ── Network ────────────────────────────────────────────────────────────────

async function fetchMorningUniverse() {
  const r = await fetch(`${API_BASE_URL}/market/morning-universe`, {
    credentials: "include",
  });
  if (!r.ok) {
    throw new Error(`GET /market/morning-universe failed: ${r.status}`);
  }
  return r.json();
}

// Build endpoint — runs the screener server-side. ~2 min wall-clock
// for the default 82-symbol pool. The handler runs on a worker thread,
// so other UI panels keep responding while this is in flight.
async function postBuild() {
  const r = await fetch(`${API_BASE_URL}/market/morning-universe/build`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),   // defaults — same knobs as the CLI
  });
  const payload = await r.json().catch(() => null);
  if (!r.ok) {
    const detail = payload && payload.detail;
    const message = (typeof detail === "string" ? detail : (detail && detail.message)) || `HTTP ${r.status}`;
    const err = new Error(message);
    err.status = r.status;
    throw err;
  }
  return payload;
}

// Bar fetch for the mini-chart grid. Uses the existing /market/chart
// endpoint, defaulting to 1Min × last 8 hours (covers premarket +
// today's session). Promise.all'd by the grid renderer so all 10
// symbols load in parallel rather than serially.
async function fetchChartBars(symbol) {
  const r = await fetch(
    `${API_BASE_URL}/market/chart/${encodeURIComponent(symbol)}?timeframe=1Min`,
    { credentials: "include" },
  );
  if (!r.ok) throw new Error(`chart ${symbol}: HTTP ${r.status}`);
  const j = await r.json();
  return j.bars || [];
}

async function postApply() {
  const r = await fetch(`${API_BASE_URL}/market/morning-universe/apply`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),   // defaults — today's snapshot, 12h cutoff
  });
  const payload = await r.json().catch(() => null);
  if (!r.ok) {
    // Backend returns {detail: {code, message}}. Surface the message
    // verbatim — it carries actionable text like "Re-run the morning
    // scanner before applying." No need to wrap it in a generic
    // "Something went wrong."
    const detail = payload && payload.detail;
    const message = (detail && detail.message) || `HTTP ${r.status}`;
    const code = (detail && detail.code) || "error";
    const err = new Error(message);
    err.code = code;
    err.status = r.status;
    throw err;
  }
  return payload;
}

// ── Rendering ──────────────────────────────────────────────────────────────

// v7.15.1: data-as-of now lives in the chart-strip's "morning-charts-meta"
// span (small text in the header). One line: "generated 08:32 ET · 4m old".
function renderDataAsOf(payload) {
  const snap = payload && payload.snapshot;
  if (!els.chartsMeta) return;
  if (!snap) {
    els.chartsMeta.textContent = "no snapshot today";
    els.chartsMeta.dataset.level = "warn";
    return;
  }
  const age = ageLabel(payload.snapshot_age_seconds);
  const generatedLabel = formatAppliedAt(snap.generated_at);
  els.chartsMeta.textContent =
    `generated ${generatedLabel} · ${age.text} · feed=${snap.feed || "?"}`;
  els.chartsMeta.dataset.level = age.level;
}

// v7.15.1: applied state shows as a compact badge in the chart-strip
// header. Green when applied this session; yellow when restored after
// restart; hidden when no apply.
function renderAppliedBadge(payload) {
  const a = payload && payload.applied;
  if (!els.appliedBadge) return;
  if (!a) {
    els.appliedBadge.hidden = true;
    return;
  }
  const restored = a.restored_from_disk;
  els.appliedBadge.hidden = false;
  els.appliedBadge.dataset.restored = String(!!restored);
  // Compact: "23+10 · 08:35 ET" (with " · restored" suffix when applicable)
  const whenLabel = formatAppliedAt(a.applied_at);
  const restoredSuffix = restored ? " · restored" : "";
  els.appliedBadgeText.textContent = `${a.n_base}+${a.n_dynamic} · ${whenLabel}${restoredSuffix}`;
  // Tooltip carries the full context for hover-to-inspect
  els.appliedBadge.title =
    `Active universe: ${a.n_base} base + ${a.n_dynamic} morning dynamic\n` +
    `Applied at:     ${a.applied_at}\n` +
    `Apply file:     ${basename(a.applied_snapshot_path)}\n` +
    `Source:         ${basename(a.source_snapshot_path) || "—"}\n` +
    `Restored after restart: ${restored ? "yes" : "no"}`;
}

// renderChip + renderTiers — DEPRECATED in v7.15.1.
// The Morning Universe panel was removed; chips and tier rendering
// no longer have a DOM target. Functions kept (unused) so any
// external caller doesn't crash on a missing-name reference; can
// be deleted in a future cleanup pass.
function renderChip(c, kind) {
  const chip = document.createElement("div");
  chip.className = `morning-chip morning-chip-${kind}`;
  chip.dataset.symbol = c.symbol;

  const sym = document.createElement("span");
  sym.className = "morning-chip-symbol";
  sym.textContent = c.symbol;

  const score = document.createElement("span");
  score.className = "morning-chip-score muted-copy";
  score.textContent = (c.score != null) ? c.score.toFixed(2) : "—";

  const tags = document.createElement("span");
  tags.className = "morning-chip-tags muted-copy";
  tags.textContent = (c.reason_tags || []).slice(0, 3).join(" · ");

  chip.appendChild(sym);
  chip.appendChild(score);
  chip.appendChild(tags);

  // Hover tooltip carries the full reason list + numeric details so
  // the chip itself stays compact.
  const ttLines = [
    `Rank ${c.rank ?? "—"} · score ${c.score ?? "—"}`,
    `Source: ${c.source}`,
    c.gap_pct != null      ? `Gap: ${c.gap_pct >= 0 ? "+" : ""}${c.gap_pct}%` : null,
    c.pm_dollar_vol != null
      ? `PM $vol: $${(c.pm_dollar_vol/1_000_000).toFixed(1)}M` : null,
    c.avg_daily_volume != null
      ? `ADV: ${(c.avg_daily_volume/1_000_000).toFixed(1)}M sh` : null,
    c.has_news_catalyst && (c.catalyst_keywords || []).length
      ? `Catalysts: ${c.catalyst_keywords.join(", ")}` : null,
    c.sector_etf && c.sector_etf_gap_pct != null
      ? `${c.sector_etf}: ${c.sector_etf_gap_pct >= 0 ? "+" : ""}${c.sector_etf_gap_pct}%` : null,
  ].filter(Boolean);
  chip.title = ttLines.join("\n");

  return chip;
}

// v7.15.1: renderTiers is a no-op now. The tier elements
// (#morning-tier-applied, #morning-tier-watchlist) were removed from
// the dashboard. Function kept so render() doesn't crash if any
// external code path still calls it; can be deleted later.
function renderTiers(payload) { /* no-op since v7.15.1 */ }

// Apply-button enable/disable logic (review feedback Phase 2 #3).
// The button is disabled when:
//   * No snapshot loaded yet (initial page load before fetch resolves)
//   * No screener snapshot for today (nothing to apply)
//   * Snapshot is already applied AND
//     applied.source_snapshot_path == current snapshot's path
//     (idempotency — one Apply per source snapshot)
// Otherwise enabled.
function renderApplyButton(payload) {
  const snap = payload && payload.snapshot;
  const applied = payload && payload.applied;

  if (!snap) {
    els.applyBtn.disabled = true;
    els.applyBtn.textContent = "Apply today's universe";
    els.applyFeedback.textContent = "";
    return;
  }
  if (
    applied &&
    applied.source_snapshot_path &&
    payload.snapshot_path &&
    basename(applied.source_snapshot_path) === basename(payload.snapshot_path)
  ) {
    // Same source already applied → don't let an accidental double-click
    // create a redundant apply file. Re-enable when the snapshot
    // changes (e.g. the operator re-ran the screener with a different
    // pool and the snapshot file rotated).
    els.applyBtn.disabled = true;
    els.applyBtn.textContent = `Already applied at ${formatAppliedAt(applied.applied_at)}`;
    els.applyFeedback.textContent = "";
    return;
  }
  els.applyBtn.disabled = false;
  els.applyBtn.textContent = "Apply today's universe";
  els.applyFeedback.textContent = "";
}

function render(payload) {
  _last_state = payload;
  renderDataAsOf(payload);            // chart-strip meta line
  renderAppliedBadge(payload);        // small badge in chart-strip header
  renderApplyButton(payload);         // Apply button enable/text
  // Chart grid is async (parallel bar fetches); fire-and-forget so the
  // synchronous panel render is unblocked. Errors per-card are
  // surfaced inline; module-level errors never break the panel.
  renderChartGrid(payload).catch((err) => {
    console.warn("morning chart grid render failed:", err);
  });
}

// ── Apply handler ──────────────────────────────────────────────────────────

async function handleApply() {
  if (els.applyBtn.disabled) return;
  // Double-click guard — disable IMMEDIATELY (before await) so a fast
  // second click can't slip through during the in-flight POST.
  els.applyBtn.disabled = true;
  els.applyBtn.textContent = "Applying…";
  els.applyFeedback.textContent = "";

  try {
    const result = await postApply();
    // Success — refetch state to pick up the freshly-written apply
    // metadata + flip the button into "Already applied" mode.
    showToast({
      title:    "Morning universe applied",
      message:  `${result.n_base} base + ${result.n_dynamic} dynamic = ${result.trading_universe.length} symbols. Snapshot: ${basename(result.applied_snapshot_path)}`,
      variant:  "success",
      icon:     "✅",
    });
    await refresh();
  } catch (err) {
    // Bring the button back to actionable state; surface the
    // backend's actionable error text inline beside the button.
    els.applyBtn.disabled = false;
    els.applyBtn.textContent = "Apply today's universe";
    els.applyFeedback.textContent =
      `${err.code ? `[${err.code}]` : ""} ${err.message}`.trim();
    els.applyFeedback.dataset.level = "error";
    showToast({
      title:    "Apply rejected",
      message:  err.message,
      variant:  "error",
      icon:     "⚠️",
      duration: 8000,
    });
  }
}

// ── Build handler ──────────────────────────────────────────────────────────

async function handleBuild() {
  if (els.buildBtn.disabled) return;
  // Track a synthetic start time so the progress label can show elapsed
  // seconds (helps the operator know the request is alive — without it,
  // a 2-minute "Building…" feels like the page froze).
  const startTs = Date.now();
  let progressTick = null;

  els.buildBtn.disabled = true;
  els.buildBtn.textContent = "⟳ Building… 0s";
  els.applyFeedback.textContent = "";
  els.applyFeedback.dataset.level = "";

  // Tick the elapsed-seconds display every 2s. Cleared in the finally.
  progressTick = setInterval(() => {
    const elapsed = Math.floor((Date.now() - startTs) / 1000);
    els.buildBtn.textContent = `⟳ Building… ${elapsed}s`;
  }, 2000);

  try {
    const result = await postBuild();
    showToast({
      title:   "Morning universe built",
      message: `${result.n_base} base + ${result.n_dynamic} dynamic + ${result.n_watchlist} watchlist · feed=${result.feed}`,
      variant: "success",
      icon:    "✅",
    });
    // Refetch state — the panel re-renders with the new snapshot, the
    // Apply button enables, and the chart grid hydrates.
    await refresh();
  } catch (err) {
    els.applyFeedback.textContent = err.message;
    els.applyFeedback.dataset.level = "error";
    showToast({
      title:    "Build failed",
      message:  err.message,
      variant:  "error",
      icon:     "⚠️",
      duration: 8000,
    });
  } finally {
    if (progressTick) clearInterval(progressTick);
    els.buildBtn.disabled = false;
    els.buildBtn.textContent = "⟳ Build today's universe";
  }
}

// ── Chart strip (v7.13 Phase 2.2 — pulse-style horizontal strip) ───────────

// Replace the strip contents with a centered call-to-action so the
// section reads as "what to do next" instead of "loading…". HTML
// is allowed in the message because we author it; the source is
// always module-internal, never user input.
function showStripPlaceholder(htmlMessage) {
  els.chartsGrid.innerHTML = "";
  els.chartsMeta.textContent = "";
  const ph = document.createElement("div");
  ph.className = "morning-charts-empty muted-copy";
  ph.id = "morning-charts-empty";
  ph.innerHTML = htmlMessage;
  els.chartsGrid.appendChild(ph);
}


// The strip always renders; what fills it depends on apply state.
// Three states:
//   * No applied universe              → "no snapshot" empty placeholder
//   * Applied with no dynamic symbols  → "base-only run" placeholder
//   * Applied with N dynamic symbols   → N chart cards, scrollable
// This way the operator can always see the section + the Build /
// Refresh buttons in its header — they're the primary morning ritual,
// not a secondary detail that hides until something is set.
async function renderChartGrid(payload) {
  const snap = payload && payload.snapshot;

  // v7.14.1 (2026-05-04) — gate ONLY on the snapshot, not on apply state.
  // Charts render whenever the morning scanner has produced ranked
  // candidates; the applied-state banner above the strip is the source
  // of truth for whether they're actively trading. This lets the
  // operator preview chart structure BEFORE deciding to apply, which
  // matches the manual-apply discipline the runbook depends on.
  //
  // State 1: no snapshot at all today.
  if (!snap) {
    showStripPlaceholder(
      "No snapshot today. Click <strong>⟳ Build</strong> above to run the morning scanner.",
    );
    _rendered_chart_symbols = [];
    return;
  }

  // v7.16.9 (2026-05-12): sort dynamic candidates alphabetically by symbol
  // for consistent visual ordering across refreshes. The backend returns
  // them in score-rank order (highest score first); each card still shows
  // its score, so the rank info is preserved on the card itself.
  const dynamicEntries = (snap.morning_watchlist || []).filter(
    (c) => c.in_trading_universe,
  ).sort((a, b) => (a.symbol || "").localeCompare(b.symbol || ""));

  // State 2: snapshot exists but no dynamic candidates passed the gates.
  if (!dynamicEntries.length) {
    showStripPlaceholder(
      "No dynamic candidates passed today's gates. The audited 23-symbol base will trade alone; this strip will populate when a future build surfaces dynamic names.",
    );
    _rendered_chart_symbols = [];
    return;
  }

  const symbols = dynamicEntries.map((c) => c.symbol);
  const symbolsKey = symbols.join("|");

  // Avoid full re-render if the symbol set hasn't changed — preserves
  // chart instances + saves a Promise.all of bar fetches every poll.
  if (_rendered_chart_symbols.join("|") === symbolsKey) {
    return;
  }

  // State 3: render N cards. Tear down placeholder + previous strip.
  els.chartsGrid.innerHTML = "";
  els.chartsMeta.textContent =
    `${dynamicEntries.length} symbols · refreshes every ${CHART_REFRESH_MS / 1000}s`;
  _rendered_chart_symbols = symbols.slice();

  // Render placeholders first so the layout settles before bars arrive.
  const cards = new Map();
  for (const c of dynamicEntries) {
    const card = renderChartCard(c);
    els.chartsGrid.appendChild(card);
    cards.set(c.symbol, card);
  }

  // Parallel-fetch bars for every applied dynamic symbol. Each card
  // updates as its own Promise resolves — slow symbols don't block
  // fast ones.
  await Promise.all(
    symbols.map(async (sym) => {
      try {
        const bars = await fetchChartBars(sym);
        const card = cards.get(sym);
        if (!card) return;
        hydrateChartCard(card, sym, bars, dynamicEntries.find((c) => c.symbol === sym));
      } catch (err) {
        const card = cards.get(sym);
        if (!card) return;
        const status = card.querySelector(".morning-chart-status");
        if (status) {
          status.textContent = `error: ${err.message}`;
          status.dataset.level = "error";
        }
      }
    }),
  );
}

function renderChartCard(candidate) {
  const card = document.createElement("div");
  card.className = "morning-chart-card";
  card.dataset.symbol = candidate.symbol;
  card.title = "Click to open full chart with ICT overlays";

  // Header row: symbol + score + reason chips
  const header = document.createElement("div");
  header.className = "morning-chart-card-header";

  const symEl = document.createElement("span");
  symEl.className = "morning-chart-card-symbol";
  symEl.textContent = candidate.symbol;
  header.appendChild(symEl);

  const scoreEl = document.createElement("span");
  scoreEl.className = "morning-chart-card-score muted-copy";
  scoreEl.textContent = (candidate.score != null) ? candidate.score.toFixed(2) : "—";
  header.appendChild(scoreEl);

  card.appendChild(header);

  // Reason tags row (compressed — 2 tags max in the card)
  if ((candidate.reason_tags || []).length > 0) {
    const tagsEl = document.createElement("div");
    tagsEl.className = "morning-chart-card-tags muted-copy";
    tagsEl.textContent = candidate.reason_tags.slice(0, 2).join(" · ");
    card.appendChild(tagsEl);
  }

  // Status / loading line. Replaced once bars arrive.
  const statusEl = document.createElement("div");
  statusEl.className = "morning-chart-status muted-copy";
  statusEl.textContent = "loading bars…";
  card.appendChild(statusEl);

  // Chart container — sized via CSS, lightweight-charts auto-resizes
  // to its parent.
  const chartContainer = document.createElement("div");
  chartContainer.className = "morning-chart-canvas";
  card.appendChild(chartContainer);

  // Click-through to the existing full-size chart-modal. The dashboard
  // already wires the modal off ``data-symbol`` clicks (see main.js);
  // we hook into that pattern by emitting a custom event.
  card.addEventListener("click", () => {
    const evt = new CustomEvent("open-chart", {
      detail: { symbol: candidate.symbol },
      bubbles: true,
    });
    card.dispatchEvent(evt);
  });

  return card;
}

function hydrateChartCard(card, symbol, bars, candidate) {
  const container = card.querySelector(".morning-chart-canvas");
  const status = card.querySelector(".morning-chart-status");
  if (!container || !status) return;

  // Compute today's % change for the badge — close vs first-bar open.
  let pct = null;
  if (bars.length >= 2) {
    const first = bars[0];
    const last = bars[bars.length - 1];
    if (first.open > 0) {
      pct = ((last.close - first.open) / first.open) * 100;
    }
  }

  // Update status line: "+1.42% · 312 bars"
  status.textContent = (pct != null)
    ? `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% · ${bars.length} bars`
    : `${bars.length} bars`;
  if (pct != null) {
    status.dataset.level = pct >= 0 ? "positive" : "negative";
  }

  // Render the chart. Use the smaller mini-chart variant (the
  // existing createEnhancedChart auto-sizes to its parent).
  container.innerHTML = "";
  const chart = createEnhancedChart(container, symbol, container.clientWidth, 140);
  addCandleSeries(chart, bars);

  // Tooltip carries the full reason list + numeric details so the
  // card stays compact.
  if (candidate) {
    const ttLines = [
      `${candidate.symbol} · rank ${candidate.rank ?? "—"} · score ${candidate.score ?? "—"}`,
      candidate.gap_pct != null      ? `Gap: ${candidate.gap_pct >= 0 ? "+" : ""}${candidate.gap_pct}%` : null,
      candidate.has_news_catalyst && (candidate.catalyst_keywords || []).length
        ? `Catalysts: ${candidate.catalyst_keywords.join(", ")}` : null,
      "Click to open full chart with ICT overlays",
    ].filter(Boolean);
    card.title = ttLines.join("\n");
  }
}

async function refreshChartGridBars() {
  // Lightweight refresh: keep the same cards, just re-fetch bars
  // and re-hydrate. Skipped when the section is hidden (no applied
  // dynamic symbols).
  // v7.16.10 (2026-05-12): fixed a stale-rename bug — was reading
  // `els.chartsSection` (which doesn't exist; the field was renamed
  // to `panel` long ago). The undefined.hidden lookup threw a
  // TypeError that the setInterval `.catch(() => {})` swallowed
  // silently, so the every-60s chart-bar refresh has been a no-op
  // since the rename. Symptom: charts only updated on manual click.
  if (!els.panel || els.panel.hidden) return;
  if (!_last_state || !_last_state.snapshot) return;

  const dynamicEntries = (_last_state.snapshot.morning_watchlist || []).filter(
    (c) => c.in_trading_universe,
  );
  await Promise.all(
    dynamicEntries.map(async (c) => {
      const card = els.chartsGrid.querySelector(
        `.morning-chart-card[data-symbol="${c.symbol}"]`,
      );
      if (!card) return;
      try {
        const bars = await fetchChartBars(c.symbol);
        hydrateChartCard(card, c.symbol, bars, c);
      } catch {
        /* keep the existing chart in place on a transient fetch fail */
      }
    }),
  );
}

// ── Detail-modal stub ──────────────────────────────────────────────────────

// Cheap "details" view — alert with full applied metadata. The
// dashboard already has a mode-modal pattern; if this view becomes
// hot, promote to a proper modal. Keeping it minimal for v1.
function showAppliedDetails() {
  const a = _last_state && _last_state.applied;
  if (!a) return;
  const lines = [
    `Applied at:           ${a.applied_at}`,
    `Apply file:           ${basename(a.applied_snapshot_path)}`,
    `Source snapshot:      ${basename(a.source_snapshot_path) || "—"}`,
    `Restored from disk:   ${a.restored_from_disk ? "yes" : "no"}`,
    `Trading universe size: ${a.trading_universe_size}`,
    `n_base / n_dynamic:   ${a.n_base} / ${a.n_dynamic}`,
  ];
  // Use the toast for visibility — replace with a proper modal if
  // the operator asks for a copy-paste flow.
  showToast({
    title:    "Applied morning universe",
    message:  lines.join("\n"),
    variant:  "info",
    icon:     "📋",
    duration: 12000,
  });
}

// ── Lifecycle ──────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const payload = await fetchMorningUniverse();
    render(payload);
  } catch (err) {
    // Auth-required (401) is expected when the page loads before the
    // user logs in — silent ignore. Other errors surface in the
    // "Data as of" line so the operator sees something is off.
    if (err && /401/.test(err.message)) return;
    // v7.15.1: error surfaces on the chart-strip meta line (the
    // dataAsOf element was removed; meta is its replacement).
    if (els.chartsMeta) {
      els.chartsMeta.textContent = `Error: ${err.message}`;
      els.chartsMeta.dataset.level = "warn";
    }
  }
}

function init() {
  if (!els.panel) return;   // panel not on this page
  els.buildBtn.addEventListener("click", handleBuild);
  els.applyBtn.addEventListener("click", handleApply);
  // Top-of-page Refresh button (v7.13 Phase 2.2) — re-fetches the
  // snapshot AND re-pulls bars for every chart card. The two are
  // bundled because the operator's mental model is "refresh
  // everything I see at the top," not "refresh just one thing."
  els.chartsRefreshBtn.addEventListener("click", async () => {
    els.chartsRefreshBtn.disabled = true;
    try {
      await refresh();
      await refreshChartGridBars();
    } finally {
      els.chartsRefreshBtn.disabled = false;
    }
  });
  // v7.15.1: appliedDetailBtn was removed — its tooltip moved into
  // the new appliedBadge's `title` attribute. Hover the badge to see
  // applied-snapshot details. The showAppliedDetails toast helper
  // is still defined but no longer wired to a click; kept for
  // optional future re-enable.

  // Click-through from the chart grid → existing full-size chart modal.
  // The dashboard's main.js already listens for `data-symbol` clicks
  // on chart triggers; we emit ``open-chart`` events that bubble up
  // and either main.js handles them OR we fall back to a global
  // helper hook (set by main.js as ``window.openChartModal``).
  document.addEventListener("open-chart", (evt) => {
    const sym = evt.detail && evt.detail.symbol;
    if (!sym) return;
    if (typeof window.openChartModal === "function") {
      window.openChartModal(sym);
    } else {
      // Fallback: synthesize the click on the symbol the modal
      // already binds to in main.js. Keeps this module decoupled.
      const trigger = document.querySelector(`[data-chart-symbol="${sym}"]`);
      if (trigger) trigger.click();
    }
  });

  // Initial fetch + slow background poll (handles snapshot age tick
  // updates AND picks up a new screener run if the operator builds a
  // fresh snapshot in another shell).
  refresh();
  if (_poll_handle == null) {
    _poll_handle = setInterval(refresh, POLL_INTERVAL_MS);
  }
  // Mini-chart refresh — re-fetch bars (not the snapshot) every minute.
  // v7.16.11 (2026-05-12): wall-clock-aligned — fires at 12:00:00,
  // 12:01:00, etc., so each fetch happens right after a 1-min Alpaca bar
  // closes, not somewhere mid-bar. Page-load drift no longer affects
  // freshness. The handle stays the same shape throughout its lifetime
  // (an object with .cancel()) so any future caller can stop the loop
  // uniformly without caring whether the first aligned tick has fired.
  if (_chart_refresh_handle == null) {
    const tick = () => {
      refreshChartGridBars().catch(() => { /* silent — already logged per-card */ });
    };
    const msUntilNext = 60_000 - (Date.now() % 60_000);
    let timeoutId = null;
    let intervalId = null;
    timeoutId = setTimeout(() => {
      timeoutId = null;
      tick();
      intervalId = setInterval(tick, 60_000);
    }, msUntilNext);
    _chart_refresh_handle = {
      cancel: () => {
        if (timeoutId != null) { clearTimeout(timeoutId); timeoutId = null; }
        if (intervalId != null) { clearInterval(intervalId); intervalId = null; }
      },
    };
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
