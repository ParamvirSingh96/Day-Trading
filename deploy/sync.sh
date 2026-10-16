#!/usr/bin/env bash
# sync.sh — push code from this Mac to the hosted VM/Pi.
#
# Usage:  ./deploy/sync.sh user@host
#   e.g.  ./deploy/sync.sh tradehack@100.x.y.z      (Tailscale IP)
#
# Pushes Backend/ + Frontend/ + start-lan.sh to /opt/tradehack on the host.
# Deliberately EXCLUDES:
#   • venv/            — rebuilt on the host (Mac wheels ≠ Linux wheels)
#   • .env             — sent once manually (scp), then never overwritten
#   • data/*.db        — the host accumulates its OWN trade DB; never clobber
#   • data/bars_cache* — regenerable, large
#   • caches / .DS_Store / __pycache__
#
# After syncing CODE you usually want to restart the services on the host:
#   ssh user@host 'sudo systemctl restart tradehack-backend tradehack-frontend'
set -euo pipefail

DEST="${1:-}"
if [ -z "$DEST" ]; then
  echo "usage: $0 user@host   (e.g. tradehack@100.x.y.z)"
  exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)/"
REMOTE_DIR="/opt/tradehack"

echo "Syncing code → ${DEST}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude 'venv/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.env' \
  --exclude '.env.example' \
  --exclude 'data/*.db' \
  --exclude 'data/*.db-shm' \
  --exclude 'data/*.db-wal' \
  --exclude 'data/bars_cache_ext_sip/' \
  --exclude 'data/bars_cache_ext_iex/' \
  --exclude 'data/bars_cache/' \
  --exclude '.DS_Store' \
  --exclude '.git/' \
  --exclude 'deploy/' \
  "$SRC" "${DEST}:${REMOTE_DIR}/"

echo "Done. If app code changed, restart services:"
echo "  ssh ${DEST} 'sudo systemctl restart tradehack-backend tradehack-frontend'"
