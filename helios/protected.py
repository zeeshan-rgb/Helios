"""Protected locations: credential stores and secrets Helios never touches on its own.

The permission policy (permissions.classify) already ASKS before reading sensitive files — but an
"ask" can be pre-approved (YOLO mode) or rubber-stamped, and listing a folder (Glob/LS) was always
allowed. For credential stores that is not enough (blueprint phase 14: no autonomous access to
credential stores or secrets without explicit configuration). So the PreToolUse gate checks every
file/shell tool call against this list FIRST, as a hard rail that also bites in YOLO mode and for
background agents: a match is denied, full stop.

The only way through is explicit configuration — a path (or path prefix) listed in
`[security] allow_protected` in settings.toml; the call then goes through the normal policy (which
still asks). Deliberately NOT covered: searching file *contents* for words like "password" (a Grep
pattern), and ordinary personal documents — those stay on the normal ask path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import conf

# (category, pattern) — matched against a lower-cased, forward-slashed path or command.
_P = r"(?:^|[/\s\"'=:(])"          # path-ish start
_E = r"(?:$|[/\s\"',;)])"          # path-ish end
RULES: list[tuple[str, re.Pattern]] = [(cat, re.compile(rx, re.I)) for cat, rx in [
    ("Helios/assistant secrets",
     r"config/secrets\.toml\b|\bcomposio_mcp\.json\b|\.session_token\b|"
     r"\.claude/\.credentials\.json\b|\boauth_creds\.json\b|\bgoogle_accounts\.json\b"),
    ("SSH keys",
     rf"{_P}\.ssh{_E}|\bid_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?\b|\bauthorized_keys\b"),
    ("password manager data",
     r"\.kdbx?\b|\bkeepass(?:xc)?\b|\b1password\b|\bbitwarden\b|\blastpass\b|\bdashlane\b|"
     r"\bkeeper(?:security)?\b|\benpass\b|\.opvault\b|\.agilekeychain\b"),
    ("browser credential store",
     r"(?:google/chrome|microsoft/edge|bravesoftware|opera software|vivaldi|chromium)/user data"
     r"(?:/[^\"'\n]*)?/(?:login data|cookies|web data|local state)\b|"
     r"mozilla/firefox/profiles/[^\"'\n]*(?:logins\.json|key[34]\.db|cookies\.sqlite)|"
     r"\blogin data\b|\blogins\.json\b|\bkey4\.db\b"),
    ("Windows credential store",
     r"appdata/(?:local|roaming)/microsoft/(?:credentials|protect|vault)\b|"
     r"\bcmdkey\b|\bvaultcmd\b|keymgr\.dll|\bmimikatz\b|system32/config/(?:sam|security)\b|"
     r"\breg(?:\.exe)?\s+save\s+hklm[/\\](?:sam|security|system)\b"),
    ("API credential file",
     rf"{_P}\.env(?:\.[\w-]+)?{_E}|\.npmrc\b|\.pypirc\b|{_P}_?\.?netrc\b|\.git-credentials\b|"
     r"\.pgpass\b|\bcredentials\.json\b|\btoken\.json\b|client_secret[\w.-]*\.json|"
     r"service[-_]?account[\w.-]*\.json|\.(?:pem|pfx|p12|ppk|jks|keystore)\b|"
     rf"{_P}[\w.-]*\.key{_E}"),
    ("cloud credentials",
     rf"{_P}\.aws{_E}|{_P}\.azure{_E}|gcloud/(?:credentials|legacy_credentials|access_tokens)|"
     r"application_default_credentials\.json|\.kube/config\b|\.docker/config\.json\b|"
     r"/gh/hosts\.yml\b|\.terraformrc\b|\.terraform\.d/credentials"),
    ("GPG keys", r"\.gnupg\b|/gnupg/|\bsecring\.gpg\b|private-keys-v1\.d"),
]]

# Tool input fields that name a PATH (Grep's `pattern` is content, not a path — never checked).
_PATH_FIELDS = {"file_path", "notebook_path", "path", "glob"}
_GLOB_TOOLS = {"Glob"}                       # for Glob, `pattern` IS a path glob
_SHELL_TOOLS = {"Bash", "PowerShell", "BashOutput"}
FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob", "LS"}


def _norm(s: str) -> str:
    s = os.path.expandvars(os.path.expanduser(str(s or "")))
    return s.replace("\\", "/").lower()


def allowlist() -> list[str]:
    """`[security] allow_protected` — explicit, user-made exceptions (paths / path prefixes)."""
    raw = conf._section("security").get("allow_protected") or []
    return [_norm(p).rstrip("/") for p in (raw if isinstance(raw, list) else [raw]) if str(p).strip()]


def _allowed(text: str) -> bool:
    t = _norm(text)
    return any(a and (t == a or t.startswith(a + "/") or a in t) for a in allowlist())


def check(text: str) -> str | None:
    """The protected category a path/command touches, or None (also None when allowlisted)."""
    t = _norm(text)
    if not t.strip():
        return None
    for cat, rx in RULES:
        if rx.search(t):
            return None if _allowed(text) else cat
    return None


def tool_check(tool_name: str, tool_input: dict | None) -> str | None:
    """For a file/shell tool call: the protected category it touches, or None."""
    inp = tool_input or {}
    texts: list[str] = []
    if tool_name in _SHELL_TOOLS:
        texts.append(str(inp.get("command", "")))
    elif tool_name in FILE_TOOLS:
        texts += [str(inp[k]) for k in _PATH_FIELDS if inp.get(k)]
        if tool_name in _GLOB_TOOLS and inp.get("pattern"):
            texts.append(str(inp["pattern"]))
    else:
        return None
    for t in texts:
        cat = check(t)
        if cat:
            return cat
    return None


def deny_reason(category: str) -> str:
    return (f"blocked: that's {category} — Helios never touches credential stores or secrets on "
            f"its own. To allow one location on purpose, add it to [security] allow_protected in "
            f"settings.toml.")


def root_problem(path: str) -> str | None:
    """For folder-level checks (project manifests): a folder that is, or sits inside, a
    protected location."""
    cat = check(str(Path(path)) + "/")
    return f"a protected location ({cat})" if cat else None
