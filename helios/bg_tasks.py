"""Long-running Helios tools run as BACKGROUND TASKS in the app, so the chat never stalls.

Why: the Antigravity agent waits only about a minute for a tool call. A business search (OpenStreetMap
scan + website audits + web searches) or a project health run takes several minutes, so calling it
inline made agy report "failed", Helios's watchdog then killed the turn, and the old retry ran the
whole search a second time. Now the tool starts a task here and returns within seconds; when the task
finishes the result is posted to the chat + dashboard (and becomes a NORMAL executive event, so it is
in the next report / SMS task-completion digest). A second identical request joins the running task
instead of starting another one.

Tasks live in helios.db (`bg_tasks`) so the brain's tool server (a separate process) can read status.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime

from . import conf, db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bg_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, args TEXT NOT NULL DEFAULT '{}', key TEXT NOT NULL,
  status TEXT NOT NULL,                 -- running | done | failed
  started TEXT NOT NULL, finished TEXT, seconds INTEGER,
  summary TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS bg_tasks_key ON bg_tasks(key, status);
"""
_inited: set[str] = set()
_lock = threading.Lock()
TITLES = {"prospects_run": "Business search", "project_checks": "Project health checks"}


def _init() -> None:
    if str(db.DB_PATH) in _inited:
        return
    db.init()
    with db._conn() as c:
        c.executescript(_SCHEMA)
    _inited.add(str(db.DB_PATH))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ the work itself

def _prospects_run(area: str = "", research: int = 8) -> tuple[str, str]:
    from .prospecting import engine
    rep = engine.run(area=area or None, max_research=max(0, min(int(research), 30)))
    lines = [f"{s['area']}: searched OpenStreetMap in {s['cells_scanned']} cell(s) — {s['candidates']} businesses, "
             f"{s['new']} new" for s in rep["scans"]]
    lines.append(f"Researched {rep['researched']}: {rep['qualified']} qualified, {rep['drafted']} drafts waiting for "
                 f"approval ({rep['seconds']}s). OpenStreetMap doesn't list every business.")
    lines += [f"! {e}" for e in rep["errors"][:5]]
    where = area or ", ".join(s["area"].split(",")[0] for s in rep["scans"]) or "your areas"
    summary = (f"Business search in {where} finished: {rep['qualified']} qualified, {rep['drafted']} drafts to review"
               + (f" ({len(rep['errors'])} problem(s))" if rep["errors"] else ""))
    return summary, "\n".join(lines)


def _project_checks(project: str = "", check: str = "") -> tuple[str, str]:
    from . import health, projects
    if check:
        text = projects.format_checks(projects.run_checks(project or None, check))
        return f"Check '{check}' finished", text
    reps = health.run_all(project or None)
    bad = [r["project"] for r in reps if r.get("verdict") in ("FAILING", "ATTENTION")]
    summary = f"Project checks finished: {len(reps)} project(s)" + (f", attention: {', '.join(bad)}" if bad else ", all OK")
    return summary, health.format_report(reps)


RUNNERS = {"prospects_run": _prospects_run, "project_checks": _project_checks}


# ------------------------------------------------------------------ lifecycle

def _row(r) -> dict:
    d = dict(r)
    d["args"] = json.loads(d.get("args") or "{}")
    return d


def get(task_id: int) -> dict | None:
    _init()
    with db._conn() as c:
        r = c.execute("SELECT * FROM bg_tasks WHERE id=?", (int(task_id),)).fetchone()
    return _row(r) if r else None


def recent(limit: int = 10) -> list[dict]:
    _init()
    with db._conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM bg_tasks ORDER BY id DESC LIMIT ?", (int(limit),))]


def start(kind: str, args: dict | None = None, publish=None, *, run_inline: bool = False) -> dict:
    """Start (or join) a task. Returns {"id", "status": started|already_running, "title"}."""
    if kind not in RUNNERS:
        raise ValueError(f"unknown task {kind!r}")
    _init()
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    key = f"{kind}:{json.dumps(args, sort_keys=True)}"
    with _lock:
        with db._conn() as c:
            # a task left "running" by an app restart can never finish — close it so it can be re-run
            for r in c.execute("SELECT id, started FROM bg_tasks WHERE status='running'").fetchall():
                if time.time() - datetime.fromisoformat(r["started"]).timestamp() > 3 * 3600:
                    c.execute("UPDATE bg_tasks SET status='failed', finished=?, summary='interrupted (app restarted?)' "
                              "WHERE id=?", (_now(), r["id"]))
            run = c.execute("SELECT id FROM bg_tasks WHERE key=? AND status='running'", (key,)).fetchone()
            if run:
                return {"id": run["id"], "status": "already_running", "title": TITLES[kind]}
            tid = c.execute("INSERT INTO bg_tasks(kind, args, key, status, started) VALUES(?,?,?,?,?)",
                            (kind, json.dumps(args), key, "running", _now())).lastrowid
    conf.log("tasks", f"task #{tid} {kind} started {args}")
    work = lambda: _run(tid, kind, args, publish)       # noqa: E731
    if run_inline:
        work()
    else:
        threading.Thread(target=work, daemon=True, name=f"task-{tid}").start()
    return {"id": tid, "status": "started", "title": TITLES[kind]}


def _run(tid: int, kind: str, args: dict, publish=None) -> None:
    t0 = time.time()
    try:
        summary, result = RUNNERS[kind](**args)
        status = "done"
    except Exception as e:                                       # reported, never hidden
        summary, result = f"{TITLES[kind]} failed: {type(e).__name__}: {e}", traceback.format_exc()[-1500:]
        status = "failed"
    secs = int(time.time() - t0)
    with db._conn() as c:
        c.execute("UPDATE bg_tasks SET status=?, finished=?, seconds=?, summary=?, result=? WHERE id=?",
                  (status, _now(), secs, summary[:400], result[:20000], tid))
    conf.log("tasks", f"task #{tid} {kind} {status} in {secs}s: {summary[:160]}")
    _announce(tid, kind, status, summary, result, publish)


def _announce(tid, kind, status, summary, result, publish=None) -> None:
    icon = "✅" if status == "done" else "⚠"
    if publish:
        try:
            publish("status", f"{icon} {summary} — task #{tid}")
            publish("dashboard", {"event": "task_done", "task": tid, "kind": kind, "status": status, "text": summary,
                                  "detail": result[:4000]})
        except Exception:
            pass
    try:
        from . import notify
        notify.toast(f"Helios — {TITLES[kind]}", summary[:200])
    except Exception:
        pass
    try:
        from . import executive
        executive.emit("tasks", "task_done" if status == "done" else "task_failed",
                       "NORMAL" if status == "done" else "HIGH", summary, detail=result[:3000],
                       related=f"task:{tid}", dedupe_key=f"task:{tid}")
    except Exception:
        pass
    if status == "done":
        try:
            from . import sms
            sms.notify("task_complete", summary, dedupe_key=f"task:{tid}")
        except Exception:
            pass


def format_status(t: dict | None) -> str:
    if not t:
        return "No such background task."
    title = TITLES.get(t["kind"], t["kind"])
    if t["status"] == "running":
        mins = int((time.time() - datetime.fromisoformat(t["started"]).timestamp()) // 60)
        return f"{title} (task #{t['id']}) is still running — started {mins} min ago."
    head = f"{title} (task #{t['id']}) {t['status']} after {t['seconds'] or 0}s: {t['summary']}"
    return head + ("\n\n" + t["result"] if t["result"] else "")
