#!/usr/bin/env python3
"""
AIOS Plugin Store — one place to browse and install everything installable.

"Plugin" means four different things across the bundled projects, so the store
normalises them rather than pretending there's one mechanism:

  channel   an OpenClaw messaging channel   -> npm package, installed into openclaw
  openclaw  an OpenClaw feature plugin      -> `openclaw plugins install`
  ruflo     a ruflo capability pack         -> `npx ruflo plugin add`
  skill     a SKILL.md every agent mounts   -> copied into skills/

Sources are read from what's actually on disk (OpenClaw's generated channel
catalog, ruflo's plugins/ tree, the bundled skills) plus a small curated list, so
the catalogue reflects this install rather than a hardcoded guess.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import aios_brain as brain
import aios_tools as tools

ROOT = Path(os.environ.get("AIOS_ROOT", Path(__file__).resolve().parent))
INSTALLED_FILE = ROOT / ".aios" / "plugins_installed.json"

# Hand-picked extras that aren't discoverable from a local tree.
CURATED = [
    {"id": "openclaw-os", "kind": "openclaw", "name": "openclaw-os",
     "blurb": "Generative-UI dashboard for the OpenClaw gateway (bundled).",
     "source": "thesysdev/openclaw-os", "bundled": True},
    {"id": "ponytail-mcp", "kind": "ruflo", "name": "ponytail-mcp",
     "blurb": "Ponytail as an MCP server — laziness ladder for any MCP client.",
     "source": "DietrichGebert/ponytail"},
    {"id": "caveman-shrink", "kind": "ruflo", "name": "caveman-shrink",
     "blurb": "MCP middleware that compresses tool descriptions to cut input tokens.",
     "source": "JuliusBrussee/caveman"},
]


def _installed() -> dict:
    try:
        return json.loads(INSTALLED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _mark(pid: str, info: dict):
    d = _installed()
    d[pid] = info
    INSTALLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTALLED_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _unmark(pid: str):
    d = _installed()
    d.pop(pid, None)
    INSTALLED_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def catalog() -> list[dict]:
    """Everything installable, from what's actually present on this machine."""
    inst = _installed()
    out: list[dict] = []

    # 1) OpenClaw channels — reuse the reader that already parses its catalog.
    for c in tools.channels():
        if c.get("source") == "core":
            continue
        out.append({
            "id": f"channel:{c['id']}", "kind": "channel", "name": c["label"],
            "blurb": c.get("blurb", ""), "package": c.get("package", ""),
            "env": c.get("envVars", []), "configured": c.get("configured", False),
            "docs": ("https://openclaw.ai" + c["docsPath"]) if c.get("docsPath") else "",
        })

    # 2) ruflo plugin packs, read from its vendored tree.
    rdir = ROOT / "ruflo-main" / "plugins"
    if rdir.exists():
        for d in sorted(p for p in rdir.iterdir() if p.is_dir()):
            skills = list(d.glob("skills/*/SKILL.md"))
            blurb = ""
            rm = d / "README.md"
            if rm.exists():
                for line in rm.read_text(encoding="utf-8", errors="replace").splitlines():
                    t = line.strip().lstrip("#").strip()
                    if t and not t.startswith("!") and len(t) > 12:
                        blurb = t[:160]
                        break
            out.append({"id": f"ruflo:{d.name}", "kind": "ruflo", "name": d.name,
                        "blurb": blurb or f"ruflo plugin ({len(skills)} skills)",
                        "skills": len(skills)})

    # 3) Skills already mounted (so the store shows what you have, not just what
    #    you could get) — and the learned ones the Curator wrote.
    for s in tools.skills():
        out.append({"id": f"skill:{s['name']}", "kind": "skill", "name": s["name"],
                    "blurb": s.get("description", ""), "bundled": not s.get("learned"),
                    "installed": True})

    out.extend({**c, "id": c["id"]} for c in CURATED)

    for p in out:
        p["installed"] = p.get("installed") or (p["id"] in inst) or p.get("bundled", False)
        if p["id"] in inst:
            p["installed_at"] = inst[p["id"]].get("ts")
    return out


def install(pid: str, extra: dict | None = None) -> dict:
    """Install by id. Each kind has its own real mechanism."""
    extra = extra or {}
    kind, _, name = (pid or "").partition(":")
    if not name:
        return {"ok": False, "error": f"bad plugin id '{pid}'"}

    if kind == "skill":
        content = extra.get("content", "")
        if not content:
            return {"ok": False, "error": "a skill needs SKILL.md content"}
        r = tools.install_skill(name, content)
        if r.get("ok"):
            _mark(pid, {"ts": __import__("time").time(), "kind": kind})
        return r

    if kind == "channel":
        env = extra.get("env") or {}
        if not env:
            return {"ok": False, "error": "this channel needs its tokens "
                                          "(see the Channels view)"}
        return {"ok": False, "error": "configure channels from the Channels view — "
                                      "it writes the tokens and restarts the gateway"}

    if kind == "ruflo":
        npx = tools.shutil.which("npx") if hasattr(tools, "shutil") else None
        if not npx:
            import shutil as _sh
            npx = _sh.which("npx")
        if not npx:
            return {"ok": False, "error": "npx not found — install Node >= 20"}
        r = tools.shell(f"npx ruflo@latest plugin add {name}", actor="plugin-store",
                        timeout=600)
        if r.get("ok"):
            _mark(pid, {"ts": __import__("time").time(), "kind": kind})
        return {"ok": r.get("ok"), "output": (r.get("out") or r.get("err") or "")[-1500:],
                "error": "" if r.get("ok") else "ruflo plugin add failed"}

    if kind == "openclaw":
        cl = ROOT / "openclaw-main" / "openclaw-main"
        if not cl.exists():
            return {"ok": False, "error": "openclaw is not installed here"}
        r = tools.shell(f'cd "{cl}" && node openclaw.mjs plugins install {name}',
                        actor="plugin-store", timeout=900)
        if r.get("ok"):
            _mark(pid, {"ts": __import__("time").time(), "kind": kind})
        return {"ok": r.get("ok"), "output": (r.get("out") or r.get("err") or "")[-1500:],
                "error": "" if r.get("ok") else "openclaw plugin install failed"}

    return {"ok": False, "error": f"unknown plugin kind '{kind}'"}


def uninstall(pid: str) -> dict:
    kind, _, name = (pid or "").partition(":")
    if kind == "skill":
        import shutil as _sh
        d = ROOT / "skills" / name
        if d.exists():
            _sh.rmtree(d, ignore_errors=True)
            _unmark(pid)
            brain.audit("plugin-store", "plugin.uninstall", pid)
            return {"ok": True}
        return {"ok": False, "error": "not found"}
    if kind == "ruflo":
        r = tools.shell(f"npx ruflo@latest plugin remove {name}", actor="plugin-store",
                        timeout=600)
        _unmark(pid)
        return {"ok": r.get("ok"), "output": (r.get("out") or "")[-800:]}
    if kind == "openclaw":
        cl = ROOT / "openclaw-main" / "openclaw-main"
        r = tools.shell(f'cd "{cl}" && node openclaw.mjs plugins remove {name}',
                        actor="plugin-store", timeout=600)
        _unmark(pid)
        return {"ok": r.get("ok"), "output": (r.get("out") or "")[-800:]}
    return {"ok": False, "error": f"cannot uninstall '{pid}'"}


if __name__ == "__main__":
    c = catalog()
    kinds: dict[str, int] = {}
    for p in c:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(json.dumps({"total": len(c), "by_kind": kinds,
                      "installed": sum(1 for p in c if p["installed"])}, indent=2))
