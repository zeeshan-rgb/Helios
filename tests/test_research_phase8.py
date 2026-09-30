"""Phase 8 research: configurable topics (settings + project manifests), validated and sourced
findings with confidence/uncertainty and new-vs-duplicate, a library kept apart from memory,
rotation, the Night Mode step, and the locked-down research mode in the agy permission gate."""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

from helios import agy_cli, conf, memory_store, research

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault, pdir = tmp_path / "vault", tmp_path / "projects"
    vault.mkdir()
    pdir.mkdir()
    monkeypatch.setattr(memory_store, "vault", lambda: vault)
    monkeypatch.setattr(conf, "projects_dir", lambda: pdir)
    cfg = {"enabled": True, "topics": ["MCP", "Gemini", "mcp"], "max_topics_per_run": 2,
           "max_findings_per_topic": 3}
    monkeypatch.setattr(research, "cfg", lambda: cfg)
    return type("Env", (), {"vault": vault, "pdir": pdir, "cfg": cfg, "tmp": tmp_path})


def F(title="MCP 2.0 released", url="https://modelcontextprotocol.io/blog/2", conf_=0.9, **kw):
    return {"title": title, "summary": kw.pop("summary", f"{title}. It adds streaming and auth."),
            "source_url": url, "source_name": "MCP blog", "published": "2026-09-20",
            "confidence": conf_, "uncertainty": "", **kw}


ok = lambda url: "ok"


# ------------------------------------------------------------------ topics

def test_topics_merge_settings_and_projects(env):
    app = env.tmp / "app"
    app.mkdir()
    (env.pdir / "app.yaml").write_text(
        f"name: Site\npath: {app.as_posix()}\nresearch_topics: [Next.js, gemini]\n", encoding="utf-8")
    ts = research.topics()
    assert [t["topic"] for t in ts] == ["MCP", "Gemini", "Next.js"]      # deduped, case-insensitive
    assert ts[2]["project"] == "Site"
    env.cfg["include_project_topics"] = False
    assert [t["topic"] for t in research.topics()] == ["MCP", "Gemini"]
    env.cfg.pop("topics")
    assert [t["topic"] for t in research.topics()] == research.DEFAULT_TOPICS


def test_rotation_prefers_least_recent(env):
    assert [t["topic"] for t in research.pick()] == ["MCP", "Gemini"]
    research.research_topic("MCP", call=lambda t, p: ([], ""), verify=ok)
    assert research.pick(1)[0]["topic"] == "Gemini"


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("raw,why", [
    (F(url=""), "source"), (F(url="http://127.0.0.1/admin"), "source"),
    (F(url="http://localhost:8769/x"), "source"), (F(url="file:///C:/secrets.txt"), "source"),
    (F(title=""), "title"), (F(summary="the api key: sk-abcdefghijklmnopqrstuvwx"), "secret"),
])
def test_bad_findings_are_dropped(raw, why):
    f, reason = research.clean(raw)
    assert f is None and why in reason


def test_clean_clamps_and_normalizes():
    f, _ = research.clean(F(conf_=7, published="2026-09-20T10:00:00Z"))
    assert f["confidence"] == 1.0 and f["published"] == "2026-09-20"
    f, _ = research.clean(F(conf_="high", published="last week"))
    assert f["confidence"] == 0.3 and f["published"] == ""


def test_redirects_to_private_addresses_are_blocked():
    h = research._SafeRedirect()
    with pytest.raises(urllib.error.URLError):
        h.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data")
    assert research.check_source("http://10.0.0.1/")[1] == "failed"


REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ"


def test_redirect_links_are_resolved_to_the_real_article(env):
    real = "https://blog.google/technology/gemini-3-8-live/"
    out = research.store("Gemini", "", [F(url=REDIRECT + "abc")], verify=lambda u: (real, "ok"))
    f = out["new"][0]
    assert f["source_url"] == real and f["source_check"] == "ok"
    # The same story next night arrives with a fresh redirect token: still a duplicate.
    out = research.store("Gemini", "", [F(title="Totally reworded headline here", url=REDIRECT + "xyz",
                                          summary="Different words entirely about something.")],
                         verify=lambda u: (real, "ok"))
    assert len(out["duplicates"]) == 1 and not out["new"]


def test_unresolved_redirect_counts_as_an_unopened_source(env):
    out = research.store("Gemini", "", [F(url=REDIRECT + "abc")], verify=lambda u: (u, "ok"))
    f = out["new"][0]
    assert f["source_check"] == "failed" and f["confidence"] == 0.45


# ------------------------------------------------------------------ storage

