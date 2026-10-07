"""Permission policy + the live Approve/Deny registry.

Autonomy model: tools in the settings allowlist run with full autonomy; everything
else is "risky" and pops an Approve/Deny prompt in the Helios UI (driven by the
PreToolUse hook, which blocks the brain until the user decides).

Flow: this Claude CLI build has no --permission-prompt-tool, so a PreToolUse HOOK is
used instead. For a "risky" tool the hook calls the app's token-authed /permission/ask
endpoint (see server.py), which uses PendingRegistry below to create a pending request,
push it to the UI (SSE), and block until the user Approves/Denies (or it times out -> deny).
classify() is the policy; PendingRegistry is the wait/resolve plumbing.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlparse

from . import conf

# --- policy patterns (reflect the user's permission choices) -----------------------------
# Files/locations that always require approval, even to read. Includes secrets/system
# locations PLUS personal/financial document keywords (read side of read-then-exfiltrate).
_SENSITIVE = re.compile(
    r"(\.env\b|\.ssh|id_rsa|id_ed25519|id_ecdsa|id_dsa|\.pem\b|\.ppk\b|\.key\b|\.kdbx\b|"
    r"\.ovpn\b|\.netrc|\.npmrc|\.pypirc|\.git-credentials|authorized_keys|"
    r"\.aws[\\/]|\.azure[\\/]|gcloud|\.kube[\\/]|\.docker[\\/]|\.gnupg|gnupg|"
    r"credential|secret|password|passwd|wallet|seed phrase|private[ _-]?key|api[_-]?key|"
    r"\\windows\\|/windows/|\\program files|/program files|\\system32|\\syswow64|"
    r"appdata\\local\\(google\\chrome|microsoft\\edge|mozilla)|cookies|login data|"
    r"local state|\\user data\\|\bbank\b|\btax(es|return)?\b|"
    r"payslip|paystub|invoice|mortgage|passport|\bssn\b|social security|\b1099\b|"
    r"\bw-?2\b|medical|brokerage|salary)",
    re.I,
)
# Write targets that are CODE-EXECUTION or persistence vectors — always ask, even though the
# file itself isn't "sensitive" to read. Covers the custom_tools dir (exec'd at startup), shell
# init/profile files, the Startup folder, and any executable script. Helios's own install dir is
# checked separately (slash-normalized) so the model can't silently rewrite its own code/config.
_HELIOS_ROOT_NORM = str(conf.ROOT).replace("\\", "/").rstrip("/").lower()
_PROTECTED_WRITE = re.compile(
    r"(custom_tools[\\/]"
    r"|\.(bash|zsh)rc\b|\.bash_profile\b|\.zprofile\b|\.profile\b"
    r"|profile\.ps1|microsoft\.powershell.*profile"
    r"|[\\/]start menu[\\/]programs[\\/]startup[\\/]"
    r"|\.(bat|cmd|ps1|vbs|scr)$)",
    re.I,
)
# mcp__helios__* tools that escalate autonomy or are destructive -> ask (the rest of Helios's
# own first-party tools — reminders/routines/RAG-read/tone/mission-internal — stay autonomous).
_HELIOS_ASK = {
    "create_tool",          # writes + exec's new code
    "create_skill",         # writes a persistent behavior pack (self-modification)
    "delete_custom_tool", "delete_routine", "delete_workflow", "rag_clear",   # destructive
    "set_screen_awareness",                                 # privacy/capability toggle
    "run_in_background", "start_mission", "spawn_agent",    # spawn autonomous agents/teams
    "approve_lesson",       # a learned rule/skill takes effect only with the user's say-so
    "delete_job",           # destructive (removes a schedule + its history)
    "gmail_send_draft",     # sends an email to a real person — only with the user's yes
    "gmail_add_business_contact",   # widens who Helios drafts replies for
    "browser_download",     # saves a file from the web (hidden browser sandbox)
}
# Helios's own tools that contact a real person (blocked outright for background agents).
_HELIOS_OUTBOUND = {"gmail_send_draft"}
# Hidden-browser actions that, with confirm=true, submit / send / buy / sign up on a website —
# the browser refuses them without confirm, and confirm=true routes to the user's prompt.
_HELIOS_CONFIRMED = {"browser_click", "browser_type"}


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")
# Verb tokens used to classify connected-app (Composio) actions. Matched against the tool id
# split into tokens (underscore + camelCase) so APP_SEND_X and APPSENDX both gate correctly.
_WRITE_TOKENS = {
    "send", "create", "update", "delete", "post", "reply", "forward", "draft", "share",
    "invite", "comment", "merge", "close", "label", "trash", "archive", "modify", "move",
    "remove", "add", "set", "write", "upload", "empty", "purge", "revoke", "grant", "disable",
    "enable", "cancel", "publish", "rename", "rotate", "terminate", "execute", "run", "trigger",
    "pay", "transfer", "book", "approve", "deny", "unsubscribe", "subscribe", "star", "unstar",
    "edit", "insert", "replace", "clear", "import", "export",
}
_READ_TOKENS = {
    "get", "list", "fetch", "search", "read", "find", "retrieve", "lookup", "check", "view",
    "count", "describe", "download", "info", "status", "history",
}


def _split_tokens(s: str) -> set[str]:
    """Lowercased word tokens of an identifier, split on underscores AND camelCase humps."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return {t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t}


