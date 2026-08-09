#!/usr/bin/env bash
# The AI OS container entrypoint.
#
#   start   (default)  render config, bring the stack up, stay in the foreground
#   setup              re-run setup (deps/build/wire) then exit
#   shell              a shell inside the container
#   <anything else>    passed straight to `aios`
set -euo pipefail

cd /aios
export PATH="/usr/local/pnpm:/usr/local/bun/bin:/root/.local/bin:$PATH"

c(){ printf '\033[1;36m%s\033[0m\n' "$*"; }
ok(){ printf '\033[1;32mOK\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m!\033[0m %s\n' "$*"; }

# .aios/ and .env come from volumes, so a fresh container inherits your state.
mkdir -p /aios/.aios
[ -f /aios/aios.config.yaml ] || cp aios.config.example.yaml /aios/aios.config.yaml

# The hub binds 0.0.0.0 so the port publish works. That is only safe because
# non-loopback requests need the token — mint it before anything starts.
if [ ! -f /aios/.env ] || ! grep -q '^AIOS_HUB_TOKEN=' /aios/.env 2>/dev/null; then
  python aios.py token >/dev/null 2>&1 || true
fi
TOKEN="$(grep -m1 '^AIOS_HUB_TOKEN=' /aios/.env 2>/dev/null | cut -d= -f2- || true)"

case "${1:-start}" in
  start)
    c "The AI OS — starting in Docker"
    if [ -z "${AIOS_LLM_API_KEY:-}" ] && ! grep -q '^AIOS_LLM_API_KEY=.\+' /aios/.env 2>/dev/null; then
      warn "no model API key set — the stack will run, but agents can't answer."
      warn "Set one in .env / compose env, or use your Claude subscription:"
      warn "  docker compose exec aios aios claude-login"
    fi

    # Render .env + config into every project, mount skills, apply exec policy.
    python aios.py setup --non-interactive --skip-tools --skip-install \
      || warn "setup finished with warnings (run: docker compose exec aios aios doctor)"

    python aios.py start all --timeout 180 || warn "some services did not report healthy"
    python aios.py status || true

    echo
    ok "Control Room:  http://localhost:8787/${TOKEN:+?token=$TOKEN}"
    [ -n "$TOKEN" ] && echo "   hub token: $TOKEN"
    echo

    # PID 1 must stay alive for the container to stay up. Watch the hub and
    # surface its log, so `docker logs` shows something useful.
    touch /aios/.aios/logs/hub.log 2>/dev/null || mkdir -p /aios/.aios/logs
    tail -n 0 -F /aios/.aios/logs/*.log 2>/dev/null &
    while true; do
      if ! curl -fsS http://127.0.0.1:8787/health >/dev/null 2>&1; then
        warn "hub stopped responding — restarting it"
        python aios.py restart hub || true
      fi
      sleep 20
    done
    ;;
  setup)  shift; exec python aios.py setup --non-interactive "$@" ;;
  shell)  exec /bin/bash ;;
  *)      exec python aios.py "$@" ;;
esac
