#!/usr/bin/env python3
"""
AIOS × Paperclip — the company layer.

Paperclip (paperclipai/paperclip) runs AI agents like a company: org chart,
tickets with dependencies, goals, budgets, heartbeats, board approvals and an
audit trail. AIOS already has the agents. This module makes them Paperclip's
employees, so the two halves become one system:

    You / chat / swarm ──► Paperclip ticket ──► heartbeat ──► AIOS agent does it
                                   ▲                                 │
                                   └──── comment + status=done ◄─────┘

The roster, hired idempotently by bootstrap():

    AIOS Brain   CEO        http adapter → hub /api/paperclip/heartbeat
    opencode     CTO        http adapter → hub (target opencode)
    CrewAI       Researcher http adapter → hub (target crewai)
    Claude Code  Engineer   Paperclip's NATIVE claude_local adapter
    Codex        Engineer   Paperclip's NATIVE codex_local adapter

Two things about Paperclip shape how this works, and both were found by probing
the live API rather than assumed:

  * Every agent write (checkout, comment, status change) must carry the id of a
    heartbeat run that is still queued/running — a long-lived agent key alone
    gets "Agent run id required". So the hub does the work INSIDE the webhook
    request and only returns once the ticket is resolved; returning early would
    close the run and every write after it would be refused.
  * Its HTTP adapter refuses private/loopback targets (an SSRF guard) unless the
    exact origin is listed in PAPERCLIP_HTTP_ADAPTER_PRIVATE_ENDPOINT_ALLOWLIST,
    which aios sets to the hub's origin when it launches Paperclip.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("AIOS_ROOT", Path(__file__).resolve().parent))
BASE = os.environ.get("AIOS_PAPERCLIP_URL", "http://127.0.0.1:3100").rstrip("/")
STATE_FILE = ROOT / ".aios" / "paperclip_aios.json"
WORKSPACE = ROOT / ".aios" / "workspace"
COMPANY_NAME = os.environ.get("AIOS_PAPERCLIP_COMPANY", "The AI OS")
COMPANY_BLURB = "Eleven open-source AI projects run as one company. Managed by The AI OS."

# slug -> how to hire it. `target` is the AIOS route() target the hub dispatches to.
ROSTER = [
    {"slug": "brain", "name": "AIOS Brain", "role": "ceo", "title": "Chief Executive",
     "capabilities": "Plans, delegates and decides. Big tasks become a parallel swarm "
                     "of specialists; small ones are answered directly.",
     "adapter": "http", "target": "team"},
    {"slug": "opencode", "name": "opencode", "role": "cto", "title": "CTO",
     "capabilities": "Writes, runs and fixes code across repositories.",
     "adapter": "http", "target": "opencode"},
    {"slug": "crewai", "name": "CrewAI", "role": "researcher", "title": "Head of Research",
     "capabilities": "Multi-step research and analysis run by a crew of roles.",
     "adapter": "http", "target": "crewai"},
    {"slug": "claude", "name": "Claude Code", "role": "engineer", "title": "Senior Engineer",
     "capabilities": "Careful, larger code changes with Claude Code.",
     "adapter": "claude_local"},
    {"slug": "codex", "name": "Codex", "role": "engineer", "title": "Engineer",
     "capabilities": "Code changes and reviews with OpenAI Codex.",
     "adapter": "codex_local"},
]


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #
class PaperclipError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Paperclip {status}: {message}")
        self.status = status


def api(method: str, path: str, body: dict | None = None, key: str = "",
        run_id: str = "", timeout: int = 30):
    url = BASE + "/api" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if run_id:
        req.add_header("X-Paperclip-Run-Id", run_id)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", str(e))
        except Exception:
            msg = str(e)
        raise PaperclipError(e.code, msg) from None


def health() -> dict:
    try:
        h = api("GET", "/health", timeout=5)
        return {"up": True, "version": h.get("version"), "mode": h.get("deploymentMode")}
    except Exception as e:
        return {"up": False, "error": str(e)[:200]}


# --------------------------------------------------------------------------- #
# State (company id, agent ids, per-agent keys)                               #
# --------------------------------------------------------------------------- #
_lock = threading.Lock()


def state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    try:  # agent API keys live here
        os.chmod(STATE_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def ui_url(path: str = "") -> str:
    return BASE + path


# --------------------------------------------------------------------------- #
# Bootstrap — idempotent: finds what exists, creates only what's missing       #
# --------------------------------------------------------------------------- #
def _native_exe(name: str) -> str:
    """The real .exe behind an npm `.cmd` shim, or "".

    Paperclip runs local adapters through cmd.exe and quotes the command itself;
    a shim under "Program Files (x86)" comes out double-quoted and cmd reports
    it "is not recognized". Spawning the vendored native binary needs no shell.
    """
    shim = shutil.which(name)
    if not shim or not shim.lower().endswith((".cmd", ".bat")):
        return ""
    root = Path(shim).parent
    try:
        text = Path(shim).read_text(errors="ignore")
    except OSError:
        text = ""
    for rel in re.findall(r'%dp0%\\([^"%]+?\.exe)', text, re.I):
        if Path(rel).stem.lower() == name and (root / rel).is_file():
            return str(root / rel)
    pkg = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code"}.get(name)
    if pkg:
        for hit in sorted((root / "node_modules" / pkg).glob(f"**/{name}.exe")):
            return str(hit)
    return ""


def _adapter_config(member: dict, hub_url: str, hub_token: str) -> dict:
    if member["adapter"] == "http":
        return {
            "url": hub_url.rstrip("/") + "/api/paperclip/heartbeat",
            "method": "POST",
            # The hub resolves the whole ticket inside the request (see module
            # docstring), so there must be no client-side timeout.
            "timeoutMs": 0,
            "headers": {"X-AIOS-Token": hub_token} if hub_token else {},
            "payloadTemplate": {"aiosTarget": member["target"], "aiosSlug": member["slug"]},
        }
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    cfg = {"cwd": str(WORKSPACE), "timeoutSec": 1800}
    if os.name == "nt":
        # Paperclip defaults these adapters to its ACP engine, which spawns
        # `codex-acp.cmd` / `claude-acp.cmd` directly. Node refuses to spawn a
        # .cmd without a shell on Windows (the CVE-2024-27980 hardening), so every
        # run failed with "Failed to spawn agent command". The CLI engine works.
        cfg["engine"] = "cli"
        if member["adapter"] == "codex_local":
            # Paperclip seeds a per-company CODEX_HOME by symlinking ~/.codex/auth.json.
            # On Windows its "is this already our symlink?" check compares a
            # \\?\-prefixed readlink() result against a plain path, never matches,
            # and the second run dies with EEXIST. A user-supplied CODEX_HOME is
            # treated as self-managed, so point it at the real one and skip seeding.
            cfg["env"] = {"CODEX_HOME": str(Path.home() / ".codex")}
        exe = _native_exe("codex" if member["adapter"] == "codex_local" else "claude")
        if exe:
            cfg["command"] = exe
    if member["adapter"] == "claude_local":
        cfg["dangerouslySkipPermissions"] = True   # headless: nobody to click approve
    return cfg


def bootstrap(hub_url: str = "http://127.0.0.1:8787", hub_token: str = "") -> dict:
    with _lock:
        if not health()["up"]:
            return {"ok": False, "error": f"Paperclip is not running at {BASE} — `aios start paperclip`"}
        st = state()
        created: list[str] = []

        # Company — reuse by id, then by name, else create.
        company = None
        if st.get("company_id"):
            try:
                company = api("GET", f"/companies/{st['company_id']}")
            except PaperclipError:
                company = None
        if not company:
            company = next((c for c in api("GET", "/companies")
                            if c.get("name") == COMPANY_NAME), None)
        if not company:
            company = api("POST", "/companies", {
                "name": COMPANY_NAME, "description": COMPANY_BLURB})
            created.append("company")
        elif company.get("description") != COMPANY_BLURB:
            api("PATCH", f"/companies/{company['id']}", {"description": COMPANY_BLURB})
        cid = company["id"]
        st["company_id"] = cid
        st.setdefault("agents", {})

        existing = {a["name"]: a for a in api("GET", f"/companies/{cid}/agents")}
        ceo_id = None
        for m in ROSTER:
            rec = st["agents"].get(m["slug"], {})
            agent = None
            if rec.get("id"):
                try:
                    agent = api("GET", f"/agents/{rec['id']}")
                    if agent.get("status") == "terminated":
                        agent = None
                except PaperclipError:
                    agent = None
            agent = agent or existing.get(m["name"])
            body = {"name": m["name"], "role": m["role"], "title": m["title"],
                    "capabilities": m["capabilities"], "adapterType": m["adapter"],
                    "adapterConfig": _adapter_config(m, hub_url, hub_token)}
            if ceo_id and m["slug"] != "brain":
                body["reportsTo"] = ceo_id
            if agent:
                # Keep the wiring current (hub URL/token can change between installs).
                agent = api("PATCH", f"/agents/{agent['id']}",
                            {k: body[k] for k in ("adapterConfig", "capabilities", "title")})
            else:
                agent = api("POST", f"/companies/{cid}/agents", body)
                created.append(m["slug"])
            if m["slug"] == "brain":
                ceo_id = agent["id"]
            rec.update({"id": agent["id"], "name": m["name"], "adapter": m["adapter"],
                        "target": m.get("target", "")})
            # Only hub-driven agents need a key: the hub writes back on their behalf.
            if m["adapter"] == "http" and not rec.get("key"):
                k = api("POST", f"/agents/{agent['id']}/keys", {"name": "aios-hub"})
                rec["key"] = k.get("key") or k.get("token") or ""
            st["agents"][m["slug"]] = rec

        st["bootstrapped_at"] = time.time()
        _save(st)
        return {"ok": True, "company_id": cid, "created": created,
                "agents": {s: {"id": a["id"], "adapter": a["adapter"]}
                           for s, a in st["agents"].items()}}


# --------------------------------------------------------------------------- #
# Read side — what the dashboard shows                                         #
# --------------------------------------------------------------------------- #
def overview() -> dict:
    h = health()
    st = state()
    out = {"health": h, "bootstrapped": bool(st.get("company_id")), "ui": ui_url(),
           "company": None, "agents": [], "issues": [], "counts": {}}
    if not (h["up"] and st.get("company_id")):
        return out
    cid = st["company_id"]
    try:
        out["company"] = api("GET", f"/companies/{cid}")
        slug_by_id = {v["id"]: k for k, v in st.get("agents", {}).items()}
        for a in api("GET", f"/companies/{cid}/agents"):
            out["agents"].append({
                "id": a["id"], "name": a.get("name"), "title": a.get("title"),
                "role": a.get("role"), "status": a.get("status"),
                "adapter": a.get("adapterType"), "reportsTo": a.get("reportsTo"),
                "slug": slug_by_id.get(a["id"], ""),
                "spentMonthlyCents": a.get("spentMonthlyCents"),
                "budgetMonthlyCents": a.get("budgetMonthlyCents")})
        issues = api("GET", f"/companies/{cid}/issues")
        issues = issues if isinstance(issues, list) else issues.get("items", [])
        names = {a["id"]: a["name"] for a in out["agents"]}
        for i in issues[:60]:
            out["issues"].append({
                "id": i["id"], "key": i.get("identifier"), "title": i.get("title"),
                "status": i.get("status"), "priority": i.get("priority"),
                "assignee": names.get(i.get("assigneeAgentId"), ""),
                "parentId": i.get("parentId"), "updatedAt": i.get("updatedAt")})
        for i in out["issues"]:
            out["counts"][i["status"]] = out["counts"].get(i["status"], 0) + 1
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


# --------------------------------------------------------------------------- #
# Write side — hand work to the company                                        #
# --------------------------------------------------------------------------- #
def wake(agent_id: str) -> None:
    """Board-side invoke: wake an agent now rather than at its next scheduled
    heartbeat. Fire-and-forget — the ticket carries the result."""
    def _go():
        try:
            api("POST", f"/agents/{agent_id}/heartbeat/invoke", {}, timeout=15)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def create_task(title: str, description: str = "", assignee: str = "brain",
                priority: str = "medium", invoke: bool = True) -> dict:
    st = state()
    if not st.get("company_id"):
        return {"ok": False, "error": "Paperclip isn't bootstrapped — `aios paperclip setup`"}
    agent = st.get("agents", {}).get(assignee) or st["agents"].get("brain")
    issue = api("POST", f"/companies/{st['company_id']}/issues", {
        "title": title[:200], "description": description or title,
        "status": "todo", "priority": priority, "assigneeAgentId": agent["id"]})
    run = None
    if invoke:
        wake(agent["id"])
        run = "invoked"
    return {"ok": True, "issue": {"id": issue["id"], "key": issue.get("identifier"),
                                  "title": issue.get("title")},
            "assignee": agent.get("name"), "heartbeat": run,
            "url": ui_url(f"/issues/{issue['id']}")}


def issue_detail(issue_id: str) -> dict:
    """One ticket plus its comment thread, for the hub's Company view."""
    issue = api("GET", f"/issues/{issue_id}")
    comments = api("GET", f"/issues/{issue_id}/comments")
    names = {v["id"]: k for k, v in state().get("agents", {}).items()}
    return {"ok": True, "issue": {
        "id": issue["id"], "key": issue.get("identifier"), "title": issue.get("title"),
        "description": issue.get("description") or "", "status": issue.get("status"),
        "priority": issue.get("priority"), "url": ui_url(f"/issues/{issue['id']}")},
        "comments": [{"body": c.get("body", ""), "at": c.get("createdAt"),
                      "by": names.get(c.get("authorAgentId"), "board")} for c in comments]}


