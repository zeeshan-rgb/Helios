"""Gemini CLI engine ([brain].engine = "gemini"): shared plumbing for spawning Google's `gemini`.

Helios drives the Gemini CLI headless — signed in with the user's own Google account, so a Gemini
Pro plan's quota applies and no API key is needed — the same way brain.py drives `claude -p`.

Isolation: every spawn runs with GEMINI_CLI_HOME = data/gemini/home, i.e. Helios's OWN ~/.gemini
(settings, Google sign-in, sessions). The user's personal Gemini CLI / Antigravity config is never
read or touched. Its settings.json (kept in sync by ensure_settings) wires:
  - a BeforeTool hook -> hooks/gemini_pretool.py, Helios's permission gate (same policy as Claude)
  - a SessionStart hook that writes a per-run marker file — PROOF the settings (and so the gate)
    actually loaded. Tools only run in --approval-mode yolo, so a turn whose marker is missing
    when the first stream event arrives is killed before the model can call anything.
  - Helios's MCP servers (config/mcp.json + config/composio_mcp.json)
  - HELIOS.md as the context file, rewritten each turn with the persona + memory digest
Per-run values travel as environment variables (the CLI forwards the full env to hooks and MCP
servers; GEMINI_CLI_* names are never redacted).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from . import conf

CREATE_NO_WINDOW = 0x08000000
GEMINI_DIR = conf.DATA_DIR / "gemini"
HOME = GEMINI_DIR / "home"              # GEMINI_CLI_HOME -> HOME/.gemini is Helios's ~/.gemini
WORKSPACE = GEMINI_DIR / "workspace"    # stable cwd for the live brain (sessions are keyed by cwd)
RUNS_DIR = GEMINI_DIR / "runs"          # per-run cwd for side agents / helpers + gate markers
HOOK_SCRIPT = conf.HOOKS_DIR / "gemini_pretool.py"
CONTEXT_FILE = "HELIOS.md"
MARKER_ENV = "GEMINI_CLI_HELIOS_MARKER"
DENY_ALL_ENV = "GEMINI_CLI_HELIOS_DENY_ALL"

# Router tier -> Gemini CLI model alias (the CLI resolves these to the current models).
_TIER_DEFAULTS = {"light": "flash-lite", "medium": "flash", "heavy": "pro"}
_CLAUDE_TIER = {"haiku": "light", "sonnet": "medium", "opus": "heavy", "fable": "heavy"}
# Useless headless (ask_user, plan mode) or would fork Helios's vault memory (save_memory).
_EXCLUDED_TOOLS = ["ask_user", "enter_plan_mode", "exit_plan_mode", "save_memory"]
# Hook timeout must outlast the app's 125s Approve/Deny wait: a timed-out hook counts as allow.
_HOOK_TIMEOUT_MS = 150_000
_AT_IN_PROMPT = re.compile(r"(?<!\\)@")
_AT_IMPORT = re.compile(r"(^|\s)@(?=[./A-Za-z])")
_SETTINGS_LOCK = threading.Lock()


class GateNotLoaded(RuntimeError):
    """The CLI started without Helios's settings, so the permission gate isn't active."""


def cfg() -> dict:
    return dict(conf.SETTINGS.get("gemini", {}))


def command() -> list[str]:
    """argv prefix that runs the CLI: [node, bundle/gemini.js] when resolvable (no cmd.exe layer
    between us and the prompt), else the gemini(.cmd) shim itself. [] if not installed."""
    explicit = cfg().get("bin")
    cand = str(explicit) if explicit else (shutil.which("gemini.cmd") or shutil.which("gemini"))
    if not cand:
        return []
    p = Path(cand)
    node = shutil.which("node")
    if p.suffix.lower() == ".js":
        return [node, str(p)] if node and p.exists() else []
    bundle = p.parent / "node_modules" / "@google" / "gemini-cli" / "bundle" / "gemini.js"
    if node and bundle.exists():
        return [node, str(bundle)]
    return [str(p)] if p.exists() else []


def model_for(tier: str | None = None, model: str | None = None) -> str:
    """Pick the Gemini model for a router decision or a requested model. Claude tier aliases
    (haiku/sonnet/opus) map to light/medium/heavy; an explicit non-Claude name is used as-is."""
    c = cfg()
    m = (model or "").strip()
    if m.lower() in _CLAUDE_TIER:
        tier = _CLAUDE_TIER[m.lower()]
    elif m and not m.lower().startswith("claude"):
        return m
    if tier in _TIER_DEFAULTS:
        return str(c.get(tier) or _TIER_DEFAULTS[tier])
    return str(c.get("model") or "auto")


def include_dirs() -> list[str]:
    """Folders outside the cwd the CLI's file tools may touch (the user's home + the vault)."""
    dirs = [str(Path.home()), str(conf.vault_path())]
    dirs += [str(d) for d in (cfg().get("include_dirs") or [])]
    seen, out = set(), []
    for d in dirs:
        if d and d.lower() not in seen and Path(d).exists():
            seen.add(d.lower())
            out.append(d)
    return out


def escape_prompt(text: str) -> str:
    """Stop the CLI expanding `@path` in a prompt into a file read that skips the permission gate
    (a pasted email could otherwise pull in ~/.ssh/...). The CLI honors `\\@` as a literal @."""
    return _AT_IN_PROMPT.sub(r"\\@", text)


def neutralize_imports(text: str) -> str:
    """Context files treat `@path` at a word start as an import; a zero-width space defuses it."""
    return _AT_IMPORT.sub(lambda m: m.group(1) + "@​", text)


def _convert_server(s: dict) -> dict | None:
    """Claude-style MCP server entry -> Gemini CLI settings entry (None if unusable here)."""
    if not isinstance(s, dict):
        return None
    out: dict = {}
    if s.get("command"):
        cmd = str(s["command"])
        if os.path.isabs(cmd) and not Path(cmd).exists():
            return None    # e.g. the cua-driver computer-use engine isn't installed
        out["command"] = cmd
        out["args"] = [str(a) for a in (s.get("args") or [])]
        if s.get("env"):
            out["env"] = {k: str(v) for k, v in s["env"].items()}
        if s.get("cwd"):
            out["cwd"] = str(s["cwd"])
    elif s.get("url"):
        kind = str(s.get("type", "")).lower()
        out["url" if kind == "sse" else "httpUrl"] = str(s["url"])
        if s.get("headers"):
            out["headers"] = {k: str(v) for k, v in s["headers"].items()}
    else:
        return None
    return out


def mcp_servers() -> dict:
    servers: dict = {}
    for name in ("mcp.json", "composio_mcp.json"):
        p = conf.CONFIG_DIR / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            conf.log("gemini", f"unreadable {name}: {e}")
            continue
        for sname, entry in (data.get("mcpServers") or {}).items():
            conv = _convert_server(entry)
            if conv:
                servers[sname] = conv
    return servers


def _python_exe() -> str:
    """Console python for the hook (pythonw has no usable stdio under some launchers)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def settings_file() -> Path:
    return HOME / ".gemini" / "settings.json"


