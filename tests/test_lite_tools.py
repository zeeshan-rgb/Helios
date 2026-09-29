"""The lite brain's tool dispatcher must enforce the SAME permission policy as the Claude hook.

Exercises helios/lite_tools.py LiteTools.execute() — the gate that protects a non-Claude engine.
Everything is isolated so the suite is safe to run while Helios is live: the abort/YOLO flag paths
are redirected to tmp files, side-effecting tool bodies are stubbed, and no network/fs/subprocess
work actually runs (we only assert the gate decision, not real execution).
"""

from __future__ import annotations

import pytest

from helios import conf
from helios.lite_tools import LiteTools


class FakePerms:
    """Stand-in PendingRegistry: records asks and returns a canned Approve/Deny decision."""

    def __init__(self, decision):
        self.decision = decision
        self.created = []

    def create(self, tool, inp, sink=""):
        self.created.append((tool, inp))
        self.sink = sink
        return "rid"

    def wait(self, rid, timeout):
        return self.decision

    def deny_all(self):
        pass


@pytest.fixture(autouse=True)
def isolate_flags(tmp_path, monkeypatch):
    """Never touch the live abort/YOLO flags."""
    monkeypatch.setattr(conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    return tmp_path


def _tools(decision="deny"):
    return LiteTools(emit=None, perms=FakePerms(decision))


# --------------------------------------------------------------------- hard rails
def test_panic_refuses_everything(isolate_flags):
    (isolate_flags / "abort.flag").write_text("stop", encoding="utf-8")
    t = _tools()
    assert t.execute("read_file", {"path": "x.txt"}).startswith("Refused")
    assert t.execute("system_health", {}).startswith("Refused")


def test_web_fetch_internal_is_ssrf_refused():
    t = _tools()
    out = t.execute("web_fetch", {"url": "http://127.0.0.1:8769/secret"})
    assert "SSRF" in out or out.startswith("Refused")


def test_write_to_claude_dir_refused():
    t = _tools()
    out = t.execute("write_file", {"path": "C:/Users/Tim/.claude/memory/x.md", "content": "y"})
    assert out.startswith("Refused")


# --------------------------------------------------------------------- classify gate
def test_mutating_shell_without_approver_is_denied():
    """run_shell with a mutating command classifies to 'ask'; no approver -> denied (not run)."""
    t = LiteTools(emit=None, perms=None)
    out = t.execute("run_shell", {"command": "Remove-Item C:/x.txt"})
    assert out.startswith("Denied")


def test_mutating_shell_denied_by_user(monkeypatch):
    t = _tools(decision="deny")
    monkeypatch.setattr(t, "_do_run_shell", lambda args: "RAN")  # would run if allowed
    out = t.execute("run_shell", {"command": "Remove-Item C:/x.txt"})
    assert out == "Denied by the user."
    assert t.perms.created and t.perms.created[0][0] == "PowerShell"


def test_mutating_shell_allowed_by_user_runs(monkeypatch):
    t = _tools(decision="allow")
    monkeypatch.setattr(t, "_do_run_shell", lambda args: "RAN")
    out = t.execute("run_shell", {"command": "Remove-Item C:/x.txt"})
    assert out == "RAN"


def test_read_only_shell_auto_allows(monkeypatch):
    """A read-only command is classified 'allow' — runs with NO approval prompt."""
    t = LiteTools(emit=None, perms=None)
    monkeypatch.setattr(t, "_do_run_shell", lambda args: "RAN")
    assert t.execute("run_shell", {"command": "Get-Process"}) == "RAN"


def test_yolo_bypasses_approval(isolate_flags, monkeypatch):
    (isolate_flags / "yolo.flag").write_text("on", encoding="utf-8")
    t = LiteTools(emit=None, perms=None)
    monkeypatch.setattr(t, "_do_run_shell", lambda args: "RAN")
    assert t.execute("run_shell", {"command": "Remove-Item C:/x.txt"}) == "RAN"


# --------------------------------------------------------------------- mapping + schemas
def test_classify_key_mapping():
    t = _tools()
    assert t._classify_key("read_file", {"path": "p"}) == ("Read", {"file_path": "p"})
    assert t._classify_key("write_file", {"path": "p"}) == ("Write", {"file_path": "p"})
    assert t._classify_key("run_shell", {"command": "c"}) == ("PowerShell", {"command": "c"})
    assert t._classify_key("web_fetch", {"url": "u"}) == ("WebFetch", {"url": "u"})
    assert t._classify_key("open_app", {"name": "n"})[0] == "mcp__computer__launch_app"
    assert t._classify_key("system_health", {})[0] == "mcp__helios__system_health"


def test_search_omitted_when_unconfigured():
    t = _tools()                       # no [search] backend in test config
    names = {s["function"]["name"] for s in t.schemas()}
    assert "web_search" not in names
    assert {"read_file", "write_file", "run_shell", "web_fetch", "memory_append"} <= names
    assert t.execute("web_search", {"query": "x"}).startswith(("Web search is not", "Unknown"))


def test_unknown_tool():
    assert _tools().execute("frobnicate", {}).startswith("Unknown tool")
