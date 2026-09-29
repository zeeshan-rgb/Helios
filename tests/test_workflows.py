"""Native workflow engine: spec validation, variable passing, condition-halt, and a full stubbed
run. Fully isolated — the DB is redirected to tmp, side_agent.run_task is stubbed (no real claude
-p spawns), notify is captured, and the abort flag points at an empty tmp path — so it's safe to
run while Helios is live.
"""

from __future__ import annotations

import json

import pytest

from helios import db, workflows


# --- validation ---------------------------------------------------------------------------------

def _good_spec():
    return {"name": "t", "trigger": {"type": "manual"},
            "steps": [{"id": "a", "type": "brain", "prompt": "hi"}]}


def test_validate_accepts_good_spec():
    ok, err = workflows.validate_spec(_good_spec())
    assert ok, err


@pytest.mark.parametrize("mutate,frag", [
    (lambda s: s.update(name=""), "name"),
    (lambda s: s.update(steps=[]), "at least one step"),
    (lambda s: s.update(steps=[{"id": "a", "type": "brain"}, {"id": "a", "type": "brain"}]), "duplicate"),
    (lambda s: s.update(steps=[{"id": "a", "type": "bogus"}]), "unknown type"),
    (lambda s: s.update(steps=[{"id": "bad id", "type": "brain"}]), "id of letters"),
    (lambda s: s.update(trigger={"type": "schedule", "schedule": "nonsense"}), "schedule is invalid"),
])
def test_validate_rejects_bad_specs(mutate, frag):
    s = _good_spec()
    mutate(s)
    ok, err = workflows.validate_spec(s)
    assert not ok and frag in err


def test_schedule_of():
    assert workflows.schedule_of({"trigger": {"type": "schedule", "schedule": "daily 08:00"}}) == "daily 08:00"
    assert workflows.schedule_of({"trigger": {"type": "manual"}}) == ""


# --- pure engine bits ---------------------------------------------------------------------------

@pytest.fixture
def mgr():
    return workflows.WorkflowManager(pool=None, emit=lambda *a, **k: None)


def test_resolve_variables(mgr):
    ctx = {"trigger": {"type": "manual", "who": "sir"}, "steps": {"a": "RESULT_A"}}
    assert mgr._resolve("x {{a}} y", ctx) == "x RESULT_A y"
    assert mgr._resolve("x {{a.output}} y", ctx) == "x RESULT_A y"
    assert mgr._resolve("hi {{trigger.who}}", ctx) == "hi sir"
    assert mgr._resolve("nothing here", ctx) == "nothing here"


def test_condition_true_false(mgr):
    ctx = {"trigger": {}, "steps": {"a": "hello world"}}
    assert mgr._condition({"source": "{{a}}", "op": "contains", "value": "world"}, ctx) == "true"
    assert mgr._condition({"source": "{{a}}", "op": "contains", "value": "zzz"}, ctx) == ""
    assert mgr._condition({"source": "", "op": "not_empty"}, ctx) == ""


# --- full run (stubbed) -------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    monkeypatch.setattr(workflows.conf, "ABORT_FLAG", tmp_path / "noabort.flag")
    toasts = []
    monkeypatch.setattr(workflows.notify, "toast", lambda title, msg: toasts.append((title, msg)))
    # No real claude -p: a brain/agent step echoes its (already variable-resolved) prompt.
    monkeypatch.setattr(workflows.side_agent, "run_task",
                        lambda prompt, persona, **kw: f"AGENT<{prompt}>")
    return {"toasts": toasts}


def test_full_run_passes_variables_and_finishes(isolated, mgr):
    spec = {"name": "t", "trigger": {"type": "manual"}, "steps": [
        {"id": "a", "type": "brain", "prompt": "hello"},
        {"id": "b", "type": "notify", "title": "T", "message": "got {{a.output}}"},
    ]}
    wid = db.add_workflow("t", json.dumps(spec), "", None)
    run_id = db.add_workflow_run(wid, "manual")
    mgr._execute(wid, run_id, spec, "manual")

    run = db.list_workflow_runs(wid)[0]
    assert run["status"] == "done"
    log = json.loads(run["log"])
    assert [e["status"] for e in log] == ["done", "done"]
    # Step a's output flowed into step b's notify message (variable substitution across steps).
    assert isolated["toasts"] and "AGENT<hello>" in isolated["toasts"][0][1]
    assert "AGENT<hello>" in run["result"]


def test_condition_halts_run(isolated, mgr):
    spec = {"name": "t", "trigger": {"type": "manual"}, "steps": [
        {"id": "guard", "type": "condition", "source": "", "op": "not_empty"},
        {"id": "after", "type": "notify", "title": "T", "message": "should not run"},
    ]}
    wid = db.add_workflow("t", json.dumps(spec), "", None)
    run_id = db.add_workflow_run(wid, "manual")
    mgr._execute(wid, run_id, spec, "manual")

    run = db.list_workflow_runs(wid)[0]
    assert run["status"] == "stopped"
    assert not isolated["toasts"]                       # the step after the failed guard never ran
    assert len(json.loads(run["log"])) == 1             # only the guard step is logged


def test_run_async_and_due_query(isolated, mgr):
    # A scheduled workflow with a past next_run shows up in due_workflows.
    spec = {"name": "s", "trigger": {"type": "schedule", "schedule": "daily 08:00"},
            "steps": [{"id": "a", "type": "brain", "prompt": "x"}]}
    wid = db.add_workflow("s", json.dumps(spec), "daily 08:00", "2000-01-01T08:00:00")
    due = db.due_workflows("2099-01-01T00:00:00")
    assert any(w["id"] == wid for w in due)
    # A disabled one is not due.
    db.set_workflow_enabled(wid, False)
    assert not any(w["id"] == wid for w in db.due_workflows("2099-01-01T00:00:00"))
