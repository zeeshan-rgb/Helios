"""The 2026-07 model-plan changes: opus reserved for genuinely hard work (Q&A verbs route
medium), a fallback tier for overloaded models, and persistent per-chat Telegram sessions.
All offline — no claude spawns, Telegram API stubbed.
"""

from __future__ import annotations

import inspect

import pytest

from helios import router
from helios.brain import Brain
from helios.lite_brain import LiteBrain
from helios.telegram_bridge import TelegramBridge


# --- opus reserved: Q&A verbs no longer route heavy ----------------------------------------------

@pytest.mark.parametrize("msg", [
    "explain the difference between TCP and UDP protocols",
    "compare these two laptop models and tell me which is better value",
    "analyze this csv file and summarize the totals for me",
    "research the best budget monitors for my desk setup",
])
def test_qa_verbs_route_medium_not_heavy(msg):
    r = router.choose_model(msg)
    assert r["tier"] == "medium", (msg, r)


@pytest.mark.parametrize("msg", [
    "debug this function",                       # engineering verbs stay heavy
    "refactor the auth module end to end",
    "why is my build broken",                    # diagnostic root-causing stays heavy
])
def test_engineering_verbs_still_heavy(msg):
    assert router.choose_model(msg)["tier"] == "heavy", msg


# --- fallback tier --------------------------------------------------------------------------------

def test_fallback_for_degrades_to_capable_tier():
    m = router._models()
    assert router.fallback_for(m["heavy"]) == m["medium"]    # opus -> sonnet
    assert router.fallback_for(m["medium"]) == m["light"]    # sonnet -> haiku
    assert router.fallback_for(m["light"]) == m["medium"]    # haiku -> sonnet (availability, not cost)


# --- signature parity: the bridge must work on either engine -------------------------------------

@pytest.mark.parametrize("cls", [Brain, LiteBrain])
def test_run_turn_signatures_match(cls):
    params = inspect.signature(cls.run_turn).parameters
    for p in ("record", "interactive", "steer", "perm_sink", "session_hold"):
        assert p in params, f"{cls.__name__}.run_turn missing {p}"


# --- persistent per-chat Telegram sessions --------------------------------------------------------

class StubBrain:
    def __init__(self):
        self.calls = []

    def run_turn(self, text, record=True, interactive=True, steer=False,
                 perm_sink="", session_hold=None):
        self.calls.append({"text": text, "hold": session_hold, "sink": perm_sink})
        if session_hold is not None and not session_hold.get("id"):
            session_hold["id"] = "sess-1"           # simulate the brain committing a session id
        return "ok"


@pytest.fixture
def bridge(monkeypatch):
    b = TelegramBridge(brain=StubBrain(), token="t", allowed_ids=["42"])
    monkeypatch.setattr(b, "_api", lambda method, **kw: {"ok": True})
    return b


def test_same_chat_reuses_session(bridge):
    bridge._run_turn_and_reply("42", "first message")
    bridge._run_turn_and_reply("42", "follow-up")
    calls = bridge.brain.calls
    assert calls[0]["hold"] is calls[1]["hold"]            # same holder object across messages
    assert calls[1]["hold"]["id"] == "sess-1"              # second turn resumes the session
    assert calls[0]["sink"] == "telegram:42"


def test_different_chats_get_isolated_sessions(bridge):
    bridge._run_turn_and_reply("42", "hi")
    bridge._run_turn_and_reply("43", "hi")
    calls = bridge.brain.calls
    assert calls[0]["hold"] is not calls[1]["hold"]


def test_new_command_resets_chat_session(bridge):
    bridge._run_turn_and_reply("42", "hi")
    old = bridge._sessions["42"]
    bridge._sessions.pop("42", None)                        # what the /new handler does
    bridge._run_turn_and_reply("42", "hi again")
    assert bridge.brain.calls[1]["hold"] is not old