def _status_body(status: str, why: str = "") -> dict:
    """PATCH body for a status change. Paperclip refuses a bare `blocked` — it
    wants a blocker, a pending approval, or an unblockDescriptor saying who acts."""
    body = {"status": status}
    if status == "blocked":
        body["unblockDescriptor"] = {"owner": "board", "action": (
            "Review the agent's comment, fix the cause, then reopen this ticket. "
            + (why or "")[:1500]).strip()}
    return body


def set_status(issue_id: str, status: str) -> dict:
    if status not in ("todo", "in_progress", "done", "blocked", "cancelled"):
        return {"ok": False, "error": f"bad status {status!r}"}
    issue = api("PATCH", f"/issues/{issue_id}", _status_body(status, "Blocked from the AIOS hub."))
    if status == "todo" and (issue or {}).get("assigneeAgentId"):
        wake(issue["assigneeAgentId"])   # reopened: have the assignee pick it back up
    return {"ok": True, "status": status}


def record_swarm(message: str, result: dict) -> dict | None:
    """Mirror a finished AIOS swarm into Paperclip as a parent ticket with one
    child per subtask, so the company's history includes work done outside it."""
    st = state()
    if not (st.get("company_id") and result.get("swarm")):
        return None
    try:
        cid, agents = st["company_id"], st.get("agents", {})
        target_to_agent = {"opencode": "opencode", "crewai": "crewai", "claudecode": "claude",
                           "codex": "codex", "brain": "brain", "team": "brain"}
        parent = api("POST", f"/companies/{cid}/issues", {
            "title": ("Swarm: " + (result.get("summary") or message))[:200],
            "description": message, "status": "done", "priority": "medium",
            "assigneeAgentId": agents.get("brain", {}).get("id")})
        for t in result.get("tasks", []):
            slug = target_to_agent.get(t.get("agent", ""), "brain")
            child = api("POST", f"/companies/{cid}/issues", {
                "title": t.get("title", "subtask")[:200],
                "description": f"Swarm subtask run by {t.get('agent')}.",
                **_status_body("done" if t.get("status") == "done" else "blocked",
                               t.get("error") or ""),
                "parentId": parent["id"],
                "assigneeAgentId": agents.get(slug, agents.get("brain", {})).get("id")})
            body = (t.get("output") or t.get("error") or "")[:20000]
            if body:
                api("POST", f"/issues/{child['id']}/comments", {"body": body})
        if result.get("reply"):
            api("POST", f"/issues/{parent['id']}/comments",
                {"body": result["reply"][:20000]})
        return {"parent": parent["id"], "key": parent.get("identifier")}
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Heartbeat — Paperclip wakes a hub-driven agent; AIOS does the actual work     #
# --------------------------------------------------------------------------- #
ACTIVE_STATUSES = ("todo", "in_progress")
# Paperclip can wake the same agent several times for one ticket (the assignment
# itself, an explicit invoke, a retry). Without this, each wake re-did the work
# the previous one was still doing.
_WORKING: set = set()
_WORKING_LOCK = threading.Lock()


