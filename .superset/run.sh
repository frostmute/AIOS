#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$WORKSPACE_DIR"

cleanup() {
  echo "Stopping AIOS services..."
  if [ -n "${TAIL_PID:-}" ]; then
    kill "$TAIL_PID" 2>/dev/null || true
  fi
  ./aios stop || true
}
trap cleanup EXIT INT TERM

echo "Starting AIOS services..."
./aios start

echo "Tailing Control Room Hub logs (Ctrl+C to stop)..."
if [ -f ".aios/logs/hub.log" ]; then
  tail -n +1 -f ".aios/logs/hub.log" &
  TAIL_PID=$!
  wait "$TAIL_PID" || true
else
  exec python3 aios_hub.py
fi
