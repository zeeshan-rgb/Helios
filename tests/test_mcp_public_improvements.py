"""Multimodal-spec phase 11 — MCP integration improvements on the public server: capability
discovery, per-tool rate limiting, the new read-only tools, and policy-name parity with the
internal server (every public tool is decided under a real internal tool name)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from helios import conf, permissions

_ROOT = Path(__file__).resolve().parent.parent
SERVER = _ROOT / "mcp" / "helios_public_server.py"


def _load():
    spec = importlib.util.spec_from_file_location("helios_public_improve", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def srv(tmp_path, monkeypatch):
    mod = _load()
    calls = []

    class Gate:
        def decide(self, tool, args, session=""):
            calls.append(tool)
            return ("allow", "autonomous")
    monkeypatch.setattr(mod, "_policy", Gate())
    cfg = {"enabled": True, "read_only": False, "disabled_tools": [], "max_calls_per_minute": 3}
    monkeypatch.setattr(mod, "cfg", lambda: cfg)
    monkeypatch.setattr(mod, "_calls", {})
    from helios import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "helios.db")
    mod.calls, mod.cfg_dict = calls, cfg
    return mod


def test_policy_names_match_real_internal_tools():
    mod = _load()
    internal = set(re.findall(r"^def (\w+)\(", (_ROOT / "mcp" / "helios_server.py").read_text(encoding="utf-8"), re.M))
    for name, (policy_name, kind) in mod.TOOLS.items():
        base = policy_name.removeprefix("mcp__helios__")
        if name == "get_capabilities":
            continue                                        # describes this server itself
        assert base in internal, (name, policy_name)
        if kind == "read":
            assert permissions.classify(policy_name, {}) == "allow", name
            assert not permissions.is_outbound_send(policy_name)


def test_capabilities_reflect_the_live_switches(srv):
    out = srv.get_capabilities()
    assert "read + write" in out and "- open_app [action] available" in out
    srv.cfg_dict.update(read_only=True, disabled_tools=["search_research"])
    out = srv.get_capabilities()
    assert "READ-ONLY" in out and "- open_app [action] blocked (read-only)" in out
    assert "- search_research [read] disabled" in out and "permission policy" in out


def test_rate_limit_per_tool_and_window(srv):
    for _ in range(3):
        assert not srv.get_capabilities().startswith("Not done")
    out = srv.get_capabilities()
    assert out.startswith("Not done") and "slow down" in out
    assert not srv.get_night_mode_status().startswith("Not done")       # other tools unaffected
    assert srv._rate_limited("get_capabilities", now=10**9) == ""       # a minute later it's fine again
    assert srv.calls.count("mcp__helios__mcp_capabilities") == 3        # refused calls never reach the gate
    log = (conf.LOGS_DIR / "mcp_public.log").read_text(encoding="utf-8")
    assert "get_capabilities [read] refused" in log


def test_rate_limit_can_be_disabled(srv):
    srv.cfg_dict["max_calls_per_minute"] = 0
    for _ in range(10):
        assert not srv.get_capabilities().startswith("Not done")


def test_new_read_tools(srv, monkeypatch):
    monkeypatch.setattr("helios.night_mode.scheduler.status_text", lambda: "Night Mode is ON")
    assert srv.get_night_mode_status() == "Night Mode is ON"
    assert srv.calls[-1] == "mcp__helios__night_mode_status"


def test_business_data_stays_internal():
    mod = _load()
    assert not [n for n in mod.TOOLS if "prospect" in n or "lead" in n]
