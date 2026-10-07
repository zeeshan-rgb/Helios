"""Helios's job scheduler (blueprint phase 13): one place for background jobs — one-shot and
recurring, enable/disable, status, next run, failure state and execution history.

It is Helios's own layer (driven by the app's existing 15-second scheduler tick — no Windows Task
Scheduler), stored in helios.db (sched_jobs + sched_runs):

  - SYSTEM jobs keep their own specialised triggers and just report here:
      night_mode        — the night window ([night_mode] start/end, missed-night detection)
      morning_briefing  — [briefing] time, once a day, read aloud on wake
    Enabling/disabling one flips its setting; it can't be deleted.
  - USER jobs are scheduled by this module: project_health, research, learn (and extra
    morning_briefing / night_mode runs), with sched_util schedules ('daily 07:30', 'weekdays 09:00',
    'weekly mon 09:00', 'hourly', 'every 30m', 'every 2h') or 'once 2026-10-02T09:00' (one-shot).

Guarantees: one run per job at a time (a worker thread, never blocking the tick); a missed run
(PC asleep / Helios closed) runs once on catch-up — never a backlog; heavy jobs wait while Night
Mode runs; every run is recorded with its trigger and outcome; a job that fails 5 times in a row is
paused (and says so); a run cut short by a shutdown is recorded as interrupted. Job types are fixed,
safe Helios actions — a job can never run an arbitrary command or prompt.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta

from . import conf, db, sched_util

PAUSE_AFTER_FAILURES = 5
HISTORY_PER_JOB = 50
CATCH_UP_GRACE = timedelta(minutes=2)
SYSTEM_JOBS = ("night_mode", "morning_briefing")

_threads: dict[int, threading.Thread] = {}
_first_tick = True
_lock = threading.Lock()


# ------------------------------------------------------------------------------ job types

def _run_project_health(args: dict) -> tuple[str, str]:
    from . import health, projects
    try:
        reps = health.run_all(args.get("project") or None)
    except projects.Busy as e:
        return "skipped", str(e)
    return "ok", "; ".join(f"{r['project']}: {r['verdict']}" for r in reps) or "no active projects"


def _run_research(args: dict) -> tuple[str, str]:
    from . import research
    if not research.enabled():
        return "skipped", "research is off ([research] enabled = false)"
    reps = research.run(args.get("topics") or None)
    if reps and all(r["error"] for r in reps):
        return "failed", "; ".join(f"{r['topic']}: {r['error']}" for r in reps)[:500]
    new = sum(len(r["new"]) for r in reps)
    return "ok", f"{len(reps)} topic(s), {new} new finding(s)"


def _run_leads(args: dict) -> tuple[str, str]:
    from . import leads
    if not leads.enabled():
        return "skipped", "the lead finder is off ([leads] enabled = false)"
    reps = leads.run(args.get("services") or None)
    if reps and all(r["error"] for r in reps):
        return "failed", "; ".join(f"{r['service']}: {r['error']}" for r in reps)[:500]
    return "ok", f"{len(reps)} service(s), {sum(len(r['new']) for r in reps)} new lead(s)"


def _run_gmail_replies(args: dict) -> tuple[str, str]:
    from . import gmail
    if not gmail.enabled():
        return "skipped", "Gmail replies are off ([gmail] enabled = false)"
    if not gmail.signed_in():
        return "skipped", "Gmail isn't connected (helios gmail login)"
    out = gmail.check()
    if out["errors"] and not out["drafted"] and not out["skipped"]:
        return "failed", "; ".join(out["errors"])[:500]
    return "ok", (f"{len(out['drafted'])} reply draft(s), {out['skipped']} skipped"
                  + (f", {len(out['errors'])} error(s)" if out["errors"] else ""))


def _run_learn(args: dict) -> tuple[str, str]:
    from . import learning
    from .night_mode.common import online
    out = learning.learn_recent(int(args.get("days") or 1), use_llm=online())
    stored = sum(len(o["lessons"]) for o in out)
    return "ok", f"{len(out)} day(s), {stored} lesson(s) handled"


def _run_briefing(args: dict) -> tuple[str, str]:
    from . import briefing
    out = briefing.prepare()
    return "ok", f"briefing written: {out['path']}"


def _run_night_mode(args: dict) -> tuple[str, str]:
    from .night_mode import scheduler as night
    rec = night.run_night(only=args.get("only") or None, record_job=False)
    if rec.get("status") in ("busy", "duplicate"):
        return "skipped", rec.get("reason", rec["status"])
    return _night_status(rec["status"]), _night_summary(rec)


# key -> (title, runner, heavy: waits while Night Mode runs)
TYPES = {
    "project_health": ("Project health checks", _run_project_health, True),
    "research": ("Research", _run_research, True),
    "leads": ("Lead finder", _run_leads, True),
    "gmail_replies": ("Draft replies to client emails", _run_gmail_replies, False),
    "learn": ("Learn from conversations", _run_learn, False),
    "morning_briefing": ("Morning briefing", _run_briefing, False),
    "night_mode": ("Night Mode", _run_night_mode, True),
}


# ------------------------------------------------------------------------------ schedules

_ONCE = re.compile(r"once\s+(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})", re.I)


def valid_schedule(schedule: str) -> bool:
    s = (schedule or "").strip()
    if _ONCE.fullmatch(s):
        try:
            _once_at(s)
            return True
        except ValueError:
            return False
    return sched_util.valid_schedule(s)


def _once_at(schedule: str) -> datetime:
    m = _ONCE.fullmatch(schedule.strip())
    return datetime.fromisoformat(f"{m.group(1)}T{int(m.group(2)):02d}:{m.group(3)}")


def next_after(schedule: str, after: datetime) -> datetime | None:
    """Next run strictly after `after` (None = a one-shot that is already past)."""
    if _ONCE.fullmatch(schedule.strip()):
        at = _once_at(schedule)
        return at if at > after else None
    return sched_util.next_run_after(schedule, after)


# ------------------------------------------------------------------------------ storage

def _row(r) -> dict:
    d = dict(r)
    try:
        d["args"] = json.loads(d.get("args") or "{}")
    except Exception:
        d["args"] = {}
    return d


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


_ready_db = None


def _init() -> None:
    """Create the tables once per database file (the tick runs every 15 s)."""
    global _ready_db
    if _ready_db != db.DB_PATH:
        db.init()
        _ready_db = db.DB_PATH


def all_jobs() -> list[dict]:
    ensure_system_jobs()
    with db._conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM sched_jobs ORDER BY system DESC, id")]


def get(ref) -> dict | None:
    """By id or (case-insensitive) name."""
    ref = str(ref or "").strip()
    for j in all_jobs():
        if str(j["id"]) == ref or j["name"].lower() == ref.lower():
            return j
    return None


def _update(job_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db._conn() as c:
        c.execute(f"UPDATE sched_jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def history(ref=None, limit: int = 20) -> list[dict]:
    _init()
    with db._conn() as c:
        if ref is None:
            rows = c.execute("SELECT r.*, j.name FROM sched_runs r JOIN sched_jobs j ON j.id=r.job_id "
                             "ORDER BY r.id DESC LIMIT ?", (limit,))
        else:
            j = get(ref)
            if not j:
                return []
            rows = c.execute("SELECT r.*, j.name FROM sched_runs r JOIN sched_jobs j ON j.id=r.job_id "
                             "WHERE r.job_id=? ORDER BY r.id DESC LIMIT ?", (j["id"], limit))
        return [dict(r) for r in rows]


def _start_run(job: dict, trigger: str, started: datetime | None = None) -> int:
    with db._conn() as c:
        cur = c.execute("INSERT INTO sched_runs(job_id, started_at, status, trigger) VALUES(?,?,?,?)",
                        (job["id"], _iso(started or datetime.now()), "running", trigger))
        return cur.lastrowid


def _finish_run(job: dict, run_id: int, status: str, summary: str,
                finished: datetime | None = None, *, emit=None) -> None:
    now = finished or datetime.now()
    summary = (summary or "")[:2000]
    with db._conn() as c:
        c.execute("UPDATE sched_runs SET finished_at=?, status=?, summary=? WHERE id=?",
                  (_iso(now), status, summary, run_id))
        c.execute("DELETE FROM sched_runs WHERE job_id=? AND id NOT IN (SELECT id FROM sched_runs "
                  "WHERE job_id=? ORDER BY id DESC LIMIT ?)", (job["id"], job["id"], HISTORY_PER_JOB))
    fails = (int(job.get("fail_count") or 0) + 1) if status == "failed" else \
        (0 if status == "ok" else int(job.get("fail_count") or 0))
    fields = {"running": 0, "last_run": _iso(now), "last_status": status, "last_summary": summary[:500],
              "fail_count": fails}
    if fails >= PAUSE_AFTER_FAILURES and not job.get("system"):
        fields["enabled"] = 0
        fields["last_summary"] = f"PAUSED after {fails} failures in a row — {summary[:400]}"
        conf.log("jobs", f"{job['name']}: paused after {fails} consecutive failures")
        _notify(f"Helios paused the job '{job['name']}' after {fails} failures in a row.", emit)
    _update(job["id"], **fields)
    conf.log("jobs", f"{job['name']}: {status} — {summary[:160]}")


def _notify(msg: str, emit=None) -> None:
    try:
        if emit:
            emit("status", "⚠ " + msg)
        from . import notify
        notify.toast("Helios jobs", msg)
    except Exception:
        pass


# ------------------------------------------------------------------------------ system jobs

def _night_status(s: str) -> str:
    return {"completed": "ok", "completed with errors": "failed", "crashed": "failed",
            "missed": "missed", "interrupted": "interrupted"}.get(s, "skipped" if s.startswith("stopped")
                                                                    else s or "unknown")


def _night_summary(rec: dict) -> str:
    if rec.get("status") == "missed":
        return f"missed — {rec.get('reason', '')}"
    tasks = rec.get("tasks") or {}
    bad = [n for n, t in tasks.items() if t.get("status") == "failed"]
    return f"{rec.get('status')}: {len(tasks)} task(s)" + (f", failed: {', '.join(bad)}" if bad else "")


def _system_state(name: str, now: datetime) -> tuple[bool, str, datetime | None]:
    """(enabled, schedule text, next run) straight from the owning module's settings."""
    if name == "night_mode":
        from .night_mode import scheduler as night
        c = night.cfg()
        on = bool(c.get("enabled", False))
        sched = f"nightly {c.get('start', night.DEFAULT_START)}–{c.get('end', night.DEFAULT_END)} window"
        return on, sched, (night.next_window(now)[0] if on else None)
    from . import briefing
    on = briefing.enabled()
    t = briefing.brief_time(now.date())
    prepared = bool(briefing._state().get(now.date().isoformat(), {}).get("prepared"))
    nxt = t if (now < t or not prepared) else briefing.brief_time(now.date() + timedelta(days=1))
    return on, f"daily {t:%H:%M} (read aloud on first wake)", (max(nxt, now) if on else None)


