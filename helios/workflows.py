"""Native workflow engine: a trigger + an ordered list of steps whose text outputs feed later
steps. Generalizes the `routines` primitive (one prompt on a schedule) into multi-step flows.

A workflow is a JSON document ``{name, trigger, steps}``. Steps run top-to-bottom; each step's
output is stored under its ``id`` and referenced by later steps as ``{{id.output}}`` / ``{{id}}``
(plus ``{{trigger.*}}``). ``agent``/``brain`` steps execute through ``side_agent.run_task`` — so
every tool they call is still gated by the PreToolUse hook and they inherit the no-outbound
side-agent rule, making unattended runs safe by construction. See ``docs/WORKFLOWS.md``.

Wired in app.py beside the side-agent pool + MissionManager; the Scheduler calls tick() each poll
(scheduled triggers) and the server calls run_async() for a manual "run now".
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta

from . import agents, conf, db, notify, permissions, sched_util, side_agent
from .proc_util import kill_tree as _kill

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}")
_MAX_DELAY = 3600.0      # cap a delay step (1h) so a bad value can't park a run forever
_STEP_OUTPUT_CAP = 4000  # cap a stored step output so a run row / downstream prompt can't balloon
_STEP_TYPES = {"agent", "brain", "notify", "http", "delay", "condition"}


def validate_spec(spec) -> tuple[bool, str]:
    """Return (ok, error) for a workflow spec — a name, a trigger, and >=1 well-formed step with a
    unique id. Used by the create/save paths so a malformed flow is rejected before it's stored."""
    if not isinstance(spec, dict):
        return False, "spec must be an object"
    if not str(spec.get("name", "")).strip():
        return False, "workflow needs a name"
    trig = spec.get("trigger") or {}
    if not isinstance(trig, dict) or trig.get("type") not in ("manual", "schedule", "webhook"):
        return False, "trigger.type must be manual, schedule, or webhook"
    if trig.get("type") == "schedule" and not sched_util.valid_schedule(str(trig.get("schedule", ""))):
        return False, ("schedule is invalid (e.g. 'daily 08:00', 'weekly sun 12:00', "
                       "'every 30m', 'hourly')")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return False, "workflow needs at least one step"
    seen: set[str] = set()
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return False, f"step {i} must be an object"
        sid = str(s.get("id", "")).strip()
        if not sid or not re.fullmatch(r"[A-Za-z0-9_\-]+", sid):
            return False, f"step {i} needs an id of letters/digits/_/-"
        if sid in seen:
            return False, f"duplicate step id '{sid}'"
        seen.add(sid)
        if s.get("type") not in _STEP_TYPES:
            return False, f"step '{sid}' has an unknown type '{s.get('type')}'"
    return True, ""


def schedule_of(spec: dict) -> str:
    """The schedule string for a scheduled-trigger spec, else '' (manual/webhook)."""
    trig = spec.get("trigger") or {}
    if isinstance(trig, dict) and trig.get("type") == "schedule":
        return str(trig.get("schedule", "")).strip()
    return ""


