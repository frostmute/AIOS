#!/usr/bin/env python3
"""
Codex API — OpenAI Codex CLI exposed as an OpenAI-compatible endpoint.

The mirror image of claude-code-api: it wraps the locally-installed `codex` CLI so
anything that speaks the OpenAI chat API can drive your ChatGPT/Codex
subscription. No API key involved — auth is whatever `codex login` established.

    POST /v1/chat/completions      the usual shape
    GET  /v1/models                the models this bridge accepts
    GET  /health                   liveness + codex version + auth state

Run by `aios start codex`. Env: AIOS_CODEX_PORT, AIOS_CODEX_MODEL,
AIOS_CODEX_SANDBOX, AIOS_CODEX_TIMEOUT.

Why `--output-last-message` rather than parsing stdout: Codex streams reasoning,
tool calls and token counts to the terminal, and scraping the final answer out of
that is guesswork. The flag writes exactly the last assistant message to a file,
which is precisely what a chat completion should return.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("AIOS_CODEX_PORT", "8010"))
# Empty means "whatever codex itself is configured to use". Naming a model here
# was a mistake: passing -m with a model the account can't use makes codex exit 1
# with "model is not supported", so the bridge only overrides when asked to.
DEFAULT_MODEL = os.environ.get("AIOS_CODEX_MODEL", "").strip()
# read-only by default: this bridge exists to answer questions, and AIOS already
# has its own guarded shell for anything that needs to touch the machine.
SANDBOX = os.environ.get("AIOS_CODEX_SANDBOX", "read-only")
TIMEOUT = int(os.environ.get("AIOS_CODEX_TIMEOUT", "600"))
# Codex is a *workspace* agent: point it at a real repo and it answers questions
# with "Ready. What would you like me to work on in <repo>?" instead of the
# question you asked. Default to a neutral scratch dir so this behaves like a
# chat endpoint; set AIOS_CODEX_WORKDIR to give it repo context deliberately.
WORKDIR = os.environ.get("AIOS_CODEX_WORKDIR", "").strip() or str(
    Path(tempfile.gettempdir()) / "aios-codex-workspace")


def codex_bin() -> str:
    return shutil.which("codex") or ""


def codex_version() -> str:
    b = codex_bin()
    if not b:
        return ""
    try:
        r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    except Exception:
        return ""


def flatten(messages: list) -> str:
    """Codex takes a single prompt, so fold the conversation into one, keeping the
    role labels that let the model tell system rules from the live question."""
    parts = []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        content = m.get("content") or ""
        if isinstance(content, list):  # OpenAI's multipart content form
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        content = str(content).strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[System instructions]\n{content}")
        elif role == "assistant":
            parts.append(f"[Previous assistant reply]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def run_codex(prompt: str, model: str = "") -> tuple[str, str]:
    """Returns (text, error)."""
    b = codex_bin()
    if not b:
        return "", ("the `codex` CLI is not installed — "
                    "npm install -g @openai/codex, then `codex login`")
    try:
        Path(WORKDIR).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    tmp = Path(tempfile.gettempdir()) / f"aios-codex-{uuid.uuid4().hex}.txt"
    cmd = [b, "exec", "--skip-git-repo-check", "-s", SANDBOX,
           "-C", WORKDIR, "-o", str(tmp)]
    if model:
        cmd += ["-m", model]
    # "-" makes codex read the instructions from stdin. Passing a multi-line
    # prompt as an argv item instead looks fine on Unix but silently breaks on
    # Windows, where `codex` is a .CMD shim and cmd.exe mangles embedded
    # newlines — codex then sees a truncated prompt and replies with a generic
    # "Ready. What would you like me to work on?" instead of answering.
    cmd.append("-")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                           input=prompt, errors="replace")
        text = tmp.read_text(encoding="utf-8", errors="replace").strip() if tmp.exists() else ""
        if not text:
            tail = ((r.stderr or "") + (r.stdout or ""))[-600:]
            low = tail.lower()
            if "not logged in" in low or "login" in low and "run" in low:
                return "", "codex is not logged in — run `codex login` in a terminal"
            return "", f"codex produced no output (exit {r.returncode}). {tail[-300:]}"
        return text, ""
    except subprocess.TimeoutExpired:
        return "", f"codex timed out after {TIMEOUT}s"
    except Exception as e:
        return "", f"codex failed: {e}"
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # aios captures stdout separately

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path in ("/health", "/"):
            v = codex_version()
            self._send(200, {"status": "healthy" if v else "degraded",
                             "service": "codex-api", "codex_version": v,
                             "installed": bool(codex_bin()),
                             "model": DEFAULT_MODEL or "(codex default)",
                             "sandbox": SANDBOX})
        elif self.path in ("/v1/models", "/models"):
            now = int(time.time())
            # "codex" = use whatever the CLI is configured for; the rest are
            # passed through as -m for anyone who knows their account supports them.
            ids = ["codex"] + ([DEFAULT_MODEL] if DEFAULT_MODEL else [])
            self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": now, "owned_by": "openai-codex"}
                for m in dict.fromkeys(ids)]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": "not found"})
            return

        prompt = flatten(payload.get("messages", []))
        if not prompt:
            self._send(400, {"error": {"message": "no messages", "type": "invalid_request"}})
            return
        model = (payload.get("model") or DEFAULT_MODEL or "").strip()
        if model in ("codex", "default", "codex-api"):
            model = DEFAULT_MODEL          # let codex pick unless told otherwise

        started = time.time()
        text, err = run_codex(prompt, model)
        if err:
            # OpenAI-shaped error so clients surface it instead of guessing.
            self._send(502, {"error": {"message": err, "type": "codex_error"}})
            return
        now = int(time.time())
        self._send(200, {
            "id": f"chatcmpl-codex-{now}", "object": "chat.completion", "created": now,
            "model": model or "codex",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": len(prompt) // 4,
                      "completion_tokens": len(text) // 4,
                      "total_tokens": (len(prompt) + len(text)) // 4},
            "aios": {"elapsed_s": round(time.time() - started, 1), "sandbox": SANDBOX},
        })


def main():
    host = os.environ.get("AIOS_CODEX_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, PORT), Handler)
    v = codex_version() or "NOT INSTALLED"
    print(f"Codex API listening on http://{host}:{PORT}/v1  (codex: {v}, "
          f"model={DEFAULT_MODEL or 'codex default'}, sandbox={SANDBOX})")
    if not codex_bin():
        print("⚠  the `codex` CLI is missing — npm install -g @openai/codex && codex login")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
