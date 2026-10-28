#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$WORKSPACE_DIR"

echo "=== Superset Workspace Setup: AIOS ==="

# 1. Resolve root repository path
ROOT_PATH="${SUPERSET_ROOT_PATH:-}"
if [ -z "$ROOT_PATH" ]; then
  ROOT_PATH="$(git worktree list 2>/dev/null | head -n 1 | awk '{print $1}')"
fi

# 2. Copy .env and config from root repo or fallback to examples
if [ -n "$ROOT_PATH" ] && [ -d "$ROOT_PATH" ] && [ "$ROOT_PATH" != "$WORKSPACE_DIR" ]; then
  if [ -f "$ROOT_PATH/.env" ] && [ ! -f ".env" ]; then
    echo "Copying .env from root repository..."
    cp -a "$ROOT_PATH/.env" .env
  fi
  if [ -f "$ROOT_PATH/aios.config.yaml" ] && [ ! -f "aios.config.yaml" ]; then
    echo "Copying aios.config.yaml from root repository..."
    cp -a "$ROOT_PATH/aios.config.yaml" aios.config.yaml
  fi
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  echo "Initializing .env from .env.example..."
  cp -a .env.example .env
fi

if [ ! -f "aios.config.yaml" ] && [ -f "aios.config.example.yaml" ]; then
  echo "Initializing aios.config.yaml from aios.config.example.yaml..."
  cp -a aios.config.example.yaml aios.config.yaml
fi

# 3. Share pre-built node_modules and dist artifacts from root repository (fast path)
LINKED_DEPS=false
if [ -n "$ROOT_PATH" ] && [ -d "$ROOT_PATH" ] && [ "$ROOT_PATH" != "$WORKSPACE_DIR" ]; then
  SHARED_DIRS=(
    "openclaw-main/openclaw-main/node_modules"
    "openclaw-main/openclaw-main/dist"
    "openclaw-os-main/openclaw-os-main/node_modules"
    "openclaw-os-main/openclaw-os-main/packages/claw-plugin/dist"
    "openclaw-os-main/openclaw-os-main/packages/claw-client/out"
    "opencode-dev/opencode-dev/node_modules"
    "opencode-dev/opencode-dev/packages/opencode/node_modules"
  )

  for dir in "${SHARED_DIRS[@]}"; do
    if [ -e "$ROOT_PATH/$dir" ] && [ ! -e "$dir" ]; then
      mkdir -p "$(dirname "$dir")"
      ln -sfn "$ROOT_PATH/$dir" "$dir"
      LINKED_DEPS=true
    fi
  done
fi

# 4. Initialize Python virtual environments
if command -v uv >/dev/null 2>&1; then
  if [ -d "hermes-agent-main/hermes-agent-main" ] && [ ! -d "hermes-agent-main/hermes-agent-main/.venv" ]; then
    echo "Setting up hermes virtualenv..."
    (cd hermes-agent-main/hermes-agent-main && uv sync)
  fi

  if [ -d "claude-code-api-main" ] && [ ! -d "claude-code-api-main/.venv" ]; then
    echo "Setting up claude-code-api virtualenv..."
    (cd claude-code-api-main && uv sync)
  fi

  if [ -d "crewAI-main/crewAI-main" ] && [ ! -d "crewAI-main/crewAI-main/.venv" ]; then
    echo "Setting up crewai virtualenv..."
    (cd crewAI-main/crewAI-main && uv venv .venv)
  fi
fi

# 5. Run aios setup to render configs, mount skills, and configure runtime
if [ "$LINKED_DEPS" = "true" ]; then
  echo "Running fast AIOS setup (dependencies linked)..."
  ./aios setup --non-interactive --skip-keys --skip-install
else
  echo "Running full AIOS setup..."
  ./aios setup --non-interactive --skip-keys
fi

# 6. Verify health
./aios doctor || true

echo "=== Setup complete ==="
