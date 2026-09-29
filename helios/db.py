"""SQLite store shared by the app and the helios-tools MCP server (separate processes).

Holds reminders, routines, background jobs, and conversation history. WAL mode keeps
cross-process reads/writes safe.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import conf


def _like_escape(s: str, e: str = "|") -> str:
    """Escape SQL LIKE wildcards so a literal path can't act as a pattern. Uses '|' as the
    ESCAPE char (illegal in Windows paths, so it never collides). Escape the escape char first."""
    return s.replace(e, e + e).replace("%", e + "%").replace("_", e + "_")

# Single SQLite file under the gitignored data/ dir (see conf.DATA_DIR). Shared across
# processes (app + helios-tools MCP server) — WAL mode in _conn() makes that safe.
DB_PATH = conf.ROOT / "data" / "helios.db"
_LEGACY_DB = conf.ROOT / "data" / "jarvis.db"   # pre-rename name; carried over once, never deleted


def _migrate_legacy_db() -> None:
    """Rename a pre-rename jarvis.db (+ its WAL/SHM side files) to helios.db, once."""
    if DB_PATH.exists() or not _LEGACY_DB.exists():
        return
    try:
        for suffix in ("", "-wal", "-shm"):
            old = _LEGACY_DB.with_name(_LEGACY_DB.name + suffix)
            if old.exists():
                old.rename(DB_PATH.with_name(DB_PATH.name + suffix))
    except OSError as e:  # another process may be doing the same at this instant
        conf.log("db", f"legacy db migration skipped: {e}")


_migrate_legacy_db()

# Full schema, applied idempotently by init() via executescript. Every CREATE uses
# IF NOT EXISTS, so init() is safe to call repeatedly; column additions to already-existing
# tables happen in _migrate() (CREATE TABLE IF NOT EXISTS will not alter an existing table).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  due_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  fired INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  schedule TEXT NOT NULL,
  next_run TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  result TEXT,
  agent TEXT NOT NULL DEFAULT 'auto',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  role TEXT,
  content TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT,
  text TEXT,
  embedding TEXT,
  indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  result TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL,
  parent_id INTEGER,
  role TEXT NOT NULL,
  task TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  result TEXT,
  depth INTEGER NOT NULL DEFAULT 0,
  pid INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL,
  sender_id INTEGER,
  sender_role TEXT,
  kind TEXT NOT NULL DEFAULT 'note',
  content TEXT NOT NULL,
  to_role TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  spec TEXT NOT NULL,                       -- full {name,trigger,steps} JSON document
  enabled INTEGER NOT NULL DEFAULT 1,
  schedule TEXT NOT NULL DEFAULT '',        -- '' for manual/webhook; a routine-style string otherwise
  next_run TEXT,                            -- ISO next fire time (scheduled triggers only)
  last_run TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',   -- running | done | error | stopped
  trigger TEXT NOT NULL DEFAULT 'manual',
  log TEXT,                                 -- JSON array of per-step {id,type,status,output,error}
  result TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS recipes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT NOT NULL,                        -- the goal (the user's message), redacted
  task_norm TEXT NOT NULL,                   -- normalized task for dedupe/upsert
  steps TEXT NOT NULL,                       -- JSON array of the tool sequence that worked
  created_at TEXT NOT NULL
);
"""


def add_chunk(source: str, text: str, embedding: list) -> None:
    """Store one text chunk + its embedding (for local semantic search / RAG).
    The embedding list is JSON-serialized into a TEXT column (SQLite has no vector type)."""
    import json as _json
    with _conn() as c:
        c.execute("INSERT INTO chunks(source, text, embedding, indexed_at) VALUES(?,?,?,?)",
                  (source, text, _json.dumps(embedding), _now()))


def all_chunks() -> list[dict]:
    """Return every chunk as {source, text, embedding(list)}, deserializing the JSON
    embedding back into a list. Rows whose embedding fails to parse are silently skipped."""
    import json as _json
    with _conn() as c:
        rows = c.execute("SELECT source, text, embedding FROM chunks").fetchall()
    out = []
    for r in rows:
        try:
            out.append({"source": r["source"], "text": r["text"], "embedding": _json.loads(r["embedding"])})
        except Exception:
            pass
    return out


