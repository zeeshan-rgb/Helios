"""BeforeTool hook for Helios's Gemini CLI brain — the same permission gate the Claude brain uses.

Gemini runs this before every tool call (wired per-spawn by helios/gemini_cli.py). The Gemini
tool call is translated to its Claude Code equivalent (run_shell_command -> PowerShell,
write_file -> Write, mcp_<server>_<tool> -> mcp__<server>__<tool>, ...) and handed to
hooks/pretooluse.decide(), so every Helios rule applies unchanged: panic stop, screen lock,
SSRF block, YOLO, the allow/ask policy and the Approve/Deny prompt in the UI / Telegram.

Gemini treats a crashed or silent hook as "allow", so this script must ALWAYS print a decision:
any error prints deny. Output: {"decision": "allow"|"deny", "reason": ...}; deny also exits 2.
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

# Gemini-only tools that are harmless bookkeeping (plans, todos, skills, topic/task state).
_BENIGN = {"write_todos", "activate_skill", "get_internal_docs", "update_topic", "complete_task",
           "invoke_agent", "tracker_create_task", "tracker_update_task", "tracker_get_task",
           "tracker_list_tasks", "tracker_add_dependency", "tracker_visualize"}
# Helios's memory is the Obsidian vault; Gemini's own memory tool would fork it.
_HARD_DENY = {"save_memory": "Helios keeps memory in the Obsidian vault automatically; "
                             "save_memory is disabled."}
_URL_RX = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _emit(decision: str, reason: str) -> None:
    print(json.dumps({"decision": decision, "reason": reason}))
    sys.stdout.flush()
    sys.exit(0 if decision == "allow" else 2)


def _first_path(inp: dict) -> str:
    for k in ("file_path", "absolute_path", "path", "dir_path"):
        if inp.get(k):
            return str(inp[k])
    return ""


def translate(tool_name: str, tool_input: dict, mcp_context: dict | None = None) -> tuple[str, dict]:
    """Map a Gemini CLI tool call to the Claude Code tool name + input Helios's policy expects."""
    inp = tool_input or {}
    if mcp_context and mcp_context.get("server_name") and mcp_context.get("tool_name"):
        return f"mcp__{mcp_context['server_name']}__{mcp_context['tool_name']}", inp
    if tool_name == "run_shell_command":
        return "PowerShell", {"command": str(inp.get("command", ""))}
    if tool_name == "write_file":
        return "Write", {"file_path": _first_path(inp), "content": str(inp.get("content", ""))[:2000]}
    if tool_name == "replace":
        return "Edit", {"file_path": _first_path(inp)}
    if tool_name == "read_file":
        return "Read", {"file_path": _first_path(inp)}
    if tool_name == "read_many_files":
        paths = inp.get("include") or inp.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        return "Read", {"file_path": " ".join(str(p) for p in paths)}
    if tool_name == "list_directory":
        return "LS", {"path": _first_path(inp)}
    if tool_name == "glob":
        return "Glob", {"pattern": str(inp.get("pattern", "")), "path": _first_path(inp)}
    if tool_name == "grep_search":
        return "Grep", {"pattern": str(inp.get("pattern", "")), "path": _first_path(inp)}
    if tool_name == "google_web_search":
        return "WebSearch", {"query": str(inp.get("query", ""))}
    if tool_name == "web_fetch":
        prompt = str(inp.get("prompt", "") or inp.get("url", ""))
        urls = _URL_RX.findall(prompt)
        return "WebFetch", {"url": urls[0] if urls else "", "urls": urls, "prompt": prompt[:500]}
    if tool_name in _BENIGN:
        return "TodoWrite", {}
    if tool_name.startswith("mcp_"):
        # No mcp_context (older CLI): mcp_<server>_<tool>, server names contain no "_".
        rest = tool_name[4:]
        server, _, tool = rest.partition("_")
        if server and tool:
            return f"mcp__{server}__{tool}", inp
    return tool_name, inp


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


def gemini_decide(tool_name: str, tool_input: dict, session_id: str = "",
                  mcp_context: dict | None = None) -> tuple[str, str]:
    if os.environ.get("GEMINI_CLI_HELIOS_DENY_ALL") == "1":
        return "deny", "tools are disabled for this call"
    if tool_name in _HARD_DENY:
        return "deny", _HARD_DENY[tool_name]
    name, inp = translate(tool_name, tool_input, mcp_context)
    policy = _load_policy()
    from helios import permissions
    if name in ("Write", "Edit") and _is_gemini_config(inp.get("file_path", "")):
        return "deny", "Helios does not modify the Gemini CLI's own config."
    if name == "WebFetch":
        urls = inp.get("urls") or []
        if any(permissions.is_internal_url(u) for u in urls):
            return "deny", "SSRF blocked: web_fetch to a private/loopback address is not allowed"
        inp = {k: v for k, v in inp.items() if k != "urls"}
    return policy.decide(name, inp, session_id)


def _is_gemini_config(path: str) -> bool:
    if not path:
        return False
    norm = lambda s: str(s).replace("\\", "/").rstrip("/").lower()
    p = norm(path)
    try:
        p_real = norm(Path(path).resolve())
    except Exception:
        p_real = p
    gemini_data = _ROOT / "data" / "gemini"
    protected = {norm(Path.home() / ".gemini"), norm(gemini_data)}
    try:
        protected.add(norm(gemini_data.resolve()))
    except Exception:
        pass
    return "/.gemini/" in p + "/" or any(c == d or c.startswith(d + "/")
                                          for c in (p, p_real) for d in protected)


def main() -> None:
    # The Claude-format policy module prints its own JSON (and may sys.exit) on load failure;
    # swallow its stdout so the only thing Gemini ever parses is our decision below.
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        payload = json.loads(raw) if raw.strip() else {}
        with contextlib.redirect_stdout(io.StringIO()):
            decision, reason = gemini_decide(
                str(payload.get("tool_name", "")),
                payload.get("tool_input") or {},
                str(payload.get("session_id", "") or ""),
                payload.get("mcp_context") or None,
            )
    except BaseException as e:  # incl. SystemExit — fail closed: Gemini treats silence as allow
        decision, reason = "deny", f"Helios permission gate error; denied for safety ({e!r})"
    _emit("allow" if decision == "allow" else "deny", reason or decision)


if __name__ == "__main__":
    main()
