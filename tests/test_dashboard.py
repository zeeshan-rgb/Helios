"""Dashboard data layer (helios/dashboard.py) + its server routes: real data only, honest
"not exposed" fields, the draft-edit lock, and token / origin checks on the new routes."""

from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from helios import conf, dashboard, memory, usage
from helios.prospecting import geo, store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(conf, "VOICE_PID_FILE", tmp_path / "data" / "voice.pid")
    monkeypatch.setattr(memory, "VAULT", str(tmp_path / "vault"))
    monkeypatch.setattr(conf, "projects_dir", lambda: tmp_path / "projects")
    store._inited.clear()
    monkeypatch.setattr(dashboard, "_voice", {"state": None, "at": None, "followup": False, "wake": None, "wake_reason": ""})
    return tmp_path


def _drafted(i=1, name="Test Salon", email="hi@test-salon.example"):
    p, _ = store.upsert(geo.to_business({"type": "node", "id": i, "lat": 17.40 + i / 1000, "lon": 78.43,
                                         "tags": {"name": name, "shop": "hairdresser"}}, "Testpur"))
    store.update(p["id"], email=email, website_status="none", website_score=0, ai_score=3, priority="A",
                 package="D", draft={"channel": "email", "to": email, "subject": "Hello", "body": "Original text",
                                     "claims": [{"text": "no website", "evidence": "OSM + search"}]})
    for st in ("RESEARCHED", "QUALIFIED", "DRAFTED"):
        store.set_status(p["id"], st)
    return store.get(p["id"])


def test_empty_install_shows_zeros_and_empty_lists_not_made_up_data(env):
    m = dashboard.metrics()
    assert m["businesses"] == 0 and m["to_approve"] == 0 and m["tokens_today"] == 0
    assert dashboard.business() == [] and dashboard.coverage() == []
    assert dashboard.map_data()["points"] == [] and "every business" in dashboard.map_data()["note"]
    assert dashboard.featured() is None


def test_metrics_and_rows_come_from_the_store(env):
    p = _drafted()
    usage.record("chat", 1000, 200)
    m = dashboard.metrics()
    assert (m["businesses"], m["qualified"], m["to_approve"], m["tokens_today"]) == (1, 1, 1, 1200)
    row = dashboard.business()[0]
    assert row["id"] == p["id"] and row["status"] == "DRAFTED" and row["lat"] is not None
    assert dashboard.featured() == p["id"]
    assert dashboard.map_data()["attribution"] == geo.ATTRIBUTION
    assert any(u["kind"] == "approval" for u in dashboard.upcoming())


def test_provider_quota_is_never_invented(env, monkeypatch):
    monkeypatch.setattr(conf, "brain_engine", lambda: "antigravity")
    monkeypatch.setattr(dashboard, "engine_health",
                        lambda force=False: {"engine": "antigravity", "ok": True, "detail": "2 models", "at": None,
                                             "models": [{"id": "gemini-3.8-flash-low", "name": "x"}]})
    usage.record("research:topic", 5000, 500)
    L = dashboard.limits()
    p = L["provider"]
    assert p["session"] is None and p["weekly"] is None and p["reset"] is None and p["context_window"] is None
    assert "quota" in p["why"]
    assert L["local"]["today"] == {"calls": 1, "in": 5000, "out": 500}
    assert L["local"]["by_purpose"][0]["purpose"] == "research"
    offered = {m["role"]: m["offered"] for m in L["models"]}
    assert offered["Background (light)"] is True           # in `agy models`
    assert offered["Live chat"] is None                    # agy default: nothing to check


def test_agy_probe_never_runs_the_real_cli_in_tests(env, monkeypatch):
    monkeypatch.setattr(dashboard, "_probe", {"at": None, "ok": None, "detail": "", "models": [], "running": False})
    dashboard._probe_agy()                                  # conftest blocks the real agy binary
    assert dashboard._probe["ok"] is False and "not found" in dashboard._probe["detail"]


def test_voice_state_cache_tracks_followup_and_wake(env, monkeypatch):
    monkeypatch.setattr("helios.health._voice_daemon_running", lambda: True)
    dashboard.note_voice({"state": "listening", "followup": True, "wake": "hey_jarvis", "wake_reason": ""})
    v = dashboard.voice_status()
    assert v["state"] == "listening" and v["followup"] and v["wake_ready"] is True
    dashboard.note_voice({"state": "speaking", "level": 0.4})
    v = dashboard.voice_status()
    assert v["state"] == "speaking" and not v["followup"] and v["wake_word"] == "hey_jarvis"
    dashboard.note_voice({"state": "idle", "wake": "custom.onnx", "wake_reason": "custom wake model not found"})
    assert dashboard.voice_status()["wake_ready"] is False


def test_voice_state_hidden_when_daemon_not_running(env, monkeypatch):
    monkeypatch.setattr("helios.health._voice_daemon_running", lambda: False)
    dashboard.note_voice({"state": "listening"})
    assert dashboard.voice_status()["state"] is None


