---
name: ruflo-orchestration
description: >
  Run ruflo — an agent meta-harness with 314+ MCP tools, multi-agent swarms, vector
  memory, federation and 30+ plugins. Use when the user wants heavy multi-agent
  orchestration, a swarm of specialised agents on one task, persistent agent memory,
  or asks for ruflo / claude-flow by name.
---

# Ruflo

[ruvnet/ruflo](https://github.com/ruvnet/ruflo) is an orchestration layer that sits
*around* coding agents (Claude Code, Codex, Cursor, and ~12 more), adding swarms,
memory, hooks and consensus. AIOS ships its skills and plugin catalogue; the runtime
comes from npm, so it's always the current release.

## Running it

Everything goes through the `aios` wrapper, which shells out to `npx ruflo@latest`:

```bash
aios ruflo --help              # ruflo's own CLI
aios ruflo init wizard         # interactive setup inside the current project
aios ruflo plugins             # list the bundled plugins + how many skills each has
aios ruflo mcp-register        # register ruflo's MCP server with Claude Code
aios ruflo status              # health/version
```

`aios ruflo <anything else>` is passed straight through to `npx ruflo`.

## What it adds

- **Swarms** — hierarchical multi-agent coordination on a single task, rather than one
  agent working alone.
- **314+ MCP tools** across core / intelligence / agents / memory / devtools groups.
- **Vector memory + AgentDB** — persistent recall across sessions.
- **Federation** — agent-to-agent collaboration across machines.
- **30+ plugins** — ADR writing, cost tracking, graph intelligence, browser control,
  AI defence, goals, DDD, and more. `aios ruflo plugins` lists them with skill counts;
  enable one with `npx ruflo@latest plugin add <name>`.

## When to reach for it

Use ruflo when the task is genuinely *big* — a migration across many files, an audit
needing several independent perspectives, or work that must survive across sessions.
For a single focused task, AIOS's own agents (opencode, the Brain, CrewAI) are lighter
and start faster.

Ruflo needs Node >= 20. If `npx` is missing, run `aios doctor`.

## Relationship to AIOS

Ruflo overlaps AIOS deliberately: both orchestrate agents. AIOS is the always-on
system (services, channels, dashboard, Task Brain); ruflo is a per-project harness you
invoke for a heavy job. They compose — run ruflo *inside* a project while the AIOS hub
keeps running.