class WorkflowManager:
    """Runs workflows: scheduled ones via tick() (from the Scheduler) and manual ones via
    run_async() (from the server). Each run executes on its own daemon thread."""

    def __init__(self, pool, emit):
        self.pool = pool            # unused for exec (steps call side_agent directly) but kept for parity
        self.emit = emit            # hub.publish(kind, data)
        self._lock = threading.Lock()
        self._procs: set = set()    # live side-agent processes, so panic() can kill in-flight steps
        try:
            self._persona = agents.persona_for(agents.GENERALIST)
        except Exception:
            self._persona = ""

    # ---- triggers ---------------------------------------------------------------------------
    def tick(self, now: datetime) -> None:
        """Called by the scheduler each poll: fire due scheduled workflows, advancing next_run
        FIRST (like routines) so a bad schedule can never wedge the loop in a tight retry."""
        for wf in db.due_workflows(now.isoformat(timespec="seconds")):
            try:
                nxt = sched_util.next_run_after(wf["schedule"], now)
            except Exception as e:  # pragma: no cover — clamps make this unlikely
                conf.log("workflows", f"bad schedule wf #{wf['id']}: {e}; deferring 1h")
                nxt = now + timedelta(hours=1)
            db.set_workflow_next(wf["id"], nxt.isoformat(timespec="seconds"))
            self.emit("status", f"▶ Running workflow '{wf['name']}'…")
            self.run_async(wf["id"], trigger="schedule")

    def run_async(self, wid: int, trigger: str = "manual") -> int | None:
        """Start a workflow run on a background thread. Returns the run id, or None if the
        workflow is missing/corrupt."""
        wf = db.get_workflow(wid)
        if not wf:
            return None
        try:
            spec = json.loads(wf["spec"])
        except Exception:
            conf.log("workflows", f"wf #{wid} spec is not valid JSON")
            return None
        run_id = db.add_workflow_run(wid, trigger=trigger)
        threading.Thread(target=self._execute, args=(wid, run_id, spec, trigger), daemon=True).start()
        return run_id

    # ---- execution --------------------------------------------------------------------------
    def _publish(self, wid: int, run_id: int, status: str, step: str | None = None) -> None:
        try:
            self.emit("workflow", {"workflow_id": wid, "run_id": run_id, "status": status, "step": step})
        except Exception:
            pass

    def _execute(self, wid: int, run_id: int, spec: dict, trigger: str) -> None:
        ctx: dict = {"trigger": {"type": trigger}, "steps": {}}
        log: list[dict] = []
        status, final = "done", ""
        self._publish(wid, run_id, "running")
        conf.log("workflows", f"run #{run_id} wf #{wid} start ({trigger})")
        for step in spec.get("steps", []):
            if conf.ABORT_FLAG.exists():          # panic — stop before the next step
                status = "stopped"
                break
            sid, stype = step.get("id"), step.get("type")
            entry = {"id": sid, "type": stype, "status": "running"}
            log.append(entry)
            db.set_workflow_run(run_id, log=json.dumps(log))
            self._publish(wid, run_id, "running", step=sid)
            try:
                out = (self._run_step(step, ctx) or "")[:_STEP_OUTPUT_CAP]
                ctx["steps"][sid] = out
                entry["output"] = out[:500]
                if stype == "condition" and out == "":   # a failed guard halts the flow cleanly
                    entry["status"] = "stopped"
                    status = "stopped"
                    db.set_workflow_run(run_id, log=json.dumps(log))
                    break
                entry["status"] = "done"
                if out:
                    final = out
            except Exception as e:
                entry["status"] = "error"
                entry["error"] = str(e)[:300]
                status = "error"
                db.set_workflow_run(run_id, log=json.dumps(log))
                conf.log("workflows", f"run #{run_id} step '{sid}' error: {e}")
                break
            db.set_workflow_run(run_id, log=json.dumps(log))
        db.set_workflow_run(run_id, status=status, log=json.dumps(log),
                            result=final[:_STEP_OUTPUT_CAP], finished=True)
        self._publish(wid, run_id, status)
        conf.log("workflows", f"run #{run_id} wf #{wid} {status}")

    def _run_step(self, step: dict, ctx: dict) -> str:
        stype = step.get("type")
        if stype == "agent":
            role = agents.normalize(step.get("agent") or "auto")
            return self._run_agent(self._resolve(step.get("prompt", ""), ctx),
                                   agents.persona_for(role), f"workflow:{step.get('id')}")
        if stype == "brain":
            return self._run_agent(self._resolve(step.get("prompt", ""), ctx),
                                   self._persona, f"workflow:{step.get('id')}")
        if stype == "notify":
            title = (self._resolve(step.get("title", "Helios"), ctx) or "Helios")[:64]
            msg = self._resolve(step.get("message", ""), ctx)
            notify.toast(title, msg[:256])
            try:
                self.emit("status", f"🔔 {msg}")
            except Exception:
                pass
            return msg
        if stype == "http":
            return self._http(step, ctx)
        if stype == "delay":
            try:
                secs = float(step.get("seconds", 0))
            except (TypeError, ValueError):
                secs = 0.0
            time.sleep(max(0.0, min(_MAX_DELAY, secs)))
            return ""
        if stype == "condition":
            return self._condition(step, ctx)
        raise ValueError(f"unknown step type '{stype}'")

    def _run_agent(self, prompt: str, persona: str, label: str) -> str | None:
        """Run an agent/brain step synchronously, tracking its process so panic() can kill it."""
        if conf.ABORT_FLAG.exists():
            return None
        holder: dict = {}

        def reg(proc):
            holder["p"] = proc
            with self._lock:
                self._procs.add(proc)

        try:
            return side_agent.run_task(prompt, persona, label=label, register=reg)
        finally:
            p = holder.get("p")
            if p is not None:
                with self._lock:
                    self._procs.discard(p)

    def _http(self, step: dict, ctx: dict) -> str:
        url = self._resolve(step.get("url", ""), ctx).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("http step needs an http(s) url")
        if permissions.is_internal_url(url):
            raise ValueError("blocked internal/loopback URL (SSRF)")
        body = self._resolve(step.get("body", ""), ctx)
        req = urllib.request.Request(
            url, data=body.encode("utf-8") if body else None,
            method=str(step.get("method", "GET")).upper())
        req.add_header("User-Agent", "Helios-workflow")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(100_000).decode("utf-8", "replace")

    def _condition(self, step: dict, ctx: dict) -> str:
        """Return 'true' when the check passes, '' when it fails. An empty result halts the flow."""
        src = self._resolve(str(step.get("source", "")), ctx)
        val = self._resolve(str(step.get("value", "")), ctx)
        op = str(step.get("op", "not_empty"))
        ok = {
            "contains": val.lower() in src.lower(),
            "equals": src.strip() == val.strip(),
            "not_empty": bool(src.strip()),
            "exists": bool(src.strip()),
        }.get(op, bool(src.strip()))
        return "true" if ok else ""

    def _resolve(self, template, ctx: dict) -> str:
        """Substitute {{step_id}} / {{step_id.output}} / {{trigger.field}} references."""
        if not isinstance(template, str):
            return str(template)
        if "{{" not in template:
            return template

        def sub(m):
            head, _, tail = m.group(1).partition(".")
            if head == "trigger":
                return str((ctx.get("trigger") or {}).get(tail, ""))
            return str(ctx["steps"].get(head, ""))   # {{id}} and {{id.output}} both -> the output
        return _VAR_RE.sub(sub, template)

    # ---- teardown ---------------------------------------------------------------------------
    def panic(self) -> None:
        """Kill every in-flight workflow step process (called from the app's panic). Runs also
        stop between steps via the abort.flag check in _execute."""
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()
        for p in procs:
            _kill(p)
        if procs:
            conf.log("workflows", f"panic: killed {len(procs)} in-flight step(s)")
