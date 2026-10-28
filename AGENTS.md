# AGENTS.md — The AI OS (`AIOS`)

Operational guide for autonomous AI coding agents working inside the AIOS repository.

---

## 1. System Overview & Core Philosophy

AIOS unifies 11 independent open-source AI projects into an orchestrated agent operating system governed by a single control plane (`./aios` / `aios.py`) and a unified web dashboard called the **Control Room / Hub** (`http://127.0.0.1:8787/`).

### Architectural Tenets
1. **Zero-Dependency Control Plane:** The primary orchestration layer (`aios.py`, `aios_hub.py`, `aios_brain.py`, `aios_sec.py`, `aios_tools.py`, `aios_updates.py`, `aios_web.py`, `aios_swarm.py`, `aios_vault.py`, `aios_proxy.py`) uses **pure Python 3.9+ standard library**. Do not introduce external Python packages into the root environment.
2. **Single Sources of Truth:**
   - Non-secret configuration: `aios.config.yaml`
   - Secrets & API credentials: `.env`
   - Runtime state: `.aios/` directory (`.aios/aios.db`, `.aios/logs/`, `.aios/pids/`, `.aios/rendered/`).
3. **Tri-Runtime Isolation:**
   - Python / `uv`: hermes-agent, CrewAI, claude-code-api.
   - Node / `pnpm`: openclaw, openclaw-os, paperclip.
   - Bun: opencode, LifeOS.
4. **Full-Control with Gated Security:**
   - Agents possess machine control (`security.full_control: true`) to execute shell tasks and manage files.
   - Guardrails intercept catastrophic destructive commands (`rm -rf /`, fork bombs, mkfs, dd).
   - Loopback (`127.0.0.1`/`::1`) is trusted; external or cross-origin requests require `AIOS_HUB_TOKEN`.

---

## 2. Service Architecture & Port Topology

| Service / Component | Role | Port | Runtime & Entry Point | Config / State Location |
|---|---|---|---|---|
| **AIOS Hub** | Unified Control Room, REST API, Interconnect Bus | `8787` | Python 3 (stdlib) via `aios_hub.py` | Front door at `docs/dashboard.html` / `index.html` |
| **opencode** | Headless coding agent engine & SDK | `4096` | Bun via `opencode-dev/opencode-dev/packages/opencode/src/index.ts serve` | `.aios/rendered/opencode.env` |
| **hermes** | Autonomous agent (memory, cron, background tasks) | `9119` | Python (`uv run hermes dashboard`) in `hermes-agent-main` | `hermes-agent-main/.../.env`, `cli-config.yaml` |
| **openclaw** | Multi-channel messaging gateway & plugin host | `18789` | Node (`node openclaw.mjs gateway`) in `openclaw-main` | `openclaw-main/.../.env`, `.aios/openclaw/openclaw.json` |
| **CrewAI** | Role-based multi-agent crew execution | `4788` | Python (`uv run --project crewAI-main ... services/crewai_service.py`) | Managed in `services/crewai_service.py` |
| **claudecode** | Claude Code CLI wrapped as an OpenAI-compatible API | `8000` | Python (`uv run uvicorn claude_code_api.main:app`) | `claude-code-api-main` |
| **codex** | ChatGPT/Codex subscription wrapped as an OpenAI API | `8010` | Python 3 (stdlib) via `services/codex_api.py` | Runs against local `codex` CLI |
| **paperclip** | Company governance layer (org chart, tickets, budgets) | `3100` | Node 24 (`paperclip run --data-dir .aios/paperclip`) | `.aios/paperclip/` |
| **openclaw-proxy** | Frame-stripping reverse proxy for embedding openclaw UI | `8791` | Python 3 (stdlib) via `aios_proxy.py` | Targets `:18789` |
| **hermes-proxy** | Frame-stripping reverse proxy for embedding hermes UI | `8792` | Python 3 (stdlib) via `aios_proxy.py` | Targets `:9119` |

### Mounted Integrations (No Separate Service Ports)
- **openclaw-os:** Generative UI dashboard plugin compiled into `openclaw-main`.
- **LifeOS:** Identity, personal context, and skill trees (`LifeOS-main`) mounted into agent skill paths.
- **OpenUI:** Generative UI spec context injected into system prompts.
- **Fabric:** 255 markdown prompt patterns (`fabric-main`).
- **Caveman:** Conciseness system-prompt overlay (~65% token reduction).
- **Ponytail:** YAGNI and anti-overengineering prompt overlay.
- **Ruflo:** Meta-harness providing 314+ MCP tools and swarm coordination.

