"""Phase 9 — memory improvements: separate kinds (lead notes, browsing / screen summaries), wider
secret refusal, episodic expiry, unified recall, and episodic notes kept private from MCP clients."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helios import memory, memory_store

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path)
    monkeypatch.setattr(memory, "VAULT", str(tmp_path))
    return tmp_path


def test_new_kinds_have_their_own_folders(vault):
    a = memory_store.remember("Example Dental prefers a call after 5 pm", "leads")
    b = memory_store.remember("Polarion docs for script injection live under Reports > Script Injection", "browsing",
                              evidence="https://docs.example.test/reports")
    c = memory_store.remember("The build failed with a missing env var in the VS Code terminal", "screen")
    assert Path(a["item"]["path"]).parent == vault / "Lead Notes"
    assert Path(b["item"]["path"]).parent == vault / "Browsing" and b["item"]["evidence"]
    assert Path(c["item"]["path"]).parent == vault / "Screen"


def test_episodic_notes_must_be_summaries(vault):
    res = memory_store.remember("word " * 200, "screen")
    assert res["status"] == "refused" and "summaries" in res["reason"]
    assert memory_store.remember("word " * 200, "user")["status"] == "saved"   # stable memory allows 1000


@pytest.mark.parametrize("secret", [
    "the staging db is postgres://admin:hunter22@db.example.test:5432/app",
    "Server=db;Database=app;User Id=sa;Password=Sup3rS3cret!;",
    "card 4111 1111 1111 1111 exp 12/29",
    "Cookie: sessionid=abcdef1234567890abcdef",
    "PHPSESSID=9f8e7d6c5b4a39281706",
    "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abc123==",
    '{"type": "service_account", "private_key_id": "abc"}',
    "aws_secret_access_key = wJalrXUtnFEMI",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
])
def test_more_secret_shapes_are_refused(vault, secret):
    for cat in ("user", "browsing", "screen", "leads"):
        res = memory_store.remember(secret, cat)
        assert res["status"] == "refused", (cat, secret)
    assert memory_store.items() == []


@pytest.mark.parametrize("fine", [
    "Order number 1234 5678 9012 3456 was delivered",          # fails the Luhn check
    "Phone +91 00000 00001 is the bakery's public line",
    "Git commit 3f2a9c1d0e8b7a6f5e4d3c2b1a0f9e8d7c6b5a49 fixed the build",
    "Uses https://docs.example.test/page for the API reference",
])
def test_ordinary_numbers_and_links_are_not_secrets(vault, fine):
    assert memory_store.remember(fine, "user")["status"] == "saved"


def test_episodic_notes_expire_but_stable_memory_stays(vault):
    old = memory_store.remember("Read the MCP spec changelog at modelcontextprotocol.io", "browsing")["item"]
    keep = memory_store.remember("Prefers dark themes", "preferences")["item"]
    later = datetime.now() + timedelta(days=memory_store.EPISODIC_DAYS + 1)
    assert memory_store.prune_episodic(now=later) == 1
    assert not Path(old["path"]).exists() and Path(keep["path"]).exists()


def test_digest_never_lets_episodic_notes_crowd_out_memory(vault):
    for i in range(5):
        memory_store.remember(f"Polarion page {i} checked for report template number {i * 7}", "browsing")
    memory_store.remember("Polarion is the user's main ALM tool at work", "user")
    d = memory_store.digest_section("polarion report template")
    assert d.count("[browsing]") <= 2 and "main ALM tool" in d


def test_recall_everywhere_keeps_sources_apart(vault, monkeypatch):
    memory_store.remember("Wants MCP servers to stay read-only by default", "preferences")
    monkeypatch.setattr("helios.research.findings", lambda limit=50: [
        {"id": "r1", "topic": "MCP", "title": "MCP adds streamable HTTP", "summary": "spec update",
         "source_name": "example.org", "source_url": "https://example.org/mcp"}])
    monkeypatch.setattr("helios.leads.all_leads", lambda: [])
    monkeypatch.setattr("helios.prospecting.store.list_prospects", lambda limit=500: [])
    found = memory_store.recall_everywhere("mcp servers")
    assert found["memory"] and found["research"][0]["id"] == "r1"
    out = memory_store.format_everywhere(found)
    assert out.index("REMEMBERED") < out.index("RESEARCH LIBRARY") and "not facts about your user" in out


def test_public_mcp_never_shows_episodic_notes(vault):
    memory_store.remember("Saw the user's bank statement summary on screen", "screen")
    memory_store.remember("Prefers metric units", "preferences")
    spec = importlib.util.spec_from_file_location("helios_public_p9", _ROOT / "mcp" / "helios_public_server.py")
    pub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pub)
    pub._guard = lambda *a, **k: None                      # isolate the memory logic from the gate
    assert "bank statement" not in pub.get_recent_memory(20) and "metric" in pub.get_recent_memory(20)
    assert "Unknown category" in pub.search_memory("bank", "screen")
    assert "bank statement" not in pub.search_memory("statement summary screen")


def test_internal_tools_document_the_new_kinds():
    src = (_ROOT / "mcp" / "helios_server.py").read_text(encoding="utf-8")
    assert "browsing (a SHORT summary" in src and 'scope="all"' in src
