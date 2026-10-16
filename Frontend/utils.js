// ── Shared utility helpers ─────────────────────────────────────────────────
// Pure functions with no DOM or module state. Imported by main.js and the
// split-out feature modules (ws.js, notifications.js, lessons.js, study.js).

// HTML escape — defence-in-depth for server data interpolated into innerHTML.
// Escapes all five chars unsafe in HTML text nodes and attribute values.
export function esc(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function fmtUSD(n) {
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

// Collapse a free-form setup name into a canonical token bag so "Bull flag
// above VWAP" and "bull-flag vwap" compare equal when grouping lessons.
export function normalizeSetup(s) {
  if (!s) return "";
  return s.toLowerCase().replace(/[^a-z0-9\s]/g, " ").split(/\s+/)
    .filter((t) => t && !["a","an","the","of","in","on","at","to","for","and","or","with","above","below","after","before","from","near"].includes(t))
    .sort()
    .join(" ");
}

