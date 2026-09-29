"""Background/side agents (HELIOS_AGENT_ROLE=side) must NEVER send anything outbound to real
people — the persona's HARD RULE, enforced structurally in the hook (bites even in YOLO). The
live interactive brain sets no role, so its behaviour is unchanged.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from helios import permissions

_ROOT = Path(__file__).resolve().parent.parent


def _load_hook():
    spec = importlib.util.spec_from_file_location("pretooluse_under_test", _ROOT / "hooks" / "pretooluse.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


# --- the detector -------------------------------------------------------------------------------

def test_is_outbound_send_detects_sends():
    assert permissions.is_outbound_send("mcp__composio__GMAIL_SEND_EMAIL")
    assert permissions.is_outbound_send("mcp__composio__GmailSendEmail")
    assert permissions.is_outbound_send("mcp__composio__GITHUB_CREATE_ISSUE_COMMENT")
    assert permissions.is_outbound_send("mcp__composio__TWITTER_POST_TWEET")


def test_is_outbound_send_ignores_reads_and_nonsends():
    assert not permissions.is_outbound_send("mcp__composio__GMAIL_LIST_MESSAGES")
    assert not permissions.is_outbound_send("mcp__composio__GMAIL_GET_SENT")   # 'sent' != 'send'
    assert not permissions.is_outbound_send("mcp__composio__GITHUB_LIST_COMMENTS")
    assert not permissions.is_outbound_send("Bash")
    assert not permissions.is_outbound_send("mcp__helios__set_reminder")


# --- structural enforcement in the hook ---------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(hook.conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(hook.conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(hook.conf, "SCREEN_LOCK", tmp_path / "screen.lock")

    def _boom(*a, **k):
        raise OSError("no app reachable in tests")
    monkeypatch.setattr(hook.urllib.request, "urlopen", _boom)
    return tmp_path


def test_side_agent_outbound_hard_denied(isolated, monkeypatch):
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    d, _ = hook.decide("mcp__composio__GMAIL_SEND_EMAIL", {"to": "x@y.com"})
    assert d == "deny"


def test_side_agent_outbound_denied_even_in_yolo(isolated, monkeypatch):
    (isolated / "yolo.flag").write_text("on", encoding="utf-8")
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    d, _ = hook.decide("mcp__composio__GMAIL_SEND_EMAIL", {"to": "x@y.com"})
    assert d == "deny"


def test_interactive_brain_outbound_not_hard_denied(isolated, monkeypatch):
    # No role env => the live assistant. Outbound is NOT hard-denied here; it routes to Tim's
    # prompt (which, with urlopen stubbed, resolves to deny — i.e. it is simply not auto-allowed).
    monkeypatch.delenv("HELIOS_AGENT_ROLE", raising=False)
    d, _ = hook.decide("mcp__composio__GMAIL_SEND_EMAIL", {"to": "x@y.com"})
    assert d == "deny"   # via the ask path, not the structural rail
    # ...and under YOLO the live brain CAN send (Tim opted in for this chat):
    (isolated / "yolo.flag").write_text("on", encoding="utf-8")
    assert hook.decide("mcp__composio__GMAIL_SEND_EMAIL", {"to": "x@y.com"})[0] == "allow"


def test_side_agent_read_still_allowed(isolated, monkeypatch):
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    assert hook.decide("mcp__composio__GMAIL_LIST_MESSAGES", {})[0] == "allow"