---

## 3. Essential Commands & Development Workflow

Always execute root management via `./aios` (POSIX) or `.\aios.ps1` (PowerShell). Both invoke `python3 aios.py`.

### Diagnostic & Inspection
```bash
./aios doctor             # Validate toolchains (bun, uv, pnpm, node), ports, config, deps
./aios status             # Inspect state, PID, port, and health check of all services
./aios debug              # Dump resolved configuration, env vars, and service map
./aios logs [service]     # Tail service log from .aios/logs/<service>.log
./aios url                # Output active URLs for Hub and agent interfaces
./aios token              # Display or rotate (?--rotate) the AIOS_HUB_TOKEN
```

### Lifecycle & Operations
```bash
./aios setup              # Idempotent setup: toolchain check, render config, mount skills, wire plugins
./aios setup --skip-keys  # Non-interactive setup without prompting for API keys
./aios setup --force      # Re-run setup wizard and overwrite configuration with backup
./aios start [all|svc...] # Launch service(s) and poll health endpoints until green
./aios stop [all|svc...]  # Gracefully terminate process trees using stored PID files
./aios restart [svc...]   # Cleanly respawn service(s) to pick up code changes
./aios wire               # Re-mount skills and reinstall openclaw-os plugin
```

### Testing
```bash
./aios test --smoke       # Health-check smoke test verifying all enabled service endpoints
./aios test opencode      # Runs bun test in packages/core (DO NOT run from opencode root)
./aios test hermes        # Runs uv run pytest -q in hermes-agent
./aios test openclaw      # Runs pnpm -r test in openclaw
./aios test openclaw_os   # Runs pnpm -r test in openclaw-os
```

### Auxiliary CLI Subcommands
```bash
./aios exec <cmd>         # Run shell command through audited, guardrailed execution engine
./aios web <url>          # Extract cleaned content/transcript from YouTube, Reddit, HN, or web
./aios channels           # List status of the 28 OpenClaw supported messaging bridges
./aios profile <name>     # Switch/isolate memory and tasks per project (AIOS_PROFILE)
./aios paperclip          # Manage company layer tickets, heartbeats, and board status
./aios updates            # Supervised upstream update analysis (SAFE / RISKY / BLOCK)
```

### Superset Workspaces (`.superset/`)
When working within a Superset git worktree:
- Setup: `./.superset/setup.sh` (fast path: symlinks `node_modules` and `dist` from root worktree if present).
- Run: `./.superset/run.sh` (starts services and tails `.aios/logs/hub.log`).
- Teardown: `./.superset/teardown.sh` (stops all services and cleans `.aios/pids/`).

---

## 4. Configuration & State Management

### 1. `aios.config.yaml` Rules
- **Parser Constraint:** `aios.py` contains a lightweight custom YAML parser written in stdlib Python.
- **Syntax Limitation:** Only use 2-space indentation and flat/nested `key: value` pairs or simple lists.
- **Never Use:** YAML anchors (`&`, `*`), multi-line scalar folding (`|`, `>`), or complex tagging.
- **Provider Settings:** `model.provider` accepts `openrouter`, `anthropic`, `openai`, `gemini`, `claudecode`, `codex`, or `9router`.

### 2. Secret Mapping (`.env`)
Updating `.env` automatically maps credentials across runtimes when running `./aios setup` or `./aios start`:
- `AIOS_LLM_PROVIDER`: Provider name.
- `AIOS_LLM_API_KEY`: Global API key mapped into `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.
- `AIOS_DEFAULT_MODEL`: Default model string (e.g. `ag/gemini-3.8-flash`, `anthropic/claude-opus-4.6`).
- `AIOS_LLM_BASE_URL`: Base URL mapped into `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL` (e.g. `http://127.0.0.1:20128/v1` for 9router).
- `AIOS_HUB_TOKEN`: Security token for external/non-loopback requests.

### 3. Database & Durability (`.aios/aios.db`)
Backed by SQLite with WAL mode (`PRAGMA journal_mode=WAL`):
- `memory`: Persistent memory table indexed with SQLite FTS5 for active recall.
- `tasks` & `task_runs`: Internal cron and background job scheduler.
- `flows` & `flow_steps`: Multi-step flows with resumption support.
- `skills_learned`: Skills written by the self-improving loop (`skills/learned/<name>/SKILL.md`).
- `audit`: Audit trail of all shell executions triggered by agents.
- **Profiles:** Setting `AIOS_PROFILE=work` redirects database to `.aios/profiles/work/aios.db`.