def managed_settings(servers: dict | None = None) -> dict:
    """The keys Helios owns in its private settings.json (hook commands run under PowerShell)."""
    turns = int(conf.SETTINGS.get("claude", {}).get("max_turns", 80))
    gate = f"& {_ps_quote(_python_exe())} {_ps_quote(str(HOOK_SCRIPT))}"
    marker = (f"if ($env:{MARKER_ENV}) {{ Set-Content -LiteralPath $env:{MARKER_ENV} "
              f"-Value ok -Encoding ascii }}")
    s = {
        "hooksConfig": {"enabled": True},
        "hooks": {
            "BeforeTool": [{"matcher": ".*", "hooks": [{
                "type": "command", "name": "helios-permission-gate", "command": gate,
                "timeout": _HOOK_TIMEOUT_MS}]}],
            "SessionStart": [{"hooks": [{
                "type": "command", "name": "helios-gate-marker", "command": marker,
                "timeout": 30_000}]}],
        },
        "context": {"fileName": [CONTEXT_FILE]},
        "tools": {"exclude": list(_EXCLUDED_TOOLS)},
        "model": {"maxSessionTurns": turns},
        "mcpServers": servers if servers is not None else mcp_servers(),
    }
    return s


def ensure_settings() -> dict:
    """Write Helios's managed keys into its private settings.json, keeping anything the CLI
    itself stored there (e.g. the auth method chosen at sign-in). Returns the MCP servers."""
    servers = mcp_servers()
    desired = managed_settings(servers)
    path = settings_file()
    with _SETTINGS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(current, dict):
                current = {}
        except Exception:
            current = {}
        merged = dict(current)
        for key, val in desired.items():
            if key == "model" and isinstance(current.get("model"), dict):
                merged["model"] = {**current["model"], **val}
            else:
                merged[key] = val
        if merged != current:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            os.replace(tmp, path)
    stray = HOME / ".gemini" / CONTEXT_FILE   # a global HELIOS.md would leak into every run
    if stray.exists():
        stray.unlink(missing_ok=True)
    return servers


