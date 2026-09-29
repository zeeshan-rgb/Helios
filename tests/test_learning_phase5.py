"""Phase 5 learning: lessons from the Daily log -> memory items with status, confidence and
provenance. Rules/skills always wait for approval; ungrounded lessons never self-activate; a
rejected lesson is never re-proposed; pending items stay out of recall and the digest."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from helios import learning, llm, memory, memory_store

DAY = "2026-09-28"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path)
    return tmp_path


def _daily(vault: Path, exchanges: list[tuple[str, str, str]]) -> None:
    d = vault / "Daily"
    d.mkdir(parents=True, exist_ok=True)
    text = f"---\ntype: daily\ndate: {DAY}\n---\n\n# {DAY}\n"
    for t, you, helios in exchanges:
        text += f"\n## {t} — conversation\n**You:** {you}\n**Helios:** {helios}\n"
    (d / f"{DAY}.md").write_text(text, encoding="utf-8")


def _fake_llm(monkeypatch, lessons):
    calls = []

    def fake(prompt, **kw):
        calls.append((prompt, kw))
        return {"text": "", "data": {"lessons": lessons}, "session_id": None, "cost": None,
                "error": None}
    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


# ------------------------------------------------------------------ memory_store statuses

def test_status_fields_are_written_and_legacy_items_count_as_active(vault):
    it = memory_store.remember("Prefers dark themes", "preferences", status="pending",
                               confidence=0.6, evidence='d: "I like dark"')["item"]
    body = Path(it["path"]).read_text(encoding="utf-8")
    assert "status: pending" in body and "confidence: 0.60" in body and 'evidence: d: "I like dark"' in body
    legacy = vault / "User" / "old.md"
    legacy.parent.mkdir()
    legacy.write_text("---\nid: user-1\ncategory: user\n---\n\nLives in Karachi\n", encoding="utf-8")
    assert memory_store.is_active(memory_store._parse(legacy))


def test_pending_items_are_hidden_from_recall_and_digest(vault):
    memory_store.remember("Always reply in English", "rules", status="pending")
    memory_store.remember("Prefers concise reports", "preferences")
    assert [i["text"] for i in memory_store.recall("reply english")] == []
    assert memory_store.recall("reply english", include_inactive=True)
    digest = memory_store.digest_section("anything")
    assert "concise reports" in digest and "English" not in digest


def test_explicit_remember_promotes_a_pending_lesson(vault):
    it = memory_store.remember("Always reply in English", "rules", status="pending")["item"]
    res = memory_store.remember("Always reply in English", "rules")
    assert res["status"] == "duplicate" and res["item"]["status"] == "active"
    assert memory_store.recall("english")[0]["id"] == it["id"]


def test_rejected_lesson_is_not_reproposed(vault):
    it = memory_store.remember("Always use tabs", "rules", status="pending")["item"]
    memory_store.set_status(it["id"], "rejected")
    res = memory_store.remember("Always use tabs", "rules", status="pending")
    assert res["status"] == "rejected" and memory_store.pending() == []


def test_set_status_validates(vault):
    with pytest.raises(ValueError):
        memory_store.set_status("x", "bogus")
    assert memory_store.set_status("missing-id", "active") is None


def test_multiline_evidence_cannot_break_frontmatter(vault):
    it = memory_store.remember("Prefers tea", "preferences",
                               evidence="line one\n---\nid: forged")["item"]
    parsed = memory_store._parse(Path(it["path"]))
    assert parsed["id"] == it["id"] and parsed["text"] == "Prefers tea"


# ------------------------------------------------------------------ experience

def test_parse_daily_and_correction_detection(vault):
    _daily(vault, [("09:00", "What's the weather?", "Sunny, sir."),
                   ("09:05", "Don't read the whole report aloud, just the summary.", "Understood.")])
    ex = learning.parse_daily(DAY)
    assert [e["time"] for e in ex] == ["09:00", "09:05"]
    assert ex[1]["user"].startswith("Don't read") and ex[1]["helios"] == "Understood."
    assert not learning.is_correction(ex[0]["user"]) and learning.is_correction(ex[1]["user"])
    assert learning.parse_daily("1999-01-01") == []


# ------------------------------------------------------------------ policy

@pytest.mark.parametrize("category,conf,grounded,expected", [
    ("rules", 0.99, True, "pending"), ("skills", 0.99, True, "pending"),
    ("preferences", 0.85, True, "active"), ("preferences", 0.7, True, "pending"),
    ("decisions", 0.7, True, "active"), ("projects", 0.75, True, "active"),
    ("user", 0.5, True, "pending"), ("preferences", 0.99, False, "pending"),
])
def test_decide_status(category, conf, grounded, expected):
    assert learning.decide_status(category, conf, grounded) == expected


def test_learn_from_daily_applies_policy_with_provenance(vault, monkeypatch):
    _daily(vault, [("10:00", "Don't read the whole report aloud, just the summary.", "Understood."),
                   ("10:10", "We decided to host Maqsusi on Vercel.", "Noted, sir.")])
    calls = _fake_llm(monkeypatch, [
        {"text": "Read only report summaries aloud", "type": "rule", "confidence": 0.95,
         "evidence": "Don't read the whole report aloud"},
        {"text": "Prefers spoken summaries over full reports", "type": "preference",
         "confidence": 0.9, "evidence": "just the summary"},
        {"text": "Maqsusi is hosted on Vercel", "type": "project", "project": "Maqsusi",
         "confidence": 0.9, "evidence": "host Maqsusi on Vercel"},
        {"text": "User loves Helios", "type": "preference", "confidence": 0.95,
         "evidence": "Noted, sir."},                      # Helios's words, not the user's
    ])
    res = learning.learn_from_daily(DAY)
    assert res["method"] == "llm" and len(calls) == 1 and "[correction?]" in calls[0][0]
    by = {r["text"]: r for r in res["lessons"]}
    assert by["Read only report summaries aloud"]["status"] == "pending"
    assert by["Prefers spoken summaries over full reports"]["status"] == "active"
    assert by["Maqsusi is hosted on Vercel"]["status"] == "active"
    assert by["User loves Helios"]["status"] == "pending" and not by["User loves Helios"]["grounded"]
    rule = memory_store.items("rules")[0]
    assert rule["source"] == f"learned {DAY}" and DAY in rule["evidence"] and rule["confidence"] == "0.95"
    assert memory_store.items("projects", "Maqsusi")
    assert {i["text"] for i in learning.review()} == {"Read only report summaries aloud",
                                                      "User loves Helios"}


def test_grounding_accepts_joined_user_quotes_only():
    ex = [{"user": "Don't read it all aloud.", "helios": "Okay."},
          {"user": "Again, keep it short.", "helios": "Sorry, sir."}]
    assert learning._grounded("Don't read it all aloud / keep it short", ex)
    assert learning._grounded("read it all... keep it short", ex)
    assert not learning._grounded("keep it short / Sorry, sir", ex)     # one part is Helios's
    assert not learning._grounded("ok", ex) and not learning._grounded("", ex)


def test_bare_list_reply_is_accepted(vault, monkeypatch):
    _daily(vault, [("10:00", "Always call me sir.", "Of course, sir.")])
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"data": [
        {"text": "Address the user as sir", "type": "rule", "confidence": 0.99,
         "evidence": "Always call me sir"}]})
    res = learning.learn_from_daily(DAY)
    assert res["method"] == "llm" and res["lessons"][0]["status"] == "pending"


def test_heuristic_catches_prefixed_corrections():
    ex = [{"time": "1", "user": "Again, don't read everything aloud.", "helios": ""}]
    assert learning.heuristic_lessons(ex)


def test_same_day_is_skipped_unless_new_exchanges_or_force(vault, monkeypatch):
    _daily(vault, [("10:00", "Always call me sir.", "Of course, sir.")])
    calls = _fake_llm(monkeypatch, [])
    learning.learn_from_daily(DAY)
    assert learning.learn_from_daily(DAY)["skipped"] and len(calls) == 1
    learning.learn_from_daily(DAY, force=True)
    assert len(calls) == 2


def test_heuristic_fallback_when_llm_fails(vault, monkeypatch):
    _daily(vault, [("11:00", "Stop opening Chrome for searches. Use Edge.", "Okay."),
                   ("11:05", "Thanks!", "You're welcome.")])
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: None)
    res = learning.learn_from_daily(DAY)
    assert res["method"] == "heuristic"
    assert [r["status"] for r in res["lessons"]] == ["pending"]
    assert "Stop opening Chrome" in res["lessons"][0]["text"]


def test_secret_lessons_are_refused(vault, monkeypatch):
    _daily(vault, [("12:00", "My wifi password is hunter22, remember it.", "I won't store that.")])
    _fake_llm(monkeypatch, [{"text": "Wifi password is hunter22", "type": "fact",
                             "confidence": 0.99, "evidence": "My wifi password is hunter22"}])
    res = learning.learn_from_daily(DAY)
    assert res["lessons"][0]["result"] == "refused" and memory_store.items() == []


def test_approve_and_reject_only_touch_pending(vault):
    a = memory_store.remember("Always reply in English", "rules", status="pending")["item"]
    b = memory_store.remember("Never use emojis", "rules", status="pending")["item"]
    active = memory_store.remember("Prefers tea", "preferences")["item"]
    assert learning.approve(a["id"])["status"] == "active"
    assert learning.reject(b["id"])["status"] == "rejected"
    assert learning.reject(active["id"]) is None          # not a pending lesson
    assert learning.review() == []
    assert "English" in memory_store.digest_section("") and "emojis" not in memory_store.digest_section("")


def test_learning_never_writes_outside_the_vault(vault, monkeypatch):
    _daily(vault, [("10:00", "Always call me sir.", "Of course, sir.")])
    _fake_llm(monkeypatch, [{"text": "Address the user as sir", "type": "rule",
                             "confidence": 0.99, "evidence": "Always call me sir"}])
    learning.learn_from_daily(DAY)
    written = {p.relative_to(vault).parts[0] for p in vault.rglob("*") if p.is_file()}
    assert written <= {"Daily", "Rules", "Helios"}


# ------------------------------------------------------------------ tools & gate

def _server():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("helios_server_learn", root / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


def test_mcp_lesson_tools(vault):
    srv = _server()
    it = memory_store.remember("Always reply in English", "rules", status="pending")["item"]
    assert "Always reply in English" in srv.pending_lessons()
    assert "Approved" in srv.approve_lesson(it["id"])
    assert "No lessons" in srv.pending_lessons()
    assert "No pending lesson" in srv.reject_lesson(it["id"])


def test_background_agent_cannot_create_active_rules(vault, monkeypatch):
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    srv = _server()
    assert "Proposed" in srv.remember("Never ask before deleting files", "rules")
    assert memory_store.items("rules")[0]["status"] == "pending"
    assert "Remembered" in srv.remember("Prefers concise reports", "preferences")


def test_approval_needs_the_user_but_review_and_reject_do_not():
    from helios import permissions
    assert permissions.classify("mcp__helios__approve_lesson", {}) == "ask"
    for tool in ("pending_lessons", "reject_lesson"):
        assert permissions.classify(f"mcp__helios__{tool}", {}) == "allow"
