"""Phase 4 memory: categorized remember / recall / forget items in the vault — dedup, secret refusal,
provenance, project scoping, digest inclusion, the MCP tools, and the no-LLM daily log."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from helios import memory, memory_store


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path)
    return tmp_path


def test_remember_writes_an_inspectable_note(vault):
    res = memory_store.remember("Prefers concise reports", "preferences", source="voice")
    assert res["status"] == "saved"
    it = res["item"]
    p = Path(it["path"])
    assert p.parent == vault / "Preferences" and p.exists()
    body = p.read_text(encoding="utf-8")
    for field in ("id:", "category: preferences", "created:", "updated:", "source: voice"):
        assert field in body
    assert body.strip().endswith("Prefers concise reports")


def test_duplicates_merge_and_keep_fuller_wording(vault):
    memory_store.remember("Prefers concise reports", "preferences")
    res = memory_store.remember("prefers concise reports!", "preferences")
    assert res["status"] == "duplicate" and res["item"]["seen"] == 2
    res = memory_store.remember("Prefers concise reports always", "preferences")
    assert res["status"] == "duplicate" and res["item"]["text"] == "Prefers concise reports always"
    assert len(memory_store.items("preferences")) == 1


@pytest.mark.parametrize("secret", [
    "My password is hunter2", "the api key: sk-abcdefghijklmnopqrstuv", "OTP is 482913",
    "bank account number: 12345678", "token = ghp_abcdefghijklmnopqrstuvwxyz0123",
])
def test_secrets_are_refused_not_stored(vault, secret):
    res = memory_store.remember(secret, "user")
    assert res["status"] == "refused" and "secret" in res["reason"]
    assert memory_store.items() == []


def test_auto_category_and_projects(vault):
    assert memory_store.remember("Never modify production config without asking")["item"]["category"] == "rules"
    assert memory_store.remember("I prefer dark themes")["item"]["category"] == "preferences"
    assert memory_store.remember("We decided to use Next.js for the site")["item"]["category"] == "decisions"
    it = memory_store.remember("Serve with next start on port 3100", project="Maqsusi")["item"]
    assert it["category"] == "projects" and it["project"] == "Maqsusi"
    assert Path(it["path"]).parent == vault / "Projects" / "Maqsusi"
    assert [i["text"] for i in memory_store.items(project="maqsusi")] == ["Serve with next start on port 3100"]


def test_recall_ranks_by_relevance(vault):
    memory_store.remember("Prefers concise reports", "preferences")
    memory_store.remember("Works on Polarion scripts at the office", "user")
    memory_store.remember("Serve Maqsusi with next start", project="Maqsusi")
    top = memory_store.recall("what is my reporting preference for reports")
    assert top and top[0]["text"] == "Prefers concise reports"
    assert memory_store.recall("", limit=2) and len(memory_store.recall("", limit=2)) == 2


def test_forget_by_id_or_unique_match_only(vault):
    a = memory_store.remember("Prefers concise reports", "preferences")["item"]
    memory_store.remember("Prefers concise emails", "preferences")
    res = memory_store.forget("concise")                      # ambiguous: nothing deleted
    assert res["deleted"] == [] and len(res["candidates"]) == 2
    res = memory_store.forget(a["id"])
    assert res["deleted"][0]["id"] == a["id"] and not Path(a["path"]).exists()
    assert memory_store.forget("emails")["deleted"]           # now unique
    assert memory_store.items() == []


def test_digest_always_carries_rules_and_preferences(vault):
    memory_store.remember("Always answer in English", "rules")
    memory_store.remember("Prefers concise reports", "preferences")
    memory_store.remember("Aegis targets the iQOO Neo 9 Pro", project="Aegis")
    d = memory_store.digest_section("tell me about the weather")
    assert "[rule] Always answer in English" in d and "[preference] Prefers concise reports" in d
    assert "Aegis" not in d
    assert "[project Aegis]" in memory_store.digest_section("how is the aegis phone app going")


def test_build_digest_includes_remembered_section(vault, monkeypatch, tmp_path):
    memory_store.remember("Prefers concise reports", "preferences")
    for name in ("VAULT", "PEOPLE", "PROJECTS", "DAILY"):
        sub = {"VAULT": "", "PEOPLE": "People", "PROJECTS": "Projects", "DAILY": "Daily"}[name]
        monkeypatch.setattr(memory, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(memory, "PROFILE", tmp_path / "Profile.md")
    monkeypatch.setattr(memory, "INDEX", tmp_path / "_index.md")
    monkeypatch.setattr(memory, "_recall_recipes", lambda m: "")
    d = memory.build_digest("status update please")
    assert "Remembered" in d and "Prefers concise reports" in d
    assert "Preferences/ — 1 item(s)" in (tmp_path / "_index.md").read_text(encoding="utf-8")


def test_log_exchange_writes_daily_without_llm(monkeypatch, tmp_path):
    for name, sub in (("VAULT", ""), ("PEOPLE", "People"), ("PROJECTS", "Projects"), ("DAILY", "Daily")):
        monkeypatch.setattr(memory, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(memory, "PROFILE", tmp_path / "Profile.md")
    monkeypatch.setattr(memory, "INDEX", tmp_path / "_index.md")
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path)
    memory.log_exchange("hello with key sk-abcdefghijklmnopqrstuv", "Hello, sir.")
    daily = next((tmp_path / "Daily").glob("*.md")).read_text(encoding="utf-8")
    assert "**You:** hello with key [redacted]" in daily and "**Helios:** Hello, sir." in daily


def test_mcp_memory_tools(vault, monkeypatch):
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("helios_server_mem", root / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert "Remembered [preferences]" in srv.remember("Prefers concise reports", "preferences")
    assert "Already knew that" in srv.remember("Prefers concise reports", "preferences")
    assert "Not saved" in srv.remember("my password is swordfish")
    assert "Prefers concise reports" in srv.recall_memory("reporting preference")
    assert "Prefers concise reports" in srv.list_memories("preferences")
    assert "Forgotten" in srv.forget_memory("concise reports")
    assert "Nothing matching" in srv.forget_memory("concise reports")


def test_memory_tools_are_autonomous_but_policy_checked():
    from helios import permissions
    for tool in ("remember", "recall_memory", "list_memories", "forget_memory"):
        assert permissions.classify(f"mcp__helios__{tool}", {}) == "allow"