---

## 5. Inter-Agent Communication & Swarms

The Hub (`aios_hub.py`) acts as the message bus across all agents:

1. **Relay API:**
   - Any agent can POST to `http://127.0.0.1:8787/api/relay` with payload `{"to": "<agent>", "message": "<prompt>"}`.
   - Target agents: `opencode`, `hermes`, `crewai`, `claudecode`, `codex`, `brain`.
2. **CrewAI Tools:**
   - `ask_peer(query: "target: message")`: Direct tool for CrewAI to invoke peers.
   - `build_dashboard_ui(spec: "Title | <html>")`: Injects live widgets directly onto the Hub Canvas.
   - `run_shell(command)`: Routes commands through `aios_tools.py` with auditing and guardrails.
3. **Swarm DAG (`aios_swarm.py`):**
   - Plan phase: Decomposes prompts into tasks with explicit dependencies.
   - Execution phase: Runs non-dependent tasks concurrently via thread pools.
   - Consultation: Agents can invoke `ASK <agent>: <question>` mid-flight.
   - Synthesis: Final agent consolidates blackboard results into the final output.

---

## 6. Critical Gotchas & Non-Obvious Patterns

1. **Monorepo Root Directory Nesting:**
   - Upstream archives were unpacked into double-nested structures (e.g., `opencode-dev/opencode-dev`, `hermes-agent-main/hermes-agent-main`, `openclaw-main/openclaw-main`, `crewAI-main/crewAI-main`, `openclaw-os-main/openclaw-os-main`, `LifeOS-main/LifeOS-main`).
   - `aios.py` uses `resolve_root()` to check both single and double-nested directory paths. When adding path references, always resolve through `PROJECTS[...]` or verify path existence.
2. **Opencode Test Execution:**
   - Running `bun test` in `opencode-dev/opencode-dev` root fails immediately with: *"do not run tests from root"*.
   - Tests must be run per package: `cd opencode-dev/opencode-dev/packages/core && bun test`.
3. **OpenClaw Build Memory Allocation:**
   - `openclaw` compilation (`pnpm build`) requires substantial memory (~8GB to 12GB heap).
   - `aios.py` dynamically computes physical RAM via `_node_heap_mb()` and sets `NODE_OPTIONS=--max-old-space-size=...`. Never trigger `pnpm build` in openclaw without setting this heap limit.
4. **OpenClaw-OS Cross-Platform Build:**
   - The native package script for `@openuidev/openclaw-os-plugin` uses Unix `rm -rf`, which fails on Windows.
   - `_build_openclaw_os()` in `aios.py` calls `esbuild` directly with explicit CLI arguments to ensure cross-platform compatibility.
5. **Paperclip Agent Heartbeat Lock:**
   - Every agent write operation in Paperclip (checkout, comments, ticket updates) requires the active run ID of a queued/running heartbeat.
   - Hub requests to Paperclip webhooks run synchronously inside the heartbeat handler. Returning early closes the run and causes subsequent writes to fail with *"Agent run id required"*.
   - Paperclip enforces SSRF protection on loopback targets: `PAPERCLIP_HTTP_ADAPTER_PRIVATE_ENDPOINT_ALLOWLIST` must explicitly match the Hub origin (`http://127.0.0.1:8787`).
6. **Codex Workspace Behavior:**
   - OpenAI Codex CLI is a workspace-oriented agent. Pointing it at an active repository causes it to prompt for task instructions rather than returning chat completions.
   - `services/codex_api.py` sets `WORKDIR` to a temporary scratch directory (`/tmp/aios-codex-workspace`) by default to function as a general-purpose OpenAI-compatible endpoint.
7. **Cross-Platform Console Stream Encoding:**
   - Windows cmd/PowerShell defaults to `cp1252` which crashes on Unicode characters (`✓`, `✗`, `—`).
   - `aios.py` explicitly forces UTF-8 stream reconfigurations (`sys.stdout.reconfigure(encoding="utf-8")`) and activates Windows virtual terminal processing. Avoid introducing unencoded Unicode writes.
8. **Vault Threat Model & Failsafe:**
   - The master password for irreversible actions (`aios uninstall`, disabling guardrails) is hashed with `scrypt(password, salt)`.
   - The vault fails closed: deleting or tampering with `.aios/vault.json` prevents protected operations from running rather than granting access.