def ensure_system_jobs(now: datetime | None = None) -> None:
    """Create/refresh the system rows so the listing mirrors their settings."""
    now = now or datetime.now()
    _init()
    with db._conn() as c:
        for name in SYSTEM_JOBS:
            on, sched, nxt = _system_state(name, now)
            row = c.execute("SELECT id FROM sched_jobs WHERE name=?", (name,)).fetchone()
            if row is None:
                c.execute("INSERT INTO sched_jobs(name, type, schedule, enabled, system, next_run, "
                          "created_at, source) VALUES(?,?,?,?,1,?,?, 'system')",
                          (name, name, sched, int(on), _iso(nxt), _iso(now)))
            else:
                c.execute("UPDATE sched_jobs SET schedule=?, enabled=?, next_run=? WHERE id=?",
                          (sched, int(on), _iso(nxt), row["id"]))


def record_system_run(name: str, status: str, summary: str, started: datetime | None = None,
                      finished: datetime | None = None, trigger: str = "system") -> None:
    """Night Mode / the briefing report their runs here (never raises)."""
    try:
        _init()
        ensure_system_jobs()
        job = get(name)
        if not job:
            return
        rid = _start_run(job, trigger, started)
        _finish_run(job, rid, status, summary, finished)
    except Exception as e:  # pragma: no cover
        conf.log("jobs", f"record_system_run({name}) failed: {e}")


