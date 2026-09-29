"""Antigravity CLI engine ([brain].engine = "antigravity"): plumbing for Google's official `agy`.

Helios drives the official agy binary through its documented headless interface — signed in with
the user's own Google account (Google AI Pro entitlement), never extracting tokens or calling
Antigravity endpoints itself. Helios stays the orchestrator (voice, memory, persona, safety);
agy is the execution backend.

Every agy process runs in a Helios-owned workspace whose .agents/ folder Helios writes:
  - hooks.json: a PreToolUse gate -> hooks/agy_pretool.py (the Helios permission policy) and a
    PreInvocation marker -> hooks/agy_marker.py. Verified on agy 1.2.x: a hook "allow" cannot grant a
    headless permission, so agy runs with --dangerously-skip-permissions and the gate is the ONLY
    approval authority; agy also drops the entire hooks.json if any entry is malformed, so the
    marker must exist by the first model step or the turn is killed.
  - rules/helios.md: persona + engine notes (rules are read at session start).
  - mcp_config.json: Helios's MCP servers (config/mcp.json + composio_mcp.json).
Per-process values travel as environment variables, which agy passes on to its hooks.
"""

from __future__ import annotations

import hashlib
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
AGY_DIR = conf.DATA_DIR / "antigravity"
WORKSPACE = AGY_DIR / "workspace"      # the live session's cwd
RUNS_DIR = AGY_DIR / "runs"            # throwaway cwd per side agent / helper call
MARKERS_DIR = AGY_DIR / "markers"
SINK_FILE = AGY_DIR / "perm_sink.txt"  # where the live turn's permission asks should surface
GATE_SCRIPT = conf.HOOKS_DIR / "agy_pretool.py"
MARKER_SCRIPT = conf.HOOKS_DIR / "agy_marker.py"
MARKER_ENV = "HELIOS_AGY_MARKER"
DENY_ALL_ENV = "HELIOS_AGY_DENY_ALL"
SINK_ENV = "HELIOS_AGY_SINK_FILE"

# Router tier -> agy model slug (see `agy models`); the live session uses [antigravity].model.
_TIER_DEFAULTS = {"light": "gemini-3.8-flash-low", "medium": "gemini-3.8-flash-medium",
                  "heavy": "gemini-3.1-pro-high"}
_CLAUDE_TIER = {"haiku": "light", "sonnet": "medium", "opus": "heavy", "fable": "heavy"}
_GATE_TIMEOUT_S = 150    # must outlast the app's 125s Approve/Deny wait
_IMPORT_AT = re.compile(r"(^|\s)@(?=[./~A-Za-z\\])")


def cfg() -> dict:
    return dict(conf.SETTINGS.get("antigravity", {}))


def command() -> list[str]:
    """argv prefix for agy: [agy.exe], or [python, script.py] for a scripted stand-in (tests)."""
    explicit = cfg().get("bin")
    cand = str(explicit) if explicit else (shutil.which("agy.exe") or shutil.which("agy"))
    if not cand or not Path(cand).exists():
        return []
    if cand.lower().endswith(".py"):
        return [sys.executable, cand]
    return [cand]


def model_for(tier: str | None = None, model: str | None = None) -> str:
    """agy model slug for a router decision / requested model ('' = agy's own default)."""
    c = cfg()
    m = (model or "").strip()
    if m.lower() in _CLAUDE_TIER:
        tier = _CLAUDE_TIER[m.lower()]
    elif m and not m.lower().startswith("claude"):
        return m
    if tier in _TIER_DEFAULTS:
        return str(c.get(tier) or _TIER_DEFAULTS[tier])
    return str(c.get("model") or "")


def neutralize(text: str) -> str:
    """Defuse agy's file-inlining syntax (`@[label](path)`, `@path`) in text Helios passes along,
    so pasted content can't pull a local file into the prompt behind the permission gate."""
    text = text.replace("@[", "@​[")
    return _IMPORT_AT.sub(lambda m: m.group(1) + "@​", text)


