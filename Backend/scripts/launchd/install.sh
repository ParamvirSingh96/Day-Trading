#!/bin/bash
# install.sh — v7.15a (2026-05-04)
#
# One-line installer for the launchd auto-build job.
#
# What this does:
#   1. chmod +x the wrapper script
#   2. Copy the plist to ~/Library/LaunchAgents/
#   3. Load it (launchctl load -w)
#   4. Verify it's registered
#
# Usage:
#   cd Backend/scripts/launchd
#   ./install.sh
#
# Uninstall:
#   ./install.sh --uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.tradehack.morning-build.plist"
PLIST_SRC="${SCRIPT_DIR}/${PLIST_NAME}"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
WRAPPER="${SCRIPT_DIR}/morning_build_cron.sh"

if [ "${1:-}" = "--uninstall" ]; then
    if [ -f "${PLIST_DEST}" ]; then
        echo "Unloading job…"
        # Use modern bootout; fall back to deprecated unload -w if
        # bootout fails (e.g. job loaded under the old API).
        launchctl bootout gui/$UID "${PLIST_DEST}" 2>/dev/null || \
            launchctl unload -w "${PLIST_DEST}" 2>/dev/null || true
        rm -f "${PLIST_DEST}"
        echo "✅ Uninstalled. The wrapper script and source plist remain"
        echo "   in ${SCRIPT_DIR} — re-run install.sh to re-enable."
    else
        echo "Job is not installed (${PLIST_DEST} not found)."
    fi
    exit 0
fi

echo "── Installing morning-universe auto-build (08:30 ET, Mon–Fri) ──"
echo ""

# 1. Make the wrapper executable
chmod +x "${WRAPPER}"
echo "✓ chmod +x ${WRAPPER##*/}"

# 2. Render the plist template (launchd needs absolute paths) into
#    LaunchAgents — the source file in the repo keeps its placeholder.
BACKEND_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
mkdir -p "${HOME}/Library/LaunchAgents"
sed "s|__TRADEHACK_BACKEND__|${BACKEND_DIR}|g" "${PLIST_SRC}" > "${PLIST_DEST}"
echo "✓ Rendered plist to ${PLIST_DEST} (Backend = ${BACKEND_DIR})"

# 3. Reload via the MODERN launchctl API (bootstrap/bootout, not the
#    deprecated load -w). bootout is best-effort to clear any prior
#    registration; bootstrap is the actual install. Modern macOS
#    silently accepts `load -w` but doesn't always honor it the same
#    way as bootstrap, which is why earlier versions of this script
#    showed false "did not register" errors even when the job was
#    correctly installed.
launchctl bootout gui/$UID "${PLIST_DEST}" 2>/dev/null || true
launchctl bootstrap gui/$UID "${PLIST_DEST}" 2>/dev/null || launchctl load -w "${PLIST_DEST}"
echo "✓ Loaded job into launchd (bootstrap)"

# 4. Verify — small retry loop because launchctl list can lag the
#    actual registration by a few hundred ms on first install.
echo ""
echo "── Verification ──"
REGISTERED=""
for i in 1 2 3 4 5; do
    if launchctl list 2>/dev/null | grep -q "com.tradehack.morning-build"; then
        REGISTERED=1
        break
    fi
    sleep 0.5
done

if [ -n "${REGISTERED}" ]; then
    echo "✅ Job registered:"
    launchctl list | grep tradehack.morning-build | head -3
    echo ""
    echo "Next run: 08:30 ET on the next weekday."
    echo ""
    echo "Inspect the registered job (path + state + last exit code):"
    echo "  launchctl print gui/\$UID/com.tradehack.morning-build"
    echo ""
    echo "To run a smoke test now (does NOT wait until 08:30):"
    echo "  launchctl start com.tradehack.morning-build"
    echo "  tail -f \"${SCRIPT_DIR%/scripts/launchd}/data/research/morning_build_\$(date +%Y-%m-%d).log\""
    echo ""
    echo "To uninstall:"
    echo "  ./install.sh --uninstall"
else
    echo "⚠️  Job didn't appear in launchctl list within ~2.5s. This"
    echo "   sometimes happens on first install — the registration is"
    echo "   often valid anyway. Verify manually:"
    echo ""
    echo "    launchctl print gui/\$UID/com.tradehack.morning-build"
    echo ""
    echo "   If that prints job details, you're set. If not, check the"
    echo "   plist syntax:"
    echo ""
    echo "    plutil -lint ${PLIST_DEST}"
    exit 1
fi