def signed_in() -> bool:
    """Best-effort: has the user signed Helios's Gemini home into a Google account?"""
    g = HOME / ".gemini"
    return (g / "oauth_creds.json").exists() or (g / "google_accounts.json").exists()


def prepare_run(system: str, *, tools: bool = True, isolated: bool = False) -> dict:
    """Set up one spawn. The live brain (isolated=False) reuses the stable WORKSPACE so --resume
    works — its turns are single-flight, so rewriting WORKSPACE/HELIOS.md is race-free. Side
    agents and helper calls (isolated=True) get their own throwaway cwd."""
    servers = ensure_settings()
    run_id = uuid.uuid4().hex
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    cwd = (RUNS_DIR / run_id) if isolated else WORKSPACE
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / CONTEXT_FILE).write_text(neutralize_imports(system), encoding="utf-8")
    return {"id": run_id, "cwd": cwd, "isolated": isolated, "tools": tools,
            "marker": RUNS_DIR / f"{run_id}.gate", "servers": list(servers) if tools else []}


def gate_loaded(run: dict) -> bool:
    return Path(run["marker"]).exists()


def args(run: dict, model: str, *, output: str = "stream-json",
         resume: str | None = None) -> list[str]:
    a = [*command(), "--model", model, "--output-format", output,
         # The BeforeTool hook is the approval gate (its load is proven per run, see module doc);
         # Gemini's own confirmation prompt can't be answered headless.
         "--approval-mode", "yolo", "--skip-trust",
         # Only Helios's own servers, and none at all for tool-less runs.
         "--allowed-mcp-server-names", *(run["servers"] or ["helios-none"])]
    if run["tools"]:
        dirs = include_dirs()
        if dirs:
            a += ["--include-directories", ",".join(dirs)]
    if resume:
        a += ["--resume", resume]
    return a


def env(run: dict, extra: dict | None = None) -> dict:
    e = dict(os.environ)
    e["GEMINI_CLI_HOME"] = str(HOME)
    e["GEMINI_CLI_NO_RELAUNCH"] = "1"   # one process per turn, so panic's tree-kill is clean
    e["NO_COLOR"] = "1"
    e[MARKER_ENV] = str(run["marker"])
    e.pop("GEMINI_CLI_SYSTEM_SETTINGS_PATH", None)
    if run["tools"]:
        e.pop(DENY_ALL_ENV, None)
    else:
        e[DENY_ALL_ENV] = "1"
    for k, v in (extra or {}).items():
        if v not in (None, ""):
            e[k] = str(v)
    return e


def cleanup(run: dict | None) -> None:
    if not run:
        return
    try:
        Path(run["marker"]).unlink(missing_ok=True)
    except Exception:
        pass
    if run.get("isolated"):
        shutil.rmtree(run["cwd"], ignore_errors=True)
    else:
        try:
            (Path(run["cwd"]) / CONTEXT_FILE).unlink(missing_ok=True)
        except Exception:
            pass


def looks_unauthenticated(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in ("please set an auth method", "login required", "not authenticated",
                                "sign in with google", "oauth", "gemini_api_key"))


def collect(run: dict, argv: list[str], prompt: str, *, extra_env: dict | None = None,
            timeout: int = 600, register=None, label: str = "gemini") -> dict:
    """Run one non-interactive spawn to completion (stream-json), enforcing the gate proof.
    Returns {text, session_id, errors, stderr, gate_missing}."""
    from .proc_util import kill_tree
    res = {"text": "", "session_id": None, "errors": [], "stderr": "", "gate_missing": False}
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW,
                                cwd=str(run["cwd"]), env=env(run, extra_env))
    except Exception as e:
        res["errors"].append(f"spawn failed: {e}")
        return res
    if register:
        try:
            register(proc)
        except Exception:
            pass
    err_buf: list[str] = []
    drain = threading.Thread(target=lambda: err_buf.extend(proc.stderr), daemon=True)
    drain.start()
    timer = threading.Timer(timeout, lambda: kill_tree(proc))
    timer.start()
    parts: list[str] = []
    try:
        try:
            proc.stdin.write(escape_prompt(prompt) + "\n")
            proc.stdin.close()
        except Exception:
            pass
        for line in proc.stdout:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            t = obj.get("type")
            if t == "init":
                res["session_id"] = obj.get("session_id")
                if not gate_loaded(run):
                    res["gate_missing"] = True
                    kill_tree(proc)
                    break
            elif t == "message" and obj.get("role") == "assistant" and obj.get("content"):
                parts.append(obj["content"])
            elif t == "error" and obj.get("severity") == "error":
                res["errors"].append(str(obj.get("message") or ""))
            elif t == "result" and obj.get("status") == "error":
                res["errors"].append(str((obj.get("error") or {}).get("message") or ""))
    finally:
        timer.cancel()
        try:
            proc.wait(timeout=10)
        except Exception:
            kill_tree(proc)
        drain.join(timeout=3)
    res["text"] = "".join(parts).strip()
    res["stderr"] = "".join(err_buf)
    if res["gate_missing"]:
        conf.log("gemini", f"[{label}] permission gate did not load — run killed")
    elif not res["text"]:
        conf.log("gemini", f"[{label}] no reply rc={proc.returncode} "
                           f"errors={res['errors'][-2:]} err={res['stderr'][-300:]}")
    return res


