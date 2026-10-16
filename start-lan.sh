#!/usr/bin/env bash
# Start TradeHack on the local network so you can access it from your phone.
# Usage: ./start-lan.sh

set -e

# Detect LAN IP
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")

DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║              TradeHack — LAN Mode                ║"
echo "  ╠══════════════════════════════════════════════════╣"
echo "  ║                                                  ║"
echo "  ║  Open on your phone:                             ║"
printf "  ║  %-48s ║\n" "http://${LAN_IP}:3000"
echo "  ║                                                  ║"
echo "  ║  Backend API:                                    ║"
printf "  ║  %-48s ║\n" "http://${LAN_IP}:8000"
echo "  ║                                                  ║"
echo "  ║  Press Ctrl+C to stop both servers               ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""

# Trap Ctrl+C to kill whichever processes we started
cleanup() {
  echo ""
  echo "Shutting down..."
  [ -n "$BACKEND_PID" ] && kill $BACKEND_PID 2>/dev/null
  [ -n "$FRONTEND_PID" ] && kill $FRONTEND_PID 2>/dev/null
  wait 2>/dev/null
  echo "Done."
}
trap cleanup INT TERM

port_in_use() {
  lsof -i :"$1" -sTCP:LISTEN -P >/dev/null 2>&1
}

# Start backend (skip if port 8000 is already listening)
if port_in_use 8000; then
  echo "[skip] Backend already running on port 8000"
else
  cd "$DIR/Backend"
  ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 &
  BACKEND_PID=$!
fi

# Start frontend file server (skip if port 3000 is already listening)
if port_in_use 3000; then
  echo "[skip] Frontend already running on port 3000"
else
  cd "$DIR/Frontend"
  # serve.py = python http.server + no-cache headers, so JS/CSS edits
  # appear on a normal reload instead of needing Cmd+Shift+R. Falls
  # back to bare http.server if serve.py is somehow missing.
  if [ -f serve.py ]; then
    python3 serve.py 3000 &
  else
    python3 -m http.server 3000 --bind 0.0.0.0 &
  fi
  FRONTEND_PID=$!
fi

# Wait for whichever processes we started
if [ -n "$BACKEND_PID" ] || [ -n "$FRONTEND_PID" ]; then
  wait $BACKEND_PID $FRONTEND_PID
else
  echo "Both servers were already running — nothing to do."
fi
