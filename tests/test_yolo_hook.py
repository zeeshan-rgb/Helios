"""YOLO mode must auto-approve the normal Approve/Deny prompts WITHOUT bypassing the hard rails.

Exercises hooks/pretooluse.py decide() directly. Everything is isolated so the suite is safe to run
while Helios is live: urlopen is stubbed to fail (so a test never reaches — or blocks 125s on — a real
/permission/ask), and the flag paths are redirected to tmp files (so a running Helios is never flipped).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load_hook():
    spec = importlib.util.spec_from_file_location("pretooluse_under_test", _ROOT / "hooks" / "pretooluse.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Redirect the flag files to tmp + make the /permission/ask HTTP call always fail."""
    monkeypatch.setattr(hook.conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(hook.conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(hook.conf, "SCREEN_LOCK", tmp_path / "screen.lock")

    def _boom(*a, **k):
        raise OSError("no app reachable in tests")
    monkeypatch.setattr(hook.urllib.request, "urlopen", _boom)
    return tmp_path


def _yolo_on(tmp_path):
    (tmp_path / "yolo.flag").write_text("on", encoding="utf-8")


def test_yolo_allows_normally_gated_tools(isolated):
    """With YOLO on, things that would normally pop an Approve/Deny prompt run autonomously."""
    _yolo_on(isolated)
    for tool, inp in [
        ("Bash", {"command": "Remove-Item C:/x.txt"}),     # mutating shell
        ("Write", {"file_path": "C:/Temp/run.ps1"}),       # executable script
        ("Bash", {"command": "git push"}),
        ("mcp__composio__GMAIL_SEND_EMAIL", {}),           # outbound send
    ]:
        decision, _ = hook.decide(tool, inp)
        assert decision == "allow", (tool, inp)


def test_yolo_still_denies_ssrf(isolated):
    _yolo_on(isolated)
    decision, _ = hook.decide("WebFetch", {"url": "http://127.0.0.1:8769/secret"})
    assert decision == "deny"


def test_yolo_still_denies_claude_dir_writes(isolated):
    _yolo_on(isolated)
    decision, _ = hook.decide("Write", {"file_path": "C:/Users/Tim/.claude/memory/x.md"})
    assert decision == "deny"


def test_yolo_still_obeys_panic(isolated):
    _yolo_on(isolated)
    (isolated / "abort.flag").write_text("stop", encoding="utf-8")   # panic engaged
    decision, _ = hook.decide("mcp__computer__click_element", {"element_index": 1})
    assert decision == "deny"


def test_without_yolo_gated_tool_is_not_auto_allowed(isolated):
    """No flag -> a normally-gated tool must NOT be auto-allowed (it routes to ask; here that path
    fails because urlopen is stubbed, i.e. it is not 'allow')."""
    decision, _ = hook.decide("Bash", {"command": "Remove-Item C:/x.txt"})
    assert decision != "allow"