def complete_json(prompt: str, *, model: str, schema: dict | None = None,
                  system: str | None = None, timeout: int = 120) -> dict | None:
    """Tool-less one-shot (router triage, memory extraction). Same return shape as
    claude_cli.complete_json: {text, data, session_id, cost, error}; None on hard failure."""
    from . import claude_cli
    if not command():
        conf.log("gemini", "complete_json: gemini CLI not found")
        return None
    sys_text = (system or "").strip() or "You are a precise assistant."
    if schema:
        sys_text += ("\n\nReturn ONLY a single minified JSON object — no prose, no code fences. "
                     "It must satisfy this JSON schema: " + json.dumps(schema))
    run = prepare_run(sys_text, tools=False, isolated=True)
    try:
        res = collect(run, args(run, model_for(model=model)), prompt, timeout=timeout,
                      label="complete_json")
    finally:
        cleanup(run)
    if res["gate_missing"] or not res["text"]:
        return None
    text = res["text"]
    data = None
    if schema:
        try:
            data = json.loads(claude_cli._strip_fences(text))
        except Exception:
            data = claude_cli._extract_json(text)
    return {"text": text, "data": data, "session_id": res["session_id"], "cost": None,
            "error": None}


def run_agent(prompt: str, system: str, *, model: str, extra_env: dict | None = None,
              timeout: int = 600, register=None, label: str = "agent") -> str | None:
    """A background agent turn (side agents, mission workers) with tools, gated by the hook.
    Returns the final reply text or None."""
    if not command():
        conf.log("gemini", f"run_agent [{label}]: gemini CLI not found")
        return None
    run = prepare_run(system, tools=True, isolated=True)
    try:
        res = collect(run, args(run, model), prompt, extra_env=extra_env, timeout=timeout,
                      register=register, label=label)
    finally:
        cleanup(run)
    return res["text"] or None


def engine_note(servers: list[str]) -> str:
    """Appended to every Gemini system prompt: maps the Claude-flavoured persona onto Gemini's
    tool names and states what is and isn't available on this engine."""
    home = str(Path.home())
    lines = [
        "## ENGINE: GEMINI CLI",
        "You are Helios, running on the Gemini CLI engine. Never call yourself Gemini, Claude or "
        "Jarvis — you are Helios.",
        "The instructions above name tools the Claude Code way. Your actual tool names:",
        "- `mcp__<server>__<tool>` is `mcp_<server>_<tool>` for you (e.g. `mcp__helios__open_app` "
        "-> `mcp_helios_open_app`).",
        "- Bash/PowerShell -> `run_shell_command` (Windows PowerShell); Read -> `read_file`; "
        "Write -> `write_file`; Edit -> `replace`; Glob -> `glob`; Grep -> `grep_search`; "
        "WebFetch -> `web_fetch`; WebSearch -> `google_web_search`.",
        "Every tool call passes Helios's permission gate. If one is blocked or denied, do not "
        "retry it — adapt, and tell the user plainly.",
        f"Your working folder is a Helios scratch folder. The user's home folder is {home}; "
        "always use absolute paths for the user's files.",
        "In user messages `\\@` is a literal @ (escaped by Helios) — write it as a plain @.",
    ]
    if "computer" not in servers:
        lines.append("PC control (the mcp__computer__* / cua-driver tools) is NOT available right "
                     "now — ignore instructions about driving the GUI by element index; use "
                     "`mcp_helios_open_app` and shell commands instead.")
    if "helios" not in servers:
        lines.append("Helios's own tools (reminders, open_app, system health, ...) are not "
                     "connected right now.")
    return "\n".join(lines)
