"""Phase 7 Night Mode: window/plan maths, isolated tasks, logging, the report sections, no
duplicate runs (per-night record + cross-process lock), missed-night reasons (off / not running /
asleep), interrupted-run recovery, the app tick, and the guarantee that Night Mode never writes into
Helios's own code."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios import conf, llm, memory_store
from helios.night_mode import common, morning_report
from helios.night_mode import scheduler as sched

_ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).as_posix()


@pytest.fixture
def env(tmp_path, monkeypatch):
    data, vault, pdir = tmp_path / "data", tmp_path / "vault", tmp_path / "projects"
    for d in (data, vault, pdir):
        d.mkdir()
    monkeypatch.setattr(conf, "DATA_DIR", data)
    monkeypatch.setattr(conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(conf, "projects_dir", lambda: pdir)
    monkeypatch.setattr(memory_store, "vault", lambda: vault)
    cfg = {"enabled": True, "start": "01:00", "end": "05:00"}
    monkeypatch.setattr(sched, "cfg", lambda: cfg)
    monkeypatch.setattr(sched, "online", lambda: False)
    monkeypatch.setattr(sched, "_first_tick", False)
    return SimpleNamespace(data=data, vault=vault, pdir=pdir, cfg=cfg, tmp=tmp_path)


def _fake_tasks(monkeypatch, fns: dict):
    monkeypatch.setattr(sched, "TASKS", {n: (f, n) for n, f in fns.items()})
    return {n: "01:00" for n in fns}


def ok_task(msg="done"):
    return lambda ctx: common.Result(summary=msg, completed=[msg])


def boom(ctx):
    raise RuntimeError("kaboom")


# ------------------------------------------------------------------ window / plan

def test_window_maths(env):
    w = sched.window_for(datetime(2026, 9, 30, 3, 0))
    assert w == (datetime(2026, 9, 30, 1, 0), datetime(2026, 9, 30, 5, 0))
    assert sched.window_for(datetime(2026, 9, 30, 0, 30))[0] == datetime(2026, 9, 29, 1, 0)
    env.cfg.update(start="23:00", end="05:00")
    assert sched.window_for(datetime(2026, 9, 30, 2, 0)) == (datetime(2026, 9, 29, 23, 0),
                                                           datetime(2026, 9, 30, 5, 0))
    env.cfg.update(start="bogus", end="25:99")                      # falls back to defaults
    assert sched.window_for(datetime(2026, 9, 30, 3, 0))[0].hour == 1


def test_plan_orders_tasks_and_reports_unknown(env):
    env.cfg.update(start="23:00", end="05:00",
                   schedule={"run_checks": "01:15", "sync_projects": "23:30", "nope": "02:00"})
    steps, problems = sched.plan(datetime(2026, 9, 29, 23, 0))
    assert [n for n, _ in steps] == ["sync_projects", "run_checks"]
    assert steps[1][1] == datetime(2026, 9, 30, 1, 15)                # wraps past midnight
    assert problems and "nope" in problems[0]
    env.cfg.pop("schedule")
    assert [n for n, _ in sched.plan(datetime(2026, 9, 30, 1, 0))[0]] == list(sched.DEFAULT_SCHEDULE)


# ------------------------------------------------------------------ the run

def test_manual_run_isolates_failures_logs_and_reports(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task("A worked"), "b": boom,
                                                    "c": ok_task("C worked")})
    rec = sched.run_night()
    assert rec["kind"] == "manual" and rec["status"] == "completed with errors"
    assert [rec["tasks"][n]["status"] for n in "abc"] == ["ok", "failed", "ok"]
    assert "kaboom" in rec["tasks"]["b"]["summary"]
    assert any("start" in l for l in rec["log"]) and any("finish" in l for l in rec["log"])
    report = Path(rec["report"]).read_text(encoding="utf-8")
    assert Path(rec["report"]).parent == env.vault / "Night Reports"
    for heading in ("## Completed", "## Observed", "## Suggested", "## Needs your approval",
                    "## Failed", "## Skipped"):
        assert heading in report
    assert "A worked" in report and "b: task failed" in report
    assert sched.load_run(rec["night"])["status"] == "completed with errors"
    assert not (env.data / "night" / "night.lock").exists()


def test_only_runs_selected_tasks(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task(), "b": ok_task()})
    rec = sched.run_night(only=["b", "zzz"])
    assert set(rec["tasks"]) == {"b", "zzz"} and rec["tasks"]["zzz"]["status"] == "failed"


def test_scheduled_run_is_once_per_night(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task()})
    now = datetime.now()
    env.cfg.update(start=(now - timedelta(minutes=5)).strftime("%H:%M"),
                   end=(now + timedelta(hours=1)).strftime("%H:%M"))
    env.cfg["schedule"] = {"a": env.cfg["start"]}
    first = sched.run_night(now, scheduled=True)
    assert first["status"] == "completed" and first["kind"] == "scheduled"
    assert sched.run_night(now, scheduled=True)["status"] == "duplicate"


def test_nothing_starts_after_the_window_end(env, monkeypatch):
    _fake_tasks(monkeypatch, {"a": ok_task()})
    real = datetime.now()
    env.cfg.update(start=(real - timedelta(hours=3)).strftime("%H:%M"),
                   end=(real - timedelta(minutes=2)).strftime("%H:%M"), schedule={"a": "00:00"})
    rec = sched.run_night(real - timedelta(hours=2), scheduled=True)
    assert rec["tasks"]["a"]["status"] == "skipped" and "window ended" in rec["tasks"]["a"]["summary"]


def test_scheduled_run_waits_for_task_time_and_honours_stop(env, monkeypatch):
    _fake_tasks(monkeypatch, {"later": ok_task()})
    real = datetime.now()
    env.cfg.update(start=(real - timedelta(minutes=10)).strftime("%H:%M"),
                   end=(real + timedelta(hours=3)).strftime("%H:%M"),
                   schedule={"later": (real + timedelta(hours=1)).strftime("%H:%M")})
    stop, naps = threading.Event(), []

    def fake_sleep(s):
        naps.append(s)
        stop.set()                                     # e.g. the app is shutting down
    rec = sched.run_night(real, scheduled=True, wait=True, stop=stop, sleep=fake_sleep)
    assert naps and naps[0] <= 30
    assert rec["tasks"]["later"]["status"] == "skipped" and "stopped" in rec["tasks"]["later"]["summary"]


def test_panic_skips_remaining_tasks(env, monkeypatch):
    def set_panic(ctx):
        conf.ABORT_FLAG.write_text("1")
        return common.Result(summary="first")
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": set_panic, "b": ok_task()})
    rec = sched.run_night()
    assert rec["tasks"]["a"]["status"] == "ok" and rec["tasks"]["b"]["status"] == "skipped"
    assert rec["status"] == "stopped (panic stop)"


def test_panic_from_earlier_in_the_day_does_not_cancel_the_night(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task()})
    conf.ABORT_FLAG.write_text("stop")
    old = datetime.now().timestamp() - 3600
    os.utime(conf.ABORT_FLAG, (old, old))
    rec = sched.run_night()
    assert rec["status"] == "completed" and rec["tasks"]["a"]["status"] == "ok"
    assert any("panic stop from earlier" in l for l in rec["log"])


def test_partial_runs_do_not_move_the_since_point(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task(), "b": ok_task()})
    sched.run_night(only=["a"])
    assert sched.last_successful_finish() is None
    sched.run_night()
    assert sched.last_successful_finish() is not None


def test_live_lock_blocks_and_is_left_alone(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task()})
    lock = env.data / "night" / "night.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()))
    assert sched.run_night()["status"] == "busy"
    assert lock.exists() and sched.is_running()


def test_stale_lock_from_dead_process_is_cleared(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task()})
    lock = env.data / "night" / "night.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("4000000")                        # no such pid
    monkeypatch.setattr(sched, "_pid_alive", lambda pid: False)
    assert sched.run_night()["status"] == "completed"


def test_crash_outside_tasks_is_recorded_not_silent(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task()})
    monkeypatch.setattr(sched, "plan", lambda ws: (_ for _ in ()).throw(ValueError("bad plan")))
    rec = sched.run_night()
    assert rec["status"] == "crashed" and any("CRASHED" in l for l in rec["log"])
    assert Path(rec["report"]).exists()


# ------------------------------------------------------------------ missed nights / tick

def test_heartbeat_records_gaps(env):
    t = datetime(2026, 9, 30, 0, 0)
    sched.heartbeat(t)
    sched.heartbeat(t + timedelta(seconds=15))
    sched.heartbeat(t + timedelta(hours=6))
    gaps = sched._state()["gaps"]
    assert len(gaps) == 1 and gaps[0][0].startswith("2026-09-30T00:00:15")


def test_missed_reasons(env, monkeypatch):
    ws, we, now = datetime(2026, 9, 30, 1), datetime(2026, 9, 30, 5), datetime(2026, 9, 30, 9)
    monkeypatch.setattr(sched, "boot_time", lambda: datetime(2026, 9, 30, 8))
    monkeypatch.setattr(sched, "app_start_time", lambda: datetime(2026, 9, 30, 8, 1))
    assert "off or restarted" in sched.missed_reason(ws, we, now)
    monkeypatch.setattr(sched, "boot_time", lambda: datetime(2026, 9, 1))
    assert "wasn't running" in sched.missed_reason(ws, we, now)
    monkeypatch.setattr(sched, "app_start_time", lambda: datetime(2026, 9, 29, 20))
    sched.heartbeat(datetime(2026, 9, 30, 0, 30))
    sched.heartbeat(datetime(2026, 9, 30, 7, 45))
    assert "asleep from 00:30" in sched.missed_reason(ws, we, now)


def test_tick_starts_marks_done_and_records_misses(env, monkeypatch):
    started = []
    env.cfg["enabled"] = False
    assert sched.tick(datetime(2026, 9, 30, 2), start_thread=started.append) == "disabled"
    env.cfg["enabled"] = True
    sched.tick(datetime(2026, 9, 29, 12))                               # enabled since noon
    assert sched.tick(datetime(2026, 9, 30, 2), start_thread=started.append) == "started"
    assert len(started) == 1
    sched.save_run({"night": "2026-09-30", "status": "completed", "tasks": {}})
    assert sched.tick(datetime(2026, 9, 30, 3), start_thread=started.append) == "done"
    monkeypatch.setattr(sched, "missed_reason", lambda ws, we, now: "the PC was asleep")
    assert sched.tick(datetime(2026, 10, 1, 8), start_thread=started.append) == "missed"
    rec = sched.load_run("2026-10-01")
    assert rec["status"] == "missed" and "asleep" in Path(rec["report"]).read_text(encoding="utf-8")
    assert sched.tick(datetime(2026, 10, 1, 8, 1), start_thread=started.append) == "done"


def test_no_false_miss_right_after_enabling(env):
    sched.tick(datetime(2026, 9, 30, 10))                  # first tick ever, after the window
    assert sched.load_run("2026-09-30") is None


def test_interrupted_run_is_recovered(env, monkeypatch):
    sched.save_run({"night": "2026-09-30", "status": "running", "kind": "scheduled",
                    "tasks": {}, "log": []})
    monkeypatch.setattr(sched, "_first_tick", True)
    sched.tick(datetime(2026, 9, 30, 12))
    rec = sched.load_run("2026-09-30")
    assert rec["status"] == "interrupted" and Path(rec["report"]).exists()


# ------------------------------------------------------------------ safety

def test_safe_write_refuses_helios_code(env, tmp_path):
    with pytest.raises(common.CoreWriteRefused):
        common.safe_write(conf.ROOT / "helios" / "evil.py", "x")
    with pytest.raises(common.CoreWriteRefused):
        common.safe_write(conf.ROOT / "config" / "settings.toml", "x")
    assert common.safe_write(tmp_path / "ok.txt", "fine").read_text() == "fine"


def _code_digest() -> str:
    h = hashlib.sha256()
    for f in sorted(list((conf.ROOT / "helios").rglob("*.py")) + list((conf.ROOT / "config").glob("*"))
                    + list((conf.ROOT / "hooks").glob("*.py")) + list((conf.ROOT / "mcp").glob("*.py"))):
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()


def _daily(vault: Path, day: str, you: str):
    d = vault / "Daily"
    d.mkdir(exist_ok=True)
    (d / f"{day}.md").write_text(f"# {day}\n\n## 10:00 — conversation\n**You:** {you}\n"
                                 f"**Helios:** Understood, sir.\n", encoding="utf-8")


def test_full_run_with_real_tasks(env, monkeypatch):
    app = env.tmp / "app"
    app.mkdir()
    (app / "main.py").write_text("x = 1\n", encoding="utf-8")
    (env.pdir / "app.yaml").write_text(
        f"name: App\npath: {app.as_posix()}\nhealth_checks:\n"
        f"  - {{name: ok, run: '{PY} -c pass'}}\n  - {{name: bad, run: '{PY} -c \"raise SystemExit(2)\"'}}\n",
        encoding="utf-8")
    today = datetime.now().date().isoformat()
    _daily(env.vault, today, "Never read the whole report aloud, keep it short.")
    monkeypatch.setattr(sched, "online", lambda: True)
    from helios import research
    monkeypatch.setattr(research, "cfg", lambda: {"enabled": True, "topics": ["MCP"],
                                                  "include_project_topics": False})
    monkeypatch.setattr(research, "check_source", lambda url: "ok")
    monkeypatch.setattr(research, "_call_agent", lambda t, p: ([{
        "title": "MCP 2.0 released", "summary": "Adds streaming.", "confidence": 0.9,
        "source_url": "https://modelcontextprotocol.io/blog/2", "source_name": "MCP blog"}], ""))
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"data": {"lessons": [
        {"text": "Keep spoken reports short", "type": "rule", "confidence": 0.95,
         "evidence": "Never read the whole report aloud"}]}})
    before = _code_digest()
    rec = sched.run_night()
    assert _code_digest() == before                              # Helios's code untouched
    t = rec["tasks"]
    assert t["run_checks"]["summary"] == "1 passed, 1 failed"
    assert t["research"]["status"] == "ok" and "1 new finding" in t["research"]["summary"]
    assert t["analyze_conversations"]["data"][today]["corrections"] == 1
    assert "1 new lesson" in t["extract_memories"]["summary"]
    report = Path(rec["report"]).read_text(encoding="utf-8")
    assert "App ok: PASS" in report and "App bad: FAIL (exit 2" in report
    assert "[MCP] MCP 2.0 released — MCP blog" in report
    assert memory_store.recall("MCP streaming") == []              # research stays out of memory
    assert report.count("Keep spoken reports short") == 1          # approvals de-duplicated
    assert memory_store.pending()[0]["text"] == "Keep spoken reports short"   # never auto-approved
    assert "App: ok PASS, bad FAIL" in report                    # project summary
    assert "bad failing" not in report                           # failures listed once, in Failed
    assert "App bad: FAIL (exit 2, " in report and "s) — \n" not in report


def test_offline_run_skips_research_and_uses_heuristics(env, monkeypatch):
    today = datetime.now().date().isoformat()
    _daily(env.vault, today, "Don't open Chrome for searches.")
    called = []
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: called.append(1))
    rec = sched.run_night(only=["research", "extract_memories"])
    assert "offline" in rec["tasks"]["research"]["summary"] and not called
    assert "no-AI fallback" in " ".join(rec["tasks"]["extract_memories"]["observed"])


def test_status_report_and_mcp_tools(env, monkeypatch):
    env.cfg["schedule"] = _fake_tasks(monkeypatch, {"a": ok_task("A worked")})
    assert "No night report" in sched.latest_report()
    rec = sched.run_night()
    assert "Night Mode is ON" in sched.status_text() and rec["night"] in sched.status_text()
    spec = importlib.util.spec_from_file_location("helios_server_p7", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert "A worked" in srv.night_report() and "next window" in srv.night_mode_status()
    from helios import permissions
    for tool in ("night_mode_status", "night_report"):
        assert permissions.classify(f"mcp__helios__{tool}", {}) == "allow"