# Verbs that actually contact a real person outward (email/message/post/PR-comment). A subset of
# _WRITE_TOKENS — creating a file or updating a record isn't "contacting someone", sending is.
_OUTBOUND_TOKENS = {"send", "reply", "forward", "post", "publish", "comment", "share",
                    "invite", "tweet", "dm", "email", "message"}


def is_outbound_send(tool_name: str) -> bool:
    """True if the tool sends something outward to real people. Used to STRUCTURALLY block
    background/side agents from contacting anyone (the persona's HARD RULE), independent of the
    normal ask/allow policy — a background agent the user isn't watching must never message/email/post.
    Covers the connected-app (Composio) surface and Helios's own send tools (gmail_send_draft)."""
    name = tool_name or ""
    if name.startswith("mcp__helios__"):
        return name.split("mcp__helios__", 1)[1] in _HELIOS_OUTBOUND
    if name.startswith("mcp__composio__"):
        return bool(_split_tokens(name.split("mcp__composio__", 1)[1]) & _OUTBOUND_TOKENS)
    return False


def is_outbound_action(tool_name: str, tool_input: dict | None = None) -> bool:
    """is_outbound_send, plus Helios tools whose INPUT makes them act on someone's behalf: a
    confirmed hidden-browser click/Enter on a submit / send / buy / sign-up control."""
    if is_outbound_send(tool_name):
        return True
    name = tool_name or ""
    if name.startswith("mcp__helios__") and name.split("mcp__helios__", 1)[1] in _HELIOS_CONFIRMED:
        return _truthy((tool_input or {}).get("confirm"))
    return False


def never_yolo(tool_name: str, tool_input: dict | None = None) -> bool:
    """Helios's own outbound actions (email send, confirmed browser submits) always go to the
    Approve/Deny prompt, even in YOLO mode."""
    name = tool_name or ""
    if not name.startswith("mcp__helios__"):
        return False
    return name.split("mcp__helios__", 1)[1] in _HELIOS_OUTBOUND or is_outbound_action(name, tool_input)


def is_claude_dir(path: str) -> bool:
    """True if a path is inside Claude Code's own config/memory dir (~/.claude). Helios must never
    write there — its long-term memory is the Obsidian vault, maintained by memory.extract_and_write.
    The brain (being Claude Code) otherwise 'helpfully' saves memory files into this shared store."""
    p = (path or "").replace("\\", "/").lower()
    return "/.claude/" in p or p.endswith("/.claude")


