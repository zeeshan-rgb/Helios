"""Conversation reliability fixes (2026-10-23):
* long tools (business search, project checks) run as background tasks — the agent's ~1-minute tool
  timeout used to make agy "fail", and the watchdog + retry then ran the search twice
* a failed turn is retried only for a genuine stale conversation, never after tools ran / a timeout
* tool activity rows carry WHAT the step did (path / command / query) for the chat's dropdown
* long conversations continue in a fresh session with a recap (speed)
All fakes: no real agy, no network.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from helios import agy_cli, bg_tasks, conf, db, executive

_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ tool details for the dropdown

def _step(name, params, state="ACTIVE", i=3):
    return {"step_index": i, "step_type": "tool", "state": state, "tool_name": name,
            "tool_info": {"name": name, "parameters": params}}


def test_tool_detail_shows_path_command_query_and_mcp_args():
    assert agy_cli.tool_detail(_step("read_file", {"AbsolutePath": r"C:\proj\settings.toml"})) == \
        r"AbsolutePath: C:\proj\settings.toml"
    assert agy_cli.tool_detail(_step("run_command", {"CommandLine": "npm test", "Cwd": "C:/proj"})) == \
        "CommandLine: npm test\nCwd: C:/proj"
    assert agy_cli.tool_detail(_step("search_web", {"query": "salons in Malakpet"})) == "query: salons in Malakpet"
    d = agy_cli.tool_detail(_step("call_mcp_tool", {"ServerName": "helios", "ToolName": "prospects_run",
                                                    "Arguments": json.dumps({"area": "Malakpet", "research": 8})}))
    assert d == "area: Malakpet\nresearch: 8"


def test_tool_detail_redacts_secrets_and_caps_length():
    d = agy_cli.tool_detail(_step("run_command", {"CommandLine": "curl -H 'x-api-key: sk-ant-abcdefghijklmnopqrstuvwxyz123456'"}))
    assert "sk-ant-abc" not in d and "[redacted]" in d
    assert len(agy_cli.tool_detail(_step("write_to_file", {"TargetFile": "a.txt", "CodeContent": "x" * 5000}))) <= 800


def test_consume_turn_reports_each_tool_once_then_done(monkeypatch):
    monkeypatch.setattr(agy_cli, "hooks_intact", lambda h: True)

    class S:
        conversation_id, handle = "c1", None

        def marker_ok(self):
            return True

        def events(self):
            yield {"event": "step_update", "step_update": {"step_index": 0, "step_type": "user_input", "state": "DONE"}}
            yield {"event": "step_update", "step_update": _step("read_file", {"AbsolutePath": "C:/a/b.txt"}, "ACTIVE", 1)}
            yield {"event": "step_update", "step_update": _step("read_file", {"AbsolutePath": "C:/a/b.txt"}, "ACTIVE", 1)}
            yield {"event": "step_update", "step_update": _step("read_file", {"AbsolutePath": "C:/a/b.txt"}, "DONE", 1)}
            yield {"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}
    seen = []
    res = agy_cli.consume_turn(S(), on_tool_info=seen.append)
    assert res["text"] == "ok" and res["tools"] == ["read_file"]
    assert [(s["id"], s["state"]) for s in seen] == [(1, "ACTIVE"), (1, "DONE")]
    assert seen[0]["detail"] == "AbsolutePath: C:/a/b.txt"


# ------------------------------------------------------------------ background tasks

@pytest.fixture
def tasks(monkeypatch):
    monkeypatch.setattr(executive, "cfg", lambda: dict(executive._DEFAULTS))
    return monkeypatch


def test_task_runs_and_is_announced(tasks):
    tasks.setattr(bg_tasks, "RUNNERS", {**bg_tasks.RUNNERS, "prospects_run": lambda area="", research=8:
                                        (f"Business search in {area} finished: 3 qualified", "details…")})
    pub = []
    t = bg_tasks.start("prospects_run", {"area": "Malakpet", "research": 8}, publish=lambda k, d: pub.append((k, d)),
                       run_inline=True)
    row = bg_tasks.get(t["id"])
    assert t["status"] == "started" and row["status"] == "done" and "3 qualified" in row["summary"]
    assert any(k == "status" and "Business search in Malakpet finished" in d for k, d in pub)
    assert any(k == "dashboard" and d["event"] == "task_done" for k, d in pub)
    assert any(e["type"] == "task_done" for e in executive.events())
    assert "finished" in bg_tasks.format_status(row) and "details" in bg_tasks.format_status(row)


def test_task_failure_is_reported_not_hidden(tasks):
    def boom(area="", research=8):
        raise RuntimeError("Overpass down")
    tasks.setattr(bg_tasks, "RUNNERS", {**bg_tasks.RUNNERS, "prospects_run": boom})
    t = bg_tasks.start("prospects_run", {"area": "X"}, run_inline=True)
    row = bg_tasks.get(t["id"])
    assert row["status"] == "failed" and "Overpass down" in row["summary"]
    assert any(e["type"] == "task_failed" and e["severity"] == "HIGH" for e in executive.events())


def test_same_request_joins_the_running_task(tasks):
    import threading
    gate = threading.Event()
    tasks.setattr(bg_tasks, "RUNNERS", {**bg_tasks.RUNNERS, "prospects_run": lambda area="", research=8:
                                        (gate.wait(5), ("done", ""))[1]})
    a = bg_tasks.start("prospects_run", {"area": "Malakpet", "research": 8})
    b = bg_tasks.start("prospects_run", {"area": "Malakpet", "research": 8})
    c = bg_tasks.start("prospects_run", {"area": "Tolichowki", "research": 8})
    gate.set()
    import time
    for _ in range(100):                                   # let the workers finish inside this test's DB
        if all(bg_tasks.get(t["id"])["status"] != "running" for t in (a, c)):
            break
        time.sleep(0.05)
    assert b =={"id": a["id"], "status": "already_running", "title": "Business search"}
    assert c["status"] == "started" and c["id"] != a["id"]


def test_unknown_task_kind_refused():
    with pytest.raises(ValueError):
        bg_tasks.start("delete_everything", {})


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("helios_mcp_server_tasks", _ROOT / "mcp" / "helios_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prospects_run_returns_at_once_when_the_app_is_up(srv, monkeypatch):
    posted = []
    monkeypatch.setattr(srv, "_app_alive", lambda: True)
    monkeypatch.setattr(srv, "_app_post", lambda path, payload: (posted.append((path, payload)),
                                                                 {"id": 7, "status": "started"})[1])
    out = srv.prospects_run("Malakpet", 8)
    assert posted == [("/tasks/start", {"kind": "prospects_run", "args": {"area": "Malakpet", "research": 8}})]
    assert "task #7" in out and "background" in out
    monkeypatch.setattr(srv, "_app_post", lambda path, payload: {"id": 7, "status": "already_running"})
    assert "already running" in srv.prospects_run("Malakpet", 8)


def test_project_checks_also_go_to_the_background(srv, monkeypatch):
    monkeypatch.setattr(srv, "_app_alive", lambda: True)
    monkeypatch.setattr(srv, "_app_post", lambda path, payload: {"id": 9, "status": "started"})
    assert "task #9" in srv.run_project_checks("Maqsusi")


def test_without_the_app_the_tool_still_runs_inline(srv, monkeypatch):
    monkeypatch.setattr(srv, "_app_alive", lambda: False)
    monkeypatch.setattr(bg_tasks, "RUNNERS", {**bg_tasks.RUNNERS, "prospects_run": lambda area="", research=8:
                                              ("Business search finished: 1 qualified", "")})
    assert "1 qualified" in srv.prospects_run("Malakpet", 8)


# ------------------------------------------------------------------ retry + context refresh in the brain

class _FakeSession:
    def __init__(self):
        self.proc, self.conversation_id, self.sent = None, "conv-new", []

    def reset_marker(self):
        pass

    def send(self, p):
        self.sent.append(p)

    def alive(self):
        return False

    def stderr_text(self):
        return ""

    def close(self):
        pass


def _brain(monkeypatch, tmp_path, results):
    from helios.agy_brain import AntigravityBrain
    import helios.agy_brain as ab
    events, sessions, resumes = [], [], []
    b = AntigravityBrain(emit=lambda k, d: events.append((k, d)))
    monkeypatch.setattr(agy_cli, "command", lambda: ["agy"])
    monkeypatch.setattr(agy_cli, "SINK_FILE", tmp_path / "sink.txt")
    monkeypatch.setattr(b, "_with_memory", lambda p: p)
    monkeypatch.setattr(b, "_rules", lambda: "")

    def ensure(model, resume):
        resumes.append(resume)
        s = _FakeSession()
        sessions.append(s)
        return s
    monkeypatch.setattr(b, "_ensure_session", ensure)
    it = iter(results)
    monkeypatch.setattr(agy_cli, "consume_turn", lambda s, **k: next(it))
    monkeypatch.setattr(ab.db, "upsert_conversation", lambda *a, **k: None)
    monkeypatch.setattr(ab.db, "add_message", lambda *a, **k: None)
    return b, events, sessions, resumes


def _res(text="", tools=(), status=None, tin=0):
    return {"text": text, "errors": [], "gate_failure": None, "conversation_id": "conv-new", "status": status,
            "usage": {"in": tin, "out": 1}, "tools": list(tools)}


def test_turn_that_ran_tools_is_never_silently_repeated(monkeypatch, tmp_path):
    b, events, sessions, _ = _brain(monkeypatch, tmp_path, [_res(tools=["mcp__helios__prospects_run"]), _res("dup")])
    b.session_id = "old-conv"
    b._turn("find businesses in Malakpet", record=False)
    assert len(sessions) == 1                                          # no second run of the same request


def test_genuine_stale_conversation_still_retries_fresh(monkeypatch, tmp_path):
    b, events, sessions, resumes = _brain(monkeypatch, tmp_path, [_res(), _res("Hello sir.", status="SUCCESS")])
    b.session_id = "stale"
    assert b._turn("hi", record=False) == "Hello sir."
    assert resumes == ["stale", None]


def test_long_conversation_continues_fresh_with_a_recap(monkeypatch, tmp_path):
    b, events, sessions, resumes = _brain(monkeypatch, tmp_path, [_res("Sure.", status="SUCCESS", tin=50_000)])
    import helios.agy_brain as ab
    monkeypatch.setattr(ab.db, "conversation_messages", lambda sid: [
        {"role": "user", "content": "find salons in Malakpet"}, {"role": "assistant", "content": "Started task #7."}])
    b.session_id, b._last_in = "big-conv", 250_000
    assert b._turn("and in Tolichowki?", record=False) == "Sure."
    assert resumes == [None]                                           # fresh session
    sent = sessions[0].sent[0]
    assert "find salons in Malakpet" in sent and sent.rstrip().endswith("and in Tolichowki?")
    assert any(k == "status" and "refreshing my context" in d for k, d in events)
    assert b._last_in == 50_000


def test_small_conversation_keeps_resuming(monkeypatch, tmp_path):
    b, events, sessions, resumes = _brain(monkeypatch, tmp_path, [_res("ok", status="SUCCESS", tin=40_000)])
    b.session_id, b._last_in = "conv", 40_000
    b._turn("next", record=False)
    assert resumes == ["conv"]
