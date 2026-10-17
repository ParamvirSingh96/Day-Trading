// ── After-Hours AI Study panel ─────────────────────────────────────────────
// Shows per-stock study insights from the backend and owns the Run-Study
// button that streams NDJSON progress back from /trading/study/run/stream.
// main.js drives it via:
//   initStudy()             — wire up the Run Study button
//   loadStudyInsights()     — re-fetch insights from backend and re-render

import { API_BASE_URL } from "./config.js";
import { esc, normalizeSetup } from "./utils.js";

const studyRunBtn = document.getElementById("study-run-btn");
const studyStatus = document.getElementById("study-status");
const studyByStockEl = document.getElementById("study-by-stock");
const studyStocksCount = document.getElementById("study-stocks-count");

export async function loadStudyInsights() {
  if (!studyByStockEl) return;
  try {
    const res = await fetch(`${API_BASE_URL}/trading/study/insights?limit=100`, { credentials: "include" });
    if (!res.ok) return;
    const data = await res.json();
    renderStudyByStock(data.insights || []);
  } catch (err) {
    console.error("Study insights load failed:", err);
  }
}

function renderStudyByStock(insights) {
  if (!studyByStockEl) return;
  if (!insights.length) {
    studyByStockEl.innerHTML = `<p class="muted-copy">No study insights yet. Run a study session to generate per-stock insights.</p>`;
    if (studyStocksCount) studyStocksCount.textContent = "";
    return;
  }

  // Group by symbol
  const bySymbol = {};
  for (const i of insights) {
    (bySymbol[i.symbol] = bySymbol[i.symbol] || []).push(i);
  }

  // Sort stocks by total occurrence weight (repeats first), then count, then alpha
  const symbols = Object.keys(bySymbol).sort((a, b) => {
    const wA = bySymbol[a].reduce((s, x) => s + (x.occurrence_count || 1), 0);
    const wB = bySymbol[b].reduce((s, x) => s + (x.occurrence_count || 1), 0);
    if (wB !== wA) return wB - wA;
    const diff = bySymbol[b].length - bySymbol[a].length;
    return diff !== 0 ? diff : a.localeCompare(b);
  });
  if (studyStocksCount) studyStocksCount.textContent = `${symbols.length} stock${symbols.length !== 1 ? "s" : ""}`;

  studyByStockEl.innerHTML = symbols.map((sym) => {
    const items = bySymbol[sym];

    // Group by normalized setup name within the stock
    const setupCounts = {};
    for (const it of items) {
      const key = normalizeSetup(it.setup_name || "");
      setupCounts[key] = setupCounts[key] || { count: 0, label: it.setup_name || "—", items: [] };
      setupCounts[key].count += (it.occurrence_count || 1);
      setupCounts[key].items.push(it);
    }

    const setupGroups = Object.values(setupCounts).sort((a, b) => {
      if (b.count !== a.count) return b.count - a.count;
      const aMax = Math.max(...a.items.map((x) => new Date(x.created_at || x.study_date || 0).getTime()));
      const bMax = Math.max(...b.items.map((x) => new Date(x.created_at || x.study_date || 0).getTime()));
      return bMax - aMax;
    });

    const dates = items
      .map((it) => it.created_at || it.study_date)
      .filter(Boolean)
      .map((d) => new Date(d.length === 10 ? d + "T12:00:00" : d));
    const oldest = dates.length ? new Date(Math.min(...dates)) : null;
    const newest = dates.length ? new Date(Math.max(...dates)) : null;
    const fmt = (d) => d ? d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
    const dateRange = oldest && newest && oldest.getTime() !== newest.getTime()
      ? `${fmt(oldest)} → ${fmt(newest)}`
      : (newest ? fmt(newest) : "");

    const totalWeight = items.reduce((s, x) => s + (x.occurrence_count || 1), 0);

    const groupsHtml = setupGroups.map((g) => {
      const isCommon = g.count >= 2;
      const countBadge = isCommon
        ? `<span class="lesson-group-badge hot" title="Seen ${esc(g.count)}x">⚡ ${esc(g.count)}×</span>`
        : "";
      const latest = g.items.slice().sort((a, b) =>
        new Date(b.created_at || b.study_date || 0) - new Date(a.created_at || a.study_date || 0)
      )[0];
      const latestLabel = latest && (latest.created_at || latest.study_date)
        ? fmt(new Date((latest.created_at || (latest.study_date + "T12:00:00"))))
        : "";

      const cardsInside = g.items
        .slice()
        .sort((a, b) => new Date(b.created_at || b.study_date || 0) - new Date(a.created_at || a.study_date || 0))
        .map((i) => {
          const dirCls = i.direction === "long" ? "long" : "short";
          const confStr = i.confidence != null ? `${(i.confidence * 100).toFixed(0)}%` : "";
          const entryStr = i.entry_price != null ? `Entry $${i.entry_price.toFixed(2)}` : "";
          const pnlStr = i.potential_pnl_pct != null ? `+${i.potential_pnl_pct.toFixed(1)}%` : "";
          const stopStr = i.optimal_stop != null ? `Stop $${i.optimal_stop.toFixed(2)}` : "";
          const targetStr = i.optimal_target != null ? `Target $${i.optimal_target.toFixed(2)}` : "";
          const metaParts = [entryStr, stopStr, targetStr, pnlStr].filter(Boolean);
          const count = i.occurrence_count || 1;
          const repeatBadge = count > 1
            ? `<span class="study-insight-repeat${count >= 3 ? ' hot' : ''}" title="Seen ${esc(count)}x across study sessions">⚡ ${esc(count)}x</span>`
            : "";
          const when = i.study_date
            ? new Date(i.study_date + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })
            : (i.created_at ? fmt(new Date(i.created_at)) : "");
          const tagChips = (i.tags || "")
            .split(",")
            .map((t) => t.trim())
            .filter(Boolean)
            .map((t) => `<span class="study-tag">${esc(t)}</span>`)
            .join("");

          return `
            <div class="stock-lesson-card${count >= 3 ? ' recurring' : ''}">
              <div class="stock-lesson-card-meta muted-copy">
                <span>${when}</span>
                <span class="study-insight-dir ${dirCls}">${esc(i.direction)}</span>
                ${confStr ? `<span>${confStr}</span>` : ""}
                ${repeatBadge}
              </div>
              <div class="lesson-card-body">${esc(i.insight || "")}</div>
              ${metaParts.length ? `<div class="study-insight-meta">${metaParts.map(p => `<span>${esc(p)}</span>`).join("")}</div>` : ""}
              ${tagChips ? `<div class="lesson-card-tags">${tagChips}</div>` : ""}
            </div>`;
        }).join("");

      return `
        <div class="lesson-group collapsible-panel${isCommon ? " common" : ""}" data-open="false">
          <div class="lesson-group-header collapsible-toggle">
            <span class="collapsible-chevron">▾</span>
            <span class="lesson-group-setup">${esc(g.label)}</span>
            ${countBadge}
            <span class="muted-copy lesson-group-latest">${latestLabel}</span>
          </div>
          <div class="collapsible-content">
            <div class="lesson-group-body">${cardsInside}</div>
          </div>
        </div>`;
    }).join("");

    const countLabel = totalWeight !== items.length
      ? `${items.length} insight${items.length !== 1 ? "s" : ""} · ${totalWeight}×`
      : `${items.length} insight${items.length !== 1 ? "s" : ""}`;

    return `
      <div class="stock-group collapsible-panel" data-open="false">
        <div class="stock-group-header collapsible-toggle">
          <span class="collapsible-chevron">▾</span>
          <span class="stock-group-symbol">${esc(sym)}</span>
          <span class="stock-group-count">${esc(countLabel)}</span>
          <span class="muted-copy stock-group-range">${dateRange}</span>
        </div>
        <div class="collapsible-content">
          <div class="stock-group-body">${groupsHtml}</div>
        </div>
      </div>`;
  }).join("");
}

