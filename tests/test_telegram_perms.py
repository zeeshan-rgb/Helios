"""Permission asks from a Telegram-started turn must surface on Telegram (Approve/Deny buttons)
and resolve from a button tap. Fully offline — the Telegram HTTP API is stubbed, no network.
"""

from __future__ import annotations

import pytest

from helios import permissions
from helios.telegram_bridge import TelegramBridge


# --- PendingRegistry sink routing ---------------------------------------------------------------

def test_create_routes_telegram_sink_to_asker():
    reg = permissions.PendingRegistry()
    got = {}
    reg.telegram_asker = lambda rid, chat, summary: got.update(rid=rid, chat=chat, summary=summary)
    rid = reg.create("Bash", {"command": "x"}, sink="telegram:999")
    assert got.get("rid") == rid and got.get("chat") == "999" and got.get("summary")


def test_create_desktop_sink_does_not_hit_telegram():
    reg = permissions.PendingRegistry()
    calls = []
    reg.telegram_asker = lambda *a: calls.append(a)
    reg.create("Bash", {"command": "x"}, sink="")          # desktop turn -> no sink
    assert calls == []


# --- bridge wiring + callback resolution --------------------------------------------------------

def _bridge(reg, monkeypatch, sent=None):
    b = TelegramBridge(brain=None, token="t", allowed_ids=["42"], perms=reg)
    monkeypatch.setattr(b, "_api", lambda method, **kw: (sent.append((method, kw)) if sent is not None else None) or {"ok": True})
    return b


def test_bridge_registers_itself_as_asker(monkeypatch):
    reg = permissions.PendingRegistry()
    b = _bridge(reg, monkeypatch)
    assert reg.telegram_asker == b.send_permission_ask


def test_telegram_ask_sends_message_and_button_tap_resolves(monkeypatch):
    reg = permissions.PendingRegistry()
    sent = []
    b = _bridge(reg, monkeypatch, sent)
    rid = reg.create("Bash", {"command": "rm x"}, sink="telegram:42")   # -> send_permission_ask -> _api
    assert any(m == "sendMessage" for m, _ in sent)
    # Tim taps "Approve" -> callback_query resolves that exact request id
    cq = {"id": "c1", "from": {"id": 42}, "message": {"chat": {"id": 42}, "message_id": 7},
          "data": f"perm:allow:{rid}"}
    b._handle_callback(cq)
    assert reg.wait(rid, 0.2) == "allow"


def test_deny_button_resolves_deny(monkeypatch):
    reg = permissions.PendingRegistry()
    b = _bridge(reg, monkeypatch)
    rid = reg.create("Bash", {"command": "rm x"}, sink="telegram:42")
    b._handle_callback({"id": "c", "from": {"id": 42}, "message": {"chat": {"id": 42}, "message_id": 1},
                        "data": f"perm:deny:{rid}"})
    assert reg.wait(rid, 0.2) == "deny"


def test_unauthorized_callback_does_not_resolve(monkeypatch):
    reg = permissions.PendingRegistry()
    b = _bridge(reg, monkeypatch)
    rid = reg.create("Bash", {"command": "rm x"}, sink="telegram:42")
    # a tap from a chat id NOT on the allowlist must be ignored
    b._handle_callback({"id": "c", "from": {"id": 999}, "message": {"chat": {"id": 999}, "message_id": 1},
                        "data": f"perm:allow:{rid}"})
    assert reg.wait(rid, 0.2) == "deny"   # never resolved -> wait times out -> fail-safe deny