def _python_exe() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


_CMD_UNSAFE = set('"&|<>^%!()')


def _cmd_path(p: str) -> str:
    """A path safe to place UNQUOTED in an agy hook command. agy runs hook commands through cmd.exe
    and escapes embedded quotes into literal \\" (verified: a quoted path is 'not recognized'), so
    paths with spaces use their 8.3 short name and paths with shell metacharacters are refused."""
    if any(c in _CMD_UNSAFE for c in p):
        raise ValueError(f"unsafe character in hook path: {p}")
    if " " in p and os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024) and " " not in buf.value:
            p = buf.value
        else:
            raise ValueError(f"hook path has spaces and no short name: {p}")
    return p.replace("\\", "/")


def _hook_cmd(script: Path) -> str:
    return f"{_cmd_path(_python_exe())} {_cmd_path(str(script))}"


def hooks_config() -> dict:
    return {
        "helios-permission-gate": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": _hook_cmd(GATE_SCRIPT), "timeout": _GATE_TIMEOUT_S}]}]},
        # PreInvocation handlers sit directly under the event (no matcher / no nested "hooks").
        "helios-gate-marker": {"PreInvocation": [
            {"type": "command", "command": _hook_cmd(MARKER_SCRIPT), "timeout": 30}]},
    }


def validate_hooks(h: dict) -> None:
    """Refuse to write a hooks.json agy would silently discard (and with it, the gate)."""
    gate = h["helios-permission-gate"]["PreToolUse"]
    if not gate or gate[0].get("matcher") != "*":
        raise ValueError("permission gate must match every tool")
    for entry in gate:
        for hk in entry.get("hooks") or []:
            if hk.get("type") != "command" or not str(hk.get("command", "")).strip():
                raise ValueError("gate hook must be a command")
    for hk in h["helios-gate-marker"]["PreInvocation"]:
        if hk.get("type") != "command" or not str(hk.get("command", "")).strip():
            raise ValueError("marker hook must be a command")
    json.loads(json.dumps(h))


def _convert_server(s: dict) -> dict | None:
    if not isinstance(s, dict):
        return None
    if s.get("command"):
        cmd = str(s["command"])
        if os.path.isabs(cmd) and not Path(cmd).exists():
            return None   # e.g. the cua-driver computer-use engine isn't installed yet
        out = {"command": cmd, "args": [str(a) for a in (s.get("args") or [])]}
        if s.get("env"):
            out["env"] = {k: str(v) for k, v in s["env"].items()}
        if s.get("cwd"):
            out["cwd"] = str(s["cwd"])
        return out
    if s.get("url"):
        out = {"serverUrl": str(s["url"])}
        if s.get("headers"):
            out["headers"] = {k: str(v) for k, v in s["headers"].items()}
        return out
    return None


def mcp_servers() -> dict:
    servers: dict = {}
    for name in ("mcp.json", "composio_mcp.json"):
        p = conf.CONFIG_DIR / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            conf.log("antigravity", f"unreadable {name}: {e}")
            continue
        for sname, entry in (data.get("mcpServers") or {}).items():
            conv = _convert_server(entry)
            if conv:
                servers[sname] = conv
    return servers