def clear_chunks(source: str | None = None) -> int:
    """Delete chunks and return how many rows were removed. With a source given, deletes the
    exact source PLUS anything beneath it (source + os.sep + …) — boundary-safe so re-indexing
    'C:/docs' does NOT also wipe 'C:/docs2', 'C:/docs-old', or 'C:/docs.txt'. Wildcards in the
    path are escaped. With no source, wipes the entire chunks table."""
    with _conn() as c:
        if source:
            subtree = _like_escape(source.rstrip("/\\") + os.sep) + "%"
            cur = c.execute(
                "DELETE FROM chunks WHERE source = ? OR source LIKE ? ESCAPE '|'",
                (source, subtree))
        else:
            cur = c.execute("DELETE FROM chunks")
        return cur.rowcount


def chunk_sources() -> list[tuple]:
    """Return [(source, chunk_count)] grouped by source — i.e. what's indexed and how much."""
    with _conn() as c:
        rows = c.execute("SELECT source, COUNT(*) n FROM chunks GROUP BY source ORDER BY source").fetchall()
        return [(r["source"], r["n"]) for r in rows]


# ----------------------------------------------------- missions / agent network
# A "mission" is a multi-agent job: a supervisor agent spawns a team of agent_runs that
# collaborate via mission_log (a shared blackboard). These tables back that system.
def create_mission(goal: str) -> int:
    """Insert a new active mission and return its id."""
    with _conn() as c:
        cur = c.execute("INSERT INTO missions(goal, status, created_at, updated_at) VALUES(?,?,?,?)",
                        (goal, "active", _now(), _now()))
        return cur.lastrowid


def set_mission(mission_id: int, status: str | None = None, result: str | None = None) -> None:
    """Patch a mission's status and/or result. COALESCE leaves a field unchanged when its
    argument is None, so callers can update just one column."""
    with _conn() as c:
        c.execute("UPDATE missions SET status=COALESCE(?,status), result=COALESCE(?,result), "
                  "updated_at=? WHERE id=?", (status, result, _now(), mission_id))


def get_mission(mission_id: int) -> dict | None:
    """Return the mission row as a dict, or None if no such id."""
    with _conn() as c:
        r = c.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        return dict(r) if r else None


def list_missions(limit: int = 30) -> list[dict]:
    """Return the most recent missions (newest first), capped at `limit`."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM missions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def add_agent_run(mission_id: int, role: str, task: str, depth: int = 0,
                  parent_id: int | None = None, status: str = "running") -> int:
    """Record one agent within a mission and return its id.

    role is the specialist kind (supervisor/researcher/operator/coder/...), parent_id is
    the agent that spawned it (None for the top-level supervisor), and depth is its level
    in the spawn tree (used to bound recursive spawning)."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO agent_runs(mission_id, parent_id, role, task, status, depth, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (mission_id, parent_id, role, task, status, depth, _now(), _now()))
        return cur.lastrowid


def set_agent_run(run_id: int, status: str | None = None, result: str | None = None,
                  pid: int | None = None) -> None:
    """Patch an agent_run's status/result/pid (COALESCE: None leaves the field as-is). pid is the
    spawned `claude -p` worker PID, recorded so panic can kill workers across process subtrees."""
    with _conn() as c:
        c.execute("UPDATE agent_runs SET status=COALESCE(?,status), result=COALESCE(?,result), "
                  "pid=COALESCE(?,pid), updated_at=? WHERE id=?",
                  (status, result, pid, _now(), run_id))


def get_agent_run(run_id: int) -> dict | None:
    """Return one agent_run row as a dict, or None if no such id."""
    with _conn() as c:
        r = c.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def mission_agents(mission_id: int) -> list[dict]:
    """All agent_runs for a mission, in spawn order (ascending id)."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM agent_runs WHERE mission_id=? ORDER BY id", (mission_id,)).fetchall()]


def count_mission_agents(mission_id: int) -> int:
    """Number of agents that count toward a mission's budget — i.e. excluding 'cancelled' rows
    (a spawn refused for over-budget is cancelled and must NOT shrink the remaining budget)."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM agent_runs WHERE mission_id=? AND status!='cancelled'",
                         (mission_id,)).fetchone()["n"]


