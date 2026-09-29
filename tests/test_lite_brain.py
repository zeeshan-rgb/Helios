"""The lite brain's turn loop: streaming, fragmented tool-call accumulation, multi-round tool use,
and the SSE event contract — verified against a MOCK OpenAI-compatible client (no network).

Isolated/safe to run live: the flag paths, db, memory, and the LLM client are all stubbed, so this
never touches Tim's vault, history, abort/YOLO flags, or any provider.
"""

from __future__ import annotations

import pytest

from helios import conf, db, llm, memory
from helios.lite_brain import LiteBrain


# ---- minimal fakes mimicking the OpenAI streaming object shapes ----
class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments) if (name or arguments) else None


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, pin, pout):
        self.prompt_tokens = pin
        self.completion_tokens = pout


class _Chunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class _Completions:
    def __init__(self, rounds):
        self._rounds = rounds
        self.calls = []
        self._i = 0

    def create(self, **kwargs):
        self.calls.append(kwargs)
        s = _Stream(self._rounds[self._i])
        self._i += 1
        return s


class _Chat:
    def __init__(self, rounds):
        self.completions = _Completions(rounds)


class _Client:
    def __init__(self, rounds):
        self.chat = _Chat(rounds)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(memory, "build_digest", lambda *_a, **_k: "")
    monkeypatch.setattr(memory, "extract_and_write", lambda *a, **k: "")
    monkeypatch.setattr(db, "get_state", lambda *a, **k: None)
    monkeypatch.setattr(db, "conversation_messages", lambda *a, **k: [])
    monkeypatch.setattr(db, "upsert_conversation", lambda *a, **k: None)
    monkeypatch.setattr(db, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(llm, "lite_model", lambda: "test-model")


def _events_collector():
    events = []
    return events, (lambda kind, data: events.append((kind, data)))


def test_two_round_tool_then_text(monkeypatch):
    """Round 1 streams a fragmented read_file tool call; round 2 streams the final answer."""
    round1 = [
        _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, id="c1", name="read_file",
                                                     arguments='{"path":')]))]),
        _Chunk([_Choice(_Delta(tool_calls=[_ToolCall(0, arguments='"notes.txt"}')]))]),
        _Chunk([_Choice(_Delta(), finish_reason="tool_calls")], usage=_Usage(10, 2)),
    ]
    round2 = [
        _Chunk([_Choice(_Delta(content="Here "))]),
        _Chunk([_Choice(_Delta(content="you go."), finish_reason="stop")], usage=_Usage(5, 3)),
    ]
    client = _Client([round1, round2])
    monkeypatch.setattr(llm, "make_client", lambda *a, **k: client)

    events, emit = _events_collector()
    brain = LiteBrain(emit=emit, perms=None)
    executed = []
    monkeypatch.setattr(brain._tools, "execute",
                        lambda name, args: executed.append((name, args)) or "FILE BODY")

    result = brain.run_turn("read my notes", record=False, interactive=True)

    assert result == "Here you go."
    # tool call args were reassembled across fragments and parsed
    assert executed == [("read_file", {"path": "notes.txt"})]
    kinds = [k for k, _ in events]
    assert kinds[0] == "model"
    assert "token" in kinds and "tool" in kinds and "usage" in kinds
    assert ("tool", {"name": "read_file"}) in events
    # tokens streamed in order
    toks = [d for k, d in events if k == "token"]
    assert "".join(toks) == "Here you go."
    # 'done' carries the final text and comes after all tokens (a 'memory' write-back event
    # may legitimately follow it from the background thread)
    assert ("done", {"text": "Here you go."}) in events
    done_idx = next(i for i, (k, _) in enumerate(events) if k == "done")
    last_tok_idx = max(i for i, (k, _) in enumerate(events) if k == "token")
    assert done_idx > last_tok_idx
    # usage aggregated across both rounds
    usage = next(d for k, d in events if k == "usage")
    assert usage["in"] == 15 and usage["out"] == 5 and usage["tools"] == ["read_file"]
    # second create() call carried the tool result back as a 'tool' role message
    second = client.chat.completions.calls[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("content") == "FILE BODY" for m in second)


def test_plain_text_no_tools(monkeypatch):
    rounds = [[_Chunk([_Choice(_Delta(content="Good evening, sir."), finish_reason="stop")],
                      usage=_Usage(3, 4))]]
    client = _Client(rounds)
    monkeypatch.setattr(llm, "make_client", lambda *a, **k: client)
    events, emit = _events_collector()
    brain = LiteBrain(emit=emit, perms=None)
    result = brain.run_turn("hi", record=False, interactive=False)
    assert result == "Good evening, sir."
    assert [k for k, _ in events][-1] == "done"


def test_busy_rejects_without_steer(monkeypatch):
    client = _Client([[_Chunk([_Choice(_Delta(content="x"), finish_reason="stop")])]])
    monkeypatch.setattr(llm, "make_client", lambda *a, **k: client)
    events, emit = _events_collector()
    brain = LiteBrain(emit=emit, perms=None)
    brain._lock.acquire()   # simulate an in-flight turn
    try:
        assert brain.run_turn("hello", steer=False) is None
        assert any(k == "error" for k, _ in events)
    finally:
        brain._lock.release()


def test_provider_unreachable_emits_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no key")
    monkeypatch.setattr(llm, "make_client", _boom)
    events, emit = _events_collector()
    brain = LiteBrain(emit=emit, perms=None)
    result = brain.run_turn("hi", record=False, interactive=False)
    assert result == ""
    assert any(k == "error" for k, _ in events)