def is_projects_config(path_or_command: str) -> bool:
    """True if a path (or a shell command) touches the project-manifest folder. Manifests decide
    which commands Helios runs as health checks, so only the user edits them (hard-deny)."""
    s = (path_or_command or "").replace("\\", "/").lower()
    if not s:
        return False
    try:
        d = str(conf.projects_dir()).replace("\\", "/").rstrip("/").lower()
        dirs = {d}
        try:
            dirs.add(str(conf.projects_dir().resolve()).replace("\\", "/").rstrip("/").lower())
        except Exception:
            pass
    except Exception:
        return False
    return any(x and (x in s) for x in dirs)


def is_internal_url(url: str) -> bool:
    """True if a URL points at loopback / private / link-local / unspecified address space
    (SSRF target). Used by the PreToolUse hook to hard-deny internal WebFetch."""
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return True  # unparseable -> treat as unsafe
    if not host:
        return True
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified)
    except ValueError:
        return False  # a real hostname (DNS) -> allowed to be asked about, not auto-internal
# Shell metacharacters that chain/redirect — the read-only allowlist only validates the
# FIRST token, so any of these means we can't trust it: gate to "ask".
_SHELL_CHAIN = re.compile(r"[;&|\n\r`<>]|\$\(")
# Shell commands that just read/report -> autonomous.
_SHELL_READ = re.compile(
    r"^\s*\(?\s*(get-(?!.*\|.*set)\w+|select-?\w*|where-object|measure-\w+|format-\w+|"
    r"out-(string|host)|sort-object|group-object|compare-object|resolve-path|test-path|"
    r"gci|gc\b|ls\b|dir\b|cat\b|type\b|echo\b|write-output|tasklist|systeminfo|whoami|"
    r"hostname|ipconfig|ping|nslookup|tracert|gwmi|get-wmiobject|wmic|find\b|findstr|"
    r"git (status|log|diff|show|branch|remote)|python --version|node --version)\b",
    re.I,
)
# Low-risk system-setting changes the user allowed (audio/wifi/display).
_SHELL_SAFE_SET = re.compile(
    r"(set-audiodevice|audiodevicecmdlets|nircmd.*(setsysvolume|mutesysvolume|setdefaultsounddevice|setbrightness)|"
    r"wmisetbrightness|set-brightness|monitorbrightness|"
    r"netsh\s+wlan\s+(connect|disconnect)|set-displayresolution|btdevice|bluetooth)",
    re.I,
)


def _path(inp: dict) -> str:
    """Best-effort extract the target path from a tool's input (keys vary by tool)."""
    return str(inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or "")


_WORK_SANDBOX = Path.home() / "Downloads" / "Helios Work"


def _in_work_sandbox(path: str) -> bool:
    """True when a path resolves inside the Helios Work design sandbox (model_3d's write
    root — writes there are autonomous, anywhere else asks)."""
    try:
        return Path(path).expanduser().resolve().is_relative_to(_WORK_SANDBOX.resolve())
    except Exception:
        return False


