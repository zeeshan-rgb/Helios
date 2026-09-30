"""Phase 13 scheduling: one-shot + recurring jobs, enable/disable, status, next run, failure state
(auto-pause), execution history, catch-up without backlog, heavy jobs waiting for Night Mode,
interrupted-run recovery, system jobs (night_mode, morning_briefing) mirroring their settings."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helios import briefing, conf, jobs, memory_store
from helios.night_mode import scheduler as night

_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 1, 12, 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path / "vault")
    monkeypatch.setattr(conf, "projects_dir", lambda: tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    ncfg = {"enabled": True, "start": "01:00", "end": "05:00"}
    bcfg = {"enabled": True, "time": "08:00"}
    monkeypatch.setattr(night, "cfg", lambda: ncfg)
    monkeypatch.setattr(briefing, "cfg", lambda: bcfg)
    outcomes = []

    def fake(args):
        return outcomes.pop(0) if outcomes else ("ok", f"did it {args}")
    monkeypatch.setattr(jobs, "TYPES", dict(jobs.TYPES) | {
        "project_health": ("Project health checks", fake, True),
        "learn": ("Learn", fake, False)})
    started = []
    return type("E", (), {"tmp": tmp_path, "ncfg": ncfg, "bcfg": bcfg, "outcomes": outcomes,
                          "started": started, "run": lambda f: (started.append(f), f())[1]})


# ------------------------------------------------------------------ schedules

@pytest.mark.parametrize("s", ["daily 07:30", "weekdays 09:00", "weekly mon 09:00", "hourly",
                               "every 30m", "every 2h", "once 2026-10-02T09:00", "once 2026-10-02 09:00"])
def test_valid_schedules(s):
    assert jobs.valid_schedule(s)


@pytest.mark.parametrize("s", ["", "sometimes", "daily 25:00", "once tomorrow", "once 2026-13-40T09:00"])
def test_invalid_schedules(s):
    assert not jobs.valid_schedule(s)


def test_one_shot_next_run():
    assert jobs.next_after("once 2026-10-02T09:00", NOW) == datetime(2026, 10, 2, 9, 0)
    assert jobs.next_after("once 2026-09-01T09:00", NOW) is None


# ------------------------------------------------------------------ system jobs

def test_system_jobs_mirror_their_settings(env, monkeypatch):
    jobs.ensure_system_jobs(NOW)
    rows = {j["name"]: j for j in jobs.all_jobs() if j["system"]}
    assert set(rows) == {"night_mode", "morning_briefing"}
    assert rows["night_mode"]["schedule"] == "nightly 01:00–05:00 window"
    # the listing refreshes system rows against the real clock
    assert rows["night_mode"]["next_run"] == night.next_window()[0].isoformat(timespec="seconds")
    assert rows["morning_briefing"]["schedule"].startswith("daily 08:00")
    changes = []
    monkeypatch.setattr(conf, "update_settings", lambda ch: changes.append(ch))
    jobs.set_enabled("night_mode", False, NOW)
    jobs.set_enabled("morning_briefing", False, NOW)
    assert changes == [{"night_mode.enabled": False}, {"briefing.enabled": False}]
    with pytest.raises(jobs.JobError, match="disable it instead"):
        jobs.remove("night_mode")
    env.ncfg["enabled"] = False
    jobs.ensure_system_jobs(NOW)
    assert jobs.get("night_mode")["enabled"] == 0 and jobs.get("night_mode")["next_run"] is None


def test_night_mode_and_briefing_record_their_runs(env, monkeypatch):
    monkeypatch.setattr(night, "TASKS", {"a": (lambda ctx: __import__("helios.night_mode.common",
                                        fromlist=["Result"]).Result(summary="x"), "a")})
    env.ncfg["schedule"] = {"a": "01:00"}
    monkeypatch.setattr(night, "online", lambda: False)
    night.run_night()
    h = jobs.history("night_mode")
    assert h[0]["status"] == "ok" and h[0]["trigger"] == "manual" and "completed: 1 task" in h[0]["summary"]
    briefing.tick(datetime.now().replace(hour=9) if datetime.now().hour < 23 else datetime.now())
    assert jobs.history("morning_briefing")[0]["status"] == "ok"


# ------------------------------------------------------------------ user jobs

def test_add_validates(env):
    with pytest.raises(jobs.JobError, match="unknown job type"):
        jobs.add("rm_rf", "daily 07:00", now=NOW)
    with pytest.raises(jobs.JobError, match="bad schedule"):
        jobs.add("learn", "whenever", now=NOW)
    with pytest.raises(jobs.JobError, match="already in the past"):
        jobs.add("learn", "once 2026-09-01T09:00", now=NOW)
    with pytest.raises(jobs.JobError, match="no project"):
        jobs.add("project_health", "daily 07:00", args={"project": "Nope"}, now=NOW)
    jobs.add("learn", "daily 07:00", name="daily-learn", now=NOW)
    with pytest.raises(jobs.JobError, match="already exists"):
        jobs.add("learn", "daily 08:00", name="daily-learn", now=NOW)
    with pytest.raises(jobs.JobError, match="system job"):
        jobs.add("learn", "daily 08:00", name="night_mode", now=NOW)


def test_due_job_runs_on_its_own_thread_and_is_recorded(env):
    j = jobs.add("learn", "daily 12:30", name="learner", args={"days": 2}, now=NOW)
    assert j["next_run"] == "2026-10-01T12:30:00"
    assert jobs.tick(NOW, start_thread=env.run) == []                      # not due yet
    assert jobs.tick(NOW.replace(minute=30), start_thread=env.run) == ["learner"]
    j = jobs.get("learner")
    assert j["last_status"] == "ok" and "did it {'days': 2}" in j["last_summary"]
    assert j["next_run"] == "2026-10-02T12:30:00" and j["running"] == 0
    h = jobs.history("learner")
    assert h[0]["trigger"] == "schedule" and h[0]["finished_at"]


def test_catch_up_runs_once_without_a_backlog(env):
    jobs.add("learn", "daily 07:00", name="learner", now=NOW - timedelta(days=4))
    later = NOW + timedelta(hours=1)                                          # PC was off for days
    assert jobs.tick(later, start_thread=env.run) == ["learner"]
    assert jobs.tick(later + timedelta(minutes=1), start_thread=env.run) == []
    assert jobs.history("learner")[0]["trigger"] == "catch-up" and len(jobs.history("learner")) == 1
    assert jobs.get("learner")["next_run"] == "2026-10-02T07:00:00"


def test_disabled_and_running_jobs_are_not_started(env):
    jobs.add("learn", "every 30m", name="a", now=NOW)
    jobs.add("learn", "every 30m", name="b", now=NOW)
    jobs.set_enabled("a", False, NOW)
    jobs.tick(NOW, start_thread=env.run)          # past the first tick (which recovers stuck jobs)
    jobs._update(jobs.get("b")["id"], running=1)
    assert jobs.tick(NOW + timedelta(hours=1), start_thread=env.run) == []


def test_one_shot_runs_once(env):
    jobs.add("learn", "once 2026-10-01T12:05", name="once", now=NOW)
    assert jobs.tick(NOW.replace(minute=5), start_thread=env.run) == ["once"]
    j = jobs.get("once")
    assert j["next_run"] is None and j["last_status"] == "ok"
    assert jobs.tick(NOW + timedelta(days=1), start_thread=env.run) == []
    with pytest.raises(jobs.JobError, match="already run"):
        jobs.set_enabled("once", True, NOW + timedelta(days=1))


def test_failures_count_and_pause_the_job(env):
    jobs.add("learn", "every 30m", name="flaky", now=NOW)
    env.outcomes += [("failed", "boom")] * 2 + [("ok", "fine")] + [("failed", "boom")] * 5
    t = NOW
    for _ in range(3):
        t += timedelta(minutes=30)
        jobs.tick(t, start_thread=env.run)
    assert jobs.get("flaky")["fail_count"] == 0                               # an ok resets it
    for _ in range(5):
        t += timedelta(minutes=30)
        jobs.tick(t, start_thread=env.run)
    j = jobs.get("flaky")
    assert j["enabled"] == 0 and j["fail_count"] == 5 and j["last_summary"].startswith("PAUSED after 5")
    assert jobs.tick(t + timedelta(hours=1), start_thread=env.run) == []
    jobs.set_enabled("flaky", True, t)
    assert jobs.get("flaky")["fail_count"] == 0


def test_a_crashing_job_is_recorded_not_raised(env, monkeypatch):
    monkeypatch.setitem(jobs.TYPES, "learn", ("Learn", lambda a: 1 / 0, False))
    jobs.add("learn", "every 30m", name="crash", now=NOW)
    r = jobs.run_now("crash")
    assert r["status"] == "failed" and "ZeroDivisionError" in r["summary"]
    assert jobs.history("crash")[0]["trigger"] == "manual"


def test_heavy_jobs_wait_for_night_mode(env, monkeypatch):
    jobs.add("project_health", "every 30m", name="health", now=NOW)
    monkeypatch.setattr(jobs, "_night_running", lambda: True)
    assert jobs.tick(NOW + timedelta(minutes=30), start_thread=env.run) == []
    assert jobs.history("health")[0]["status"] == "skipped"
    assert "Night Mode was running" in jobs.get("health")["last_summary"]


def test_interrupted_runs_are_recovered(env):
    j = jobs.add("learn", "every 30m", name="cut", now=NOW)
    rid = jobs._start_run(j, "schedule", NOW)
    jobs._update(j["id"], running=1, last_status="running")
    jobs._first_tick = True
    jobs.tick(NOW, start_thread=env.run)
    assert jobs.get("cut")["last_status"] == "interrupted" and jobs.get("cut")["running"] == 0
    assert jobs.history("cut")[0]["status"] == "interrupted"


def test_remove_deletes_the_job_and_its_history(env):
    jobs.add("learn", "every 30m", name="gone", now=NOW)
    jobs.run_now("gone")
    jobs.remove("gone")
    assert jobs.get("gone") is None and jobs.history("gone") == []


def test_real_project_health_job(env, monkeypatch):
    monkeypatch.setattr(jobs, "TYPES", dict(jobs.TYPES) | {"project_health": (
        "Project health checks", jobs._run_project_health, True)})
    app = env.tmp / "app"
    app.mkdir()
    (env.tmp / "projects" / "app.yaml").write_text(
        f"name: App\npath: {app.as_posix()}\ndependencies: off\nhealth_checks:\n"
        f"  - {{name: test, run: '{Path(sys.executable).as_posix()} -c pass'}}\n", encoding="utf-8")
    jobs.add("project_health", "weekdays 09:00", name="app-health", args={"project": "App"}, now=NOW)
    r = jobs.run_now("app-health")
    assert r["status"] == "ok" and r["summary"] == "App: OK"


def test_formatting(env):
    jobs.add("learn", "daily 07:00", name="learner", now=NOW)
    jobs.run_now("learner")
    text = jobs.format_jobs()
    assert "night_mode [system]" in text and "learner — Learn" in text and "schedule: daily 07:00" in text
    assert "learner" in jobs.format_history(jobs.history())


# ------------------------------------------------------------------ brain tools

def test_mcp_job_tools(env):
    spec = importlib.util.spec_from_file_location("helios_server_p13", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert srv.create_job("learn", "daily 07:15", name="mcp-learn").startswith("Scheduled #")
    assert "Not scheduled: bad schedule" in srv.create_job("learn", "later")
    assert "mcp-learn" in srv.list_jobs() and "night_mode" in srv.list_jobs()
    assert "is now off" in srv.set_job_enabled("mcp-learn", False)
    assert srv.run_job_now("mcp-learn").startswith("mcp-learn: ok")
    assert "mcp-learn" in srv.job_history("mcp-learn")
    assert srv.delete_job("mcp-learn") == "Deleted mcp-learn."
    assert "disable it instead" in srv.delete_job("night_mode")
    assert jobs.get("mcp-learn") is None
    from helios import permissions
    assert permissions.classify("mcp__helios__delete_job", {}) == "ask"
    for t in ("list_jobs", "job_history", "create_job", "set_job_enabled", "run_job_now"):
        assert permissions.classify(f"mcp__helios__{t}", {}) == "allow"