def fail_orphaned_agents() -> int:
    """Called once at startup: nothing of ours is running yet, so any agent_run still in
    'starting'/'running' is a crash/restart orphan. Mark those (and their still-'active' missions)
    'error' so a mission can't hang 'active' forever after a crash. 'queued' rows are left alone
    (they're legitimately awaiting dispatch; the scheduler's stale sweep handles old ones)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, mission_id FROM agent_runs WHERE status IN ('starting','running')").fetchall()
        for r in rows:
            c.execute("UPDATE agent_runs SET status='error', "
                      "result=COALESCE(result,'(orphaned — app restarted)'), updated_at=? WHERE id=?",
                      (_now(), r["id"]))
            c.execute("UPDATE missions SET status='error', updated_at=? WHERE id=? AND status='active'",
                      (_now(), r["mission_id"]))
        return len(rows)


def count_running_agents() -> int:
    """Global count of agents currently executing (for the concurrency cap)."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM agent_runs WHERE status='running'").fetchone()["n"]


def try_claim_running_slot(run_id: int, cap: int) -> bool:
    """Atomically claim a global concurrency slot: flip this run to 'running' ONLY if fewer than
    `cap` agents are already running. The count-check and the flip happen in ONE statement, so there
    is no TOCTOU window (the old read-count-then-set-running had one); WAL + busy_timeout serialize
    concurrent claimers across the separate per-agent MCP-server processes. True iff we won the slot."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE agent_runs SET status='running', updated_at=? "
            "WHERE id=? AND status IN ('queued', 'starting') "
            "AND (SELECT COUNT(*) FROM agent_runs WHERE status='running') < ?",
            (_now(), run_id, cap))
        return cur.rowcount == 1


def running_agent_pids() -> list[int]:
    """PIDs of agents currently 'running' that recorded one — used by panic to kill workers spawned
    in a different process subtree (which taskkill /T on the supervisor can't reach). Best-effort."""
    with _conn() as c:
        return [r["pid"] for r in c.execute(
            "SELECT pid FROM agent_runs WHERE status='running' AND pid IS NOT NULL").fetchall()]


def queued_supervisors() -> list[dict]:
    """Mission supervisors waiting to be dispatched by the app (the mission kickoff)."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM agent_runs WHERE status='queued' AND role='supervisor' ORDER BY id"
        ).fetchall()]


def add_mission_log(mission_id: int, sender_id: int | None, sender_role: str | None,
                    kind: str, content: str, to_role: str | None = None) -> int:
    """Append one entry to a mission's shared blackboard and return its id.

    sender_id/sender_role identify the posting agent; kind classifies the entry
    (e.g. 'note', a task hand-off, a result); to_role optionally addresses it to a
    specific specialist role. Agents poll read_mission_log() to see new entries."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO mission_log(mission_id, sender_id, sender_role, kind, content, to_role, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (mission_id, sender_id, sender_role, kind, content, to_role, _now()))
        return cur.lastrowid


def read_mission_log(mission_id: int, since_id: int = 0) -> list[dict]:
    """Return blackboard entries for a mission with id > since_id (oldest first).
    Pass the last id you saw as since_id to poll incrementally for new messages."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mission_log WHERE mission_id=? AND id>? ORDER BY id",
            (mission_id, since_id)).fetchall()]


def get_state(key: str, default: str | None = None) -> str | None:
    """Read a value from the simple key/value `state` table (used for small persisted
    settings like the current 'tone'); returns `default` if the key isn't set."""
    with _conn() as c:
        r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_state(key: str, value: str) -> None:
    """Upsert a key/value pair into the `state` table (insert, or overwrite on conflict)."""
    with _conn() as c:
        c.execute("INSERT INTO state(key, value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _conn() -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and Row factory.

    Every public function opens a fresh short-lived connection (used as a context manager
    so it commits on success). timeout=10 waits up to 10s for the write lock instead of
    failing immediately when another process holds it. WAL mode lets readers and a writer
    work concurrently across the multiple processes that share this DB. row_factory=Row
    makes rows behave like dicts (column-name indexing)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")  # per-connection; wait on the write lock
    return c


def init() -> None:
    """Create all tables (idempotent) and run column migrations. Call once at startup."""
    with _conn() as c:
        # WAL is persistent in the DB file once set, so set it here once instead of on every
        # short-lived _conn() (which was dozens of redundant pragma round-trips per minute).
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        _migrate(c)


def _migrate(c) -> None:
    """Add columns to pre-existing tables (CREATE TABLE IF NOT EXISTS won't alter them)."""
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "agent" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN agent TEXT NOT NULL DEFAULT 'auto'")
    except Exception:  # pragma: no cover
        pass
    try:  # agent_runs.pid — records the spawned worker PID so panic can kill across subtrees
        cols = {r[1] for r in c.execute("PRAGMA table_info(agent_runs)").fetchall()}
        if "pid" not in cols:
            c.execute("ALTER TABLE agent_runs ADD COLUMN pid INTEGER")
    except Exception:  # pragma: no cover
        pass


def _now() -> str:
    """Current local time as an ISO-8601 string (second precision). All timestamp columns
    store this text format, so they sort and compare lexicographically as datetimes."""
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------- reminders
def add_reminder(text: str, due_at: str) -> int:
    """Schedule a one-shot reminder (due_at is an ISO timestamp string); returns its id."""
    with _conn() as c:
        cur = c.execute("INSERT INTO reminders(text, due_at, created_at) VALUES(?,?,?)",
                        (text, due_at, _now()))
        return cur.lastrowid


def due_reminders(now_iso: str) -> list[dict]:
    """Unfired reminders whose due_at <= now_iso — i.e. those ready to fire. The scheduler
    polls this and then calls mark_reminder_fired() on each."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM reminders WHERE fired=0 AND due_at<=? ORDER BY due_at",
                         (now_iso,)).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_fired(rid: int) -> None:
    """Mark a reminder fired so it won't be returned by due_reminders again."""
    with _conn() as c:
        c.execute("UPDATE reminders SET fired=1 WHERE id=?", (rid,))


def list_reminders(include_fired: bool = False) -> list[dict]:
    """List reminders ordered by due time; by default excludes already-fired ones."""
    q = "SELECT * FROM reminders" + ("" if include_fired else " WHERE fired=0") + " ORDER BY due_at"
    with _conn() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


def cancel_reminder(rid: int) -> bool:
    """Delete a reminder; returns True if a row was actually removed."""
    with _conn() as c:
        cur = c.execute("DELETE FROM reminders WHERE id=?", (rid,))
        return cur.rowcount > 0


# -------------------------------------------------------------------- routines
def add_routine(name: str, prompt: str, schedule: str, next_run: str) -> int:
    """Create a recurring routine and return its id. `prompt` is fed to the brain each time
    it fires; `schedule` is the recurrence spec; `next_run` is the first scheduled ISO time."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO routines(name, prompt, schedule, next_run) VALUES(?,?,?,?)",
            (name, prompt, schedule, next_run))
        return cur.lastrowid


def due_routines(now_iso: str) -> list[dict]:
    """Enabled routines whose next_run <= now_iso (ready to run). After running, the caller
    computes the following occurrence and calls set_routine_next()."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM routines WHERE enabled=1 AND next_run<=? ORDER BY next_run",
                         (now_iso,)).fetchall()
        return [dict(r) for r in rows]


def set_routine_next(rid: int, next_run: str) -> None:
    """Reschedule a routine: set its next_run and stamp last_run to now."""
    with _conn() as c:
        c.execute("UPDATE routines SET next_run=?, last_run=? WHERE id=?", (next_run, _now(), rid))


def list_routines() -> list[dict]:
    """All routines (enabled or not), in creation order."""
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM routines ORDER BY id").fetchall()]


def set_routine_enabled(rid: int, enabled: bool) -> None:
    """Enable/disable a routine (stored as 1/0; disabled routines are skipped by due_routines)."""
    with _conn() as c:
        c.execute("UPDATE routines SET enabled=? WHERE id=?", (1 if enabled else 0, rid))


def delete_routine(rid: int) -> bool:
    """Delete a routine; returns True if a row was removed."""
    with _conn() as c:
        cur = c.execute("DELETE FROM routines WHERE id=?", (rid,))
        return cur.rowcount > 0


# ------------------------------------------------------------------- workflows
# A "workflow" generalizes a routine: a trigger + an ordered list of steps whose outputs feed
# later steps (see helios/workflows.py). `spec` stores the whole JSON document; schedule/next_run/
# enabled are denormalized so due_workflows() can poll cheaply, exactly like routines.
def add_workflow(name: str, spec: str, schedule: str = "", next_run: str | None = None,
                 enabled: bool = True) -> int:
    """Insert a workflow and return its id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO workflows(name, spec, enabled, schedule, next_run, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (name, spec, 1 if enabled else 0, schedule or "", next_run, _now(), _now()))
        return cur.lastrowid


def update_workflow(wid: int, name: str, spec: str, schedule: str = "",
                    next_run: str | None = None) -> None:
    """Overwrite a workflow's definition (name/spec/schedule/next_run). Enabled is left as-is
    (toggled via set_workflow_enabled)."""
    with _conn() as c:
        c.execute("UPDATE workflows SET name=?, spec=?, schedule=?, next_run=?, updated_at=? WHERE id=?",
                  (name, spec, schedule or "", next_run, _now(), wid))


def get_workflow(wid: int) -> dict | None:
    """Return one workflow row as a dict, or None."""
    with _conn() as c:
        r = c.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
        return dict(r) if r else None


def list_workflows() -> list[dict]:
    """All workflows, newest first (for the UI + list_workflows tool)."""
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM workflows ORDER BY id DESC").fetchall()]


def set_workflow_enabled(wid: int, enabled: bool) -> None:
    """Enable/disable a workflow (disabled scheduled workflows are skipped by due_workflows)."""
    with _conn() as c:
        c.execute("UPDATE workflows SET enabled=?, updated_at=? WHERE id=?",
                  (1 if enabled else 0, _now(), wid))


def delete_workflow(wid: int) -> bool:
    """Delete a workflow and its run history; True if a row was removed."""
    with _conn() as c:
        c.execute("DELETE FROM workflow_runs WHERE workflow_id=?", (wid,))
        cur = c.execute("DELETE FROM workflows WHERE id=?", (wid,))
        return cur.rowcount > 0


def due_workflows(now_iso: str) -> list[dict]:
    """Enabled, scheduled workflows whose next_run <= now_iso (ready to fire)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM workflows WHERE enabled=1 AND schedule!='' AND next_run IS NOT NULL "
            "AND next_run<=? ORDER BY next_run", (now_iso,)).fetchall()
        return [dict(r) for r in rows]


