"""Mission agents: the execution core of the multi-agent network.

A "mission" is one goal driven by a SUPERVISOR agent that decomposes it and spawns WORKER
agents (each its own `claude -p` process). Agents collaborate through a shared blackboard
(the mission_log table) — they read what teammates posted and post their own findings.

Process model: the supervisor runs in the app process (so it outlives a chat turn); workers
are spawned by the supervisor/worker that needs them, forming a process tree. Killing the
supervisor (panic) tears down the whole tree. Caps (depth, per-mission budget, global
concurrency) keep it from running away.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from . import agents, conf, db, router
from .proc_util import kill_pid, kill_tree  # re-exported: missions.py uses mission_agent.kill_*

CREATE_NO_WINDOW = 0x08000000


def _caps() -> dict:
    """Resolve the mission safety caps from the [agents] config section (with defaults).

    Caps keep a runaway mission from spawning forever: max_concurrent (global agents at once),
    max_depth (nesting/recursion limit), max_mission_agents (per-mission budget), plus the
    supervisor (mission_timeout) and per-worker (turn_timeout, from [claude]) timeouts in seconds.
    """
    a = conf.SETTINGS.get("agents", {}) if isinstance(conf.SETTINGS.get("agents"), dict) else {}

    def _int(key, default):
        # Coerce to int and floor at 1; on a bad/missing value floor the DEFAULT too (so the
        # contract holds even if a default were ever passed as 0/negative/non-int).
        try:
            return max(1, int(a.get(key, default)))
        except (TypeError, ValueError):
            return max(1, int(default))
    return {
        "max_concurrent": _int("max_concurrent", 5),
        "max_depth": _int("max_depth", 3),
        "max_mission_agents": _int("max_mission_agents", 8),
        "mission_timeout": _int("mission_timeout_sec", 1800),
        "turn_timeout": int(conf.SETTINGS.get("claude", {}).get("turn_timeout", 600)),
    }


def _claim_slot(run_id: int, timeout: float = 120.0) -> bool:
    """Atomically claim a global concurrency slot for this run, flipping it to 'running' ONLY if
    fewer than max_concurrent agents are already running — in a single SQL statement, so there's no
    TOCTOU window (the old read-count-then-set-running let a fan-out burst blow past the cap, since
    these run in separate per-agent MCP-server processes). Returns False if no slot frees up in time."""
    cap = _caps()["max_concurrent"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if db.try_claim_running_slot(run_id, cap):
                return True
        except Exception:
            # Transient DB hiccup: best-effort flip so a mission doesn't wedge forever.
            try:
                db.set_agent_run(run_id, status="running")
            except Exception:
                pass
            return True
        time.sleep(1.5)  # poll every 1.5s until a slot frees or we hit the deadline
    return False


def _blackboard(mission_id: int, limit: int = 30) -> str:
    """Render the last `limit` blackboard entries for a mission as text for the prompt.

    The blackboard is the mission_log table — agents post findings/status/spawn events here
    and read it to see teammates' work. Each entry is shown as "[role#id] kind: content"
    with content truncated to 600 chars to keep the prompt bounded.
    """
    try:
        log = db.read_mission_log(mission_id)
    except Exception:
        log = []
    if not log:
        return "(empty so far)"
    lines = []
    for e in log[-limit:]:
        who = f"{e['sender_role'] or '?'}#{e['sender_id'] if e['sender_id'] is not None else '-'}"
        lines.append(f"[{who}] {e['kind']}: {(e['content'] or '')[:600]}")
    return "\n".join(lines)


def _build(mission: dict, run: dict, custom_note: str | None):
    """Compose (system, prompt, model) for one mission agent."""
    role, depth, mid = run["role"], run["depth"], run["mission_id"]
    caps = _caps()
    can_spawn = depth < caps["max_depth"]  # deepest agents must do the work themselves, not delegate
    persona = agents.persona_for(role, custom_note)

    spawn_line = (
        "- spawn_agent(task, role, wait) and wait_for_agent(agent_id) — hand sub-tasks to helper "
        "agents (role = a preset specialist or 'auto'; wait=true to get the result inline)."
        if can_spawn else
        "- (You are at the maximum nesting depth — do this task yourself; do NOT spawn helpers.)"
    )
    finish_line = (
        "When the whole goal is satisfied, call mission_result(answer) with the final synthesized "
        "result for the user."
        if role == agents.SUPERVISOR else
        "When you finish, call post_finding(your_result) so teammates can use it, then reply with "
        "a short summary."
    )
    frame = (
        f"## Mission #{mid}\n"
        f"GOAL: {mission['goal']}\n\n"
        f"You are agent #{run['id']} on this mission — role: {role}, depth: {depth}.\n"
        f"YOUR TASK:\n{run['task']}\n\n"
        f"Shared blackboard (what the team has posted so far):\n{_blackboard(mid)}\n\n"
        "Team tools (talk to your teammates through these):\n"
        "- post_finding(content, to_role) — post a result or note to the shared blackboard.\n"
        "- read_mission() — re-read the blackboard for the latest posts.\n"
        f"{spawn_line}\n"
        f"{finish_line}"
    )
    system = persona + "\n\n" + frame  # persona (role focus) + this turn's mission framing
    model = router.choose_model(run["task"]).get("model", "sonnet")  # per-task model pick (see router.py)
    return system, run["task"], model


def execute(run_id: int, custom_note: str | None = None, register=None) -> str | None:
    """Run one mission agent to completion; record its result on the blackboard."""
    run = db.get_agent_run(run_id)
    if not run:
        return None
    mission = db.get_mission(run["mission_id"])
    if not mission or mission["status"] != "active":
        db.set_agent_run(run_id, status="cancelled")
        return None

    # Atomically claim a global concurrency slot (flips status to 'running') before launching
    # another claude -p process — no separate set-running step, so the cap can't be raced past.
    if not _claim_slot(run_id):
        db.set_agent_run(run_id, status="error", result="(no capacity)")
        db.add_mission_log(run["mission_id"], run_id, run["role"], "status", "gave up waiting for a slot")
        return None

    system, prompt, model = _build(mission, run, custom_note)
    caps = _caps()
    # Supervisors run long (they coordinate the whole team); workers get the shorter turn timeout.
    timeout = caps["mission_timeout"] if run["role"] == agents.SUPERVISOR else caps["turn_timeout"]

    if conf.brain_engine() in ("gemini", "antigravity"):
        from . import agy_cli, gemini_cli
        engine = agy_cli if conf.brain_engine() == "antigravity" else gemini_cli

        def _reg(proc):
            if register:
                try:
                    register(proc)
                except Exception:
                    pass
            try:
                db.set_agent_run(run_id, pid=proc.pid)   # so panic can kill it (see below)
            except Exception:
                pass

        identity = {"HELIOS_AGENT_ROLE": "side", "HELIOS_MISSION_ID": str(run["mission_id"]),
                    "HELIOS_AGENT_ID": str(run_id), "HELIOS_AGENT_DEPTH": str(run["depth"]),
                    "HELIOS_MISSION_ROLE": str(run["role"])}
        if engine is gemini_cli:   # agy_cli.run_agent appends its own engine note
            system = system + "\n\n" + gemini_cli.engine_note(list(gemini_cli.mcp_servers()))
        conf.log("mission", f"exec run#{run_id} role={run['role']} depth={run['depth']} "
                            f"{conf.brain_engine()}")
        result = engine.run_agent(prompt, system, model=engine.model_for(model=model),
                                  extra_env=identity, timeout=timeout, register=_reg,
                                  label=f"mission run#{run_id}")
        status = "done" if result else "error"
        db.set_agent_run(run_id, status=status, result=result or "(no output)")
        db.add_mission_log(run["mission_id"], run_id, run["role"], "result",
                           (result or "(no output)")[:4000])
        conf.log("mission", f"done run#{run_id} status={status}")
        return result

    mcp_configs = [str(conf.CONFIG_DIR / "mcp.json")]
    composio = conf.CONFIG_DIR / "composio_mcp.json"
    if composio.exists():
        mcp_configs.append(str(composio))

    if prompt.strip().startswith("/"):
        prompt = " " + prompt  # don't let claude -p treat it as a slash command

    args = [
        conf.CLAUDE_BIN, "-p", "--model", model, "--output-format", "json",
        "--append-system-prompt", system,
        "--mcp-config", *mcp_configs, "--strict-mcp-config",
        "--settings", str(conf.CONFIG_DIR / "claude_settings.json"),
        "--permission-mode", "default", "--add-dir", str(conf.vault_path()),
        "--plugin-dir", str(conf.ROOT / "agent_skills"),   # Helios's own SKILL.md skills
        "--max-turns", str(int(conf.SETTINGS.get("claude", {}).get("max_turns", 80))),
        "--fallback-model", router.fallback_for(model),
    ]
    # These env vars identify the agent to its in-process MCP team tools (post_finding,
    # spawn_agent, etc.) so a spawned worker knows which mission/agent/depth it belongs to.
    env = dict(os.environ)
    env["HELIOS_AGENT_ROLE"] = "side"            # screen-lock deferral, like any side agent
    env["HELIOS_MISSION_ID"] = str(run["mission_id"])
    env["HELIOS_AGENT_ID"] = str(run_id)
    env["HELIOS_AGENT_DEPTH"] = str(run["depth"])
    env["HELIOS_MISSION_ROLE"] = str(run["role"])

    conf.log("mission", f"exec run#{run_id} role={run['role']} depth={run['depth']} model={model}")
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW, cwd=str(conf.workspace_path()), env=env)
    except Exception as e:
        db.set_agent_run(run_id, status="error", result=f"(spawn failed: {e})")
        return None
    if register:
        try:
            register(proc)
        except Exception:
            pass
    try:
        # Record the worker PID so panic can kill it even when it was spawned inside a different
        # process subtree (a wait=False worker whose parent agent already exited) that taskkill /T
        # on the supervisor can't reach. See missions.MissionManager.panic.
        db.set_agent_run(run_id, pid=proc.pid)
    except Exception:
        pass

    try:
        out, err = proc.communicate(input=prompt + "\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        db.set_agent_run(run_id, status="error", result="(timed out)")
        db.add_mission_log(run["mission_id"], run_id, run["role"], "status", "timed out")
        return None
    except Exception as e:
        kill_tree(proc)
        db.set_agent_run(run_id, status="error", result=f"(error: {e})")
        return None

    # Parse the JSON envelope (--output-format json); fall back to the raw tail if not JSON.
    result = None
    out = (out or "").strip()
    if out:
        try:
            env_obj = json.loads(out)
            if isinstance(env_obj, dict):
                result = (env_obj.get("result") or "").strip() or None
        except Exception:
            result = out[-2000:]
    status = "done" if result else "error"
    db.set_agent_run(run_id, status=status, result=result or "(no output)")
    # Always record the outcome on the blackboard (capped at 4000 chars) so teammates see it.
    db.add_mission_log(run["mission_id"], run_id, run["role"], "result",
                       (result or "(no output)")[:4000])
    conf.log("mission", f"done run#{run_id} status={status}")
    return result


def spawn_worker(mission_id: int, parent_id: int | None, parent_depth: int, task: str,
                 role: str = "auto", custom_note: str | None = None, wait: bool = True):
    """Create + run a worker agent. Returns (run_id|None, message_or_result).

    Enforces the depth and per-mission budget caps. wait=True runs it inline and returns its
    result; wait=False starts it in the background and returns once it's launched.
    """
    caps = _caps()
    depth = parent_depth + 1
    if depth > caps["max_depth"]:
        return None, (f"Spawn refused — maximum nesting depth ({caps['max_depth']}) reached. "
                      "Do this part yourself.")
    if db.count_mission_agents(mission_id) >= caps["max_mission_agents"]:
        return None, (f"Spawn refused — this mission already has {caps['max_mission_agents']} "
                      "agents (budget reached). Do this part yourself or wrap up.")

    # custom_note -> ad-hoc helper: store a short free-text role label (<=24 chars).
    # Otherwise keep a recognized role as-is, or let agents.resolve() route 'auto'/unknown.
    if custom_note:
        stored_role = (role or "helper").strip().lower()[:24] or "helper"
    else:
        stored_role = role if (role in agents.SPECIALISTS or role == agents.SUPERVISOR) \
            else agents.resolve(role, task)
    run_id = db.add_agent_run(mission_id, stored_role, task, depth, parent_id, status="starting")
    # Insert-then-verify so concurrent wait=False spawns can't both pass the pre-check above and
    # overshoot the budget: if OUR insert pushed the (cancelled-excluding) count over, cancel it.
    if db.count_mission_agents(mission_id) > caps["max_mission_agents"]:
        db.set_agent_run(run_id, status="cancelled", result="(over mission agent budget)")
        return None, (f"Spawn refused — this mission already has {caps['max_mission_agents']} "
                      "agents (budget reached). Do this part yourself or wrap up.")
    db.add_mission_log(mission_id, parent_id, None, "spawn",
                       f"spawned {stored_role} #{run_id}: {task[:160]}")

    if wait:
        # Inline: block on the helper so the caller can use its result in the next step.
        result = execute(run_id, custom_note=custom_note)
        return run_id, (result or "(the agent returned no result)")
    # Background: fire-and-forget on a daemon thread; caller polls later via wait_for().
    threading.Thread(target=execute, args=(run_id,),
                     kwargs={"custom_note": custom_note}, daemon=True).start()
    return run_id, f"agent #{run_id} ({stored_role}) started in the background"


def wait_for(run_id: int, timeout: float = 1500.0) -> str | None:
    """Block until a (background) mission agent finishes; return its result."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = db.get_agent_run(run_id)
        if not run:
            return None
        if run["status"] in ("done", "error", "cancelled"):
            return run["result"]
        time.sleep(2.0)  # poll the run's status every 2s until it's terminal or we time out
    return "(still running — timed out waiting)"