def test_store_records_every_required_field(env):
    out = research.store("MCP", "", [F()], verify=ok)
    assert len(out["new"]) == 1
    f = research.findings("MCP")[0]
    for k in ("id", "found", "topic", "source_url", "source_name", "summary", "confidence",
              "uncertainty", "status", "source_check"):
        assert k in f
    assert f["status"] == "new" and f["source_check"] == "ok" and f["confidence"] == 0.9
    assert Path(f["path"]).parent == env.vault / "Research Library" / "mcp"
    datetime.fromisoformat(f["found"])


def test_duplicates_by_url_or_text(env):
    research.store("MCP", "", [F()], verify=ok)
    out = research.store("MCP", "", [
        F(title="Totally different words", url="https://www.modelcontextprotocol.io/blog/2/"),
        F(title="MCP 2.0 released", url="https://news.example.com/mcp-2",
          summary="MCP 2.0 released. It adds streaming and auth."),
        F(title="Gemini 4 preview", url="https://blog.google/gemini-4",
          summary="Google previewed Gemini 4 with longer context."),
    ], verify=ok)
    assert len(out["duplicates"]) == 2 and len(out["new"]) == 1
    orig = [f for f in research.findings("MCP") if f["title"] == "MCP 2.0 released"][0]
    assert orig["seen"] == 3


def test_limits_and_unopened_sources(env):
    many = [F(title=f"Item {i} about something unique{i}", url=f"https://ex.com/{i}",
              summary=f"Distinct summary number {i} with words{i} alpha{i}") for i in range(6)]
    out = research.store("MCP", "", many, verify=lambda u: "failed")
    assert len(out["new"]) == 3 and "over the per-topic limit" in out["dropped"]
    f = out["new"][0]
    assert f["confidence"] == 0.45 and "did not open" in f["uncertainty"]


def test_findings_are_kept_apart_from_memory(env):
    research.store("MCP", "", [F()], verify=ok)
    assert memory_store.items() == []
    assert memory_store.recall("MCP streaming auth") == []
    assert "MCP" not in memory_store.digest_section("tell me about MCP streaming")
    kept = research.keep(research.findings()[0]["id"])                 # only on explicit request
    assert kept["status"] == "saved" and kept["item"]["category"] == "research"
    assert kept["item"]["source"].startswith("research finding res-")
    assert research.keep("nope")["status"] == "refused"


def test_findings_filters(env):
    research.store("MCP", "", [F()], verify=ok)
    research.store("Gemini", "", [F(title="Gemini 4 preview", url="https://blog.google/g4",
                                    summary="Google previewed Gemini 4.")], verify=ok)
    assert [f["topic"] for f in research.findings("gemini")] == ["Gemini"]
    assert [f["topic"] for f in research.findings(query="streaming")] == ["MCP"]
    assert len(research.findings(days=1)) == 2
    assert research.get(research.findings("MCP")[0]["id"])["title"] == "MCP 2.0 released"


# ------------------------------------------------------------------ running

def test_research_topic_reports_errors_and_counts(env):
    rep = research.research_topic("MCP", call=lambda t, p: ([], "no reply"), verify=ok)
    assert rep["error"] == "no reply" and rep["new"] == []
    rep = research.research_topic("MCP", call=lambda t, p: ([F(), "junk", F(url="")], ""), verify=ok)
    assert len(rep["new"]) == 1 and len(rep["dropped"]) == 2
    assert "MCP: 1 new" in research.format_run([rep])


def test_call_agent_uses_locked_down_research_mode(env, monkeypatch):
    seen = {}

    def fake_run_once(prompt, rules, **kw):
        seen.update(kw, prompt=prompt, rules=rules)
        return {"text": json.dumps({"findings": [F()]}), "errors": [], "gate_failure": None}
    monkeypatch.setattr(agy_cli, "run_once", fake_run_once)
    monkeypatch.setattr(conf, "brain_engine", lambda: "antigravity")
    raw, err = research._call_agent("MCP", "")
    assert not err and raw[0]["title"] == "MCP 2.0 released"
    assert seen["allow_only"] == agy_cli.RESEARCH_TOOLS and seen["tools"] is True
    assert "untrusted DATA" in seen["rules"] and "Research topic: MCP" in seen["prompt"]
    monkeypatch.setattr(conf, "brain_engine", lambda: "lite")
    assert "Antigravity" in research._call_agent("MCP", "")[1]


def test_call_agent_rejects_bad_replies(env, monkeypatch):
    monkeypatch.setattr(conf, "brain_engine", lambda: "antigravity")
    for reply, err in (({"text": "sorry, no", "errors": [], "gate_failure": None}, "JSON"),
                       ({"text": "", "errors": [], "gate_failure": "marker"}, "gate")):
        monkeypatch.setattr(agy_cli, "run_once", lambda *a, _r=reply, **k: _r)
        assert err in research._call_agent("MCP", "")[1]