def _issues_for(agent_id: str) -> list[dict]:
    cid = state().get("company_id")
    items = api("GET", f"/companies/{cid}/issues?assigneeAgentId={agent_id}")
    items = items if isinstance(items, list) else items.get("items", [])
    return [i for i in items if i.get("assigneeAgentId") == agent_id
            and i.get("status") in ACTIVE_STATUSES]


def handle_heartbeat(payload: dict, route, act=lambda *a, **k: None) -> dict:
    """Resolve every open ticket assigned to the woken agent, inside the run.

    `route(target, prompt)` is the hub's router. Returns a summary that becomes
    the run's result in Paperclip."""
    st = state()
    run_id = payload.get("runId") or ""
    agent_id = payload.get("agentId") or ""
    slug = payload.get("aiosSlug") or next(
        (s for s, a in st.get("agents", {}).items() if a.get("id") == agent_id), "")
    rec = st.get("agents", {}).get(slug, {})
    key, target = rec.get("key", ""), payload.get("aiosTarget") or rec.get("target") or "brain"
    if not (run_id and key):
        return {"ok": False, "error": "heartbeat missing runId or agent key — re-run "
                                      "`aios paperclip setup`"}

    ctx = payload.get("context") or {}
    wanted = ctx.get("taskId") or ctx.get("issueId")
    try:
        issues = _issues_for(agent_id)
    except Exception as e:
        return {"ok": False, "error": f"could not list tickets: {e}"}
    # Only fresh work, plus an in-progress ticket this wake was explicitly about.
    issues = [i for i in issues if i.get("status") == "todo" or i["id"] == wanted]
    if wanted:
        issues = [i for i in issues if i["id"] == wanted] or issues
    with _WORKING_LOCK:
        issues = [i for i in issues if i["id"] not in _WORKING][:3]
        _WORKING.update(i["id"] for i in issues)
    if not issues:
        return {"ok": True, "handled": 0, "note": "no unclaimed tickets"}
    try:
        return _work(issues, agent_id, key, run_id, target, route, act)
    finally:
        with _WORKING_LOCK:
            _WORKING.difference_update(i["id"] for i in issues)


