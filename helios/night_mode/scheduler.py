"""Night Mode scheduling and the run itself.

Config ([night_mode] in settings.toml):

    enabled = true
    start = "01:00"          # window start ...
    end = "05:00"            # ... and hard end: no task starts after this
    [night_mode.schedule]    # task -> earliest start time (remove a line to turn a task off)
    sync_projects = "01:00"
    run_checks = "01:15"
    ...

Guarantees (the blueprint's list):
  - clear start/end boundaries: tasks wait for their time, and nothing starts after `end`;
  - every operation is logged (logs/night.log + the run record's own log);
  - each task is isolated: an exception fails that task only, the rest still run;
  - never silent: a crashed run, a skipped task and a missed night are all recorded and reported;
  - no duplicate runs: one scheduled run per night (run record) + a cross-process lock;
  - asleep/offline detection: a heartbeat from the app's scheduler tick records gaps, and a missed
    night says whether the PC was off, asleep, or Helios wasn't running; tasks know if we're online.
Night Mode writes only to data/, logs/ and the vault (common.safe_write) — never Helios's code.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import conf
from . import (conversation_analyzer, memory_extractor, morning_report, project_scanner,
               research_agent, skill_builder, test_runner)
from .common import Result, online, safe_write

TASKS = {
    "sync_projects": (project_scanner.sync_projects, "sync project state"),
    "run_checks": (test_runner.run, "run configured tests/builds"),
    "research": (research_agent.run, "research configured topics"),
    "analyze_conversations": (conversation_analyzer.run, "analyze recent interactions"),
    "extract_memories": (memory_extractor.run, "extract memories / lessons"),
    "update_rules_skills": (skill_builder.run, "review learned rules and skills"),
    "project_summaries": (project_scanner.project_summaries, "generate project summaries"),
}
DEFAULT_SCHEDULE = {
    "sync_projects": "01:00", "run_checks": "01:15", "research": "02:00",
    "analyze_conversations": "03:00", "extract_memories": "03:30",
    "update_rules_skills": "03:45", "project_summaries": "04:00",
}
DEFAULT_START, DEFAULT_END = "01:00", "05:00"
GAP_SECONDS = 180            # a heartbeat gap longer than this = asleep / not running
MAX_DAYS = 3                 # never learn from more than this many days in one run
LOCK_STALE_HOURS = 12

_run_lock = threading.Lock()
_running = threading.Event()
_first_tick = True


# ------------------------------------------------------------------------------ config

def cfg() -> dict:
    return conf._section("night_mode")


def enabled() -> bool:
    return bool(cfg().get("enabled", False))


def _hm(s: str, default: str) -> tuple[int, int]:
    try:
        h, m = str(s or default).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    h, m = default.split(":")
    return int(h), int(m)


def window_for(now: datetime) -> tuple[datetime, datetime]:
    """The latest night window that started at or before `now` (it may already be over)."""
    sh, sm = _hm(cfg().get("start"), DEFAULT_START)
    eh, em = _hm(cfg().get("end"), DEFAULT_END)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    dur = (timedelta(hours=eh, minutes=em) - timedelta(hours=sh, minutes=sm)) % timedelta(days=1)
    return start, start + (dur or timedelta(days=1))


def night_key(window_start: datetime) -> str:
    return window_start.date().isoformat()


def plan(window_start: datetime) -> tuple[list[tuple[str, datetime]], list[str]]:
    """([(task, earliest start)] in time order, [config problems])."""
    sched = cfg().get("schedule")
    sched = sched if isinstance(sched, dict) and sched else DEFAULT_SCHEDULE
    out, problems = [], []
    for name, at in sched.items():
        if name not in TASKS:
            problems.append(f"unknown task {name!r} in [night_mode.schedule]")
            continue
        h, m = _hm(at, window_start.strftime("%H:%M"))
        t = window_start.replace(hour=h, minute=m)
        if t < window_start:
            t += timedelta(days=1)
        out.append((name, t))
    out.sort(key=lambda x: x[1])
    return out, problems


# ------------------------------------------------------------------------------ state

def night_dir() -> Path:
    return conf.DATA_DIR / "night"


def _read_json(f: Path) -> dict:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _state() -> dict:
    return _read_json(night_dir() / "state.json")


def _save_state(st: dict) -> None:
    safe_write(night_dir() / "state.json", json.dumps(st, indent=1))


def run_file(key: str) -> Path:
    return night_dir() / "runs" / f"{key}.json"


def load_run(key: str) -> dict | None:
    f = run_file(key)
    return _read_json(f) if f.exists() else None


def save_run(rec: dict) -> None:
    safe_write(run_file(rec["night"]), json.dumps(rec, indent=1, default=str))


def runs(limit: int = 10) -> list[dict]:
    d = night_dir() / "runs"
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    return [r for r in (_read_json(f) for f in files) if r]


def last_successful_finish() -> datetime | None:
    for r in runs(20):     # partial (--only) runs don't count: the other tasks didn't run
        if r.get("status", "").startswith("completed") and r.get("finished") and not r.get("only"):
            try:
                return datetime.fromisoformat(r["finished"])
            except ValueError:
                continue
    return None


# ------------------------------------------------------------------------------ sleep/offline

def boot_time() -> datetime | None:
    try:
        import psutil
        return datetime.fromtimestamp(psutil.boot_time())
    except Exception:
        return None


def app_start_time() -> datetime | None:
    try:
        import psutil
        return datetime.fromtimestamp(psutil.Process().create_time())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True            # can't tell: assume alive (never steal a live lock)


def heartbeat(now: datetime) -> None:
    """Called on every scheduler tick. A long gap since the last one means the PC slept (or
    Helios wasn't running) — recorded so a missed night can say why."""
    st = _state()
    last = st.get("heartbeat")
    if last:
        try:
            gap = (now - datetime.fromisoformat(last)).total_seconds()
            if gap > GAP_SECONDS:
                st.setdefault("gaps", []).append([last, now.isoformat(timespec="seconds")])
                st["gaps"] = st["gaps"][-30:]
                conf.log("night", f"heartbeat gap {last} -> {now:%Y-%m-%d %H:%M:%S} "
                                  f"({gap / 60:.0f} min: asleep or Helios not running)")
        except ValueError:
            pass
    st["heartbeat"] = now.isoformat(timespec="seconds")
    if enabled():
        st.setdefault("enabled_since", now.isoformat(timespec="seconds"))
    else:
        st.pop("enabled_since", None)
    _save_state(st)


def missed_reason(ws: datetime, we: datetime, now: datetime) -> str:
    boot, started = boot_time(), app_start_time()
    if boot and boot > ws:
        return f"the PC was off or restarted during the night (it booted at {boot:%Y-%m-%d %H:%M})"
    if started and started > ws:
        return f"Helios wasn't running during the night (it started at {started:%Y-%m-%d %H:%M})"
    for a, b in _state().get("gaps", []):
        try:
            a, b = datetime.fromisoformat(a), datetime.fromisoformat(b)
        except ValueError:
            continue
        if a < we and b > ws:
            return f"the PC was asleep from {a:%H:%M} to {b:%Y-%m-%d %H:%M}"
    return "unknown — Helios was running but the run didn't start (see logs/night.log)"


# ------------------------------------------------------------------------------ lock

def _lock_file() -> Path:
    return night_dir() / "night.lock"


def _acquire_lock() -> bool:
    f = _lock_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        try:
            pid = int((f.read_text() or "0").split()[0])
        except Exception:
            pid = 0
        age_h = (time.time() - f.stat().st_mtime) / 3600
        if (pid and not _pid_alive(pid)) or age_h > LOCK_STALE_HOURS:
            conf.log("night", f"removing stale lock (pid {pid}, {age_h:.1f} h old)")
            f.unlink(missing_ok=True)
    try:
        fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock() -> None:
    _lock_file().unlink(missing_ok=True)


def is_running() -> bool:
    if _running.is_set():
        return True
    f = _lock_file()
    if not f.exists():
        return False
    try:
        return _pid_alive(int(f.read_text().split()[0]))
    except Exception:
        return False


# ------------------------------------------------------------------------------ the run

def _days_since(since: datetime, now: datetime) -> list[str]:
    days, d = [], max(since.date(), (now - timedelta(days=MAX_DAYS - 1)).date())
    while d <= now.date():
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _panic(since: float = 0.0) -> bool:
    """A panic stop pressed since `since` (epoch seconds). An older flag is a leftover from the
    day (it only clears on the user's next message) and must not silently cancel the night."""
    try:
        return conf.ABORT_FLAG.exists() and conf.ABORT_FLAG.stat().st_mtime >= since
    except Exception:
        return False


def run_night(now: datetime | None = None, *, scheduled: bool = False, only: list[str] | None = None,
              wait: bool = False, emit=None, stop: threading.Event | None = None,
              sleep=time.sleep) -> dict:
    """Run Night Mode. scheduled=True: tonight's window run (waits for each task's time when
    `wait`, stops at the window end, one per night). Otherwise a manual run of every scheduled
    task (or `only`) right now. Returns the run record (also saved + reported)."""
    now = now or datetime.now()
    ws, we = window_for(now)
    key = night_key(ws) if scheduled else f"manual-{now:%Y-%m-%d-%H%M%S}"
    if scheduled and load_run(key):
        return {"night": key, "status": "duplicate", "reason": "tonight's run already exists"}
    busy = {"night": key, "status": "busy", "reason": "Night Mode is already running"}
    if not _run_lock.acquire(blocking=False):
        return busy
    try:
        if not _acquire_lock():
            return busy                          # another process holds it — leave its lock alone
        try:
            _running.set()
            return _run(key, ws, we, now, scheduled=scheduled, only=only, wait=wait, emit=emit,
                        stop=stop, sleep=sleep)
        finally:
            _running.clear()
            _release_lock()
    finally:
        _run_lock.release()


def _run(key, ws, we, now, *, scheduled, only, wait, emit, stop, sleep) -> dict:
    rec = {"night": key, "kind": "scheduled" if scheduled else "manual",
           "window": [ws.isoformat(timespec="minutes"), we.isoformat(timespec="minutes")],
           "started": datetime.now().isoformat(timespec="seconds"), "finished": None,
           "status": "running", "tasks": {}, "log": [], "online": None, "only": only or None}

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        rec["log"].append(line)
        conf.log("night", f"[{key}] {msg}")

    t_start = time.time()
    try:
        log(f"start ({rec['kind']}; window {ws:%H:%M}-{we:%H:%M})")
        if conf.ABORT_FLAG.exists():
            log("note: a panic stop from earlier is still set — only a panic pressed during this "
                "run stops it")
        steps, problems = plan(ws)
        for p in problems:
            log(f"config: {p}")
        if only:
            unknown = [o for o in only if o not in TASKS]
            for u in unknown:
                rec["tasks"][u] = Result(status="failed", summary="unknown task").as_dict()
            steps = [(n, t) for n, t in steps if n in only] + \
                    [(n, now) for n in only if n in TASKS and n not in dict(steps)]
        since = last_successful_finish() or (now - timedelta(hours=24))
        rec["online"] = online()
        ctx = {"night": key, "window_start": ws, "window_end": we, "since": since,
               "days": _days_since(since, datetime.now()), "online": rec["online"]}
        log(f"since {since:%Y-%m-%d %H:%M}; days {ctx['days']}; {'online' if rec['online'] else 'OFFLINE'}")
        save_run(rec)
        for name, at in steps:
            fn, desc = TASKS[name]
            if wait and scheduled:
                while datetime.now() < at and datetime.now() < we and not _panic(t_start) \
                        and not (stop and stop.is_set()):
                    sleep(min(30.0, max(1.0, (at - datetime.now()).total_seconds())))
            if _panic(t_start) or (stop and stop.is_set()):
                stopped = "panic stop" if _panic(t_start) else "Helios shutting down"
                res, why = Result.skipped(f"stopped ({stopped})"), "stopped"
                rec["stopped"] = stopped
            elif scheduled and datetime.now() >= we:
                res, why = Result.skipped(f"the night window ended at {we:%H:%M} before it could start"), "window over"
            else:
                why = ""
                log(f"{name}: start — {desc}")
                t0 = time.monotonic()
                try:
                    res = fn(ctx)
                except Exception as e:           # one task failing never stops the others
                    res = Result(status="failed", summary=f"{type(e).__name__}: {e}"[:300])
                took = round(time.monotonic() - t0, 1)
                log(f"{name}: {res.status} in {took}s — {res.summary}")
            if why:
                log(f"{name}: skipped — {res.summary}")
            rec["tasks"][name] = res.as_dict() | {"at": datetime.now().isoformat(timespec="seconds")}
            save_run(rec)
        failed = any(t["status"] == "failed" for t in rec["tasks"].values())
        if rec.get("stopped"):
            rec["status"] = f"stopped ({rec['stopped']})"
        else:
            rec["status"] = "completed with errors" if failed else "completed"
    except Exception as e:                       # the run itself broke: record it, loudly
        rec["status"] = "crashed"
        log(f"CRASHED: {type(e).__name__}: {e}")
    rec["finished"] = datetime.now().isoformat(timespec="seconds")
    log(f"finish: {rec['status']}")
    try:
        rec["report"] = str(morning_report.write(rec))
    except Exception as e:
        log(f"report failed: {e}")
    save_run(rec)
    _announce(rec, emit)
    return rec


def _announce(rec: dict, emit) -> None:
    tasks = rec.get("tasks") or {}
    bad = [n for n, t in tasks.items() if t["status"] == "failed"]
    msg = (f"Night Mode {rec['status']}: {len(tasks)} task(s)" + (f", failed: {', '.join(bad)}" if bad else "")
           if rec["status"] != "missed" else f"Night Mode missed last night — {rec.get('reason')}")
    try:
        if emit:
            emit("status", "\U0001f319 " + msg)
        if rec["status"] != "completed" or rec["kind"] == "manual":
            from .. import notify
            notify.toast("Helios Night Mode", msg)
    except Exception:
        pass


# ------------------------------------------------------------------------------ app hook

def _recover_interrupted() -> None:
    """A run left 'running' by a crash/shutdown is marked interrupted (and reported)."""
    if is_running():
        return
    for r in runs(5):
        if r.get("status") == "running":
            r["status"] = "interrupted"
            r["log"] = (r.get("log") or []) + ["interrupted — Helios stopped mid-run"]
            r["finished"] = r.get("finished") or datetime.now().isoformat(timespec="seconds")
            save_run(r)
            try:
                r["report"] = str(morning_report.write(r))
                save_run(r)
            except Exception:
                pass
            conf.log("night", f"[{r['night']}] marked interrupted")


def tick(now: datetime | None = None, emit=None, stop: threading.Event | None = None,
         start_thread=None) -> str:
    """Called from the app scheduler every tick. Returns what it did (for tests/logs)."""
    global _first_tick
    now = now or datetime.now()
    heartbeat(now)
    if _first_tick:
        _first_tick = False
        _recover_interrupted()
    if not enabled():
        return "disabled"
    ws, we = window_for(now)
    key = night_key(ws)
    if load_run(key):
        return "done"
    if ws <= now < we:
        if is_running():
            return "running"
        conf.log("night", f"[{key}] window open — starting the scheduled run")
        target = lambda: run_night(now, scheduled=True, wait=True, emit=emit, stop=stop)
        (start_thread or (lambda f: threading.Thread(target=f, daemon=True, name="night-mode").start()))(target)
        return "started"
    # The window is over and there is no run for it: record the miss once, with the reason —
    # but only if Night Mode was already enabled before that window opened.
    since = _state().get("enabled_since")
    if since and datetime.fromisoformat(since) <= ws and now - we < timedelta(hours=20):
        rec = {"night": key, "kind": "scheduled", "status": "missed",
               "window": [ws.isoformat(timespec="minutes"), we.isoformat(timespec="minutes")],
               "reason": missed_reason(ws, we, now), "tasks": {}, "log": [],
               "finished": now.isoformat(timespec="seconds")}
        conf.log("night", f"[{key}] missed: {rec['reason']}")
        try:
            rec["report"] = str(morning_report.write(rec))
        except Exception as e:
            conf.log("night", f"[{key}] missed-report failed: {e}")
        save_run(rec)
        _announce(rec, emit)
        return "missed"
    return "idle"


# ------------------------------------------------------------------------------ status

def next_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now()
    ws, we = window_for(now)
    return (ws, we) if now < we and not load_run(night_key(ws)) else (ws + timedelta(days=1), we + timedelta(days=1))


def status_text(now: datetime | None = None) -> str:
    now = now or datetime.now()
    lines = [f"Night Mode is {'ON' if enabled() else 'OFF'}"
             + (" — running now" if is_running() else "")]
    if enabled():
        ws, we = next_window(now)
        lines.append(f"next window: {ws:%a %Y-%m-%d %H:%M}–{we:%H:%M}")
        steps, problems = plan(ws)
        lines.append("tasks: " + ", ".join(f"{n} {t:%H:%M}" for n, t in steps))
        lines += [f"config problem: {p}" for p in problems]
    for r in runs(3):
        tasks = r.get("tasks") or {}
        bad = sum(1 for t in tasks.values() if t["status"] == "failed")
        extra = f" — {r.get('reason')}" if r.get("status") == "missed" else \
            f" ({len(tasks)} tasks, {bad} failed)"
        lines.append(f"{r['night']}: {r.get('status')}{extra}")
    return "\n".join(lines)


def latest_report(key: str | None = None) -> str:
    rec = load_run(key) if key else next(iter(runs(1)), None)
    if not rec:
        return "No night report yet."
    return morning_report.render(rec)