def classify(tool_name: str, tool_input: dict | None) -> str:
    """Decide 'allow' (autonomous) vs 'ask' (needs the user's confirmation), per his policy."""
    inp = tool_input or {}
    # explicit overrides from settings.toml — EXACT membership for both lists (consistent &
    # predictable; a substring 'always_ask' entry would surprisingly over-match e.g. Bash->BashOutput).
    if tool_name in conf.autonomy_always_ask():
        return "ask"
    if tool_name in conf.autonomy_allow():
        return "allow"

    # Blank-slate onboarding posture: nothing runs autonomously except what the user explicitly
    # allow-listed above — everything else is confirmed. (Normal installs leave mode unset.)
    if conf.autonomy_mode() == "blank":
        return "ask"

    # PC control (mouse/keyboard/screen/apps): the real gate lives in the PreToolUse hook
    # (_computer_use_gate — panic stop + single-driver lock). classify just permits it; if
    # classify is ever consulted outside the hook, the hook's safety layer still applies.
    if tool_name.startswith("mcp__computer__"):
        return "allow"
    # Helios's own first-party tools: autonomous EXCEPT the destructive / autonomy-escalating
    # ones (and create_tool, which writes+exec's code) — those need the user's nod.
    if tool_name.startswith("mcp__helios__"):
        base = tool_name.split("mcp__helios__", 1)[1]
        if base == "model_3d":
            # Sandbox rule (Helios-main parity): inspect/measure/render/show only ever
            # write inside Downloads\Helios Work (or beside the model, read-only-ish) —
            # autonomous. convert writes a NEW file at destination-or-beside-the-source,
            # so it asks unless that target is inside the sandbox. Bare model-folder
            # names ("coaster") resolve inside the sandbox by construction.
            if str(inp.get("action", "")).strip().lower() == "convert":
                target = str(inp.get("destination") or inp.get("path") or "").strip()
                if not (_in_work_sandbox(target)
                        or _in_work_sandbox(str(_WORK_SANDBOX / target))):
                    return "ask"
            return "allow"
        if base in _HELIOS_CONFIRMED and _truthy(inp.get("confirm")):
            return "ask"
        return "ask" if base in _HELIOS_ASK else "allow"
    # benign discovery / planning — autonomous. NOTE: WebFetch is deliberately NOT here; it's
    # gated below (exfiltration/SSRF vector). WebSearch returns summaries, not raw page fetch.
    if tool_name in {"WebSearch", "ToolSearch", "TodoWrite", "Skill", "Glob", "LS"}:
        return "allow"
    # WebFetch — always ask (can carry exfiltrated data out in the URL/query). The hook
    # additionally hard-denies private/loopback targets via is_internal_url (SSRF).
    if tool_name == "WebFetch":
        return "ask"
    # reading files/content — allow unless a sensitive/personal location
    if tool_name in {"Read", "Grep"}:
        return "ask" if _SENSITIVE.search(_path(inp) or str(inp)) else "allow"
    # creating/editing files — allow unless sensitive OR a code-exec/persistence target OR
    # inside Helios's own install (self-modification). Path normalized so / and \ both match.
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        p = _path(inp)
        pn = p.replace("\\", "/").lower()
        if (_SENSITIVE.search(p) or _PROTECTED_WRITE.search(p)
                or (_HELIOS_ROOT_NORM and _HELIOS_ROOT_NORM in pn)):
            return "ask"
        return "allow"
    # shell — read-only & safe-setting commands autonomous; anything that changes -> ask
    if tool_name in {"Bash", "PowerShell", "BashOutput"}:
        cmd = str(inp.get("command", ""))
        # Touches a sensitive target (e.g. `cat .env`, `gc id_rsa`) -> always ask. The
        # sensitive check otherwise only covers Read/Write tools, not shell.
        if _SENSITIVE.search(cmd):
            return "ask"
        # Chained/redirected commands can't be trusted by a first-token allowlist
        # (`echo hi & del x`, `gc f | out-file g`) -> ask.
        if _SHELL_CHAIN.search(cmd):
            return "ask"
        if _SHELL_SAFE_SET.search(cmd):
            return "allow"
        if _SHELL_READ.search(cmd):
            return "allow"
        return "ask"
    # connected apps (Composio) — allow ONLY clearly read-only actions; sends/writes and any
    # unrecognized action default to ask. Token-split so APP_SEND_X and APPSENDX both gate.
    if tool_name.startswith("mcp__composio__"):
        toks = _split_tokens(tool_name.split("mcp__composio__", 1)[1])
        if toks & _WRITE_TOKENS:
            return "ask"
        if toks & _READ_TOKENS:
            return "allow"
        return "ask"  # unknown action -> treat as write (fail safe)

    # everything else (installs, power, unknown tools, other MCP) -> ask
    return "ask"


