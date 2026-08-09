# The AI OS in Docker

Nine open-source AI projects, three runtimes (Python/uv, Node/pnpm, Bun), one
container. Everything is driven by `aios`, exactly as it is on the host.

## Why run it in a container

The AI OS gives every agent **full control of the machine it runs on** — shell,
filesystem, network. That's the point of an agent OS, and on your laptop it means
your laptop. In a container it means *the container*: a blast radius you can delete
and recreate in one command. The guardrails (`rm -rf /`, `mkfs`, fork bombs) and the
hub token still apply inside, so you get defence in depth rather than a swap.

It also removes the messiest part of the host install: you don't need Bun, pnpm,
Node 22, uv, ripgrep and the Claude CLI on your own machine.

## Quick start

```bash
git clone https://github.com/ZDStudios/AIOS.git
cd AIOS
cp .env.example .env          # add your model key, or skip and use Claude below
docker compose up -d --build  # first build takes ~10 min (it installs 9 projects)
docker compose logs -f        # watch it come up; the Control Room URL is printed
```

Then open **http://localhost:8787**. The hub token is in the logs, and the printed
URL already includes it.

## Using your Claude Pro/Max subscription instead of an API key

```bash
docker compose exec aios aios claude-login
```

This runs the Claude CLI login inside the container and prints the authorize link.
Alternatively the compose file mounts `~/.claude` from your host, so if you're
already logged in on the host the container inherits it.

## Everyday commands

```bash
docker compose exec aios aios status          # what's running
docker compose exec aios aios doctor          # diagnose + exact fixes
docker compose exec aios aios logs hub        # tail a service
docker compose exec aios aios channels        # the 28 messaging channels
docker compose exec aios aios web youtube URL # read a video transcript
docker compose exec aios bash                 # a shell inside
docker compose down                           # stop (state is preserved)
```

Anything after `aios` in `docker compose exec aios aios …` is the normal CLI.

## What's exposed

| Port | Service |
|---|---|
| **8787** | ★ Control Room (the only one you normally need) |
| 4096 | opencode |
| 9119 | hermes |
| 18789 | openclaw gateway + openclaw-os dashboard |
| 4788 | CrewAI |
| 8000 | claude-code API (OpenAI-compatible) |
| 8791 / 8792 | embed proxies for openclaw / hermes |

If you only want the dashboard reachable, delete the other `ports:` lines — the
services still talk to each other inside the container.

## State and secrets

| Path | What | Where it lives |
|---|---|---|
| `/aios/.aios` | memory, tasks, flows, audit log, logs, pidfiles | named volume `aios-state` |
| `/aios/.env` | your keys + the hub token | bind-mounted from the repo |
| `/aios/aios.config.yaml` | service config | bind-mounted from the repo |
| `/root/.claude` | Claude CLI login | bind-mounted from `~/.claude` |

Because state is a named volume, `docker compose down` keeps it and
`docker compose down -v` wipes it. Rebuilding the image never loses your memory
or tasks.

## Security notes

- The hub binds `0.0.0.0` inside the container so the port publish works. That's
  only safe because **every non-loopback request needs `AIOS_HUB_TOKEN`** — which
  the entrypoint mints on first boot. Publishing 8787 to a hostile network without
  that token would be an open control plane.
- `no-new-privileges` is set and `SYS_ADMIN`/`NET_ADMIN` are dropped; nothing AIOS
  runs needs them.
- Full control is on by default *inside the container*. Turn it off with
  `security.full_control: false` in `aios.config.yaml` if you want read-only agents.
- Don't commit `.env` — it holds your keys and the hub token.

## Troubleshooting

**First boot is slow.** It installs nine projects. `docker compose logs -f` shows
progress; the healthcheck has a 180s grace period.

**A service isn't healthy.** `docker compose exec aios aios doctor` prints the exact
fix, and `aios logs <service>` tails it.

**Build runs out of memory.** openclaw's build is memory-hungry; give Docker at
least 8GB (Docker Desktop → Settings → Resources).

**Rebuild from scratch:**
```bash
docker compose build --no-cache && docker compose up -d
```
