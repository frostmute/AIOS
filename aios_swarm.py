#!/usr/bin/env python3
"""
AIOS Swarm — decompose a big task, run specialists in parallel, let them talk.

`run_team` already delegates, but it's one shallow round: up to three sequential
calls, no dependencies, no way for an agent to consult a peer mid-task. That's
fine for "check X then answer" and useless for "build me a thing".

A swarm instead:

  1. PLAN      an agent turns the request into a DAG of subtasks, each assigned to
               the specialist that suits it, with explicit dependencies.
  2. EXECUTE   independent subtasks run CONCURRENTLY; a subtask starts only once
               its dependencies have produced output.
  3. CONSULT   a running agent can emit `ASK <agent>: <question>` and gets a real
               answer back before finishing — this is the "agents talk to each
               other" part, not just output chaining.
  4. SYNTHESIZE one agent reads the whole blackboard and writes the final answer.

The hub injects `route` and `act` so this module never imports it back — that
would be circular, and it keeps the planner testable with a stub router.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

MAX_TASKS = 8          # a plan bigger than this is usually the model rambling
MAX_PARALLEL = 4       # each slot is a live model call; more just queues upstream
MAX_CONSULTS = 2       # per subtask, so a chatty agent can't loop forever

PLANNER_SYS = """You break a request into a plan of subtasks for a team of AI agents.

Available agents — pick the one that genuinely fits each subtask:
  brain       reasoning, writing, planning, synthesis, general knowledge
  opencode    reading/writing/running code, repos, builds, tests, file inspection
  claudecode  coding via Claude Code (good for careful, larger code changes)
  crewai      multi-step research or workflows that suit a crew of roles
  fabric      applying a known analysis pattern to text (summarise, extract wisdom)

Reply with ONLY a JSON object, no prose and no code fence:

{"summary": "<one line: what the team will do>",
 "tasks": [
   {"id": "t1", "agent": "opencode", "title": "<short label>",
    "prompt": "<the full self-contained instruction for that agent>", "deps": []},
   {"id": "t2", "agent": "brain", "title": "...", "prompt": "...", "deps": ["t1"]}
 ]}

Rules:
- 2 to 6 subtasks. If the request genuinely needs only one agent, return ONE task.
- `deps` lists task ids whose OUTPUT this subtask needs. Leave it empty when the
  subtask can start immediately — tasks with no deps run in parallel, so keep
  them independent.
- Each `prompt` must stand alone. The agent running it cannot see this plan.
- Don't add a final "combine the results" task; synthesis happens automatically."""

CONSULT_PROTOCOL = (
    "\n\nIf you need something only another agent can answer, write a line:\n"
    "ASK <agent>: <your question>\n"
    "(agents: brain, opencode, claudecode, crewai). You'll get the answer back and "
    "can continue. Use it only when you genuinely need it — otherwise just answer.")

_ASK = re.compile(r"^\s*ASK\s+([a-zA-Z-]+)\s*:\s*(.+)$", re.M)
AGENTS = {"brain", "opencode", "claudecode", "crewai", "fabric", "team"}


def _extract_json(text: str) -> dict | None:
    """Models fence JSON or wrap it in prose no matter how firmly you ask."""
    if not text:
        return None
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


