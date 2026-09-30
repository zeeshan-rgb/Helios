"""Security self-check (`helios security`, blueprint phase 14): is every Helios protection in
place on THIS install? Read-only — it inspects files, settings and flags and changes nothing.

Each check returns (name, status, detail) with status ok | warn | fail. See docs/SECURITY.md for
what each protection does and where it is enforced.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import conf


def _check_gate() -> tuple[str, str]:
    missing = [n for n in ("pretooluse.py", "agy_pretool.py", "agy_marker.py")
               if not (conf.ROOT / "hooks" / n).exists()]
    if missing:
        return "fail", f"missing gate script(s): {', '.join(missing)}"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sec_gate", conf.ROOT / "hooks" / "pretooluse.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        verdict, _ = mod.decide("Read", {"file_path": str(Path.home() / ".ssh" / "id_rsa")})
        if verdict != "deny":
            return "fail", "the gate did NOT deny reading ~/.ssh/id_rsa"
        verdict, _ = mod.decide("WebFetch", {"url": "http://127.0.0.1:8769/health"})
        if verdict != "deny":
            return "fail", "the gate did NOT block an internal-address fetch (SSRF)"
    except Exception as e:
        return "fail", f"the permission gate failed to load: {e}"
    return "ok", "loads; denies credential stores and internal-address fetches"


def _check_internal_mcp() -> tuple[str, str]:
    cfg = conf.CONFIG_DIR / "mcp.json"
    if not cfg.exists():
        return "warn", "config/mcp.json not generated yet (run `helios onboard`)"
    try:
        env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["helios"].get("env") or {}
    except Exception as e:
        return "fail", f"config/mcp.json unreadable: {e}"
    if env.get("HELIOS_MCP_ROLE") != "internal":
        return "fail", "the internal tool server isn't launched with HELIOS_MCP_ROLE=internal"
    return "ok", "internal tool server only runs when Helios launches it"


def _check_public_mcp() -> tuple[str, str]:
    c = conf._section("mcp_public")
    if not c.get("enabled", True):
        return "ok", "public MCP server is off"
    extra = []
    if c.get("read_only"):
        extra.append("read-only")
    if c.get("disabled_tools"):
        extra.append(f"disabled: {', '.join(c['disabled_tools'])}")
    return "ok", "on — curated tools, every call through the gate" + (f" ({'; '.join(extra)})" if extra else "")


def _check_flags() -> list[tuple[str, str, str]]:
    out = []
    out.append(("YOLO mode", "warn" if conf.YOLO_FLAG.exists() else "ok",
                "ON — asks are auto-approved for this chat (hard rails still apply)"
                if conf.YOLO_FLAG.exists() else "off"))
    out.append(("panic stop", "ok",
                "engaged (clears on your next message)" if conf.ABORT_FLAG.exists() else "ready"))
    return out


def _check_protected() -> tuple[str, str]:
    from . import protected
    allow = protected.allowlist()
    if allow:
        return "warn", (f"{len(protected.RULES)} credential-store categories blocked; EXPLICITLY "
                        f"allowed by you: {', '.join(allow)}")
    return "ok", f"{len(protected.RULES)} credential-store categories hard-denied to every caller"


def _check_night_allowlist() -> tuple[str, str]:
    try:
        from . import projects
        ps, errors = projects.load_all()
    except Exception as e:
        return "warn", f"couldn't read project manifests: {e}"
    bad = [f"{p['name']}: {'; '.join(p['problems'])}" for p in ps
           if any("sensitive" in x or "protected" in x for x in p["problems"])]
    if bad:
        return "fail", "manifest points at a protected location: " + " | ".join(bad)
    names = ", ".join(p["name"] for p in ps if p["active"]) or "none"
    return "ok", f"Night Mode inspects only manifest folders: {names}"


def _check_gitignore() -> tuple[str, str]:
    gi = (conf.ROOT / ".gitignore")
    text = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
    need = ["config/secrets.toml", "config/composio_mcp.json", "data/", "logs/"]
    missing = [n for n in need if n not in text]
    if missing:
        return "fail", f"not git-ignored: {', '.join(missing)}"
    return "ok", "secrets, data and logs are never committed"


def _check_logs() -> tuple[str, str]:
    hits = 0
    files = 0
    for f in conf.LOGS_DIR.glob("*.log"):
        files += 1
        try:
            hits += len(conf.SECRET_RE.findall(f.read_text(encoding="utf-8", errors="replace")[-400000:]))
        except Exception:
            continue
    if hits:
        return "warn", (f"{hits} secret-shaped string(s) found in older log lines (new lines are "
                        f"redacted) — consider deleting logs\\*.log")
    return "ok", f"no secrets in {files} log file(s); new log lines are redacted"


def self_check() -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    for name, fn in (("permission gate", _check_gate), ("internal MCP server", _check_internal_mcp),
                     ("public MCP server", _check_public_mcp), ("protected paths", _check_protected),
                     ("Night Mode allowlist", _check_night_allowlist), ("git hygiene", _check_gitignore),
                     ("secrets in logs", _check_logs)):
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "fail", f"check crashed: {e}"
        checks.append((name, status, detail))
    checks += _check_flags()
    return checks


def format_checks(checks: list[tuple[str, str, str]]) -> str:
    mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    return "\n".join(f"[{mark.get(s, s)}] {n}: {d}" for n, s, d in checks)