def set_workflow_next(wid: int, next_run: str | None) -> None:
    """Reschedule a workflow: set next_run and stamp last_run to now."""
    with _conn() as c:
        c.execute("UPDATE workflows SET next_run=?, last_run=?, updated_at=? WHERE id=?",
                  (next_run, _now(), _now(), wid))


def add_workflow_run(workflow_id: int, trigger: str = "manual", status: str = "running") -> int:
    """Open a run record for a workflow and return its id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO workflow_runs(workflow_id, status, trigger, started_at) VALUES(?,?,?,?)",
            (workflow_id, status, trigger, _now()))
        return cur.lastrowid


def set_workflow_run(run_id: int, status: str | None = None, log: str | None = None,
                     result: str | None = None, finished: bool = False) -> None:
    """Patch a run's status/log/result (COALESCE: None leaves a field as-is); finished stamps
    finished_at."""
    fin = _now() if finished else None
    with _conn() as c:
        c.execute("UPDATE workflow_runs SET status=COALESCE(?,status), log=COALESCE(?,log), "
                  "result=COALESCE(?,result), finished_at=COALESCE(?,finished_at) WHERE id=?",
                  (status, log, result, fin, run_id))


def list_workflow_runs(workflow_id: int, limit: int = 20) -> list[dict]:
    """Recent runs for a workflow, newest first."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY id DESC LIMIT ?",
            (workflow_id, limit)).fetchall()]