def plan(message: str, ask_llm) -> dict:
    """ask_llm(messages, system) -> str. Returns a validated plan dict.

    The instructions ride in the USER message, not the system prompt. Backends
    that shell out to a CLI (claude-code) returned an empty body for a long
    system prompt full of JSON braces — costing 100s and yielding no plan. Put
    it in the user turn and every backend handles it the same way."""
    raw = ask_llm(
        [{"role": "user", "content": f"{PLANNER_SYS}\n\n--- REQUEST ---\n{message}"}],
        "You reply with a single JSON object and nothing else.")
    p = _extract_json(raw) or {}
    if not p:  # one retry, blunter, for models that narrated instead
        raw = ask_llm(
            [{"role": "user",
              "content": f"{PLANNER_SYS}\n\n--- REQUEST ---\n{message}\n\n"
                         "Output the JSON object now. No prose, no code fence."}], None)
        p = _extract_json(raw) or {}
    tasks, seen = [], set()
    for t in (p.get("tasks") or [])[:MAX_TASKS]:
        tid = str(t.get("id") or f"t{len(tasks)+1}").strip()
        agent = str(t.get("agent") or "brain").strip().lower()
        prompt = str(t.get("prompt") or "").strip()
        if not prompt or tid in seen:
            continue
        seen.add(tid)
        tasks.append({"id": tid, "agent": agent if agent in AGENTS else "brain",
                      "title": str(t.get("title") or prompt[:60])[:80],
                      "prompt": prompt,
                      "deps": [str(d) for d in (t.get("deps") or [])]})
    # Drop dependencies on tasks the planner never emitted, or nothing can start.
    ids = {t["id"] for t in tasks}
    for t in tasks:
        t["deps"] = [d for d in t["deps"] if d in ids and d != t["id"]]
    return {"summary": str(p.get("summary") or "")[:300], "tasks": tasks,
            "raw": "" if tasks else (raw or "")[:600]}


def _consult(text: str, route, act, budget: int) -> str:
    """Answer any `ASK <agent>:` lines. Returns the collected answers."""
    out = []
    for agent, question in _ASK.findall(text or "")[:budget]:
        agent = agent.strip().lower()
        if agent not in AGENTS:
            continue
        act("consult", f"{agent} ← {question.strip()[:100]}")
        try:
            answer = route(agent, question.strip())
        except Exception as e:
            answer = f"(peer '{agent}' unavailable: {e})"
        out.append(f"[{agent} replied] {answer[:2500]}")
    return "\n\n".join(out)


