"""Phase 11 Helios public MCP server: only the curated tools, every call decided by the same
PreToolUse gate (deny on gate failure / unanswered ask), [mcp_public] switches, audit log, memory
provenance, and the internal server refusing to run unless Helios launched it."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helios import conf, memory_store

_ROOT = Path(__file__).resolve().parent.parent
SERVER = _ROOT / "mcp" / "helios_public_server.py"

BLUEPRINT_TOOLS = {"get_system_status", "get_project_status", "search_memory", "save_memory",
                   "get_recent_memory", "create_reminder", "get_morning_report", "open_app",
                   "safe_diagnostic", "run_project_tests", "get_project_changes", "search_research",
                   "get_active_projects"}


def _load():
    spec = importlib.util.spec_from_file_location("helios_public_server_t", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def srv(tmp_path, monkeypatch):
    mod = _load()
    calls = []

    class Gate:
        verdict = ("allow", "autonomous")

        def decide(self, tool, args, session=""):
            calls.append((tool, args, session))
            return self.verdict
    gate = Gate()
    monkeypatch.setattr(mod, "_policy", gate)
    cfg = {"enabled": True, "read_only": False, "disabled_tools": []}
    monkeypatch.setattr(mod, "cfg", lambda: cfg)
    vault, pdir = tmp_path / "vault", tmp_path / "projects"
    vault.mkdir()
    pdir.mkdir()
    monkeypatch.setattr(memory_store, "vault", lambda: vault)
    monkeypatch.setattr(conf, "projects_dir", lambda: pdir)
    from helios import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "helios.db")
    mod.gate, mod.calls, mod.cfg_dict = gate, calls, cfg
    return mod


# ------------------------------------------------------------------ surface

def test_exposes_exactly_the_curated_tools():
    mod = _load()
    names = {t.name for t in asyncio.run(mod.mcp.list_tools())}
    assert names == set(mod.TOOLS) == BLUEPRINT_TOOLS
    risky = ("delete", "remove", "forget", "create_tool", "spawn", "mission", "notify", "send",
             "shell", "command", "cancel", "yolo", "approve")
    assert not [n for n in names if any(r in n for r in risky)]


def test_every_tool_maps_to_a_policy_name():
    mod = _load()
    for name, (internal, kind) in mod.TOOLS.items():
        assert internal.startswith("mcp__helios__") and kind in ("read", "write", "action")


# ------------------------------------------------------------------ policy

def test_calls_go_through_the_gate(srv):
    srv.get_active_projects()
    srv.search_memory("tea", "preferences")
    assert srv.calls[0] == ("mcp__helios__list_projects", {}, "mcp-public")
    assert srv.calls[1] == ("mcp__helios__recall_memory", {"query": "tea", "category": "preferences"},
                            "mcp-public")


def test_denied_by_policy(srv):
    srv.gate.verdict = ("deny", "denied by you")
    out = srv.open_app("notepad")
    assert out == "Not done: denied by Helios's permission policy: denied by you."
    assert srv.calls[-1][0] == "mcp__helios__open_app"


def test_gate_failure_means_deny(srv, monkeypatch):
    class Broken:
        def decide(self, *a):
            raise RuntimeError("boom")
    monkeypatch.setattr(srv, "_policy", Broken())
    assert "permission gate error" in srv.get_active_projects()


def test_real_gate_denies_an_unanswered_ask(srv, monkeypatch):
    monkeypatch.setattr(srv, "_policy", None)
    policy = srv._load_policy()
    from helios import permissions
    real = permissions.classify
    monkeypatch.setattr(permissions, "classify",
                        lambda t, i=None: "ask" if t == "mcp__helios__open_app" else real(t, i or {}))
    monkeypatch.setattr(policy.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("app not running")))
    assert srv.open_app("notepad").startswith("Not done: denied by Helios's permission policy")
    assert not srv.get_active_projects().startswith("Not done")         # reads stay allowed


# ------------------------------------------------------------------ settings

def test_switches(srv):
    srv.cfg_dict["read_only"] = True
    assert "read-only" in srv.save_memory("Prefers tea", "preferences")
    assert "read-only" in srv.create_reminder("stretch", in_minutes=5)
    assert not srv.get_active_projects().startswith("Not done")
    srv.cfg_dict.update(read_only=False, disabled_tools=["get_active_projects"])
    assert "disabled" in srv.get_active_projects()
    srv.cfg_dict["enabled"] = False
    assert "turned off" in srv.get_morning_report()
    # only the one allowed read reached the gate; every refusal happened before it
    assert [c[0] for c in srv.calls] == ["mcp__helios__list_projects"]


def test_every_call_is_audited(srv):
    srv.get_active_projects()
    srv.cfg_dict["read_only"] = True
    srv.open_app("notepad")
    log = (conf.LOGS_DIR / "mcp_public.log").read_text(encoding="utf-8")
    assert "get_active_projects [read] allow" in log and "open_app [action] refused" in log


# ------------------------------------------------------------------ tools

def test_memory_tools_and_provenance(srv):
    assert srv.save_memory("Prefers dark mode", "preferences").startswith("Remembered [preferences]")
    assert srv.save_memory("Always ask before deleting", "rules").startswith("Proposed to the user")
    assert srv.save_memory("my password is hunter22").startswith("Not saved")
    items = {i["text"]: i for i in memory_store.items()}
    assert items["Prefers dark mode"]["source"] == "mcp client"
    assert items["Always ask before deleting"]["status"] == "pending"
    found = srv.search_memory("dark mode")
    assert "Prefers dark mode" in found
    assert "Always ask" not in srv.search_memory("deleting")           # pending never exposed
    assert "Prefers dark mode" in srv.get_recent_memory()
    assert "Unknown category" in srv.search_memory("x", "passwords")


def test_create_reminder(srv):
    out = srv.create_reminder("Stretch", in_minutes=30)
    assert out.startswith("Reminder #") and "Stretch" in out
    from helios import db
    assert db.list_reminders()[0]["text"] == "Stretch"
    assert srv.create_reminder("   ", in_minutes=5) == "Nothing to remind about."


def test_safe_diagnostic_is_a_fixed_list(srv, monkeypatch):
    assert "Available:" in srv.safe_diagnostic("rm -rf /")
    assert srv.calls == []
    assert "GB free" in srv.safe_diagnostic("disk")
    from helios.night_mode import common
    monkeypatch.setattr(common, "online", lambda: False)
    assert srv.safe_diagnostic("network").startswith("OFFLINE")


def test_project_tools(srv, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (tmp_path / "projects" / "app.yaml").write_text(
        f"name: App\npath: {app.as_posix()}\ndependencies: off\nhealth_checks:\n"
        f"  - {{name: test, run: '{Path(sys.executable).as_posix()} -c pass'}}\n", encoding="utf-8")
    assert "App" in srv.get_active_projects()
    assert "App — UNKNOWN" in srv.get_project_status("app")
    assert "Tests: PASS" in srv.run_project_tests("app")
    assert srv.run_project_tests("") == "Name the project (see get_active_projects)."
    assert "no project" in srv.get_project_status("nope")


def test_briefing_and_research_tools(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    assert srv.get_morning_report(spoken=True).startswith("Good morning, sir.")
    assert "No research findings" in srv.search_research("MCP")


# ------------------------------------------------------------------ internal server guard

def test_internal_server_refuses_outside_launches():
    env = {k: v for k, v in os.environ.items() if k != "HELIOS_MCP_ROLE"}
    r = subprocess.run([sys.executable, str(_ROOT / "mcp" / "helios_server.py")], env=env,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 2 and "helios_public_server.py" in r.stderr


def test_helios_launches_its_internal_server_with_the_marker():
    tpl = json.loads((_ROOT / "config" / "mcp.json.template").read_text(encoding="utf-8"))
    assert tpl["mcpServers"]["helios"]["env"]["HELIOS_MCP_ROLE"] == "internal"
    live = _ROOT / "config" / "mcp.json"
    if live.exists():
        from helios import agy_cli
        assert agy_cli.mcp_servers()["helios"]["env"]["HELIOS_MCP_ROLE"] == "internal"


def test_public_server_starts_over_stdio():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}
    assert asyncio.run(asyncio.wait_for(go(), 60)) == BLUEPRINT_TOOLS
