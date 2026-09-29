"""Side agents: background tasks run as their own headless `claude -p` processes.

The interactive brain (orchestrator) stays free to talk to the user while these run in
parallel. Each side agent is isolated (fresh session, no memory write-back) and tagged
`HELIOS_AGENT_ROLE=side` so its computer-use server defers to the orchestrator and to
the user's own activity (one agent drives the screen at a time — see the screen lock).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading

from . import agents, conf, db, memory, router
from .proc_util import kill_tree as _kill  # shared process-tree kill (was a local duplicate)

CREATE_NO_WINDOW = 0x08000000

_SIDE_NOTE = (
    "\n\nYou are running as a BACKGROUND side agent — NOT the live chat. Work autonomously, "
    "finish the task, then reply with a short result summary (a few sentences). Another agent "
    "may be using the screen: if a tool says the screen is busy or that the user is active, wait a "
    "moment and try again — do NOT retry in a tight loop."
)


def run_task(prompt: str, persona: str, *, label: str = "task", register=None) -> str | None:
    """Run one background task in a separate Claude process. Returns the final text, or None."""
    # router (see router.py) picks the model and may rewrite the prompt to a cleaner "message".
    route = router.choose_model(prompt)
    msg = (route.get("message") or prompt).strip()
    if msg.startswith("/"):
        msg = " " + msg  # leading space: keep claude -p from treating it as a slash command

    # Build the system prompt: persona (specialist focus) + live tone override + the
    # background-agent rules + a memory digest relevant to this prompt (see memory.py).
    system = persona
    try:
        tone = db.get_state("tone")
    except Exception:
        tone = None
    if tone:
        system += f"\n\nCURRENT TONE (the user adjusted this on the fly): {tone}"
    system += _SIDE_NOTE
    system += "\n\n" + memory.build_digest(prompt)

    timeout = int(conf.SETTINGS.get("claude", {}).get("turn_timeout", 600))  # seconds; default 10 min
    if conf.brain_engine() == "antigravity":
        from . import agy_cli
        model = agy_cli.model_for(route.get("tier"), route.get("model"))
        conf.log("side_agent", f"start [{label}] agy model={model or 'default'}")
        return agy_cli.run_agent(msg, system, model=model, extra_env={"HELIOS_AGENT_ROLE": "side"},
                                 timeout=timeout, register=register, label=label)
    if conf.brain_engine() == "gemini":
        from . import gemini_cli
        model = gemini_cli.model_for(route.get("tier"), route.get("model"))
        system += "\n\n" + gemini_cli.engine_note(list(gemini_cli.mcp_servers()))
        conf.log("side_agent", f"start [{label}] gemini model={model}")
        return gemini_cli.run_agent(msg, system, model=model,
                                    extra_env={"HELIOS_AGENT_ROLE": "side"},
                                    timeout=timeout, register=register, label=label)

    mcp_configs = [str(conf.CONFIG_DIR / "mcp.json")]
    composio_cfg = conf.CONFIG_DIR / "composio_mcp.json"
    if composio_cfg.exists():
        mcp_configs.append(str(composio_cfg))

    args = [
        conf.CLAUDE_BIN, "-p",
        "--model", route["model"],
        "--output-format", "json",
        "--append-system-prompt", system,
        "--mcp-config", *mcp_configs, "--strict-mcp-config",
        "--settings", str(conf.CONFIG_DIR / "claude_settings.json"),
        "--permission-mode", "default",
        "--add-dir", str(conf.vault_path()),
        "--plugin-dir", str(conf.ROOT / "agent_skills"),   # Helios's own SKILL.md skills
        "--max-turns", str(int(conf.SETTINGS.get("claude", {}).get("max_turns", 80))),
        "--fallback-model", router.fallback_for(route["model"]),
    ]
    env = dict(os.environ)
    env["HELIOS_AGENT_ROLE"] = "side"  # its computer-use server defers to you + the orchestrator

    conf.log("side_agent", f"start [{label}] model={route['model']}")
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW, cwd=str(conf.workspace_path()), env=env,
        )
    except Exception as e:
        conf.log("side_agent", f"spawn failed [{label}]: {e}")
        return None
    if register:
        try:
            register(proc)
        except Exception:
            pass

    try:
        out, err = proc.communicate(input=msg + "\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        conf.log("side_agent", f"timeout [{label}]")
        return None
    except Exception as e:
        _kill(proc)
        conf.log("side_agent", f"run error [{label}]: {e}")
        return None

    out = (out or "").strip()
    if not out:
        conf.log("side_agent", f"empty output [{label}] rc={proc.returncode} err={(err or '')[:200]}")
        return None
    # --output-format json: parse the envelope and pull the .result field (the final reply).
    try:
        env_obj = json.loads(out)
        if isinstance(env_obj, dict):
            return (env_obj.get("result") or "").strip() or None
    except Exception:
        return out[-2000:]  # non-json fallback: return the tail (last 2000 chars)
    return None


class SideAgentPool:
    """Runs background tasks as side agents, capped at `max_parallel` concurrent processes."""

    def __init__(self, emit, max_parallel: int = 3):
        """emit: UI callback (event, payload). max_parallel: concurrent process cap (>=1).

        The semaphore enforces the cap; _lock guards _active/_procs (touched from worker
        threads + panic). _procs tracks live processes so panic() can kill them all.
        """
        self.emit = emit
        self.max = max(1, int(max_parallel))
        self._sem = threading.Semaphore(self.max)  # gate: at most `max` tasks run at once
        self._lock = threading.Lock()
        self._active = 0
        self._procs: set = set()
        self._persona = (conf.CONFIG_DIR / "persona_helios.md").read_text(encoding="utf-8")

    def available(self) -> int:
        """How many more side agents could start right now (cap minus active)."""
        with self._lock:
            return self.max - self._active

    def submit(self, label: str, prompt: str, on_done=None, agent=None) -> None:
        """Queue a background task (returns immediately).

        Runs on a daemon thread; the thread blocks on the semaphore if the pool is full.
        on_done(text) is invoked with the result when finished; agent selects the
        specialist ('auto'/name/None — resolved via agents.resolve).
        """
        # Reserve the slot at SUBMIT time (not when the worker finally starts) so available()
        # reflects submitted-but-not-yet-started work and a caller (e.g. the scheduler) can't
        # over-submit within one tick, spawning a pile of parked daemon threads.
        with self._lock:
            self._active += 1
        threading.Thread(target=self._run, args=(label, prompt, on_done, agent), daemon=True).start()

    def _run(self, label: str, prompt: str, on_done, agent=None) -> None:
        """Worker body: acquire a slot, run the task, then always release + deregister. The slot
        was already reserved in submit() (self._active), so we don't increment it again here."""
        self._sem.acquire()  # blocks here until a concurrency slot frees up
        holder: dict = {}  # captures the Popen so the finally block can deregister it

        def reg(proc):
            holder["p"] = proc
            with self._lock:
                self._procs.add(proc)

        # Pick the specialist (explicit name, or 'auto' -> heuristic) and use its persona.
        specialist = agents.resolve(agent, prompt)
        persona = agents.persona_for(specialist)
        if specialist != agents.GENERALIST:
            label = f"{label} · {specialist}"
            try:
                self.emit("status", f"🤝 Handed to the {specialist} agent.")
            except Exception:
                pass
        try:
            if conf.ABORT_FLAG.exists():  # panic engaged — don't launch a fresh claude -p
                conf.log("side_agent", f"skipped [{label}] — panic engaged")
                return
            text = run_task(prompt, persona, label=label, register=reg)
            if on_done:
                try:
                    on_done(text)
                except Exception as e:  # pragma: no cover
                    conf.log("side_agent", f"on_done error [{label}]: {e}")
        finally:
            p = holder.get("p")
            if p is not None:
                with self._lock:
                    self._procs.discard(p)
            with self._lock:
                self._active -= 1
            self._sem.release()

    def panic(self) -> None:
        """Kill every running side agent (called from the app's panic)."""
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()  # keep the set authoritative; workers' finally also discards
        for p in procs:
            _kill(p)
        conf.log("side_agent", f"panic: killed {len(procs)} side agent(s)")
