"""Background engine (runs in the app process): polls the DB and fires reminders,
runs due routines through the brain, and processes queued background jobs.

Single daemon thread on a 15s tick. Schedule strings are parsed by sched_util; reminders
and routines are persisted in db; user-facing pings go out via notify (Windows toast) and
self.emit (SSE to the UI). Also dispatches queued multi-agent mission supervisors and runs
proactive observers (battery/disk/Downloads thresholds, quiet hours) + opt-in screen checks.
"""

from __future__ import annotations

import ctypes
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import conf, db, notify, sched_util


def _battery() -> tuple[int, bool]:
    """(percent 0-100 or 255 if unknown, charging?).

    Reads the live power state via the Win32 GetSystemPowerStatus API (kernel32) into a
    SYSTEM_POWER_STATUS struct. 255 is Windows' own "unknown/no battery" sentinel for pct;
    we also return (255, True) if the call fails so the battery alert can't false-fire."""
    # ctypes mirror of Win32 SYSTEM_POWER_STATUS; only `pct`/`ac` are used below.
    # These are BYTE (unsigned) — c_ubyte, NOT c_byte: the 255 "unknown" sentinel must read as
    # 255, not -1, or the `pct < 255` guard fails and a desktop false-fires "Battery at -1%".
    class SPS(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                    ("r", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
    s = SPS()
    try:
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
        return s.pct, s.ac == 1
    except Exception:
        return 255, True


class Scheduler:
    """Owns the polling loop and all proactive behaviour. Wired up in app.py with the brain,
    the side-agent pool (background work), the mission manager, and an emit callback."""

    def __init__(self, brain, pool, missions, workflows, emit):
        self.brain = brain          # orchestrator (interactive turns only)
        self.pool = pool            # SideAgentPool — background work runs here, in parallel
        self.missions = missions    # MissionManager — runs multi-agent mission supervisors
        self.workflows = workflows  # WorkflowManager — fires due scheduled workflows
        self.emit = emit            # hub.publish(kind, data)
        self._stop = threading.Event()
        self._next_observe = datetime.now() + timedelta(minutes=1)
        self._next_screen = datetime.now() + timedelta(minutes=2)

    def start(self) -> None:
        """Init the DB and launch the polling loop on a daemon thread (dies with the app)."""
        db.init()
        orphaned = db.fail_orphaned_agents()  # crash/restart left agents stuck running -> fail them
        if orphaned:
            conf.log("scheduler", f"failed {orphaned} orphaned agent_run(s) from a previous run")
        wf_orphans = db.fail_orphaned_workflow_runs()  # same for workflow runs left mid-flight
        if wf_orphans:
            conf.log("scheduler", f"failed {wf_orphans} interrupted workflow run(s) from a previous run")
        threading.Thread(target=self._loop, daemon=True).start()
        conf.log("scheduler", "started")

    def stop(self) -> None:
        """Signal the loop to exit (it checks the event each iteration and on its 15s wait)."""
        self._stop.set()

    def _loop(self) -> None:
        # Tick forever; never let one bad tick kill the thread. `_stop.wait(15)` doubles as
        # both the inter-tick delay and a prompt, interruptible response to stop().
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # pragma: no cover
                conf.log("scheduler", f"tick error: {e}")
            self._stop.wait(15)     # poll every 15s

    def _tick(self) -> None:
        """One poll cycle: fire due reminders/routines, drain queued jobs + mission
        supervisors, then run the slower proactive observers and screen check on their
        own sub-intervals."""
        now = datetime.now()
        now_iso = now.isoformat(timespec="seconds")

        # 1) due reminders -> toast + chat
        for r in db.due_reminders(now_iso):
            db.mark_reminder_fired(r["id"])
            notify.toast("⏰ Reminder", r["text"])
            self.emit("status", f"⏰ Reminder: {r['text']}")

        # 2) due routines -> side-agent pool (run in parallel; never blocks the chat)
        for rt in db.due_routines(now_iso):
            # Always advance next_run FIRST (even if parsing is odd) so a bad schedule
            # can never wedge the scheduler in a tight retry loop.
            try:
                nxt = sched_util.next_run_after(rt["schedule"], now)
            except Exception as e:  # pragma: no cover — clamps make this unlikely
                conf.log("scheduler", f"bad schedule routine #{rt['id']}: {e}; deferring 1h")
                nxt = now + timedelta(hours=1)
            db.set_routine_next(rt["id"], nxt.isoformat(timespec="seconds"))
            self.emit("status", f"▶ Running routine '{rt['name']}'…")
            notify.toast("Helios routine", rt["name"])
            self._submit_routine(rt)

        # 3) queued background jobs -> pool, up to its free capacity
        slots = self.pool.available()
        while slots > 0:
            job = db.next_queued_job()
            if not job:
                break
            db.update_job(job["id"], "running")
            self._submit_job(job)
            slots -= 1

        # 3a-stale) Fail supervisors that have sat queued too long (e.g. queued by the MCP server
        # while the app was down — see helios_server.start_mission) so a stale mission can't
        # silently spring to life hours later. Fresh ones (queued seconds ago) fall through to 3b.
        stale_before = (now - timedelta(minutes=60)).isoformat(timespec="seconds")
        for run in db.queued_supervisors():
            if (run.get("created_at") or "") < stale_before:
                db.set_agent_run(run["id"], status="error", result="(never dispatched — app was down)")
                db.set_mission(run["mission_id"], status="error")
                conf.log("scheduler", f"failed stale queued mission supervisor #{run['id']}")

        # 3b) multi-agent mission supervisors waiting to start -> MissionManager (background)
        for run in db.queued_supervisors():
            db.set_agent_run(run["id"], status="starting")  # claim so we don't re-dispatch it
            self.missions.run_supervisor(run["id"])

        # 3c) due scheduled workflows -> WorkflowManager (advances next_run + runs in background)
        self.workflows.tick(now)

        # 3d) Night Mode: heartbeat (sleep detection) + start tonight's run in its window /
        # record a missed night. Runs on its own thread; never blocks this tick.
        try:
            from . import night_mode
            night_mode.tick(now, emit=self.emit, stop=self._stop)
        except Exception as e:  # pragma: no cover
            conf.log("scheduler", f"night mode tick error: {e}")

        # 3e) Morning briefing: prepared once a day at/after [briefing].time (after the night tick,
        # so a missed night is already on record).
        try:
            from . import briefing
            briefing.tick(now, emit=self.emit)
        except Exception as e:  # pragma: no cover
            conf.log("scheduler", f"briefing tick error: {e}")

        # 3f) Scheduled jobs (helios/jobs.py): due user jobs start on their own threads.
        try:
            from . import jobs
            jobs.tick(now, emit=self.emit)
        except Exception as e:  # pragma: no cover
            conf.log("scheduler", f"jobs tick error: {e}")

        # 4) proactive observers (~every 5 min)
        if datetime.now() >= self._next_observe:
            self._next_observe = datetime.now() + timedelta(minutes=5)
            try:
                self._observe()
            except Exception as e:  # pragma: no cover
                conf.log("scheduler", f"observe error: {e}")

        # 5) opt-in screen awareness (off by default; toggled via set_screen_awareness)
        if db.get_state("screen_awareness") == "on" and datetime.now() >= self._next_screen \
                and not self._in_quiet():
            iv = int(conf.SETTINGS.get("screen", {}).get("interval_min", 10))
            self._next_screen = datetime.now() + timedelta(minutes=iv)
            self._screen_check()

    def _observe(self) -> None:
        """Proactive system checks (~every 5 min): warn on low battery while unplugged, low
        free disk on C:, and a cluttered Downloads folder. Skipped entirely during quiet
        hours or when proactive mode is disabled; each alert is rate-limited in _alert."""
        cfg = conf.proactive_cfg()
        if not cfg.get("enabled", True) or self._in_quiet():
            return
        pct, charging = _battery()
        if pct < 255 and not charging and pct <= int(cfg.get("battery_pct", 20)):
            self._alert("battery", f"\U0001f50b Battery at {pct}% and unplugged, sir — worth charging.")
        try:
            free_gb = shutil.disk_usage("C:\\").free / 1e9
            if free_gb < float(cfg.get("disk_gb", 10)):
                self._alert("disk", f"\U0001f4be Low disk space — only {free_gb:.0f} GB free on C:.")
        except Exception:
            pass
        dl = Path(r"C:\Downloads")
        if not dl.exists():
            dl = Path.home() / "Downloads"
        try:
            n = sum(1 for _ in dl.iterdir())
            if n > int(cfg.get("downloads_count", 60)):
                self._alert("downloads", f"\U0001f5c2 Your Downloads has {n} items — want me to tidy it?")
        except Exception:
            pass

    def _alert(self, key: str, msg: str) -> None:
        """Toast+emit a proactive alert, but at most once per 6h per `key` (the last-fired
        timestamp is persisted in db state) so observers don't nag every cycle."""
        last = db.get_state(f"alert_{key}")
        now = datetime.now()
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=6):
                    return
            except Exception:
                pass
        db.set_state(f"alert_{key}", now.isoformat(timespec="seconds"))
        notify.toast("Helios", msg)
        self.emit("status", msg)

    def _in_quiet(self) -> bool:
        """True if now is inside the configured quiet-hours window. Handles the common
        overnight case where the window wraps midnight (qs > qe, e.g. 22:00 -> 08:00)."""
        cfg = conf.proactive_cfg()
        h = datetime.now().hour
        qs, qe = int(cfg.get("quiet_start", 22)), int(cfg.get("quiet_end", 8))
        if qs == qe:
            return False  # equal start==end = quiet hours disabled (explicit, not an empty range)
        return (qs <= h or h < qe) if qs > qe else (qs <= h < qe)

    def _screen_check(self) -> None:
        """Opt-in screen awareness: ask a side agent to glance at one screenshot and only
        speak up if it can genuinely help. The 'nothing' sentinel (and any trivially short
        reply) is suppressed so this stays quiet unless there's something worth saying."""
        prompt = ("Take ONE screenshot of my screen. If, and only if, there's something "
                  "genuinely useful you could proactively help with based on what you see, "
                  "reply with a single short sentence offering it. Otherwise reply with exactly: nothing")

        def done(text):
            t = (text or "").strip()
            if t and t.lower().strip(". ") != "nothing" and len(t) > 4:
                notify.toast("Helios noticed", t)
                self.emit("status", "\U0001f441 " + t)
        self.pool.submit(label="screen check", prompt=prompt, on_done=done, agent="generalist")

    def _submit_routine(self, rt: dict) -> None:
        """Run a due routine's prompt on the side-agent pool; toast its result if any."""
        def done(text, rt=rt):
            if text:
                notify.toast(f"Helios — {rt['name']}", text)
        self.pool.submit(label=f"routine: {rt['name']}", prompt=rt["prompt"], on_done=done, agent="auto")

    def _submit_job(self, job: dict) -> None:
        """Run a queued background job on the pool and write its terminal state back to db
        (done/error). A None result from the pool means the side agent failed or timed out."""
        jid = job["id"]
        self.emit("status", f"▶ Background job #{jid} started…")

        def done(text, jid=jid):
            if text is None:                       # side agent failed/timed out
                db.update_job(jid, "error", "no result (failed or timed out)")
                self.emit("status", f"✗ Background job #{jid} failed.")
                return
            db.update_job(jid, "done", text or "(done)")
            notify.toast(f"Helios — job #{jid} done", text or "Done.")
            self.emit("status", f"✓ Background job #{jid} finished.")
        self.pool.submit(label=f"job #{jid}", prompt=job["prompt"], on_done=done,
                         agent=job.get("agent", "auto"))