def summarize(tool_name: str, tool_input: dict | None) -> str:
    """A short human-readable description of what the brain wants to do."""
    ti = tool_input or {}
    if tool_name in ("Bash", "BashOutput"):
        return f"Run command: {str(ti.get('command', '')).strip()[:300]}"
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"{tool_name} file: {ti.get('file_path') or ti.get('notebook_path') or '?'}"
    if tool_name == "WebFetch":
        return f"Fetch URL: {ti.get('url', '?')}"
    if tool_name.startswith("mcp__"):
        inner = ", ".join(f"{k}={v}" for k, v in list(ti.items())[:4])
        return f"{tool_name}({inner})"
    if ti:
        inner = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(ti.items())[:4])
        return f"{tool_name}: {inner}"
    return tool_name


class PendingRegistry:
    """Holds in-flight permission asks, keyed by id, each with a wait Event."""

    def __init__(self, notifier=None):
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.notifier = notifier  # callable(event_name, payload) -> push to UI (SSE)
        # Optional callable(rid, chat_id, summary) that surfaces an ask on Telegram. Set by app.py
        # when the Telegram bridge is up; used when a turn started FROM Telegram (sink="telegram:<id>")
        # so the user can Approve/Deny from his phone. The dashboard SSE notify still fires too.
        self.telegram_asker = None

    def create(self, tool_name: str, tool_input: dict | None, sink: str = "") -> str:
        """Register a new pending ask and notify the UI; returns its id (used by wait/resolve).
        `sink` (e.g. 'telegram:<chat_id>') routes the ask to the surface that started the turn."""
        rid = secrets.token_urlsafe(12)  # unguessable — a CSRF page can't pre-approve it
        item = {"id": rid, "tool": tool_name,
                "summary": summarize(tool_name, tool_input),
                "event": threading.Event(), "decision": None, "sink": sink}
        with self._lock:
            self._items[rid] = item
        # Log only the tool name + id, NOT the summary — the summary embeds up to 300 chars of
        # the raw command/path for exactly the sensitive actions routed to 'ask' (would land in
        # a plaintext log). The full summary still goes to the UI prompt below for the user to see.
        conf.log("permissions", f"ASK [{rid}] {tool_name}")
        if self.notifier:
            try:
                self.notifier("permission", {"id": rid, "tool": tool_name,
                                             "summary": item["summary"]})
            except Exception:
                pass
        # Also surface on Telegram when the turn came from there, so an Approve/Deny prompt reaches
        # the user's phone instead of only the desktop (where it would otherwise time out -> deny).
        if sink.startswith("telegram:") and self.telegram_asker:
            try:
                self.telegram_asker(rid, sink.split(":", 1)[1], item["summary"])
            except Exception as e:
                conf.log("permissions", f"telegram ask failed: {e}")
        return rid

    def wait(self, rid: str, timeout: float) -> str:
        """Block until this ask is resolved, returning 'allow'/'deny'. Times out -> 'deny'
        (fail-safe) and an unknown id -> 'deny'. The pending entry is popped once decided."""
        with self._lock:
            item = self._items.get(rid)
        if not item:
            return "deny"
        if item["event"].wait(timeout):
            decision = item["decision"] or "deny"
        else:
            decision = "deny"  # fail-safe on timeout
        with self._lock:
            self._items.pop(rid, None)
        conf.log("permissions", f"RESOLVED [{rid}] -> {decision}")
        return decision

    def resolve(self, rid: str, decision: str) -> bool:
        """Record the user's decision and wake the waiting hook. Returns False for an unknown id.
        Anything other than the exact string 'allow' is coerced to 'deny' (fail-closed)."""
        decision = "allow" if decision == "allow" else "deny"
        with self._lock:
            item = self._items.get(rid)
        if not item:
            return False
        item["decision"] = decision
        item["event"].set()
        if self.notifier:
            # Let the app close an auto-opened dashboard (and the UI clear its prompt).
            try:
                self.notifier("permission_resolved", {"id": rid, "decision": decision})
            except Exception:
                pass
        return True

    def deny_all(self) -> None:
        """Used on panic: resolve every pending ask as deny."""
        with self._lock:
            items = list(self._items.values())
        for item in items:
            item["decision"] = "deny"
            item["event"].set()