def test_modules_are_truthful(env, monkeypatch):
    monkeypatch.setattr(conf, "SETTINGS", {"voice": {"enabled": False}, "gmail": {"enabled": True},
                                           "prospecting": {"enabled": False}, "night_mode": {"enabled": False}})
    monkeypatch.setattr("helios.gmail.signed_in", lambda: False)
    mods = {m["id"]: m for m in dashboard.modules()}
    assert mods["voice"]["state"] == "off"
    assert mods["gmail"]["state"] == "warn" and "not signed in" in mods["gmail"]["detail"]
    assert mods["leads"]["state"] == "warn" and mods["night"]["state"] == "off"
    assert mods["projects"]["state"] == "off"


def test_prospect_view_has_audit_and_no_audit_for_no_site(env):
    p = _drafted()
    v = dashboard.prospect(p["id"])
    assert v["audit_view"]["audited"] is False and v["draft"]["body"] == "Original text"
    assert dashboard.prospect(99999) is None


def test_draft_edit_only_before_approval(env):
    p = _drafted()
    e = store.edit_draft(p["id"], subject="New subject", body="My own words", by="you")
    assert e["draft"]["body"] == "My own words" and e["draft"]["claims"]      # evidence kept
    assert e["contact_log"][-1]["event"] == "draft edited"
    with pytest.raises(store.StatusError, match="empty"):
        store.edit_draft(p["id"], subject="x", body="   ")
    store.approve(p["id"])
    with pytest.raises(store.StatusError, match="only a DRAFTED"):
        store.edit_draft(p["id"], subject="x", body="changed after approval")
    assert store.get(p["id"])["draft"]["body"] == "My own words"


def test_run_action_is_limited_and_reports_back(env, monkeypatch):
    import threading
    done = threading.Event()
    got = {}
    monkeypatch.setattr("helios.health.run_all", lambda name: [{"project": "A", "verdict": "OK"}])
    monkeypatch.setattr(dashboard, "_running", {})
    assert dashboard.run_action("rm -rf")["ok"] is False
    r = dashboard.run_action("project_health", emit=lambda d: (got.update(d), done.set()))
    assert r["ok"] and done.wait(5)
    assert got["event"] == "action_done" and "all OK" in got["text"]


# ------------------------------------------------------------------ server routes

@pytest.fixture
def srv(env, monkeypatch):
    from helios import server
    port = 8813
    monkeypatch.setattr(conf, "PORT", port)
    monkeypatch.setattr(conf, "auth_token", lambda: "tok-123")
    monkeypatch.setattr(dashboard, "engine_health",
                        lambda force=False: {"engine": "antigravity", "ok": None, "detail": "", "at": None, "models": []})

    class Brain:
        def busy(self):
            return False

        def __getattr__(self, n):
            return lambda *a, **k: None
    s = server.start(Brain(), None, server.Hub())
    yield port
    try:
        s.shutdown()
    except Exception:
        pass


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)


def test_dashboard_routes_need_the_token(srv):
    assert _req(srv, "GET", "/dashboard/summary")[0] == 401
    st, data = _req(srv, "GET", "/dashboard/summary", headers={"X-Auth-Token": "tok-123"})
    assert st == 200 and "metrics" in data and "modules" in data
    for path in ("limits", "business", "map", "activity", "upcoming", "projects", "report", "module?id=night"):
        assert _req(srv, "GET", "/dashboard/" + path, headers={"X-Auth-Token": "tok-123"})[0] == 200, path
    assert _req(srv, "GET", "/dashboard/prospect?id=abc", headers={"X-Auth-Token": "tok-123"})[0] == 400
    assert _req(srv, "GET", "/dashboard/module?id=nope", headers={"X-Auth-Token": "tok-123"})[0] == 404


def test_edit_and_run_routes_check_origin(srv):
    p = _drafted()
    tok = {"X-Auth-Token": "tok-123", "Content-Type": "application/json"}
    st, _ = _req(srv, "POST", "/prospects/action", {"id": p["id"], "action": "edit", "subject": "s", "body": "b"},
                 {**tok, "Origin": "http://evil.example"})
    assert st == 403
    st, data = _req(srv, "POST", "/prospects/action", {"id": p["id"], "action": "edit", "subject": "s", "body": "b"}, tok)
    assert st == 200 and data["item"]["draft"]["body"] == "b"
    assert _req(srv, "POST", "/dashboard/run", {"action": "bogus"}, tok)[0] == 409


def test_ui_has_no_hardcoded_quota_numbers():
    ui = Path(__file__).resolve().parent.parent / "helios" / "ui"
    js = (ui / "dashboard.js").read_text(encoding="utf-8")
    assert "Not exposed" in js
    for bad in ("% used", "weekly limit:", "tokens left"):
        assert bad not in js