export function initStudy() {
  if (!studyRunBtn) return;
  studyRunBtn.addEventListener("click", async () => {
    studyRunBtn.disabled = true;
    studyRunBtn.textContent = "Studying…";
    if (studyStatus) studyStatus.textContent = "Starting…";

    let total = 0;
    let done = 0;
    let newCount = 0;
    let mergedCount = 0;
    let skippedCount = 0;

    const resetBtn = () => {
      studyRunBtn.textContent = "Run Study";
      studyRunBtn.disabled = false;
    };

    try {
      const res = await fetch(`${API_BASE_URL}/trading/study/run/stream`, {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}));
        if (studyStatus) studyStatus.textContent = `Failed: ${data.detail || res.status}`;
        resetBtn();
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      const handle = (evt) => {
        if (evt.event === "start") {
          total = evt.total || 0;
          studyRunBtn.textContent = `Studying 0/${total}…`;
          if (studyStatus) studyStatus.textContent = `Analyzing ${total} stocks…`;
        } else if (evt.event === "progress") {
          done = evt.index || (done + 1);
          if (evt.status === "skipped") skippedCount += 1;
          newCount += evt.new || 0;
          mergedCount += evt.merged || 0;
          studyRunBtn.textContent = `Studying ${done}/${total}…`;
          if (studyStatus) {
            const icon = evt.status === "done" ? "✓" : evt.status === "skipped" ? "↷" : "✗";
            studyStatus.textContent = `${icon} ${evt.symbol} (${done}/${total}) — ${newCount} new, ${mergedCount} merged`;
          }
        } else if (evt.event === "done") {
          studyRunBtn.textContent = "Done ✓";
          if (studyStatus) {
            const parts = [];
            if (evt.new_insights) parts.push(`${evt.new_insights} new`);
            if (evt.merged_insights) parts.push(`${evt.merged_insights} merged w/ prior`);
            if (skippedCount) parts.push(`${skippedCount} skipped`);
            const breakdown = parts.length ? ` (${parts.join(", ")})` : "";
            studyStatus.textContent = `Study complete — ${evt.insights_found || 0} insights${breakdown} from ${evt.symbols_studied || 0} symbols`;
          }
          loadStudyInsights();
          setTimeout(resetBtn, 1800);
        } else if (evt.event === "error") {
          if (studyStatus) studyStatus.textContent = `Study failed: ${evt.error || "unknown"}`;
          resetBtn();
        }
      };

      // Stream NDJSON — one JSON object per line
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try { handle(JSON.parse(line)); } catch (e) { console.warn("bad ndjson line:", line); }
        }
      }
      const trailing = buf.trim();
      if (trailing) {
        try { handle(JSON.parse(trailing)); } catch (e) { /* ignore */ }
      }

      // Safety net in case the "done" event never arrived
      if (studyRunBtn.disabled) resetBtn();
    } catch (err) {
      console.error("Study run failed:", err);
      if (studyStatus) studyStatus.textContent = "Study failed — check console";
      resetBtn();
    }
  });
}
