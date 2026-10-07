"""PreToolUse hook for Helios's Claude brain.

Claude runs this before every tool call. Allowlisted tools are approved instantly
(full autonomy); anything risky is sent to the running Helios app, which shows the user an
Approve/Deny prompt and blocks until he decides. If the app can't be reached, we
fail safe and deny.

Output: a PreToolUse permissionDecision JSON on stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# Make the repo importable no matter where it's cloned (this file is <repo>/hooks/pretooluse.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_LOCK_TTL = 20.0  # seconds a computer-use lock stays valid without refresh (stale-takeover)

# --- Content safety for computer-use (borrowed from Hermes tools/computer_use/tool.py) ---------
# Helios auto-allows every mcp__computer__* action (permissions.classify), so nothing inspects the
# CONTENT of a keystroke/typed string. That lets the brain (or a runaway turn) lock the box, close
# the user's foreground work, or type a shell command straight into a focused terminal — bypassing the
# Bash/PowerShell permission gate entirely. We add a deterministic content check: hard-deny the
# unmistakably destructive things (survives YOLO, like the other hard rails) and route the merely
# risky ones (alt+f4, pipe-to-shell) to the user's Approve/Deny prompt.
_COMBO_MODS = {"ctrl", "alt", "shift", "win"}
_COMBO_ALIAS = {"control": "ctrl", "ctl": "ctrl", "windows": "win", "super": "win", "meta": "win",
                "cmd": "win", "command": "win", "del": "delete", "return": "enter", "esc": "escape"}
_COMBO_MOD_RANK = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}
_HARD_DENY_COMBOS = {"win+l", "ctrl+alt+delete"}  # lock workstation / secure-attention — no automation value
_ASK_COMBOS = {"alt+f4"}                           # closes the target window — legit sometimes, so ask
# cua-driver tools that go beyond "look, click, type": they kill processes, read/plant clipboard
# contents (passwords get copied), move local files into web pages (exfiltration), record the
# screen, or change the driver itself. Each one asks first (YOLO can still pre-approve).
_ASK_ACTIONS = {"kill_app", "clipboard_read", "clipboard_write", "browser_set_input_files",
                "browser_download", "start_recording", "replay_trajectory", "install_extension",
                "install_ffmpeg", "set_config"}


def _canon_combo(tokens) -> str:
    """Normalize a key list (['Win','L'] or modifiers+key) to a canonical 'mod+...+key' string,
    modifiers first in a fixed order, so ['l','win'] and ['win','l'] both canonicalize to 'win+l'."""
    toks = []
    for t in tokens:
        s = str(t).strip().lower()
        if s:
            toks.append(_COMBO_ALIAS.get(s, s))
    mods = sorted((t for t in toks if t in _COMBO_MODS), key=_COMBO_MOD_RANK.get)
    keys = [t for t in toks if t not in _COMBO_MODS]
    return "+".join(mods + keys)


# Unmistakably destructive typed commands -> hard deny (root wipe, fork bomb, mkfs, format drive).
_DENY_TEXT_PATTERNS = [
    re.compile(r"\brm\s+-\w*(?:rf|fr)\w*\s+(?:/|~)\s*(?:\*|$)", re.I),   # rm -rf /  |  ~  |  /*
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),             # bash fork bomb
    re.compile(r"\bmkfs\.\w+", re.I),                                    # mkfs.ext4 ...
    re.compile(r"\bformat\s+[a-z]:", re.I),                              # format c:
]
# Looks like arbitrary code execution typed into a terminal -> ask (the user can approve).
_ASK_TEXT_PATTERNS = [
    re.compile(r"\|\s*(?:bash|sh|zsh)\b", re.I),                        # ... | bash
    re.compile(r"\b(?:iex|Invoke-Expression)\b", re.I),                # iex / Invoke-Expression
    re.compile(r"\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b.{0,80}\|\s*(?:bash|sh|iex)", re.I),
]


def _screen_content_verdict(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Inspect a computer-use action's CONTENT (independent of panic/lock). Returns
    ('allow'|'ask'|'deny', reason). Covers the two ways to inject keys — hotkey(keys=[...]) and
    press_key(key=..., modifiers=[...]) — and the two ways to inject text — type_text(text=...) and
    set_value(value=...). Fails OPEN (allow) on any inspection error so a bug here can't wedge normal
    computer use; the panic + single-driver-lock layers still apply."""
    try:
        action = tool_name[len("mcp__computer__"):]
        if action in _ASK_ACTIONS:
            return "ask", f"{action.replace('_', ' ')} goes beyond normal screen control — confirm?"
        tokens = None
        if action == "hotkey":
            tokens = tool_input.get("keys")
        elif action == "press_key":
            k = tool_input.get("key")
            if k:
                tokens = list(tool_input.get("modifiers") or []) + [k]
        if tokens:
            combo = _canon_combo(tokens)
            if combo in _HARD_DENY_COMBOS:
                return "deny", f"blocked destructive key-combo ({combo}) — would lock or interrupt the machine"
            if combo in _ASK_COMBOS:
                return "ask", f"key-combo {combo} closes the window — confirm?"
        text = ""
        if action in ("type_text", "type_text_in"):
            text = str(tool_input.get("text") or "")
        elif action == "set_value":
            text = str(tool_input.get("value") or "")
        if text:
            if any(rx.search(text) for rx in _DENY_TEXT_PATTERNS):
                return "deny", "blocked destructive typed command (would wipe files or crash the machine)"
            if any(rx.search(text) for rx in _ASK_TEXT_PATTERNS):
                return "ask", "typed text looks like a shell command — confirm?"
    except Exception:
        return "allow", ""
    return "allow", ""