# ------------------------------------------------------------------------------ user API

class JobError(ValueError):
    pass


def add(job_type: str, schedule: str, *, name: str | None = None, args: dict | None = None,
        source: str = "user", now: datetime | None = None) -> dict:
    now = now or datetime.now()
    if job_type not in TYPES:
        raise JobError(f"unknown job type {job_type!r} (use: {', '.join(TYPES)})")
    schedule = " ".join((schedule or "").split())
    if not valid_schedule(schedule):
        raise JobError("bad schedule — use 'daily HH:MM', 'weekdays HH:MM', 'weekly mon HH:MM', "
                       "'hourly', 'every 30m', 'every 2h' or 'once YYYY-MM-DDTHH:MM'")
    nxt = next_after(schedule, now)
    if nxt is None:
        raise JobError("that one-shot time is already in the past")
    args = {k: v for k, v in (args or {}).items() if v not in (None, "", [])}
    if job_type == "project_health" and args.get("project"):
        from . import projects
        if not projects.get(args["project"]):
            raise JobError(f"no project called {args['project']!r}")
    name = (name or f"{job_type}{('-' + str(args['project']).lower()) if args.get('project') else ''}"
            f"-{now:%m%d%H%M%S}").strip()[:60]
    if name.lower() in SYSTEM_JOBS:
        raise JobError(f"{name!r} is a system job name")
    _init()
    try:
        with db._conn() as c:
            c.execute("INSERT INTO sched_jobs(name, type, schedule, args, enabled, system, next_run, "
                      "created_at, source) VALUES(?,?,?,?,1,0,?,?,?)",
                      (name, job_type, schedule, json.dumps(args), _iso(nxt), _iso(now), source))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise JobError(f"a job called {name!r} already exists") from None
        raise
    conf.log("jobs", f"added {name}: {job_type} '{schedule}' next {_iso(nxt)} ({source})")
    return get(name)