def run(message: str, route, ask_llm, act=lambda *a, **k: None,
        on_update=lambda s: None) -> dict:
    """Plan, execute the DAG in parallel with peer consultation, synthesize."""
    started = time.time()
    act("thinking", "Planning the work and picking agents…")
    p = plan(message, ask_llm)
    tasks = p["tasks"]

    if not tasks:
        # Some backends won't emit clean JSON — claude-code in particular is an
        # agent, not a chat model, and may return nothing for a planning prompt.
        # Report the failure so the caller can fall back, rather than inventing
        # a plan or handing back an empty reply.
        act("note", "Planner returned no usable plan — falling back to the team.")
        return {"ok": False, "swarm": False, "summary": "", "reply": "",
                "tasks": [], "reason": "planner produced no valid JSON plan",
                "planner_raw": p.get("raw", "")[:400]}
    if len(tasks) == 1:
        # Not actually a swarm — don't dress up a single call as one.
        t = tasks[0]
        act("agent", f"Single agent suffices: {t['agent']}")
        return {"ok": True, "swarm": False, "summary": p["summary"],
                "reply": route(t["agent"], t["prompt"]),
                "tasks": [{**t, "status": "done"}]}

    act("plan", f"{len(tasks)} subtasks across "
                f"{len(set(t['agent'] for t in tasks))} agents: {p['summary'][:120]}")

    state = {t["id"]: {**t, "status": "waiting", "output": "", "error": ""} for t in tasks}
    lock = threading.Lock()
    done: set[str] = set()

    def snapshot():
        return {"summary": p["summary"], "elapsed": round(time.time() - started, 1),
                "tasks": [{k: v for k, v in s.items() if k != "prompt"}
                          for s in state.values()]}

    def ready() -> list[str]:
        return [tid for tid, s in state.items()
                if s["status"] == "waiting" and all(d in done for d in s["deps"])]

    def execute(tid: str):
        s = state[tid]
        with lock:
            s["status"] = "running"
        on_update(snapshot())
        act("run", f"[{s['agent']}] {s['title']}")

        prompt = s["prompt"]
        if s["deps"]:  # hand dependents what their prerequisites produced
            ctx = "\n\n".join(
                f"--- output of '{state[d]['title']}' ({state[d]['agent']}) ---\n"
                f"{state[d]['output'][:4000]}" for d in s["deps"] if d in state)
            prompt = f"{prompt}\n\nContext from earlier subtasks:\n{ctx}"
        prompt += CONSULT_PROTOCOL

        try:
            out = route(s["agent"], prompt)
            answers = _consult(out, route, act, MAX_CONSULTS)
            if answers:  # let it finish now that it has what it asked for
                out = route(s["agent"],
                            f"{prompt}\n\nYour earlier draft:\n{out[:3000]}\n\n"
                            f"Answers from the agents you asked:\n{answers}\n\n"
                            "Now give your final result for this subtask.")
            with lock:
                s["output"], s["status"] = out or "", "done"
                done.add(tid)
            act("result", f"[{s['agent']}] {s['title']} — done")
        except Exception as e:
            with lock:
                s["error"], s["status"] = str(e)[:300], "error"
                done.add(tid)  # unblock dependents rather than deadlocking
            act("blocked", f"[{s['agent']}] {s['title']} failed", str(e)[:200])
        on_update(snapshot())

    # Wave-by-wave: everything currently unblocked runs at once.
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        guard = 0
        while len(done) < len(state) and guard < MAX_TASKS * 3:
            guard += 1
            batch = ready()
            if not batch:
                stuck = [t for t, s in state.items() if s["status"] == "waiting"]
                if stuck:  # circular deps the planner invented — run them anyway
                    batch = stuck[:MAX_PARALLEL]
                    for t in batch:
                        state[t]["deps"] = []
                else:
                    break
            if len(batch) > 1:
                act("parallel", f"Running {len(batch)} subtasks in parallel: " +
                    ", ".join(state[t]["agent"] for t in batch))
            list(pool.map(execute, batch[:MAX_PARALLEL]))

    act("thinking", "Synthesizing the team's work…")
    board = "\n\n".join(
        f"### {s['title']}  ({s['agent']})\n"
        f"{s['output'][:6000] if s['status'] == 'done' else 'FAILED: ' + s['error']}"
        for s in state.values())
    reply = ask_llm(
        [{"role": "user", "content": f"Original request:\n{message}\n\n"
                                     f"What the team produced:\n\n{board}"}],
        "You are the AI OS lead. Write the single final answer to the user's request "
        "from your team's work below. Integrate it into one coherent response — don't "
        "narrate the delegation or list who did what. If a subtask failed, work with "
        "what you have and say plainly what's missing.")
    ok_count = sum(1 for s in state.values() if s["status"] == "done" and not s["error"])
    act("done", f"Swarm complete — {ok_count}/{len(state)} subtasks succeeded")
    return {"ok": True, "swarm": True, "summary": p["summary"], "reply": reply,
            "elapsed": round(time.time() - started, 1),
            "tasks": [{k: v for k, v in s.items() if k != "prompt"} for s in state.values()]}


# --------------------------------------------------------------------------- #
# Is this big enough to be worth a swarm?                                      #
# --------------------------------------------------------------------------- #
_BIG_HINTS = re.compile(
    r"\b(and then|after that|also|then |as well as|plus |step \d|first.*then|"
    r"build|implement|refactor|migrate|audit|research|compare|analyse|analyze|"
    r"design|plan|investigate|end.to.end|full|entire|whole|everything)\b", re.I)


def looks_big(message: str) -> bool:
    """Cheap pre-filter so ordinary chat never pays for a planning call."""
    m = (message or "").strip()
    if len(m) < 40:
        return False
    signals = 0
    if len(m) > 120:
        signals += 1
    hints = len(_BIG_HINTS.findall(m))
    if hints >= 2:          # "build … and then … also …" — more than one ask
        signals += 1
    if hints >= 3:          # dense multi-verb requests are the real swarm case
        signals += 1
    if m.count("?") >= 2 or m.count("\n") >= 2:
        signals += 1
    if re.search(r"\b(\d+\s*(things|tasks|steps|parts)|swarm|team of agents)\b", m, re.I):
        signals += 2
    return signals >= 2