try:
    from helios import conf, permissions, protected
except Exception as e:  # pragma: no cover
    # Can't even load policy -> deny risky-by-default, but let reads through.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"Helios policy unavailable: {e}"}}))
    sys.exit(0)


def _computer_use_gate(session_id: str) -> tuple[str, str]:
    """Safety layer for mcp__computer__* (cua-driver mouse/keyboard/screen). The old pyautogui
    server enforced this; it died with that server, so we re-do it here in the live path:

      1. PANIC SOFT-STOP — if abort.flag exists (brain.panic wrote it), deny so an in-flight
         screen action can't keep going after the user hits panic. Cleared when he sends a new turn.
      2. SINGLE-DRIVER LOCK — a best-effort TTL lock keyed by the calling session so the
         foreground turn + side agents + mission workers can't drive input concurrently and
         fight over the cursor. Each computer action refreshes the lock; it frees after
         _LOCK_TTL of inactivity (or instantly when app.panic() unlinks it)."""
    try:
        if conf.ABORT_FLAG.exists():
            return "deny", "panic engaged — computer-use halted until you send a new message"
    except Exception:
        pass
    owner = session_id or str(os.getpid())
    now = time.time()
    try:
        cur = json.loads(conf.SCREEN_LOCK.read_text(encoding="utf-8"))
        if (cur.get("owner") and cur["owner"] != owner
                and (now - float(cur.get("ts", 0))) < _LOCK_TTL):
            return "deny", "another agent is driving the screen right now — wait and retry shortly"
    except Exception:
        pass  # missing/corrupt lock -> treat as free
    try:
        conf.SCREEN_LOCK.parent.mkdir(parents=True, exist_ok=True)
        conf.SCREEN_LOCK.write_text(json.dumps({"owner": owner, "ts": now}), encoding="utf-8")
    except Exception:
        pass
    return "allow", "autonomous (computer-use; screen lock held)"