def set_enabled(ref, on: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    job = get(ref)
    if not job:
        raise JobError(f"no job {ref!r}")
    if job["system"]:
        conf.update_settings({f"{job['name'] if job['name'] == 'night_mode' else 'briefing'}.enabled": bool(on)})
        ensure_system_jobs(now)
    else:
        fields = {"enabled": int(on)}
        if on:
            fields["fail_count"] = 0
            nxt = next_after(job["schedule"], now)
            fields["next_run"] = _iso(nxt)
            if nxt is None:
                raise JobError("that one-shot job has already run — add a new one")
        _update(job["id"], **fields)
    conf.log("jobs", f"{job['name']}: {'enabled' if on else 'disabled'}")
    return get(job["id"])


def remove(ref) -> dict:
    job = get(ref)
    if not job:
        raise JobError(f"no job {ref!r}")
    if job["system"]:
        raise JobError(f"{job['name']} is a system job — disable it instead")
    if job["running"]:
        raise JobError(f"{job['name']} is running — try again when it finishes")
    with db._conn() as c:
        c.execute("DELETE FROM sched_runs WHERE job_id=?", (job["id"],))
        c.execute("DELETE FROM sched_jobs WHERE id=?", (job["id"],))
    conf.log("jobs", f"removed {job['name']}")
    return job


def _execute(job: dict, trigger: str, emit=None) -> dict:
    """Run a job to completion in THIS thread and record it."""
    title, runner, _ = TYPES[job["type"]]
    rid = _start_run(job, trigger)
    _update(job["id"], running=1, last_status="running")
    conf.log("jobs", f"{job['name']}: start ({trigger})")
    try:
        status, summary = runner(job["args"])
    except Exception as e:                         # a job failing never takes the scheduler down
        status, summary = "failed", f"{type(e).__name__}: {e}"
    _finish_run(job, rid, status, summary, emit=emit)
    return {"job": job["name"], "status": status, "summary": summary}


def run_now(ref, emit=None) -> dict:
    """Run a job immediately (manual trigger), in the caller's thread."""
    job = get(ref)
    if not job:
        raise JobError(f"no job {ref!r}")
    if job["running"]:
        raise JobError(f"{job['name']} is already running")
    return _execute(job, "manual", emit)


# ------------------------------------------------------------------------------ the tick

def _night_running() -> bool:
    try:
        from .night_mode import scheduler as night
        return night.is_running()
    except Exception:
        return False


def _recover_interrupted() -> None:
    with db._conn() as c:
        stuck = [dict(r) for r in c.execute("SELECT * FROM sched_jobs WHERE running=1")]
        for j in stuck:
            c.execute("UPDATE sched_runs SET status='interrupted', finished_at=?, "
                      "summary='Helios stopped mid-run' WHERE job_id=? AND status='running'",
                      (_iso(datetime.now()), j["id"]))
            c.execute("UPDATE sched_jobs SET running=0, last_status='interrupted' WHERE id=?", (j["id"],))
            conf.log("jobs", f"{j['name']}: marked interrupted (Helios stopped mid-run)")


def tick(now: datetime | None = None, emit=None, start_thread=None) -> list[str]:
    """App scheduler hook: start every due user job on its own thread. Returns what it started."""
    global _first_tick
    now = now or datetime.now()
    _init()
    with _lock:
        if _first_tick:
            _first_tick = False
            _recover_interrupted()
        ensure_system_jobs(now)
        with db._conn() as c:
            due = [_row(r) for r in c.execute(
                "SELECT * FROM sched_jobs WHERE system=0 AND enabled=1 AND running=0 "
                "AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run", (_iso(now),))]
        started = []
        for job in due:
            late = now - datetime.fromisoformat(job["next_run"]) > CATCH_UP_GRACE
            trigger = "catch-up" if late else "schedule"
            nxt = next_after(job["schedule"], now)       # never a backlog: next from NOW
            _update(job["id"], next_run=_iso(nxt))
            if TYPES[job["type"]][2] and _night_running():
                rid = _start_run(job, trigger, now)
                _finish_run(job, rid, "skipped", "Night Mode was running — will run at the next time",
                            now, emit=emit)
                continue
            _update(job["id"], running=1, last_status="running")
            job["running"] = 1
            target = (lambda j=job, t=trigger: _execute(j, t, emit))
            (start_thread or (lambda f: threading.Thread(target=f, daemon=True,
                                                        name=f"job-{job['name']}").start()))(target)
            started.append(job["name"])
        return started


# ------------------------------------------------------------------------------ formatting

def _when(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    today = datetime.now().date()
    day = "today" if dt.date() == today else "tomorrow" if dt.date() == today + timedelta(days=1) \
        else dt.strftime("%a %d %b")
    return f"{day} {dt:%H:%M}"


def format_jobs(jobs: list[dict] | None = None) -> str:
    jobs = jobs if jobs is not None else all_jobs()
    if not jobs:
        return "No jobs."
    lines = []
    for j in jobs:
        state = "running" if j["running"] else ("on" if j["enabled"] else "off")
        tag = " [system]" if j["system"] else ""
        args = f" {json.dumps(j['args'])}" if j["args"] else ""
        lines.append(f"#{j['id']} {j['name']}{tag} — {TYPES.get(j['type'], (j['type'],))[0]}{args}")
        lines.append(f"    schedule: {j['schedule']} · {state} · next: {_when(j['next_run'])}")
        if j["last_run"]:
            fails = f" · {j['fail_count']} failure(s) in a row" if j["fail_count"] else ""
            lines.append(f"    last: {_when(j['last_run'])} {j['last_status']}{fails} — {(j['last_summary'] or '')[:140]}")
    return "\n".join(lines)


def format_history(rows: list[dict]) -> str:
    if not rows:
        return "No runs yet."
    return "\n".join(f"{r['started_at'][:16].replace('T', ' ')} {r['name']:18} {r['status']:11} "
                     f"({r['trigger']}) {(r['summary'] or '')[:120]}" for r in rows)
