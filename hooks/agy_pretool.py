"""PreToolUse hook for Helios's Antigravity brain — the same permission gate the Claude brain uses.

agy runs this before every tool call (wired per workspace by helios/agy_cli.py). The agy tool call
is translated to its Claude Code equivalent (run_command -> PowerShell, write_to_file -> Write,
call_mcp_tool -> mcp__<server>__<tool>, ...) and handed to hooks/pretooluse.decide(), so every
Helios rule applies unchanged: panic stop, screen lock, SSRF block, YOLO, allow/ask policy and the
Approve/Deny prompt in the UI / Telegram / voice.

Helios runs agy with --dangerously-skip-permissions (a hook "allow" can't grant a headless permission),
so THIS hook is the only gate. Verified agy behaviour: a crashing or timed-out hook blocks the tool,
but a hook that exits 0 with no output lets it run — so this script must ALWAYS print a decision.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# agy bookkeeping / orchestration tools with no side effects outside the conversation.
_BENIGN = {"command_status", "wait", "wait_5_seconds", "finish", "ask_permission",
           "ask_custom_permission", "ask_question", "list_permissions", "list_resources",
           "read_resource", "invoke_subagent", "define_subagent", "manage_subagents",
           "generate_image"}
# Reading the browser's state is observation; acting in it (click/type/js/navigate) asks.
_BROWSER_READ = {"list_browser_pages", "read_browser_page", "browser_get_dom",
                 "capture_browser_screenshot", "capture_browser_console_logs",
                 "browser_list_network_requests", "browser_get_network_request"}
# Helios owns scheduling and messaging (reminders, routines, Telegram); agy's own versions would
# create autonomous work Helios can't see or stop.
_HELIOS_OWNED = {"schedule", "manage_task", "manage_inbox", "send_message"}
_URL_RX = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _first(inp: dict, *keys: str) -> str:
    for k in keys:
        v = inp.get(k)
        if v:
            return str(v)
    return ""


def _path(inp: dict) -> str:
    return _first(inp, "TargetFile", "AbsolutePath", "FilePath", "File", "Path", "DirectoryPath",
                  "SearchPath", "SearchDirectory", "NotebookPath", "file_path", "path")


def translate(name: str, inp: dict) -> tuple[str, dict]:
    """Map an agy tool call to the Claude Code tool name + input Helios's policy expects."""
    inp = inp or {}
    if name == "run_command":
        return "PowerShell", {"command": _first(inp, "CommandLine", "Command", "command")}
    if name == "send_command_input":
        return "PowerShell", {"command": _first(inp, "Input", "Text", "CommandLine", "input")}
    if name == "write_to_file":
        return "Write", {"file_path": _path(inp),
                         "content": _first(inp, "CodeContent", "Content")[:2000]}
    if name in ("replace_file_content", "multi_replace_file_content", "sed_file"):
        return "Edit", {"file_path": _path(inp)}
    if name == "notebook_edit":
        return "NotebookEdit", {"notebook_path": _path(inp)}
    if name == "notebook_execution":
        return "PowerShell", {"command": f"notebook_execution {_path(inp)}"}
    if name == "view_file":
        return "Read", {"file_path": _path(inp)}
    if name == "list_dir":
        return "LS", {"path": _path(inp)}
    if name == "find_by_name":
        return "Glob", {"pattern": _first(inp, "Pattern", "pattern"), "path": _path(inp)}
    if name == "grep_search":
        return "Grep", {"pattern": _first(inp, "Query", "Pattern", "pattern"), "path": _path(inp)}
    if name == "search_web":
        return "WebSearch", {"query": _first(inp, "Query", "query")}
    if name in ("read_url_content", "open_browser_url"):
        url = _first(inp, "Url", "URL", "url")
        return "WebFetch", {"url": url, "urls": _URL_RX.findall(url) or [url]}
    if name in _BENIGN:
        return "TodoWrite", {}
    if name in _BROWSER_READ:
        return "LS", {"path": "browser"}
    if name == "call_mcp_tool":
        server = _first(inp, "ServerName", "serverName", "server")
        tool = _first(inp, "ToolName", "toolName", "tool")
        args = inp.get("Arguments") or inp.get("arguments") or inp.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"raw": args}
        return f"mcp__{server}__{tool}", args if isinstance(args, dict) else {}
    return name, inp   # unknown / browser actions / delete_knowledge -> classify() asks