def fail_orphaned_workflow_runs() -> int:
    """At startup, any run still 'running' is a crash orphan — mark it error so the UI isn't stuck."""
    with _conn() as c:
        cur = c.execute("UPDATE workflow_runs SET status='error', "
                        "result=COALESCE(result,'(interrupted — app restarted)'), finished_at=? "
                        "WHERE status='running'", (_now(),))
        return cur.rowcount


# ------------------------------------------------------------------- recipes
# Procedural memory: the tool sequence that successfully accomplished a computer-use task, keyed
# by the goal, so a similar future task can reuse the approach (see memory.save_recipe/build_digest).
def add_recipe(task: str, task_norm: str, steps: str, cap: int = 200) -> None:
    """Upsert a recipe by normalized task (latest steps win), then cap the table to `cap` newest."""
    with _conn() as c:
        c.execute("DELETE FROM recipes WHERE task_norm=?", (task_norm,))
        c.execute("INSERT INTO recipes(task, task_norm, steps, created_at) VALUES(?,?,?,?)",
                  (task, task_norm, steps, _now()))
        c.execute("DELETE FROM recipes WHERE id NOT IN "
                  "(SELECT id FROM recipes ORDER BY id DESC LIMIT ?)", (cap,))


def list_recipes(limit: int = 200) -> list[dict]:
    """Recent recipes, newest first (for keyword-overlap recall in build_digest)."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM recipes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


# ------------------------------------------------------------------------ jobs
def add_job(prompt: str, agent: str = "auto") -> int:
    """Queue a background job and return its id. `agent` selects the specialist to run it
    ('auto' lets the router decide). Worker picks it up via next_queued_job()."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO jobs(prompt, status, agent, created_at, updated_at) VALUES(?,?,?,?,?)",
            (prompt, "queued", agent, _now(), _now()))
        return cur.lastrowid


