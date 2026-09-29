"""Mission manager (runs in the app process).

Owns the lifecycle of mission SUPERVISORS: it runs each supervisor in a background thread (so
it outlives the chat turn that launched it), tracks the supervisor process so panic can kill
the whole mission tree, and notifies the user when a mission finishes. Workers are spawned by the
supervisor itself (see mission_agent), forming a process tree underneath it.
"""

from __future__ import annotations

import threading

from . import conf, db, mission_agent, notify


class MissionManager:
    """Tracks and supervises running missions inside the app process.

    One instance per app. start_mission lives elsewhere (it creates the DB rows, see db.py);
    this class owns running the SUPERVISOR agent and tearing missions down on panic. The
    supervisor's workers are spawned in mission_agent.spawn_worker, forming a process tree.
    """

    def __init__(self, emit):
        """emit: UI callback (event, payload). _procs holds live supervisor processes so
        panic() can kill each tree; _lock guards it (touched from worker threads + panic)."""
        self.emit = emit
        self._procs: set = set()
        self._lock = threading.Lock()

    def run_supervisor(self, run_id: int) -> None:
        """Launch a mission supervisor on a daemon thread so it outlives the chat turn."""
        threading.Thread(target=self._run, args=(run_id,), daemon=True).start()

    def _run(self, run_id: int) -> None:
        """Run one supervisor to completion: announce, execute, then finalize + notify the user."""
        run = db.get_agent_run(run_id)
        if not run:
            return
        mid = run["mission_id"]
        goal = (db.get_mission(mid) or {}).get("goal", "")
        self.emit("status", f"🧭 Mission #{mid} underway: {goal[:80]}")
        self.emit("mission", {"id": mid, "status": "active"})

        def reg(proc):
            with self._lock:
                self._procs.add(proc)

        result = None
        try:
            # Blocks until the supervisor process exits; register() records its proc for panic.
            result = mission_agent.execute(run_id, register=reg)
        except Exception as e:  # pragma: no cover
            conf.log("missions", f"supervisor run#{run_id} error: {e}")
        finally:
            m = db.get_mission(mid)
            if m and m["status"] == "active":
                # Supervisor ended without calling mission_result -> use its final text.
                db.set_mission(mid, status="done", result=result or "(mission ended)")
            # Prefer the result the supervisor stored via mission_result; fall back to its text.
            final = (db.get_mission(mid) or {}).get("result") or result or "Done."
            notify.toast(f"Helios — mission #{mid} done", str(final)[:200])  # toast body capped at 200
            self.emit("status", f"✅ Mission #{mid} complete.")
            self.emit("mission", {"id": mid, "status": "done"})

    def panic(self) -> None:
        """Kill every running supervisor tree and mark active missions cancelled."""
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()
        for p in procs:
            mission_agent.kill_tree(p)
        # Also kill workers by their recorded PID: a wait=False worker is spawned inside a
        # different process subtree (the MCP-server child of whatever agent called spawn_agent),
        # so once that parent exits, taskkill /T on the supervisor can't reach it. The DB has
        # every running agent's PID (mission_agent.execute records it).
        killed_pids = 0
        try:
            for pid in db.running_agent_pids():
                mission_agent.kill_pid(pid)
                killed_pids += 1
        except Exception:
            pass
        try:
            for m in db.list_missions(50):
                if m["status"] == "active":
                    db.set_mission(m["id"], status="cancelled")
        except Exception:
            pass
        conf.log("missions", f"panic: killed {len(procs)} supervisor tree(s) + {killed_pids} worker pid(s)")
