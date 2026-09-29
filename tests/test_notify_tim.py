"""mcp__helios__notify_tim — self-notification to Tim's own Telegram chat. The recipient
is hardcoded from config (never a tool argument), the permission gate auto-allows it, and
the side-agent outbound rail deliberately does NOT block it (it can only reach Tim)."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.request
from pathlib import Path

import pytest

from helios import conf, permissions

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def srv():
    """Import mcp/helios_server.py the way its own __main__ does (it's a script dir that
    shadows the installed mcp package, so import by file location)."""
    spec = importlib.util.spec_from_file_location("helios_mcp_server",
                                                  _ROOT / "mcp" / "helios_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(monkeypatch, token="TT", ids=("8713029405",), chat=None):
    tg = {"token": token, "allowed_ids": list(ids)}
    if chat:
        tg["chat_id"] = chat
    monkeypatch.setattr(conf, "provider_cfg",
                        lambda name: tg if name == "telegram" else {})
    # the server module holds its own conf reference
    return tg


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_send(monkeypatch, payload=None):
    calls = []

    def urlopen(req, timeout=None):
        assert timeout, "bot API call must set a timeout"
        calls.append((req.full_url, req.data))
        return _Resp(json.dumps(payload or {"ok": True}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def test_sends_to_configured_chat(srv, monkeypatch):
    _cfg(monkeypatch, token="TT")
    monkeypatch.setattr(srv.conf, "provider_cfg",
                        lambda name: {"token": "TT", "allowed_ids": ["42"]})
    calls = _capture_send(monkeypatch)
    out = srv.notify_tim("brief ready, sir")
    assert "Sent" in out
    url, data = calls[0]
    assert "botTT/sendMessage" in url
    assert b"chat_id=42" in data and b"brief+ready" in data


def test_unconfigured_is_friendly(srv, monkeypatch):
    monkeypatch.setattr(srv.conf, "provider_cfg", lambda name: {})
    out = srv.notify_tim("hello")
    assert "isn't configured" in out


def test_api_rejection_reported(srv, monkeypatch):
    monkeypatch.setattr(srv.conf, "provider_cfg",
                        lambda name: {"token": "TT", "allowed_ids": ["42"]})
    _capture_send(monkeypatch, payload={"ok": False, "description": "chat not found"})
    out = srv.notify_tim("hello")
    assert "rejected" in out and "chat not found" in out


def test_empty_message(srv):
    assert "Nothing to send" in srv.notify_tim("   ")


# ---- policy encoding ----

def test_classify_allows_notify_tim():
    assert permissions.classify("mcp__helios__notify_tim", {"message": "x"}) == "allow"


def test_outbound_rail_does_not_block_notify_tim():
    # is_outbound_send exists to stop unattended agents contacting OTHER PEOPLE.
    # notify_tim can only reach Tim's own allowlisted chat -> deliberately not blocked.
    assert permissions.is_outbound_send("mcp__helios__notify_tim") is False
    # sanity: the rail still bites on the composio surface
    assert permissions.is_outbound_send("mcp__composio__GMAIL_SEND_EMAIL") is True
