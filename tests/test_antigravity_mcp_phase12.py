"""Phase 12 Antigravity -> MCP -> Helios: the supported direction only, Helios independent of
Antigravity, no shadowing of the internal server inside Helios's own brain sessions, and no use of
Antigravity internals anywhere in Helios's code."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from helios import agy_cli, conf

_ROOT = Path(__file__).resolve().parent.parent
SERVER = _ROOT / "mcp" / "helios_public_server.py"


def _list_tools(env: dict) -> set[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}
    return asyncio.run(asyncio.wait_for(go(), 60))


def test_public_server_serves_nothing_inside_a_helios_brain_session():
    base = {k: v for k, v in os.environ.items() if not k.startswith("HELIOS_")}
    assert len(_list_tools(base)) == 13                                  # e.g. the Antigravity IDE
    assert _list_tools(base | {agy_cli.MARKER_ENV: "C:/x/marker.ok"}) == set()   # Helios's own agy


def test_registration_name_never_shadows_the_internal_server():
    r = subprocess.run([sys.executable, str(_ROOT / "helios_cli.py"), "mcp", "antigravity"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert " mcp add helios-public " in r.stdout and "helios_public_server.py" in r.stdout
    assert "helios_server.py" not in r.stdout.replace("helios_public_server.py", "")


# ------------------------------------------------------------------ independence

def test_brain_without_antigravity_fails_politely(monkeypatch):
    monkeypatch.setattr(agy_cli, "command", lambda: [])
    from helios.agy_brain import AntigravityBrain
    events = []
    b = AntigravityBrain(emit=lambda k, d: events.append((k, d)))
    assert b.run_turn("hello", record=False) == ""
    assert any(k == "error" and "isn't installed" in d for k, d in events)
    b.warm()                                                             # no crash, no session
    assert b._session is None


def test_ai_steps_degrade_without_antigravity(monkeypatch, tmp_path):
    monkeypatch.setattr(agy_cli, "command", lambda: [])
    monkeypatch.setattr(conf, "brain_engine", lambda: "antigravity")
    from helios import learning, memory_store, research
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path)
    ex = [{"time": "09:00", "user": "Never read the whole report aloud.", "helios": "Ok."}]
    lessons, method = learning.extract_lessons(ex)
    assert method == "heuristic" and lessons                             # no-AI fallback
    raw, err = research._call_agent("MCP", "")
    assert raw == [] and "agy not found" in err                          # a clear failure


def test_non_ai_features_do_not_import_antigravity():
    # Module-level imports only: health.py's runtime probe imports agy_cli lazily, and only when the
    # brain engine IS antigravity, to report whether agy is present — that's not a dependency.
    for rel in ("helios/projects.py", "helios/health.py", "helios/briefing.py",
                "helios/night_mode/scheduler.py", "mcp/helios_public_server.py", "helios/voice/daemon.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"^(from \.+ import .*agy_cli|import agy_cli|from helios import .*agy_cli)",
                             src, re.M), rel


def test_helios_never_uses_antigravity_internals():
    """Blueprint phase 12: no private APIs, tokens, hidden agentapi endpoints, conversation files."""
    forbidden = re.compile(r"agentapi|ANTIGRAVITY_LS_ADDRESS|language_server|\.pb\b|"
                           r"conversations[\\/]|antigravity[\\/]brain", re.I)
    # Credentials may be checked for EXISTENCE (is the user signed in?) but never read.
    reads_creds = re.compile(r"(oauth_creds|credentials|access_token)[^\n]*(read_text|read_bytes|open\()"
                             r"|(read_text|read_bytes|open\()[^\n]*(oauth_creds|access_token)", re.I)
    hits = []
    for folder in ("helios", "hooks", "mcp"):
        for f in (_ROOT / folder).rglob("*.py"):
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if forbidden.search(line) or reads_creds.search(line):
                    hits.append(f"{f.relative_to(_ROOT)}:{i}: {line.strip()[:80]}")
    assert not hits, hits


def test_setup_guide_exists_and_covers_the_essentials():
    doc = (_ROOT / "docs" / "ANTIGRAVITY_MCP.md").read_text(encoding="utf-8")
    for must in ("agy mcp add helios-public", "agy mcp remove helios-public", "helios_public_server.py",
                 "Do not", "Independence", "always_ask", "mcp_config.json"):
        assert must in doc, must
