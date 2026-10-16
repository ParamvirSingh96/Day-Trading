#!/bin/bash
# morning_build_cron.sh — v7.15a (2026-05-04)
#
# Launchd-driven daily morning-universe build. Runs at 08:30 ET on
# market days. Builds the snapshot + sends a macOS notification.
# Does NOT auto-apply and does NOT start autotrade — those remain
# manual operator actions per the runbook's "manual-only for first
# 30 days" rule.
#
# Why this exists: missing the 09:30–10:00 ET Judas window is an
# operational miss, not a strategy miss. With the build done by
# 08:30, the operator only has to inspect + click Apply. No
# strategy logic touched.
#
# Output:
#   - Backend/data/morning_universe_<YYYY-MM-DD>.json (the snapshot)
#   - Backend/data/research/morning_build_<YYYY-MM-DD>.log (run log)
#   - macOS notification (success or failure)
#
# Holiday handling: the script checks `cal` heuristically against a
# light set of obvious closures (Jan 1, Jul 4, Dec 25, etc.). On
# half-days or surprise closures the build still runs and produces
# whatever Alpaca returns; manual review on the dashboard catches
# anomalies. We err on the side of building too often, never not
# enough — a wasted build is harmless.

set -euo pipefail

# ── Paths (launchd doesn't inherit shell PATH/cwd, so resolve from this
#    script's own location: scripts/launchd/ → Backend/) ─────────────────
DEV_BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_PYTHON="${DEV_BACKEND}/venv/bin/python"
LOG_DIR="${DEV_BACKEND}/data/research"

mkdir -p "${LOG_DIR}"

# ── Skip on weekends (defensive — launchd already filters Mon-Fri) ──
DOW=$(date +%u)   # 1=Mon, 7=Sun
if [ "$DOW" -gt 5 ]; then
    echo "[$(date)] Skipping weekend build (DOW=${DOW})" >> "${LOG_DIR}/morning_build_$(date +%Y-%m-%d).log"
    exit 0
fi

# ── Skip obvious US holidays (heuristic; Alpaca returns no bars anyway) ──
# Format: MM-DD. Conservative list — covers fixed-date closures only.
# Floating ones (MLK Day, Presidents' Day, Good Friday, Memorial Day,
# Independence Day observed, Labor Day, Thanksgiving, Christmas Eve
# half-day) require a calendar lookup we're not doing here. The build
# on those days is harmless (Alpaca returns 0 bars, screener fails
# fast or produces a tiny universe).
TODAY_MMDD=$(date +%m-%d)
case "$TODAY_MMDD" in
    01-01|07-04|12-25)
        echo "[$(date)] Skipping known holiday (${TODAY_MMDD})" >> "${LOG_DIR}/morning_build_$(date +%Y-%m-%d).log"
        # Send a "skipped — holiday" notification so the operator knows
        # the cron didn't break, just that the market is closed.
        osascript -e 'display notification "Holiday — no build today" with title "Morning Universe"' || true
        exit 0
        ;;
esac

LOG_FILE="${LOG_DIR}/morning_build_$(date +%Y-%m-%d).log"

# ── Run the builder ──
cd "${DEV_BACKEND}"
{
    echo "================================================================"
    echo "Morning build starting at $(date)"
    echo "ET wall clock: $(TZ='America/New_York' date)"
    echo "================================================================"
} >> "${LOG_FILE}"

if env PYTHONPATH=. "${VENV_PYTHON}" -m scripts.build_morning_universe \
    >> "${LOG_FILE}" 2>&1; then
    {
        echo ""
        echo "[$(date)] Build SUCCEEDED"
    } >> "${LOG_FILE}"

    # Notification: tell the operator + give them what they need to act
    SUMMARY=$(env PYTHONPATH=. "${VENV_PYTHON}" -c "
import json
from datetime import date
p = 'data/morning_universe_' + date.today().isoformat() + '.json'
try:
    d = json.load(open(p))
    applied = [c for c in d['morning_watchlist'] if c['in_trading_universe']]
    if applied:
        top = ', '.join(c['symbol'] for c in applied[:5])
        print(f'{len(applied)} dynamic: {top}' + (', …' if len(applied) > 5 else ''))
    else:
        print('No dynamic candidates met the gates today (base only)')
except Exception:
    print('snapshot ready')
" 2>/dev/null || echo "snapshot ready")

    osascript -e "display notification \"${SUMMARY}\" with title \"Morning Universe — ready to Apply\" sound name \"Glass\"" || true
else
    {
        echo ""
        echo "[$(date)] Build FAILED — check log"
    } >> "${LOG_FILE}"

    # Loud notification on failure — operator needs to know before
    # the open so they can fall back to base-only or fix the issue.
    osascript -e 'display notification "Build failed — check Backend/data/research/morning_build_*.log" with title "⚠️ Morning Universe FAILED" sound name "Sosumi"' || true
    exit 1
fi
