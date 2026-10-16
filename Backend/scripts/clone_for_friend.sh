#!/bin/bash
# clone_for_friend.sh — v7.14a (2026-05-03)
#
# Clones the Dev folder for a friend in the test cohort. Produces a
# self-contained "Day Trading FRIEND-<friend-id>/" sibling folder that:
#   - Inherits the audited scanner, frozen config, dashboard UI
#   - Has secrets stripped from .env (placeholders only)
#   - Is configured for the friend instance: EXPORT_TRADES=true,
#     FRIEND_INSTANCE_ID=<friend-id>, isolated DB filename
#   - Excludes venv + bar caches (friend rebuilds venv on their box)
#
# Usage from Backend/:
#   ./scripts/clone_for_friend.sh <friend-id>
#
# friend-id MUST match ^[a-z0-9_-]{1,32}$ (refinement #5 — sanitized).
# After cloning:
#   1. Friend signs up at https://app.alpaca.markets and generates
#      their OWN paper API keys (refinement #8 — never share yours).
#   2. Friend fills ALPACA_API_KEY + ALPACA_SECRET_KEY in their .env
#      (the clone leaves YOUR_VALUE_HERE placeholders).
#   3. Operator zips the folder + AirDrops/emails it to the friend.
#
# Exit codes:
#   0 — clone succeeded
#   1 — invalid friend-id (regex mismatch)
#   2 — destination folder already exists
#   3 — rsync or sed failed

set -euo pipefail

# ── Argument validation (refinement #5: friend-ID regex) ──────────────────
if [ $# -ne 1 ]; then
    echo "ERROR: usage: $0 <friend-id>"
    echo "  friend-id must match ^[a-z0-9_-]{1,32}$"
    echo "  example: $0 alex"
    exit 1
fi

FRIEND_ID="$1"
FRIEND_ID_REGEX='^[a-z0-9_-]{1,32}$'

if ! [[ "$FRIEND_ID" =~ $FRIEND_ID_REGEX ]]; then
    echo "ERROR: friend-id $(printf %q "$FRIEND_ID") does not match $FRIEND_ID_REGEX"
    echo "  Friend IDs end up in filenames AND DB rows. Allowed:"
    echo "    - lowercase letters a-z"
    echo "    - digits 0-9"
    echo "    - underscore _"
    echo "    - hyphen -"
    echo "  Length: 1-32 chars."
    exit 1
fi

# ── Resolve paths ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_BACKEND="$(cd "$SCRIPT_DIR/.." && pwd)"
DEV_ROOT="$(cd "$DEV_BACKEND/.." && pwd)"
PARENT_DIR="$(cd "$DEV_ROOT/.." && pwd)"

DEST_FOLDER_NAME="Day Trading FRIEND-${FRIEND_ID}"
DEST_PATH="${PARENT_DIR}/${DEST_FOLDER_NAME}"

if [ -e "$DEST_PATH" ]; then
    echo "ERROR: destination already exists: $DEST_PATH"
    echo "  Refusing to overwrite. Either:"
    echo "    - rm -rf $(printf %q "$DEST_PATH") and re-run"
    echo "    - choose a different friend-id"
    exit 2
fi

echo "── Clone source: $DEV_ROOT"
echo "── Clone dest:   $DEST_PATH"
echo "── Friend ID:    $FRIEND_ID"
echo ""

# ── rsync (Phase 0 exclude list) ──────────────────────────────────────────
# Excludes:
#   - venv/ (regenerable; venv encodes absolute paths so a copy from
#     this machine breaks on the friend's box anyway — they rebuild)
#   - __pycache__/ + *.pyc (compiled bytecode)
#   - bars_cache_ext_*/ (Alpaca historical bar caches; large,
#     regenerable on first backtest)
#   - data/trade_exports/ (operator's exports never propagate)
#   - data/applied_morning_universe_*.json (operator's apply audit
#     trail; friend starts fresh)
#   - .DS_Store (macOS metadata)
echo "[1/5] rsync …"
rsync -a \
    --exclude='venv/' \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='Backend/data/bars_cache_ext_*/' \
    --exclude='Backend/data/bars_cache/' \
    --exclude='Backend/data/trade_exports/' \
    --exclude='Backend/data/applied_morning_universe_*.json' \
    --exclude='Backend/data/morning_universe_*.json' \
    --exclude='Backend/data/iex_parity_*' \
    --exclude='.DS_Store' \
    "${DEV_ROOT}/" "${DEST_PATH}/"

# ── Strip secrets from .env (refinement #3 — friend never sees keys) ──
echo "[2/5] Redacting secrets in .env …"
DEST_ENV="${DEST_PATH}/Backend/.env"
if [ ! -f "$DEST_ENV" ]; then
    echo "ERROR: $DEST_ENV missing after rsync"
    exit 3
fi

# Save a copy of the original (the friend's redacted .env) as .env.example
# so they have a reference even after they fill in their own values
cp "$DEST_ENV" "${DEST_ENV}.example.tmp"

# Redact: any credential or identity line (*_KEY, *_SECRET*, *_PASSWORD,
# *_TOKEN, *_EMAIL, SMTP_USER/TO/FROM) gets its value replaced with
# YOUR_VALUE_HERE. Keep everything else (CORS, session config, universe
# list, etc.) as-is.
python3 - "$DEST_ENV" <<'PY'
import re, sys
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()
redacted = []
for line in lines:
    # Match lines like FOO_KEY=... or FOO_SECRET_KEY=... but not commented lines
    if re.match(r'^[A-Z_]+(_KEY|_SECRET[A-Z_]*|_PASSWORD|_TOKEN|_EMAIL|_USER|_TO|_FROM)=', line):
        name = line.split('=', 1)[0]
        redacted.append(f"{name}=YOUR_VALUE_HERE\n")
    else:
        redacted.append(line)
with open(path, 'w') as f:
    f.writelines(redacted)
PY

# Move the .example to its final name (friend keeps both as reference)
mv "${DEST_ENV}.example.tmp" "${DEST_ENV}.example"
# Re-redact the .example so even THAT file has no secrets
python3 - "${DEST_ENV}.example" <<'PY'
import re, sys
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()
redacted = []
for line in lines:
    if re.match(r'^[A-Z_]+(_KEY|_SECRET[A-Z_]*|_PASSWORD|_TOKEN|_EMAIL|_USER|_TO|_FROM)=', line):
        name = line.split('=', 1)[0]
        redacted.append(f"{name}=YOUR_VALUE_HERE\n")
    else:
        redacted.append(line)
with open(path, 'w') as f:
    f.writelines(redacted)
PY

# ── Set friend-specific .env values ────────────────────────────────────────
echo "[3/5] Configuring friend-specific .env values …"
python3 - "$DEST_ENV" "$FRIEND_ID" <<'PY'
import re, sys
path, friend_id = sys.argv[1], sys.argv[2]
db_url = f"sqlite:///./data/tradehack-friend-{friend_id}.db"
with open(path) as f:
    text = f.read()

def replace_or_append(text, key, value):
    """Replace `KEY=...` line or append at end if missing."""
    pattern = rf'^{re.escape(key)}=.*$'
    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, f'{key}={value}', text, flags=re.MULTILINE)
    return text.rstrip("\n") + f"\n{key}={value}\n"