def prepare_workspace(ws: Path, rules: str, *, tools: bool = True) -> dict:
    """Write ws/.agents (gate + marker, persona rules, MCP servers). Returns the workspace handle
    used for integrity checks: {"ws", "hooks_path", "hooks_digest", "servers"}."""
    agents = ws / ".agents"
    (agents / "rules").mkdir(parents=True, exist_ok=True)
    h = hooks_config()
    validate_hooks(h)
    body = json.dumps(h, indent=2)
    # Exact bytes (no CRLF translation) so hooks_intact() can compare digests.
    (agents / "hooks.json").write_bytes(body.encode("utf-8"))
    (agents / "rules" / "helios.md").write_text(neutralize(rules), encoding="utf-8")
    servers = mcp_servers() if tools else {}
    mcp_path = agents / "mcp_config.json"
    if servers:
        mcp_path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    else:
        mcp_path.unlink(missing_ok=True)
    return {"ws": ws, "hooks_path": agents / "hooks.json",
            "hooks_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "servers": sorted(servers)}


def hooks_intact(handle: dict) -> bool:
    try:
        data = Path(handle["hooks_path"]).read_bytes()
    except Exception:
        return False
    return hashlib.sha256(data).hexdigest() == handle["hooks_digest"]


def base_args(model: str = "", *, stream_input: bool, resume: str | None = None,
              prompt: str | None = None) -> list[str]:
    a = [*command(), "--output-format", "stream-json", "--disable-slash-commands",
         # Required for headless tools; the PreToolUse gate is the approval authority instead.
         "--dangerously-skip-permissions"]
    if stream_input:
        a += ["--input-format", "stream-json"]
    if model:
        a += ["--model", model]
    effort = str(cfg().get("effort") or "").strip()
    if effort:
        a += ["--effort", effort]
    if resume:
        a += ["--conversation", resume]
    if prompt is not None:
        a += ["-p", prompt]
    return a


def child_env(marker: Path, *, tools: bool = True, extra: dict | None = None,
              sink_file: Path | None = None) -> dict:
    e = dict(os.environ)
    e[MARKER_ENV] = str(marker)
    if tools:
        e.pop(DENY_ALL_ENV, None)
    else:
        e[DENY_ALL_ENV] = "1"
    if sink_file is not None:
        e[SINK_ENV] = str(sink_file)
    else:
        e.pop(SINK_ENV, None)
    for k, v in (extra or {}).items():
        if v not in (None, ""):
            e[k] = str(v)
    return e


def looks_unauthenticated(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in ("authentication required", "not signed in", "sign in", "login"))


class AgySession:
    """One agy process. stream_input=True keeps a persistent multi-turn session on stdin;
    otherwise it's a single `-p` run. Owns its marker file and stderr buffer."""

    def __init__(self, handle: dict, model: str = "", *, resume: str | None = None,
                 prompt: str | None = None, tools: bool = True, extra_env: dict | None = None,
                 sink_file: Path | None = None):
        MARKERS_DIR.mkdir(parents=True, exist_ok=True)
        self.handle = handle
        self.marker = MARKERS_DIR / f"{uuid.uuid4().hex}.ok"
        self.stream_input = prompt is None
        self.args = base_args(model, stream_input=self.stream_input, resume=resume, prompt=prompt)
        self.env = child_env(self.marker, tools=tools, extra=extra_env, sink_file=sink_file)
        self.proc: subprocess.Popen | None = None
        self.stderr: list[str] = []
        self.conversation_id: str | None = resume
        self._drain: threading.Thread | None = None

    def start(self) -> subprocess.Popen:
        self.proc = subprocess.Popen(
            self.args, stdin=subprocess.PIPE if self.stream_input else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW,
            cwd=str(self.handle["ws"]), env=self.env)
        self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drain.start()
        return self.proc

    def _drain_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                self.stderr.append(line)
                if len(self.stderr) > 400:
                    del self.stderr[:200]
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def reset_marker(self) -> None:
        self.marker.unlink(missing_ok=True)

    def marker_ok(self) -> bool:
        return self.marker.exists()

    def send(self, text: str) -> None:
        self.proc.stdin.write(json.dumps({"event": "user", "message": {"content": text}}) + "\n")
        self.proc.stdin.flush()

    def events(self):
        """Parsed NDJSON events from stdout until EOF."""
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj

    def stderr_text(self) -> str:
        if self._drain is not None:
            self._drain.join(timeout=2)
        return "".join(self.stderr)

    def close(self) -> None:
        from .proc_util import kill_tree
        if self.proc is not None:
            try:
                if self.stream_input and self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except Exception:
                pass
            if self.proc.poll() is None:
                kill_tree(self.proc)
        self.marker.unlink(missing_ok=True)


def display_tool_name(step: dict) -> str:
    """Tool name for the dashboard timeline / procedural memory; MCP calls become the
    Claude-style mcp__<server>__<tool> those features key on."""
    info = step.get("tool_info") or {}
    name = str(step.get("tool_name") or info.get("name") or "tool")
    if name == "call_mcp_tool":
        p = info.get("parameters") or {}
        server = p.get("ServerName") or p.get("serverName") or p.get("server")
        tool = p.get("ToolName") or p.get("toolName") or p.get("tool")
        if server and tool:
            return f"mcp__{server}__{tool}"
    return name


def consume_turn(session: AgySession, *, on_token=None, on_tool=None) -> dict:
    """Read one turn's events (through its `result`). Enforces the gate proof (marker must exist
    at the first model step) and hooks.json integrity after every tool step. Returns
    {text, conversation_id, status, usage, tools, errors, gate_failure}."""
    out = {"text": "", "conversation_id": session.conversation_id, "status": None, "usage": {},
           "tools": [], "errors": [], "gate_failure": None}
    parts: list[str] = []
    gate_checked = False
    seen_tools: set = set()
    for ev in session.events():
        kind = ev.get("event")
        if kind == "init":
            out["conversation_id"] = ev.get("conversation_id") or out["conversation_id"]
        elif kind == "step_update":
            s = ev.get("step_update") or {}
            stype = s.get("step_type")
            if stype != "user_input" and not gate_checked:
                gate_checked = True
                if not session.marker_ok():
                    out["gate_failure"] = "marker"
                    session.close()
                    break
            if stype == "agent_response" and s.get("text_delta"):
                parts.append(s["text_delta"])
                if on_token:
                    on_token(s["text_delta"])
            elif stype == "tool":
                key = (s.get("step_index"), s.get("tool_name"))
                if key not in seen_tools:
                    seen_tools.add(key)
                    name = display_tool_name(s)
                    out["tools"].append(name)
                    if on_tool:
                        on_tool(name)
                if s.get("state") == "DONE" and not hooks_intact(session.handle):
                    out["gate_failure"] = "tampered"
                    session.close()
                    break
        elif kind == "result":
            r = ev.get("result") or {}
            out["conversation_id"] = r.get("conversation_id") or out["conversation_id"]
            out["status"] = r.get("status")
            u = r.get("usage") or {}
            out["usage"] = {"in": u.get("input_tokens", 0) or 0,
                            "out": u.get("output_tokens", 0) or 0,
                            "cost": None, "ms": int((r.get("duration_seconds") or 0) * 1000)}
            if r.get("error"):
                out["errors"].append(str(r["error"]))
            out["text"] = (r.get("response") or "").strip() or "".join(parts).strip()
            break
    else:
        out["text"] = "".join(parts).strip()
    session.conversation_id = out["conversation_id"]
    return out


def run_once(prompt: str, rules: str, *, model: str = "", tools: bool = True,
             extra_env: dict | None = None, timeout: int = 600, register=None,
             label: str = "agy", resume: str | None = None) -> dict:
    """One headless `agy -p` run in a throwaway workspace (side agents, missions, helper calls)."""
    from .proc_util import kill_tree
    res = {"text": "", "conversation_id": None, "errors": [], "gate_failure": None, "tools": []}
    if not command():
        res["errors"].append("agy not found")
        return res
    ws = RUNS_DIR / uuid.uuid4().hex
    ws.mkdir(parents=True, exist_ok=True)
    session = None
    try:
        handle = prepare_workspace(ws, rules, tools=tools)
        session = AgySession(handle, model, resume=resume, prompt=neutralize(prompt), tools=tools,
                             extra_env=extra_env)
        proc = session.start()
        if register:
            try:
                register(proc)
            except Exception:
                pass
        timer = threading.Timer(timeout, lambda: kill_tree(proc))
        timer.start()
        try:
            res.update(consume_turn(session))
        finally:
            timer.cancel()
            try:
                proc.wait(timeout=15)
            except Exception:
                kill_tree(proc)
        if res.get("gate_failure"):
            conf.log("antigravity", f"[{label}] gate failure ({res['gate_failure']}) — run killed")
        elif not res["text"]:
            conf.log("antigravity", f"[{label}] no reply rc={proc.returncode} "
                                    f"err={session.stderr_text()[-300:]}")
    except Exception as e:
        res["errors"].append(str(e))
        conf.log("antigravity", f"[{label}] run failed: {e}")
    finally:
        if session is not None:
            session.close()
        shutil.rmtree(ws, ignore_errors=True)
    return res


def complete_json(prompt: str, *, model: str, schema: dict | None = None,
                  system: str | None = None, timeout: int = 120) -> dict | None:
    """Tool-less one-shot (router triage, memory extraction). Same shape as
    claude_cli.complete_json: {text, data, session_id, cost, error}; None on failure."""
    from . import claude_cli
    sys_text = (system or "").strip() or "You are a precise assistant."
    if schema:
        sys_text += ("\n\nReturn ONLY a single minified JSON object — no prose, no code fences. "
                     "It must satisfy this JSON schema: " + json.dumps(schema))
    res = run_once(prompt, sys_text, model=model_for(model=model), tools=False, timeout=timeout,
                   label="complete_json")
    if res.get("gate_failure") or not res.get("text"):
        return None
    text = res["text"]
    data = None
    if schema:
        try:
            data = json.loads(claude_cli._strip_fences(text))
        except Exception:
            data = claude_cli._extract_json(text)
    return {"text": text, "data": data, "session_id": res.get("conversation_id"), "cost": None,
            "error": None}


def run_agent(prompt: str, system: str, *, model: str, extra_env: dict | None = None,
              timeout: int = 600, register=None, label: str = "agent") -> str | None:
    """Background agent turn (side agents, mission workers) with tools, gated by the hook."""
    res = run_once(prompt, system + "\n\n" + engine_note(sorted(mcp_servers())), model=model,
                   tools=True, extra_env=extra_env, timeout=timeout, register=register, label=label)
    return res.get("text") or None


def engine_note(servers: list[str]) -> str:
    """Maps the Claude-flavoured persona onto agy's tool names; states what's available."""
    home = str(Path.home())
    lines = [
        "## ENGINE: ANTIGRAVITY",
        "You are Helios. You run on the Antigravity agent engine, but never call yourself "
        "Antigravity, Gemini, Claude or Jarvis — you are Helios.",
        "Instructions above name tools the Claude Code way. Your equivalents: Bash/PowerShell -> "
        "run_command (Windows PowerShell); Read -> view_file; Write -> write_to_file; Edit -> "
        "replace_file_content; Glob -> find_by_name; Grep -> grep_search; WebFetch -> "
        "read_url_content; WebSearch -> search_web.",
        "Helios's own tools (`mcp__<server>__<tool>`) are reached with call_mcp_tool "
        "(ServerName=<server>, ToolName=<tool>).",
        "Every tool call passes Helios's permission gate. If one is blocked or denied, do not "
        "retry it — adapt and tell the user plainly.",
        "Helios owns reminders, routines and messaging: use Helios's tools for those, never your "
        "own schedule / manage_task / send_message tools.",
        "When you start a long-running command, wait for it to finish (command_status) before "
        "you end your turn — the turn ending can stop it.",
        f"Your working folder is a Helios scratch folder. The user's home folder is {home}; use "
        "absolute paths for the user's files.",
    ]
    if "computer" not in servers:
        lines.append("Desktop control (the mcp__computer__* / cua-driver tools) is NOT available "
                     "right now — ignore instructions about driving the GUI by element index.")
    if "helios" not in servers:
        lines.append("Helios's own tools (reminders, open_app, system health, ...) are not "
                     "connected right now.")
    return "\n".join(lines)
