"""Phase 3 (PC control / cua-driver): normal screen control stays autonomous, while cua-driver tools
that kill processes, touch the clipboard, move local files into web pages, record the screen or
reconfigure the driver always need approval — through every engine's gate.

Isolated like test_yolo_hook: flag files in tmp, the app's /permission/ask stubbed to fail.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load(_ROOT / "hooks" / "pretooluse.py", "pretooluse_for_cua_tests")
agy_gate = _load(_ROOT / "hooks" / "agy_pretool.py", "agy_pretool_for_cua_tests")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(hook.conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(hook.conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(hook.conf, "SCREEN_LOCK", tmp_path / "screen.lock")

    def _boom(*a, **k):
        raise OSError("no app reachable in tests")
    monkeypatch.setattr(hook.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(agy_gate, "_policy", hook)
    monkeypatch.delenv("HELIOS_AGY_DENY_ALL", raising=False)
    return tmp_path


@pytest.mark.parametrize("action", ["click", "get_window_state", "get_desktop_state",
                                    "launch_app", "list_windows", "type_text", "set_value"])
def test_normal_screen_control_is_autonomous(isolated, action):
    assert hook.decide(f"mcp__computer__{action}", {"text": "Hello Helios"})[0] == "allow"


@pytest.mark.parametrize("action", sorted(hook._ASK_ACTIONS))
def test_risky_driver_tools_need_approval(isolated, action):
    decision, reason = hook.decide(f"mcp__computer__{action}", {})
    assert decision == "deny" and "unreachable" in reason     # routed to ask; no app -> deny


def test_yolo_can_preapprove_risky_driver_tools_but_not_panic(isolated):
    (isolated / "yolo.flag").write_text("on")
    assert hook.decide("mcp__computer__kill_app", {"pid": 1})[0] == "allow"
    (isolated / "abort.flag").write_text("stop")
    assert hook.decide("mcp__computer__click", {})[0] == "deny"


def test_antigravity_gate_applies_the_same_rules(isolated):
    assert agy_gate.agy_decide("call_mcp_tool", {"ServerName": "computer",
                                                 "ToolName": "clipboard_read"})[0] == "deny"
    assert agy_gate.agy_decide("call_mcp_tool", {"ServerName": "computer",
                                                 "ToolName": "get_desktop_state"})[0] == "allow"


def test_destructive_typing_still_hard_denied(isolated):
    assert hook.decide("mcp__computer__type_text", {"text": "format c:"})[0] == "deny"
    assert hook.decide("mcp__computer__hotkey", {"keys": ["win", "l"]})[0] == "deny"


def test_mcp_config_includes_driver_only_when_installed(tmp_path, monkeypatch):
    from helios import agy_cli
    exe = tmp_path / "cua-driver.exe"
    assert agy_cli._convert_server({"command": str(exe), "args": ["mcp"]}) is None
    exe.write_bytes(b"")
    assert agy_cli._convert_server({"command": str(exe), "args": ["mcp"]}) == \
        {"command": str(exe), "args": ["mcp"]}