def test_night_task(env, monkeypatch):
    monkeypatch.setattr(research, "_call_agent", lambda t, p: ([F(title=f"{t} news",
                        url=f"https://ex.com/{t}", summary=f"News about {t} this week.")], ""))
    monkeypatch.setattr(research, "check_source", ok)
    res = research.night_task({})
    assert res.status == "ok" and res.data["new"] == 2 and len(res.completed) == 2
    assert any("MCP news" in o for o in res.observed)
    res = research.night_task({})                       # same news again -> already known
    assert res.data["new"] == 0 and res.data["duplicates"] == 2
    monkeypatch.setattr(research, "_call_agent", lambda t, p: ([], "boom"))
    res = research.night_task({})
    assert res.status == "failed" and len(res.failed) == 2
    env.cfg["enabled"] = False
    assert research.night_task({}).status == "skipped"


def test_night_mode_research_step_uses_research(env, monkeypatch):
    from helios.night_mode import research_agent
    monkeypatch.setattr(research, "_call_agent", lambda t, p: ([], ""))
    assert research_agent.run({"online": True}).summary.startswith("2 topic(s)")
    assert "offline" in research_agent.run({"online": False}).summary


# ------------------------------------------------------------------ gate

def _agy_hook():
    spec = importlib.util.spec_from_file_location("agy_pretool_p8", _ROOT / "hooks" / "agy_pretool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("tool,args,verdict", [
    ("search_web", {"Query": "MCP news"}, "allow"),
    ("read_url_content", {"Url": "https://modelcontextprotocol.io/blog"}, "allow"),
    ("read_url_content", {"Url": "http://127.0.0.1:8769/health"}, "deny"),
    ("read_url_content", {"Url": "file:///C:/Users/x/.ssh/id_rsa"}, "deny"),
    ("run_command", {"CommandLine": "whoami"}, "deny"),
    ("write_to_file", {"TargetFile": "C:/x.txt", "CodeContent": "x"}, "deny"),
    ("view_file", {"AbsolutePath": "C:/Users/x/.ssh/id_rsa"}, "deny"),
    ("call_mcp_tool", {"ServerName": "helios", "ToolName": "remember"}, "deny"),
    ("open_browser_url", {"Url": "https://example.com"}, "deny"),
])
def test_research_mode_gate(monkeypatch, tool, args, verdict):
    monkeypatch.setenv("HELIOS_AGY_ALLOW_ONLY", "read_url_content,search_web")
    assert _agy_hook().agy_decide(tool, args)[0] == verdict


def test_research_mode_env_cannot_grant_other_tools(monkeypatch):
    monkeypatch.setenv("HELIOS_AGY_ALLOW_ONLY", "run_command,write_to_file")
    hook = _agy_hook()
    assert hook.agy_decide("run_command", {"CommandLine": "whoami"})[0] == "deny"
    assert hook.agy_decide("search_web", {"Query": "x"})[0] == "deny"


def test_child_env_sets_and_clears_research_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIOS_AGY_ALLOW_ONLY", "search_web")        # leaked from a parent
    assert "HELIOS_AGY_ALLOW_ONLY" not in agy_cli.child_env(tmp_path / "m")
    e = agy_cli.child_env(tmp_path / "m", allow_only={"search_web", "run_command"})
    assert e["HELIOS_AGY_ALLOW_ONLY"] == "search_web"


def test_research_runs_get_no_mcp_servers(monkeypatch):
    seen = {}

    def fake_prepare(ws, rules, *, tools=True):
        seen["tools"] = tools
        raise RuntimeError("stop here")
    monkeypatch.setattr(agy_cli, "command", lambda: ["agy"])
    monkeypatch.setattr(agy_cli, "prepare_workspace", fake_prepare)
    agy_cli.run_once("x", "rules", allow_only=agy_cli.RESEARCH_TOOLS)
    assert seen["tools"] is False
    agy_cli.run_once("x", "rules")
    assert seen["tools"] is True


# ------------------------------------------------------------------ tools

def test_mcp_research_tools(env):
    research.store("MCP", "", [F()], verify=ok)
    spec = importlib.util.spec_from_file_location("helios_server_p8", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert "MCP 2.0 released" in srv.research_findings("MCP")
    assert "streaming" in srv.research_findings(query="streaming", details=True)
    assert "No research findings" in srv.research_findings("Gemini")
    assert "Research is ON" in srv.research_topics() and "- Gemini: last researched never" in srv.research_topics()
    from helios import permissions
    for tool in ("research_findings", "research_topics"):
        assert permissions.classify(f"mcp__helios__{tool}", {}) == "allow"
