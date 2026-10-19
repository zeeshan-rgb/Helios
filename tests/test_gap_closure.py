"""Final-audit gap closure: the editable proposal (rate card only, evidence, never overwrites your
edits), the NEEDS_PRICING view, Antigravity process lifecycle (timeout kill, stderr / garbage
separation, missing CLI), and model-limit states (missing data, stale → refresh, provider failure)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helios import agy_cli, conf, dashboard, memory, memory_store
from helios.prospecting import engine, geo, proposal, quote, store

_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ proposal + NEEDS_PRICING

@pytest.fixture
def biz(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path / "vault")
    monkeypatch.setattr(memory, "VAULT", str(tmp_path / "vault"))
    store._inited.clear()
    cfg = {"currency": "INR", "pricing": {}}
    monkeypatch.setattr(quote, "cfg", lambda: cfg)
    return cfg


def _prospect(i=1, status="DRAFTED"):
    p, _ = store.upsert(geo.to_business({"type": "node", "id": 700 + i, "lat": 17.4, "lon": 78.4,
                                         "tags": {"name": f"Test Dental {i}", "amenity": "dentist"}}, "Testpur"))
    reasons = {"website": [{"factor": "no website discovered", "detail": "OSM lists none and a web search found none",
                            "evidence": "https://www.openstreetmap.org/node/701"}],
               "ai": [{"factor": "appointment business without online booking", "detail": "dentist; no booking found"}],
               "priority": "clear gap and a public contact"}
    store.update(p["id"], package="F", reasons=reasons, website_status="none", email="hi@test-dental.example")
    p = store.get(p["id"])
    store.update(p["id"], quote=quote.build(p))
    for st in ("RESEARCHED", "QUALIFIED", "DRAFTED")[: ("RESEARCHED", "QUALIFIED", "DRAFTED").index(status) + 1]:
        store.set_status(p["id"], st)
    return store.get(p["id"])


def test_proposal_without_rate_card_never_invents_a_price(biz):
    p = _prospect()
    res = proposal.write(p)
    t = res["text"]
    assert res["status"] == "created" and Path(res["path"]).parent.name == "Proposals"
    assert "Price to be confirmed" in t and "website_ai_leads_whatsapp = [min, max]" in t
    assert "₹" not in t and not __import__("re").search(r"\d{1,3},\d{3}", t)
    for section in ("What we noticed", "Recommended package", "Scope and deliverables", "Investment",
                    "Timeline", "Assumptions", "Not included", "Sources"):
        assert f"## {section}" in t, section
    assert "evidence: https://www.openstreetmap.org/node/701" in t
    assert "never sends" in t


def test_proposal_uses_the_rate_card_when_priced(biz):
    biz["pricing"] = {"website_ai_leads_whatsapp": [60000, 100000]}
    p = _prospect()
    store.update(p["id"], quote=quote.build(p))
    t = proposal.write(store.get(p["id"]))["text"]
    assert "**₹60,000–₹100,000** (from your rate card)" in t


def test_an_edited_proposal_is_never_overwritten(biz):
    p = _prospect()
    first = proposal.write(p)
    assert proposal.write(p)["status"] == "unchanged"
    Path(first["path"]).write_text(Path(first["path"]).read_text(encoding="utf-8").replace(
        "## Timeline", "## Timeline\n\nMy own note for this client."), encoding="utf-8")
    again = proposal.write(p)
    assert again["status"] == "new_version" and again["path"] != first["path"] and again["path"].endswith("-v2.md")
    assert "My own note for this client." in Path(first["path"]).read_text(encoding="utf-8")


def test_needs_pricing_view(biz):
    a = _prospect(1)
    biz["pricing"] = {"basic_website": [15000, 25000]}       # F still unpriced
    assert [p["id"] for p in engine.needs_pricing()] == [a["id"]]
    biz["pricing"]["website_ai_leads_whatsapp"] = [60000, 100000]
    store.update(a["id"], quote=quote.build(a))
    assert engine.needs_pricing() == []


def test_proposal_tool_and_route_are_wired_and_send_nothing():
    src = (_ROOT / "mcp" / "helios_server.py").read_text(encoding="utf-8")
    assert "def prospect_proposal(" in src
    from helios import permissions
    assert permissions.classify("mcp__helios__prospect_proposal", {"prospect_id": 1}) == "allow"
    assert not permissions.is_outbound_send("mcp__helios__prospect_proposal")
    p_src = (_ROOT / "helios" / "prospecting" / "proposal.py").read_text(encoding="utf-8")
    for verb in ("smtplib", "gmail", "send_", "requests.post", "urlopen"):
        assert verb not in p_src
    assert 'act == "proposal"' in (_ROOT / "helios" / "server.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ Antigravity process lifecycle

_FAKE = r'''
import json, os, sys, time, pathlib
mode = os.environ.get("FAKE_MODE", "")
if mode == "hang":
    time.sleep(120)
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
sys.stderr.write("WARN: telemetry disabled\n"); sys.stderr.flush()
out({"event": "init", "conversation_id": "c9"})
print("this line is not json")                      # garbage on stdout must be ignored
pathlib.Path(os.environ["HELIOS_AGY_MARKER"]).write_text("ok")
out({"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "Hi"}})
sys.stderr.write('{"event": "result", "result": {"response": "FROM STDERR"}}\n'); sys.stderr.flush()
out({"event": "result", "result": {"conversation_id": "c9", "status": "SUCCESS", "response": "Hi",
                                   "usage": {"input_tokens": 5, "output_tokens": 1}}})
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_agy.py"
    script.write_text(_FAKE, encoding="utf-8")
    base = tmp_path / "agy"
    monkeypatch.setattr(agy_cli, "RUNS_DIR", base / "runs")
    monkeypatch.setattr(agy_cli, "MARKERS_DIR", base / "markers")
    monkeypatch.setitem(agy_cli.conf.SETTINGS, "antigravity", {"bin": str(script)})
    monkeypatch.setattr(agy_cli, "mcp_servers", lambda: {})
    return script


def test_stdout_and_stderr_are_kept_apart(fake, monkeypatch):
    monkeypatch.delenv("FAKE_MODE", raising=False)
    res = agy_cli.run_once("hello", "rules", tools=False, timeout=60)
    assert res["text"] == "Hi" and res["conversation_id"] == "c9"      # not "FROM STDERR"
    assert res["usage"]["in"] == 5 and not res["gate_failure"]


def test_a_hung_agy_is_killed_at_the_timeout(fake, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    t0 = time.monotonic()
    res = agy_cli.run_once("hello", "rules", tools=False, timeout=2)
    assert time.monotonic() - t0 < 30 and res["text"] == ""
    assert not list((agy_cli.RUNS_DIR).glob("*"))                       # workspace cleaned up


def test_missing_agy_fails_gracefully(monkeypatch):
    monkeypatch.setattr(agy_cli, "command", lambda: [])
    assert agy_cli.run_once("hi", "rules")["errors"] == ["agy not found"]
    from helios.agy_brain import AntigravityBrain
    events = []
    b = AntigravityBrain(emit=lambda k, d: events.append((k, d)))
    assert b._turn("hello", record=False) == ""
    assert any(k == "error" and "isn't installed" in d for k, d in events)
    assert ("done", {"text": ""}) in events


# ------------------------------------------------------------------ voice: dormant is reported

def test_dormant_state_is_reported_not_shown_as_ready(monkeypatch):
    monkeypatch.setattr(dashboard, "_voice", {"state": None, "at": None, "followup": False, "wake": None, "wake_reason": ""})
    monkeypatch.setattr("helios.health._voice_daemon_running", lambda: True)
    monkeypatch.setattr(conf, "SETTINGS", {**conf.SETTINGS, "voice": {**conf.voice_cfg(), "enabled": True}})
    dashboard.note_voice({"state": "dormant", "wake": "hey_helios", "wake_reason": "custom wake model not found"})
    v = dashboard.voice_status()
    assert v["state"] == "dormant" and v["wake_ready"] is False
    voice = next(m for m in dashboard.modules() if m["id"] == "voice")
    assert voice["state"] == "ok" and "asleep" in voice["detail"] and "wake word not available" in voice["detail"]
    src = (_ROOT / "helios" / "voice" / "daemon.py").read_text(encoding="utf-8")
    assert 'post_voice_state("dormant"' in src and '_set_state("dormant")' in src
    js = (_ROOT / "helios" / "ui" / "dashboard.js").read_text(encoding="utf-8")
    assert '"Asleep — double-clap to wake"' in js


def test_night_indicator_never_passes_a_manual_run_off_as_a_night(monkeypatch):
    from helios.night_mode import scheduler as night
    monkeypatch.setattr(night, "enabled", lambda: True)
    monkeypatch.setattr(night, "is_running", lambda: False)
    monkeypatch.setattr(night, "runs", lambda n=10: [
        {"night": "manual-2026-10-14-002324", "kind": "manual", "status": "completed", "finished": "2026-10-14T00:23:29"},
        {"night": "2026-10-13", "kind": "scheduled", "status": "missed", "finished": "2026-10-13T19:21:00"}])
    m = next(x for x in dashboard.modules() if x["id"] == "night")
    assert m["state"] == "warn" and m["detail"] == "last night: missed · manual run: completed"


# ------------------------------------------------------------------ found by the live night of 2026-10-14

def _night_rec():
    return {"night": "2026-10-14", "kind": "scheduled", "status": "completed", "finished": "2026-10-14T11:20:35",
            "slept": [["2026-10-14T02:41:00", "2026-10-14T11:19:50", "research"]],
            "tasks": {"sync_projects": {"status": "ok", "summary": "10 project(s) scanned"},
                      "run_checks": {"status": "skipped", "summary": "on battery at 14% (below 30%) — skipped to save power"},
                      "research": {"status": "ok", "summary": "5 topic(s): 4 new finding(s)"},
                      "find_leads": {"status": "skipped", "summary": "the night window ended at 05:00 before it could start"},
                      "extract_memories": {"status": "skipped", "summary": "the night window ended at 05:00 before it could start"}}}


def test_briefing_never_calls_a_night_with_skips_problem_free(monkeypatch):
    from helios import briefing
    monkeypatch.setattr("helios.health.reports", lambda *a, **k: [])
    b = briefing.build(datetime(2026, 10, 14, 12, 0), rec=_night_rec())
    s = briefing._night_sentence(b)
    assert "without problems" not in s and "3 of 5 task(s) were skipped" in s
    assert "low battery: run checks" in s and "the night window ended: find leads, extract memories" in s
    text = briefing.render(b)
    assert "## Skipped tonight" in text and "asleep from 02:41 to 2026-10-14 11:19 during research" in text
    assert "skipped" in briefing.spoken(b)


def test_dashboard_counts_skipped_tasks(monkeypatch):
    from helios.night_mode import scheduler as night
    monkeypatch.setattr(night, "enabled", lambda: True)
    monkeypatch.setattr(night, "is_running", lambda: False)
    monkeypatch.setattr(night, "runs", lambda n=10: [_night_rec()])
    m = next(x for x in dashboard.modules() if x["id"] == "night")
    assert m["state"] == "warn" and "3 of 5 tasks skipped" in m["detail"]


def test_stale_running_briefing_is_refreshed(tmp_path, monkeypatch):
    from helios import briefing
    from helios.night_mode import scheduler as night
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path / "vault")
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("helios.health.reports", lambda *a, **k: [])
    day = datetime.now().date().isoformat()
    briefing._save_state({day: {"prepared": "x", "night_status": "running",
                                "spoken_text": "Good morning, sir. Night Mode is still running.", "spoken": False}})
    monkeypatch.setattr(night, "is_running", lambda: False)
    monkeypatch.setattr(briefing, "last_night_record", lambda now=None: _night_rec())
    assert "still running" not in briefing.latest_text(spoken_version=True)
    assert briefing._state()[day]["night_status"] == "completed"
    monkeypatch.setattr(night, "is_running", lambda: True)            # while it runs: leave it alone
    briefing._save_state({day: {"prepared": "x", "night_status": "running", "spoken_text": "still running"}})
    assert briefing._refresh_if_stale(day) is False


def test_research_stops_starting_topics_after_the_deadline(monkeypatch):
    from helios import research
    monkeypatch.setattr(research, "pick", lambda: [{"topic": "A", "project": ""}, {"topic": "B", "project": ""}])
    calls = []
    monkeypatch.setattr(research, "research_topic", lambda t, p, **k: (calls.append(t), {
        "topic": t, "project": p, "error": "", "new": [], "duplicates": [], "seconds": 1})[1])
    monkeypatch.setattr("helios.web.browser.close_shared", lambda: None)
    out = research.run(deadline=datetime.now() - timedelta(minutes=1))
    assert calls == [] and all(r["skipped"] for r in out)
    out = research.run(deadline=None)
    assert calls == ["A", "B"]


def test_sleep_during_a_task_is_recorded(tmp_path, monkeypatch):
    from helios.night_mode import common
    from helios.night_mode import scheduler as sched
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path / "vault")
    monkeypatch.setattr(conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(sched, "cfg", lambda: {"enabled": True, "start": "01:00", "end": "05:00",
                                               "schedule": {"research": "01:00"}})
    monkeypatch.setattr(sched, "online", lambda: True)

    def research_task(ctx):
        now = datetime.now()   # simulate: the app's heartbeat saw a 3-hour gap while this task ran
        sched._save_state({"gaps": [[(now - timedelta(hours=3)).isoformat(timespec="seconds"),
                                     now.isoformat(timespec="seconds")]]})
        return common.Result(summary="done")
    monkeypatch.setattr(sched, "TASKS", {"research": (research_task, "research")})
    t_before = datetime.now() - timedelta(hours=4)
    monkeypatch.setattr(sched, "slept_during", lambda a, b, real=sched.slept_during: real(t_before, b))
    rec = sched.run_night(record_job=False)
    assert rec.get("slept") and rec["slept"][0][2] == "research"
    assert any("asleep" in o for o in rec["tasks"]["research"]["observed"])


def test_overpass_falls_back_on_busy_servers_only(monkeypatch):
    import urllib.error
    monkeypatch.setattr(conf, "SETTINGS", {**conf.SETTINGS, "prospecting": {}})
    hits = []

    def fetch(url, data=None, timeout=60):
        hits.append(url)
        if url == geo.OVERPASS:
            raise urllib.error.HTTPError(url, 504, "Gateway Timeout", {}, None)
        return {"elements": [{"id": 1}]}
    assert geo._overpass("[out:json];", fetch=fetch, sleep=lambda s: None) == {"elements": [{"id": 1}]}
    assert hits == [geo.OVERPASS, "https://overpass.private.coffee/api/interpreter"]

    def bad_query(url, data=None, timeout=60):
        hits.append(url)
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
    hits.clear()
    with pytest.raises(urllib.error.HTTPError):
        geo._overpass("broken", fetch=bad_query, sleep=lambda s: None)
    assert hits == [geo.OVERPASS]                                       # a bad query isn't retried elsewhere
    monkeypatch.setattr(conf, "SETTINGS", {**conf.SETTINGS, "prospecting": {
        "overpass_endpoints": ["https://my.overpass.example/api/interpreter", "http://insecure.example"]}})
    assert geo.overpass_endpoints() == ["https://my.overpass.example/api/interpreter"]


def test_scan_only_never_researches(biz, monkeypatch):
    _prospect(1, status="RESEARCHED")
    called = []
    monkeypatch.setattr(engine, "scan_area", lambda *a, **k: {"area": "Testpur", "cells_scanned": 0,
                                                              "candidates": 0, "new": 0, "errors": []})
    monkeypatch.setattr(engine, "areas", lambda: [])
    monkeypatch.setattr(engine, "verify_presence", lambda **k: called.append("search") or {})
    monkeypatch.setattr(engine, "research", lambda pid, **k: called.append(pid) or {"status": "QUALIFIED"})
    monkeypatch.setattr(engine, "_research_queue", lambda n: [{"id": 1, "name": "x"}][:n])
    rep = engine.run(area="Testpur", max_research=0)
    assert called == [] and rep["researched"] == 0                      # "scan" = OSM only
    rep = engine.run(area="Testpur", max_research=1)
    assert called == ["search", 1] and rep["researched"] == 1


def test_schools_are_not_called_appointment_businesses():
    from helios.prospecting import scoring
    school = {"category": "education", "website_status": "ok", "website": "https://school.example",
              "audit": {"url": "https://school.example", "contact_form": False, "booking": False, "whatsapp": False}}
    _, reasons = scoring.ai_score(school)
    factors = [r["factor"] for r in reasons]
    assert "appointment business without online booking" not in factors
    assert "no online enquiry or admission form" in factors
    assert not any("works by appointment" in r["detail"] for r in reasons)
    school["audit"]["contact_form"] = True                              # has an enquiry form → no claim
    assert "no online enquiry or admission form" not in [r["factor"] for r in scoring.ai_score(school)[1]]
    from helios.prospecting.outreach import _AI_PITCH
    assert "no online enquiry or admission form" in _AI_PITCH


def test_rereseach_withdraws_a_draft_that_no_longer_qualifies(biz, monkeypatch):
    p = _prospect()
    store.update(p["id"], draft={"channel": "email", "to": "hi@test-dental.example", "subject": "s", "body": "b"})
    assert p["status"] == "DRAFTED"
    monkeypatch.setattr(engine.audit, "audit_prospect", lambda *a, **k: {})
    from helios.prospecting import scoring
    monkeypatch.setattr(scoring, "score", lambda p: {"website_score": 4, "ai_score": 1, "priority": "C",
                                                      "package": "", "reasons": {"website": [], "ai": []},
                                                      "qualified": False})
    r = engine.research(p["id"])
    assert r["status"] == "RESEARCHED" and r["draft"].get("withdrawn")
    assert r["contact_log"][-1]["event"].startswith("no longer qualifies")
    assert p["id"] not in [x["id"] for x in store.list_prospects(status="DRAFTED")]


def test_reresearch_never_overwrites_your_edited_draft(biz, monkeypatch):
    p = _prospect()
    store.edit_draft(p["id"], subject="My subject", body="My own carefully written email", by="you")
    monkeypatch.setattr(engine.audit, "audit_prospect", lambda *a, **k: {})
    from helios.prospecting import scoring
    monkeypatch.setattr(scoring, "score", lambda p: {"website_score": 0, "ai_score": 3, "priority": "A",
                                                      "package": "F", "reasons": p.get("reasons") or {},
                                                      "qualified": True})
    r = engine.research(p["id"])
    assert r["draft"]["body"] == "My own carefully written email" and r["draft"].get("outdated")


# ------------------------------------------------------------------ model-limit states

@pytest.fixture
def lim(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "brain_engine", lambda: "antigravity")
    monkeypatch.setattr(dashboard, "_probe", {"at": None, "ok": None, "detail": "not checked yet",
                                              "models": [], "running": False})
    class Started(list):
        run = None
    started = Started()
    pending = []

    class T:
        """Like a background thread, but deterministic: the check runs when the test calls
        `lim.run()` — after engine_health() returned (the real thread also runs after the lock
        is released; running it inline would deadlock on _probe_lock)."""
        def __init__(self, target=None, **k):
            self.target = target

        def start(self):
            started.append(1)
            pending.append(self.target)

    def run():
        while pending:
            pending.pop(0)()
    started.run = run
    # Replace the module's reference only — patching threading.Thread itself would hit every thread.
    import threading
    from types import SimpleNamespace
    monkeypatch.setattr(dashboard, "threading", SimpleNamespace(Thread=T, Lock=threading.Lock))
    return started


def test_missing_usage_and_never_checked(lim, monkeypatch):
    monkeypatch.setattr(dashboard, "_probe_agy", lambda: dashboard._probe.update(
        at=datetime.now().isoformat(timespec="seconds"), ok=True, detail="2 models", models=[], running=False))
    L = dashboard.limits()
    assert L["local"]["today"] == {"calls": 0, "in": 0, "out": 0} and L["local"]["last_call"] is None
    assert all(L["provider"][k] is None for k in ("session", "weekly", "reset", "context_window"))
    assert L["health"]["checking"] is True and lim == [1]                 # first view starts one check
    lim.run()
    assert dashboard.engine_health()["ok"] is True                        # result picked up next view


def test_fresh_result_is_reused_and_stale_result_is_refreshed(lim, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "_probe_agy", lambda: (calls.append(1), dashboard._probe.update(
        at=datetime.now().isoformat(timespec="seconds"), ok=True, detail="6 models", running=False)))
    dashboard.engine_health(); lim.run()
    dashboard.engine_health(); lim.run()
    assert len(calls) == 1                                                # cached within the TTL
    dashboard._probe["at"] = (datetime.now() - timedelta(seconds=dashboard.PROBE_TTL + 5)).isoformat(timespec="seconds")
    dashboard.engine_health(); lim.run()
    assert len(calls) == 2                                                # stale → refreshed
    dashboard.engine_health(force=True); lim.run()
    assert len(calls) == 3                                                # refresh button


def test_provider_failure_is_reported_not_hidden(lim, monkeypatch):
    class R:
        stdout, stderr, returncode = "", "Error: authentication required — please sign in", 1
    monkeypatch.setattr(agy_cli, "command", lambda: ["agy.exe"])
    monkeypatch.setattr(dashboard.subprocess, "run", lambda *a, **k: R())
    dashboard.engine_health(force=True); lim.run()
    h = dashboard.engine_health()
    assert h["ok"] is False and "sign in" in h["detail"]
    monkeypatch.setattr(dashboard.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(dashboard.subprocess.TimeoutExpired("agy", 45)))
    dashboard.engine_health(force=True); lim.run()
    h = dashboard.engine_health()
    assert h["ok"] is False and "45 s" in h["detail"]
