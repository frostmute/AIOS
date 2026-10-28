#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$WORKSPACE_DIR"

echo "=== Superset Workspace Teardown: AIOS ==="

if [ -f "./aios" ]; then
  echo "Stopping all running AIOS services..."
  ./aios stop || true
fi

# Clean up any lingering PID files
if [ -d ".aios/pids" ]; then
  rm -f .aios/pids/*.pid 2>/dev/null || true
fi

echo "=== Teardown complete ==="
