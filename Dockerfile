# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# The AI OS — containerised.
#
# Nine open-source AI projects across three runtimes (Python/uv, Node/pnpm, Bun)
# in one image, driven by `aios`.
#
#   docker compose up -d          then open http://localhost:8787
#
# Why a container is the *right* place to run this: The AI OS gives every agent
# full control of the machine it runs on — shell, filesystem, network. On your
# laptop that means your laptop. Here it means this container, which is a blast
# radius you can delete. Guardrails and the hub token still apply inside.
# ─────────────────────────────────────────────────────────────────────────────

# ---------- stage 1: toolchains ---------------------------------------------
FROM python:3.12-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_MAJOR=22 \
    PNPM_HOME=/usr/local/pnpm \
    BUN_INSTALL=/usr/local/bun \
    PATH=/usr/local/pnpm:/usr/local/bun/bin:/root/.local/bin:$PATH

# Node 22 (openclaw + Next.js need >=20), plus the build deps native modules want.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git unzip xz-utils ripgrep procps tini \
        build-essential python3-dev \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g pnpm@9 \
    && curl -fsSL https://bun.sh/install | bash \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get purge -y --auto-remove \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# Claude Code CLI — powers the claude-code service (subscription auth, no API key).
RUN npm install -g @anthropic-ai/claude-code || \
    echo "claude CLI unavailable at build time; install later with: npm i -g @anthropic-ai/claude-code"

# yt-dlp powers YouTube transcripts (the caption endpoint needs a PO token).
RUN pip install --no-cache-dir --upgrade yt-dlp

# ---------- stage 2: runtime ------------------------------------------------
FROM base AS runtime
WORKDIR /aios

LABEL org.opencontainers.image.title="The AI OS" \
      org.opencontainers.image.description="Nine open-source AI projects. One operating system." \
      org.opencontainers.image.source="https://github.com/ZDStudios/AIOS" \
      org.opencontainers.image.licenses="MIT"

COPY . /aios

# `aios setup` does the installing rather than a hand-written RUN, because it
# already encodes every per-project workaround: --ignore-scripts for the native
# postinstalls that need VS Build Tools, NODE_OPTIONS heap sizing for openclaw's
# build, and uv sync for the Python projects. Installing from package.json alone
# silently produces empty node_modules — opencode and openclaw are workspace
# monorepos and need their full source present to resolve.
RUN set -eux; \
    cp -n aios.config.example.yaml aios.config.yaml 2>/dev/null || true; \
    chmod +x aios docker/entrypoint.sh 2>/dev/null || true; \
    mkdir -p /aios/.aios; \
    python aios.py setup --non-interactive --skip-keys --skip-tools --skip-wire \
      || echo "setup reported warnings — see 'docker compose exec aios aios doctor'"; \
    # Same layer as the install, or the image keeps the deleted bytes anyway.
    # pnpm hardlinks node_modules into its content-addressable store, so dropping
    # the store frees ~4.7GB while the linked files stay intact (verified).
    rm -rf /usr/local/pnpm/store /root/.cache /root/.npm /tmp/* /var/tmp/*; \
    find /aios -type d -name ".git" -prune -exec rm -rf {} + 2>/dev/null || true; \
    apt-get purge -y --auto-remove build-essential python3-dev 2>/dev/null || true; \
    rm -rf /var/lib/apt/lists/*

# Login shells (docker exec ... bash) re-read /etc/profile and would otherwise
# lose the toolchain PATH baked into ENV.
RUN printf 'export PATH=/usr/local/pnpm:/usr/local/bun/bin:/root/.local/bin:$PATH\n' \
      > /etc/profile.d/aios-path.sh

ENV AIOS_ROOT=/aios \
    AIOS_HUB_HOST=0.0.0.0 \
    AIOS_IN_DOCKER=1 \
    AIOS_NO_UPDATE_CHECK=1

#   8787 hub · 4096 opencode · 9119 hermes · 18789 openclaw
#   4788 crewai · 8000 claude-code · 8791/8792 embed proxies
EXPOSE 8787 4096 9119 18789 4788 8000 8791 8792

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8787/health || exit 1

# tini reaps the process trees aios spawns, so stopping the container doesn't
# strand bun/node/uv children as zombies.
ENTRYPOINT ["/usr/bin/tini", "--", "/aios/docker/entrypoint.sh"]
CMD ["start"]
