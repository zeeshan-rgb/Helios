"""Regression net for the permission policy — helios/permissions.py classify().

classify() decides 'allow' (autonomous) vs 'ask' (needs Tim's confirmation) for every tool the
brain calls. It's the security-critical surface the 2026-06-24 audit hardened, so these cases lock
in each decision path: a future edit that re-opens (say) WebFetch or a Composio send to autonomous
fails here.

Note: classify() short-circuits on the settings allowlist (config/settings.toml [autonomy].allow),
so cases like Glob/WebSearch/mcp__computer__click assert the REAL merged policy, while the
Read/Write/Bash/Composio cases exercise the hardcoded logic (those tools aren't in the list).
"""

from __future__ import annotations

import pytest

from helios import conf, permissions

ROOT = str(conf.ROOT).replace("\\", "/")

# Each case: (tool_name, tool_input, expected). Comment marks the path being pinned.
CASES = [
    # --- settings allowlist (override) -> allow ---------------------------------------
    ("Glob", {"pattern": "**/*.py"}, "allow"),
    ("WebSearch", {"query": "weather miami"}, "allow"),
    ("ToolSearch", {"query": "x"}, "allow"),
    ("mcp__computer__screenshot", None, "allow"),          # explicitly listed
    # --- computer-use (hardcoded startswith) -> allow ---------------------------------
    ("mcp__computer__click_element", {"element_index": 3}, "allow"),  # NOT in the list
    ("mcp__computer__get_window_state", {"window_id": 1}, "allow"),
    # --- Helios first-party tools -----------------------------------------------------
    ("mcp__helios__set_reminder", {"text": "x"}, "allow"),
    ("mcp__helios__create_routine", {"name": "brief"}, "allow"),
    ("mcp__helios__delete_routine", {"name": "brief"}, "ask"),   # destructive
    ("mcp__helios__create_tool", {"name": "t"}, "ask"),          # writes+exec's code
    ("mcp__helios__start_mission", {"goal": "x"}, "ask"),        # spawns a team
    ("mcp__helios__spawn_agent", {"role": "x"}, "ask"),
    ("mcp__helios__rag_clear", {}, "ask"),
    ("mcp__helios__set_screen_awareness", {"on": True}, "ask"),  # capability toggle
    # --- model_3d (2026-07-08 port): sandbox-scoped writes stay autonomous ------------
    ("mcp__helios__model_3d", {"action": "inspect", "path": "C:/anywhere/x.glb"}, "allow"),
    ("mcp__helios__model_3d", {"action": "measure", "path": "C:/anywhere/x.glb"}, "allow"),
    ("mcp__helios__model_3d", {"action": "render", "path": "C:/anywhere/x.glb"}, "allow"),
    ("mcp__helios__model_3d", {"action": "show", "path": "C:/anywhere/x.glb"}, "allow"),
    ("mcp__helios__model_3d", {"action": "convert", "path": "coaster",
                               "format": "stl"}, "allow"),       # bare sandbox folder name
    ("mcp__helios__model_3d",
     {"action": "convert", "path": str(permissions._WORK_SANDBOX / "stand" / "model.glb"),
      "format": "stl"}, "allow"),                                # in-sandbox source
    ("mcp__helios__model_3d",
     {"action": "convert", "path": "C:/anywhere/x.glb",
      "destination": str(permissions._WORK_SANDBOX / "out" / "x.stl")}, "allow"),
    ("mcp__helios__model_3d", {"action": "convert", "path": "C:/anywhere/x.glb",
                               "format": "stl"}, "ask"),         # writes beside outside source
    ("mcp__helios__model_3d",
     {"action": "convert", "path": str(permissions._WORK_SANDBOX / "stand" / "model.glb"),
      "destination": "C:/Users/Tim/Desktop/x.stl"}, "ask"),      # outside destination
    # --- web -------------------------------------------------------------------------
    ("WebFetch", {"url": "https://example.com"}, "ask"),         # exfiltration/SSRF vector
    # --- reads -----------------------------------------------------------------------
    ("Read", {"file_path": "C:/Temp/notes.txt"}, "allow"),
    ("Read", {"file_path": "C:/Users/Tim/.env"}, "ask"),
    ("Read", {"file_path": "C:/docs/passport_scan.jpg"}, "ask"),
    ("Read", {"file_path": "C:/secret_keys.txt"}, "ask"),        # 'secret' keyword
    ("Grep", {"pattern": "x", "path": "C:/Users/Tim/code"}, "allow"),
    ("Grep", {"pattern": "x", "path": "C:/Users/Tim/.ssh/id_rsa"}, "ask"),
    # --- writes ----------------------------------------------------------------------
    ("Write", {"file_path": "C:/Temp/notes.txt"}, "allow"),
    ("Write", {"file_path": "C:/Users/Tim/.env"}, "ask"),        # sensitive
    ("Write", {"file_path": "C:/x/custom_tools/evil.py"}, "ask"),  # exec'd at startup
    ("Write", {"file_path": "C:/Temp/run.ps1"}, "ask"),         # executable script
    ("Write", {"file_path": "C:/Users/Tim/.bashrc"}, "ask"),    # shell init
    ("Write", {"file_path": ROOT + "/helios/app.py"}, "ask"),   # self-modification
    ("Edit", {"file_path": "C:/Temp/ok.md"}, "allow"),
    ("Edit", {"file_path": ROOT + "/config/settings.toml"}, "ask"),
    # --- shell -----------------------------------------------------------------------
    ("Bash", {"command": "Get-Process"}, "allow"),             # read-only
    ("Bash", {"command": "git status"}, "allow"),
    ("Bash", {"command": "ls C:/Users/Tim"}, "allow"),
    ("Bash", {"command": "Set-AudioDevice -PlaybackVolume 30"}, "allow"),  # safe setting
    ("Bash", {"command": "Remove-Item C:/x.txt"}, "ask"),      # mutating
    ("Bash", {"command": "Get-Process | Stop-Process"}, "ask"),  # chained/piped
    ("Bash", {"command": "echo hi & del x"}, "ask"),           # chained
    ("Bash", {"command": "cat .env"}, "ask"),                  # sensitive target
    ("PowerShell", {"command": "type id_rsa"}, "ask"),         # sensitive target
    ("PowerShell", {"command": "Get-ChildItem"}, "allow"),
    # --- connected apps (Composio) ---------------------------------------------------
    ("mcp__composio__GMAIL_FETCH_EMAILS", None, "allow"),      # read verb
    ("mcp__composio__GITHUB_GET_REPO", None, "allow"),
    ("mcp__composio__GMAIL_SEND_EMAIL", None, "ask"),          # write verb
    ("mcp__composio__GITHUB_CREATE_ISSUE", None, "ask"),
    ("mcp__composio__SOMEAPP_FROBNICATE", None, "ask"),        # unknown verb -> fail safe
    # --- unknown ---------------------------------------------------------------------
    ("SomeRandomMcpTool", {}, "ask"),
]


@pytest.mark.parametrize("tool,inp,expected", CASES)
def test_classify(tool, inp, expected):
    assert permissions.classify(tool, inp) == expected


def test_webfetch_not_in_allowlist():
    """WebFetch must never be silently auto-approved (the read-then-exfiltrate vector)."""
    assert "WebFetch" not in conf.autonomy_allow()


def test_is_internal_url_blocks_loopback():
    assert permissions.is_internal_url("http://127.0.0.1:8769/x") is True
    assert permissions.is_internal_url("http://localhost/x") is True
    assert permissions.is_internal_url("http://10.0.0.5/x") is True
    assert permissions.is_internal_url("https://example.com/x") is False


def test_is_claude_dir():
    assert permissions.is_claude_dir("C:/Users/Tim/.claude/memory/x.md") is True
    assert permissions.is_claude_dir("C:/Users/Tim/helios/x.md") is False