def _work(issues, agent_id, key, run_id, target, route, act) -> dict:
    done = []
    for issue in issues:
        label = issue.get("identifier") or issue["id"][:8]
        try:
            if issue.get("status") == "todo":
                api("POST", f"/issues/{issue['id']}/checkout",
                    {"agentId": agent_id, "expectedStatuses": ["todo"]},
                    key=key, run_id=run_id)
        except PaperclipError as e:
            if e.status == 409:   # another agent owns it — Paperclip says don't retry
                continue
            raise
        act("agent", f"Paperclip {label} → {target}: {issue.get('title', '')[:80]}")
        prompt = issue.get("title", "")
        if issue.get("description") and issue["description"] != prompt:
            prompt += "\n\n" + issue["description"]
        # Recent human comments are part of the brief.
        try:
            comments = api("GET", f"/issues/{issue['id']}/comments", key=key, run_id=run_id)
            comments = comments if isinstance(comments, list) else comments.get("items", [])
            human = [c.get("body", "") for c in comments[-5:] if not c.get("authorAgentId")]
            if human:
                prompt += "\n\nComments on the ticket:\n" + "\n---\n".join(human)
        except Exception:
            pass
        try:
            reply = str(route(target, prompt) or "").strip() or "(the agent returned no output)"
            api("POST", f"/issues/{issue['id']}/comments", {"body": reply[:60000]},
                key=key, run_id=run_id)
            # An error OR an empty answer is not "done" — marking it done would
            # tell the org the work happened when it didn't.
            failed = reply.lstrip().startswith("⚠️") or reply.startswith("(the agent returned")
            api("PATCH", f"/issues/{issue['id']}",
                _status_body("blocked" if failed else "done", reply), key=key, run_id=run_id)
            done.append({"key": label, "status": "blocked" if failed else "done"})
        except Exception as e:
            try:
                api("POST", f"/issues/{issue['id']}/comments",
                    {"body": f"AIOS could not finish this: {e}"}, key=key, run_id=run_id)
                api("PATCH", f"/issues/{issue['id']}", _status_body("blocked", str(e)),
                    key=key, run_id=run_id)
            except Exception:
                pass
            done.append({"key": label, "status": "error", "error": str(e)[:200]})
    return {"ok": True, "handled": len(done), "tickets": done}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        print(json.dumps(bootstrap(os.environ.get("AIOS_HUB_URL", "http://127.0.0.1:8787"),
                                   os.environ.get("AIOS_HUB_TOKEN", "")), indent=2))
    else:
        o = overview()
        print(json.dumps({k: o[k] for k in ("health", "bootstrapped", "counts")}, indent=2))