def _protected_roots() -> list[str]:
    """Helios's own agent configuration + the agy/Gemini config dirs. Writing (or shelling into)
    these could remove this very gate for the next tool call, so they are hard-denied."""
    norm = lambda s: str(s).replace("\\", "/").rstrip("/").lower()
    roots = {norm(Path.home() / ".gemini"), norm(_ROOT / "hooks"), norm(_ROOT / "data" / "antigravity")}
    try:
        roots.add(norm((_ROOT / "data" / "antigravity").resolve()))
    except Exception:
        pass
    return sorted(roots)


def _projects_config(s: str) -> bool:
    try:
        from helios import permissions
        return permissions.is_projects_config(s)
    except Exception:
        return False


def _touches_protected(name: str, inp: dict) -> bool:
    if name == "PowerShell":
        cmd = str(inp.get("command", "")).replace("\\", "/").lower()
        return (any(s in cmd for s in (".agents", ".gemini", "hooks.json", "mcp_config.json"))
                or _projects_config(cmd))
    if name in ("Write", "Edit", "NotebookEdit"):
        p = str(inp.get("file_path") or inp.get("notebook_path") or "").replace("\\", "/").lower()
        return ("/.agents/" in p or p.endswith("/.agents") or _projects_config(p)
                or any(p == r or p.startswith(r + "/") for r in _protected_roots()))
    return False


_policy = None


def _load_policy():
    global _policy
    if _policy is None:
        spec = importlib.util.spec_from_file_location("helios_pretooluse",
                                                      _ROOT / "hooks" / "pretooluse.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _policy = mod
    return _policy


def _sink() -> str:
    s = os.environ.get("HELIOS_PERM_SINK", "")
    f = os.environ.get("HELIOS_AGY_SINK_FILE", "")
    if not s and f:
        try:
            s = Path(f).read_text(encoding="utf-8").strip()
        except Exception:
            s = ""
    return s


_RESEARCH_TOOLS = {"search_web", "read_url_content"}


def _allow_only_decide(tool_name: str, tool_input: dict, allowed_env: str) -> tuple[str, str]:
    """Research mode (set only by Helios's own code): web search + reading PUBLIC pages, nothing
    else — no files, commands, MCP tools or browser. The env list can never grant more than
    _RESEARCH_TOOLS."""
    allowed = {t.strip() for t in allowed_env.split(",")} & _RESEARCH_TOOLS
    if tool_name not in allowed:
        return "deny", "research mode: only web search and reading public pages are allowed"
    if tool_name == "read_url_content":
        from helios import permissions
        url = _first(tool_input or {}, "Url", "URL", "url")
        urls = _URL_RX.findall(url)
        if not urls or any(permissions.is_internal_url(u) for u in urls):
            return "deny", "research mode: only public http(s) pages may be read"
    return "allow", "research mode: read-only web access"


def agy_decide(tool_name: str, tool_input: dict, conversation_id: str = "") -> tuple[str, str]:
    if os.environ.get("HELIOS_AGY_DENY_ALL") == "1":
        return "deny", "tools are disabled for this call"
    allow_only = os.environ.get("HELIOS_AGY_ALLOW_ONLY")
    if allow_only is not None:
        return _allow_only_decide(tool_name, tool_input, allow_only)
    if tool_name in _HELIOS_OWNED:
        return "deny", ("Helios owns scheduling and messaging — use Helios's reminders/routines "
                        "(mcp helios set_reminder / create_routine) instead.")
    name, inp = translate(tool_name, tool_input)
    if _touches_protected(name, inp):
        return "deny", "Helios protects its own agent configuration (.agents, .gemini, hooks)."
    policy = _load_policy()
    from helios import permissions
    if name == "WebFetch":
        urls = [u for u in (inp.get("urls") or []) if u]
        if any(permissions.is_internal_url(u) for u in urls):
            return "deny", "SSRF blocked: fetching a private/loopback address is not allowed"
        inp = {k: v for k, v in inp.items() if k != "urls"}
    sink = _sink()
    if sink:
        os.environ["HELIOS_PERM_SINK"] = sink
    return policy.decide(name, inp, conversation_id)


def main() -> None:
    # The Claude-format policy module prints its own JSON (and may sys.exit) on load failure;
    # swallow its stdout so the only thing agy ever parses is our decision below.
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace").lstrip("﻿")   # tolerate a BOM
        payload = json.loads(raw) if raw.strip() else {}
        call = payload.get("toolCall") or {}
        with contextlib.redirect_stdout(io.StringIO()):
            decision, reason = agy_decide(str(call.get("name", "")), call.get("args") or {},
                                          str(payload.get("conversationId", "") or ""))
    except BaseException as e:  # incl. SystemExit — fail closed: silence would mean allow
        decision, reason = "deny", f"Helios permission gate error; denied for safety ({e!r})"
    print(json.dumps({"decision": "allow" if decision == "allow" else "deny",
                      "reason": reason or decision}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