def next_queued_job() -> dict | None:
    """Oldest still-queued job (FIFO by id), or None if the queue is empty."""
    with _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        return dict(r) if r else None


def update_job(jid: int, status: str, result: str | None = None) -> None:
    """Advance a job's status (e.g. queued -> running -> done/failed) and optionally store
    its result. COALESCE keeps the existing result when result is None."""
    with _conn() as c:
        c.execute("UPDATE jobs SET status=?, result=COALESCE(?, result), updated_at=? WHERE id=?",
                  (status, result, _now(), jid))


def get_job(jid: int) -> dict | None:
    """Return one job row as a dict, or None if no such id."""
    with _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return dict(r) if r else None


def list_jobs(limit: int = 20) -> list[dict]:
    """Most recent jobs (newest first), capped at `limit`."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


# ----------------------------------------------------------- conversation history
def upsert_conversation(cid: str, title: str | None = None) -> None:
    """Create or touch a conversation. `cid` is the Claude session id (see brain.py's
    --resume). On an existing row this bumps updated_at and only sets the title if one is
    given (COALESCE keeps the prior title when title is None)."""
    with _conn() as c:
        c.execute("INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
                  "title=COALESCE(excluded.title, conversations.title)",
                  (cid, title, _now(), _now()))


def add_message(cid: str, role: str, content: str) -> None:
    """Append a message ('user' or 'assistant') to a conversation's history."""
    with _conn() as c:
        c.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                  (cid, role, content, _now()))


def list_conversations(limit: int = 50) -> list[dict]:
    """Conversations ordered by most-recently-updated, capped at `limit` (for the UI list)."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()]


def conversation_messages(cid: str) -> list[dict]:
    """All messages in a conversation, in chronological order (ascending id)."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()]
