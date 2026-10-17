// ── Lessons panel ──────────────────────────────────────────────────────────
// Shows the "Last losses" list and the "Lessons by stock" grouped view.
// main.js drives it via:
//   initLessons()            — wire up the refresh button + review-click handler
//   loadLessons()            — re-fetch lessons from the backend and re-render
//   setRecentTrades(trades)  — main.js feeds the latest closed trades
//                              (populated by renderPerfHistory) so the losses
//                              list can show them alongside their AI lesson.

import { API_BASE_URL } from "./config.js";
import { esc, fmtUSD, normalizeSetup } from "./utils.js";

const lessonsRefreshBtn = document.getElementById("lessons-refresh-btn");
const lossesList = document.getElementById("losses-list");
const lossesCount = document.getElementById("losses-count");
const lessonsByStock = document.getElementById("lessons-by-stock");
const stocksCount = document.getElementById("stocks-count");

// Most recent batch of trades (populated by renderPerfHistory) and lessons.
let _recentTrades = [];
let _recentLessons = [];
// trade_id → lesson object, for pairing loss rows with their lesson.
let _lessonByTradeId = {};

export function setRecentTrades(trades) {
  _recentTrades = trades || [];
}

export async function loadLessons() {
  try {
    const res = await fetch(`${API_BASE_URL}/trading/lessons?limit=100`, { credentials: "include" });
    if (!res.ok) return;
    const data = await res.json();
    _recentLessons = data.lessons || [];
    _lessonByTradeId = {};
    for (const l of _recentLessons) {
      if (l.trade_id != null) _lessonByTradeId[l.trade_id] = l;
    }
    renderLastLosses();
    renderLessonsByStock();
  } catch (err) {
    console.error("Lessons load failed:", err);
  }
}