def decide(tool_name: str, tool_input: dict, session_id: str = "") -> tuple[str, str]:
    ask_reason = ""  # non-empty => this call must go to the user's Approve/Deny prompt (skip classify)

    # Background/side agents (HELIOS_AGENT_ROLE=side, set by side_agent/mission_agent) run where the user
    # isn't watching. They must NEVER contact real people — the persona's HARD RULE. Enforce it here
    # structurally (a hard rail: bites even in YOLO), not just in the prompt, so a runaway background
    # agent physically cannot send an email/message/post. The live interactive brain sets no role, so
    # this never touches normal use.
    if os.environ.get("HELIOS_AGENT_ROLE") == "side" and permissions.is_outbound_send(tool_name):
        return "deny", "a background agent can't send outbound messages — only the live assistant can, with your ok"

    # Credential stores and secrets (SSH keys, password managers, browser credential stores, API
    # credential files, cloud credentials, Helios's own secrets): hard-deny any file/shell access —
    # even listing — for every caller, and even in YOLO mode. Only an explicit entry in
    # [security] allow_protected lets a location through to the normal (asking) policy.
    cat = protected.tool_check(tool_name, tool_input)
    if cat:
        conf.log("security", f"DENY {tool_name}: protected ({cat})"
                             + (" [background agent]" if os.environ.get("HELIOS_AGENT_ROLE") == "side" else ""))
        return "deny", protected.deny_reason(cat)

    # Computer-use is physical/dangerous — gate on content safety, then panic + the single-driver lock.
    if tool_name.startswith("mcp__computer__"):
        verdict, reason = _screen_content_verdict(tool_name, tool_input)
        if verdict == "deny":
            return "deny", reason                    # hard rail: bites even in YOLO
        try:
            if conf.ABORT_FLAG.exists():             # panic soft-stop applies to every screen action
                return "deny", "panic engaged — computer-use halted until you send a new message"
        except Exception:
            pass
        if verdict == "allow":
            return _computer_use_gate(session_id)    # clean -> single-driver lock + allow
        ask_reason = reason                          # verdict == "ask" -> route to the user below
    # WebFetch to a private/loopback/link-local target is SSRF — hard-deny (don't even ask).
    elif tool_name == "WebFetch" and permissions.is_internal_url(str(tool_input.get("url", ""))):
        return "deny", "SSRF blocked: WebFetch to a private/loopback address is not allowed"
    # Helios must never write into Claude Code's own config/memory dir (~/.claude) — its memory is
    # the Obsidian vault (maintained automatically). Hard-deny so it can't pollute that shared store.
    elif tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        _p = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if permissions.is_claude_dir(_p):
            return "deny", "Helios does not write to Claude Code's config/memory dir; memory is the Obsidian vault."
        if permissions.is_projects_config(_p):
            return "deny", "project manifests decide which commands Helios runs — only the user edits them."
    # The same folder via a shell (Set-Content, copy, etc.): hard-deny, bites even in YOLO.
    elif tool_name in ("Bash", "PowerShell") and permissions.is_projects_config(
            str(tool_input.get("command", ""))):
        return "deny", "project manifests decide which commands Helios runs — only the user edits them."
    # YOLO mode: auto-approve everything that would otherwise pop an Approve/Deny prompt, for the
    # current chat. Placed AFTER the hard-rails above (panic + SSRF + ~/.claude + destructive screen
    # actions) so those still bite even in YOLO — it only short-circuits the classify()->ask path.
    # Exception: Helios's own email send (gmail_send_draft) always asks — YOLO never sends mail.
    try:
        if conf.YOLO_FLAG.exists() and not permissions.never_yolo(tool_name):
            return "allow", "autonomous (YOLO mode — all permissions for this chat)"
    except Exception:
        pass
    if not ask_reason and permissions.classify(tool_name, tool_input) == "allow":
        return "allow", "autonomous (allowlisted)"
    # Risky (or a screen action the user must confirm) -> ask the running Helios app. Pass along the
    # permission SINK (e.g. the Telegram chat that started this turn), injected into our env by the
    # brain, so the app can route the Approve/Deny prompt to the right surface.
    try:
        sink = os.environ.get("HELIOS_PERM_SINK", "")
        body = json.dumps({"tool": tool_name, "input": tool_input, "sink": sink}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = conf.auth_token()
        if token:
            headers["X-Auth-Token"] = token
        req = urllib.request.Request(f"{conf.BASE_URL}/permission/ask", data=body,
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=125) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        decision = "allow" if data.get("decision") == "allow" else "deny"
        return decision, "approved by you" if decision == "allow" else "denied by you"
    except Exception as e:
        return "deny", f"Helios app unreachable; denied for safety ({e})"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    session_id = payload.get("session_id", "") or ""

    try:
        decision, reason = decide(tool_name, tool_input, session_id)
    except Exception as e:  # never emit nothing — fail closed on any policy error
        decision, reason = "deny", f"policy error; denied for safety ({e})"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))
    sys.exit(0)


if __name__ == "__main__":
    main()
