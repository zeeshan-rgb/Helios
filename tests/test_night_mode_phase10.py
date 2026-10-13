"""Phase 10 — Night Mode improvements: power-aware heavy tasks, the hidden browser closed after the
night, and nightly memory hygiene (expired browsing / screen notes pruned and reported)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from helios import conf, memory, memory_store
from helios.night_mode import common, memory_extractor
from helios.night_mode import scheduler as sched


@pytest.fixture
def env(tmp_path, monkeypatch):
    data, vault = tmp_path / "data", tmp_path / "vault"
    data.mkdir(), vault.mkdir()
    monkeypatch.setattr(conf, "DATA_DIR", data)
    monkeypatch.setattr(conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(memory_store, "vault", lambda: vault)
    monkeypatch.setattr(memory, "VAULT", str(vault))
    cfg = {"enabled": True, "start": "01:00", "end": "05:00", "min_battery": 30}
    monkeypatch.setattr(sched, "cfg", lambda: cfg)
    monkeypatch.setattr(sched, "online", lambda: False)
    monkeypatch.setattr(sched, "_first_tick", False)
    return SimpleNamespace(cfg=cfg, vault=vault)


def _tasks(monkeypatch, names):
    ran = []

    def mk(n):
        def f(ctx):
            ran.append(n)
            return common.Result(summary=f"{n} done", completed=[n])
        return f
    monkeypatch.setattr(sched, "TASKS", {n: (mk(n), n) for n in names})
    sched.cfg()["schedule"] = {n: "01:00" for n in names}
    return ran


@pytest.mark.parametrize("bat,expect", [
    (None, ""),                     # desktop / unknown → run
    ((80, False), ""),              # on battery but plenty left
    ((20, True), ""),               # low but plugged in
    ((20, False), "on battery at 20%"),
])
def test_power_rule(env, bat, expect):
    why = sched.power_reason("research", read=lambda: bat)
    assert (expect in why) if expect else why == ""
    assert sched.power_reason("extract_memories", read=lambda: (5, False)) == ""   # light tasks always run


def test_power_rule_can_be_switched_off(env):
    env.cfg["min_battery"] = 0
    assert sched.power_reason("run_checks", read=lambda: (3, False)) == ""


def test_low_battery_skips_heavy_tasks_and_reports_it(env, monkeypatch):
    ran = _tasks(monkeypatch, ["research", "find_leads", "extract_memories"])
    monkeypatch.setattr(sched, "battery", lambda: (12, False))
    rec = sched.run_night(datetime(2026, 10, 14, 10, 0), record_job=False)
    assert ran == ["extract_memories"]
    assert rec["tasks"]["research"]["status"] == "skipped" and "battery at 12%" in rec["tasks"]["research"]["summary"]
    assert rec["status"] == "completed"                     # a power skip is not a failure
    assert any("save power" in line for line in rec["log"])


def test_hidden_browser_is_closed_after_the_night(env, monkeypatch):
    _tasks(monkeypatch, ["research"])
    closed = []
    fake = SimpleNamespace(close_shared=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, "helios.web.browser", fake)
    sched.run_night(datetime(2026, 10, 14, 10, 0), record_job=False)
    assert closed == [True]


def test_browser_close_survives_a_crashed_run(env, monkeypatch):
    closed = []
    monkeypatch.setitem(sys.modules, "helios.web.browser", SimpleNamespace(close_shared=lambda: closed.append(1)))
    monkeypatch.setattr(sched, "plan", lambda ws: (_ for _ in ()).throw(RuntimeError("bad config")))
    rec = sched.run_night(datetime(2026, 10, 14, 10, 0), record_job=False)
    assert rec["status"] == "crashed" and closed == [1]


def test_memory_task_prunes_expired_episodic_notes(env, monkeypatch):
    it = memory_store.remember("Checked the release notes page for the build tool", "browsing")["item"]
    # age it past the expiry
    old = (datetime.now() - timedelta(days=memory_store.EPISODIC_DAYS + 2)).isoformat(timespec="seconds")
    it["updated"] = it["created"] = old
    memory_store._write(it)
    monkeypatch.setattr("helios.learning.learn_from_daily", lambda day, use_llm: {"skipped": True})
    res = memory_extractor.run({"online": False, "days": ["2026-10-13"]})
    assert res.data["pruned_episodic"] == 1
    assert any("expired browsing/screen" in c for c in res.completed)
    assert memory_store.items("browsing") == []