# Force friend-specific values
text = replace_or_append(text, 'DATABASE_URL', db_url)
text = replace_or_append(text, 'EXPORT_TRADES', 'true')
text = replace_or_append(text, 'FRIEND_INSTANCE_ID', friend_id)

# Force paper-mode safety (defense in depth — already set, but enforce)
text = replace_or_append(text, 'ALPACA_PAPER', 'true')
text = replace_or_append(text, 'LIVE_TRADING_ENABLED', 'no')
text = replace_or_append(text, 'ALPACA_TRADING_URL', 'https://paper-api.alpaca.markets')

# Audit-frozen config carries over verbatim — friend reuses these
# values (refinement: frozen-config rules from PAPER_TRADING_RUNBOOK.md
# apply equally to friend instances)
text = replace_or_append(text, 'ENABLE_AI_GATE', 'false')
text = replace_or_append(text, 'ALPACA_DATA_FEED', 'sip')

with open(path, 'w') as f:
    f.write(text)
PY

# ── Drop in the friend's database file (empty, fresh) ──────────────────
# The rsync may have copied tradehack-dev.db (the operator's). Even
# though DATABASE_URL now points to a different filename, leaving the
# operator's DB in the friend's data/ folder is information leakage.
echo "[4/5] Cleaning operator-side state …"
DEST_DATA="${DEST_PATH}/Backend/data"
# Keep only source-of-truth runtime seed files. SQLite sidecars
# (tradehack*.db-wal / *.db-shm) and old research/backtest artifacts are
# operator-local state and should never ship to a friend clone.
find "$DEST_DATA" -maxdepth 1 -type f \
    ! -name '.gitkeep' \
    ! -name 'edge_lookup_v710.json' \
    -delete
find "$DEST_DATA" -maxdepth 1 -type d \
    ! -path "$DEST_DATA" \
    ! -name 'research' \
    -exec rm -rf {} +
rm -rf "$DEST_DATA/research" 2>/dev/null || true
mkdir -p "$DEST_DATA/research"  # recreate empty for the friend's audit script
touch "$DEST_DATA/.gitkeep" "$DEST_DATA/research/.gitkeep"

# ── Cleanup and summary ───────────────────────────────────────────────────
echo "[5/5] Done."
echo ""
echo "✅ Clone created at: $DEST_PATH"
echo ""
echo "Configured:"
echo "  EXPORT_TRADES=true"
echo "  FRIEND_INSTANCE_ID=${FRIEND_ID}"
echo "  DATABASE_URL=sqlite:///./data/tradehack-friend-${FRIEND_ID}.db"
echo "  ALPACA_PAPER=true, LIVE_TRADING_ENABLED=no (forced)"
echo "  ENABLE_AI_GATE=false, ALPACA_DATA_FEED=sip (audit-frozen carry-over)"
echo ""
echo "Redacted (friend must fill in their OWN values per refinement #8):"
grep -E '_KEY=|_SECRET=' "$DEST_ENV" | head -10 || true
echo ""
echo "Next steps (operator):"
echo "  1. (Friend creates their OWN Alpaca paper account at"
echo "      https://app.alpaca.markets/paper/dashboard/keys"
echo "      and gives you the keys, OR fills them in themselves.)"
echo "  2. Optionally edit ${DEST_ENV} to drop in friend's keys NOW,"
echo "     OR leave placeholders and let the friend fill them."
echo "  3. Zip + send:"
echo "       cd \"${PARENT_DIR}\""
echo "       zip -r \"${DEST_FOLDER_NAME}.zip\" \"${DEST_FOLDER_NAME}\" \\"
echo "           -x '*/venv/*' '*/__pycache__/*' '*.pyc' '*/.DS_Store'"
echo "  4. AirDrop/email \"${DEST_FOLDER_NAME}.zip\" to the friend."
echo "  5. Friend follows ${DEST_FOLDER_NAME}/FRIEND_README.md to set up."