function renderLastLosses() {
  if (!lossesList) return;
  const losses = (_recentTrades || [])
    .filter((t) => t.pnl != null && t.pnl < 0 && t.closed_at)
    .slice(0, 20);
  if (lossesCount) lossesCount.textContent = losses.length ? `${losses.length} shown` : "";

  if (!losses.length) {
    lossesList.innerHTML = `<p class="muted-copy">No closed losing trades in this window.</p>`;
    return;
  }

  lossesList.innerHTML = losses.map((t) => {
    const when = t.closed_at
      ? new Date(t.closed_at).toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
        " " + new Date(t.closed_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
      : "";
    const dirCls = t.side === "buy" ? "buy" : "sell";
    const dir = t.direction || (t.side === "buy" ? "long" : "short");
    const pnlStr = t.pnl != null ? fmtUSD(t.pnl) : "";
    const rStr = t.r_multiple != null ? `${t.r_multiple > 0 ? "+" : ""}${t.r_multiple.toFixed(1)}R` : "";
    const lesson = _lessonByTradeId[t.id];
    const hasLesson = !!lesson;
    const reviewLabel = hasLesson ? "View" : "Review";
    const reviewTitle = hasLesson ? "View the AI lesson for this trade" : "Have the AI review this trade and add a lesson";
    return `
      <div class="loss-row collapsible-panel" data-trade-id="${esc(t.id)}" data-open="false">
        <div class="loss-row-header collapsible-toggle">
          <span class="collapsible-chevron">▾</span>
          <span class="loss-time">${when}</span>
          <span class="autotrade-log-side ${dirCls}">${esc(dir)}</span>
          <span class="loss-symbol">${esc(t.symbol)}</span>
          <span class="loss-setup">${t.setup ? esc(t.setup) : "—"}</span>
          <span class="loss-pnl negative">${pnlStr}</span>
          <span class="loss-r negative">${rStr}</span>
          <button class="perf-trade-review${hasLesson ? " reviewed" : ""}" data-trade-id="${esc(t.id)}" title="${reviewTitle}">${reviewLabel}</button>
        </div>
        <div class="collapsible-content">
          <div class="loss-row-body" id="loss-body-${esc(t.id)}">
            ${hasLesson ? renderLessonBody(lesson) : `<p class="muted-copy">No lesson generated yet — click <strong>Review</strong> to have the AI analyze this trade.</p>`}
          </div>
        </div>
      </div>`;
  }).join("");
}

function renderLessonBody(lesson) {
  const when = lesson.created_at
    ? new Date(lesson.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
      " " + new Date(lesson.created_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
    : "";
  const tagChips = (lesson.tags || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => `<span class="lesson-tag">${esc(t)}</span>`)
    .join("");
  return `
    <div class="lesson-body-wrap">
      <div class="lesson-body-meta muted-copy">Reviewed ${when}</div>
      <div class="lesson-card-body">${esc(lesson.lesson || "")}</div>
      ${tagChips ? `<div class="lesson-card-tags">${tagChips}</div>` : ""}
    </div>`;
}

function renderLessonsByStock() {
  if (!lessonsByStock) return;
  if (!_recentLessons.length) {
    lessonsByStock.innerHTML = `<p class="muted-copy">No lessons yet. When a trade closes at a loss, click <strong>Review</strong> above and the AI will write a lesson so it can avoid the same mistake next time.</p>`;
    if (stocksCount) stocksCount.textContent = "";
    return;
  }

  // Group by symbol
  const bySymbol = {};
  for (const l of _recentLessons) {
    (bySymbol[l.symbol] = bySymbol[l.symbol] || []).push(l);
  }

  // Sort stocks by lesson count DESC, then alpha
  const symbols = Object.keys(bySymbol).sort((a, b) => {
    const diff = bySymbol[b].length - bySymbol[a].length;
    return diff !== 0 ? diff : a.localeCompare(b);
  });
  if (stocksCount) stocksCount.textContent = `${symbols.length} stock${symbols.length !== 1 ? "s" : ""}`;

  lessonsByStock.innerHTML = symbols.map((sym) => {
    const lessons = bySymbol[sym];

    // Count occurrences per normalized setup
    const setupCounts = {};
    for (const l of lessons) {
      const key = normalizeSetup(l.setup);
      (setupCounts[key] = setupCounts[key] || { count: 0, label: l.setup || "—", lessons: [] });
      setupCounts[key].count += 1;
      setupCounts[key].lessons.push(l);
    }

    // Sort by count DESC (common first), then by most recent lesson
    const setupGroups = Object.values(setupCounts).sort((a, b) => {
      if (b.count !== a.count) return b.count - a.count;
      const aMax = Math.max(...a.lessons.map((x) => new Date(x.created_at || 0).getTime()));
      const bMax = Math.max(...b.lessons.map((x) => new Date(x.created_at || 0).getTime()));
      return bMax - aMax;
    });

    // Date range
    const dates = lessons.map((l) => l.created_at).filter(Boolean).map((d) => new Date(d));
    const oldest = dates.length ? new Date(Math.min(...dates)) : null;
    const newest = dates.length ? new Date(Math.max(...dates)) : null;
    const fmt = (d) => d ? d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
    const dateRange = oldest && newest && oldest.getTime() !== newest.getTime()
      ? `${fmt(oldest)} → ${fmt(newest)}`
      : (newest ? fmt(newest) : "");

    const groupsHtml = setupGroups.map((g) => {
      const isCommon = g.count >= 2;
      const recentLesson = g.lessons.slice().sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))[0];
      const countBadge = isCommon
        ? `<span class="lesson-group-badge hot" title="Seen ${g.count}x">⚡ ${g.count}×</span>`
        : "";
      const lessonsInside = g.lessons
        .slice()
        .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
        .map((l) => {
          const w = l.created_at
            ? new Date(l.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
              " " + new Date(l.created_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
            : "";
          const pnlStr = l.pnl != null ? fmtUSD(l.pnl) : "";
          const tagChips = (l.tags || "")
            .split(",")
            .map((t) => t.trim())
            .filter(Boolean)
            .map((t) => `<span class="lesson-tag">${esc(t)}</span>`)
            .join("");
          return `
            <div class="stock-lesson-card">
              <div class="stock-lesson-card-meta muted-copy">
                <span>${w}</span>
                <span class="lesson-pnl negative">${pnlStr}</span>
              </div>
              <div class="lesson-card-body">${esc(l.lesson || "")}</div>
              ${tagChips ? `<div class="lesson-card-tags">${tagChips}</div>` : ""}
            </div>`;
        }).join("");

      return `
        <div class="lesson-group collapsible-panel${isCommon ? " common" : ""}" data-open="false">
          <div class="lesson-group-header collapsible-toggle">
            <span class="collapsible-chevron">▾</span>
            <span class="lesson-group-setup">${esc(g.label)}</span>
            ${countBadge}
            <span class="muted-copy lesson-group-latest">${recentLesson && recentLesson.created_at ? new Date(recentLesson.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : ""}</span>
          </div>
          <div class="collapsible-content">
            <div class="lesson-group-body">${lessonsInside}</div>
          </div>
        </div>`;
    }).join("");

    return `
      <div class="stock-group collapsible-panel" data-open="false">
        <div class="stock-group-header collapsible-toggle">
          <span class="collapsible-chevron">▾</span>
          <span class="stock-group-symbol">${esc(sym)}</span>
          <span class="stock-group-count">${lessons.length} lesson${lessons.length !== 1 ? "s" : ""}</span>
          <span class="muted-copy stock-group-range">${dateRange}</span>
        </div>
        <div class="collapsible-content">
          <div class="stock-group-body">${groupsHtml}</div>
        </div>
      </div>`;
  }).join("");
}

export function initLessons() {
  // "Review" button handler — works for both the trade history and losses list
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".perf-trade-review");
    if (!btn) return;
    e.stopPropagation();  // don't trigger the collapsible toggle
    const tradeId = btn.dataset.tradeId;
    if (!tradeId) return;

    // If we already have the lesson cached, just open the row
    if (_lessonByTradeId[tradeId]) {
      const row = btn.closest(".loss-row");
      if (row) {
        row.setAttribute("data-open", "true");
      }
      return;
    }

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Reviewing…";
    try {
      const res = await fetch(`${API_BASE_URL}/trading/lessons/generate/${tradeId}`, {
        method: "POST",
        credentials: "include",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        btn.textContent = "Failed";
        console.error("Lesson generation failed:", data.detail || res.status);
        setTimeout(() => { btn.textContent = originalText; btn.disabled = false; }, 2500);
        return;
      }
      btn.textContent = "View";
      btn.classList.add("reviewed");
      btn.disabled = false;
      await loadLessons();
      // Re-open the row now that the lesson body is filled in
      const freshRow = document.querySelector(`.loss-row[data-trade-id="${tradeId}"]`);
      if (freshRow) freshRow.setAttribute("data-open", "true");
    } catch (err) {
      btn.textContent = "Error";
      console.error(err);
      setTimeout(() => { btn.textContent = originalText; btn.disabled = false; }, 2500);
    }
  });

  if (lessonsRefreshBtn) {
    lessonsRefreshBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      loadLessons();
    });
  }
}
