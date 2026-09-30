"""Helios PUBLIC MCP server — a curated, policy-enforced tool set for OTHER MCP clients (the
Antigravity IDE, Claude Desktop, …). See docs/HELIOS_MCP.md.

Why a separate server: mcp/helios_server.py is the brain's INTERNAL tool server (~60 tools, incl.
writing/running new tools, spawning agents, deleting routines). It is safe only because every call
from Helios's own brain passes the PreToolUse permission gate first — an outside client would call it
directly, so it now refuses to start unless Helios launched it.

This server exposes only the blueprint's tool set, and EVERY call is decided by the very same gate
(hooks/pretooluse.decide): panic, YOLO, the allow/ask policy and [autonomy] always_ask all apply, and
anything that needs approval pops the normal Approve/Deny prompt (dashboard / Telegram / voice). If
the prompt can't be shown (Helios not running) the call is denied. On top of that, [mcp_public] in
settings.toml can switch the server off, make it read-only, or disable single tools; every call is
written to logs/mcp_public.log. No tool deletes, pushes, publishes, installs, sends messages to
people, or runs arbitrary commands.

GOTCHA (same as the internal server): import the installed `mcp` package BEFORE putting the repo
root on sys.path — the local mcp/ folder would shadow it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime

from mcp.server.fastmcp import FastMCP  # installed pkg — import BEFORE touching sys.path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(_ROOT)

from helios import conf  # noqa: E402

mcp = FastMCP("helios-public")

# public tool -> (the internal tool name the Helios policy knows, kind)
# kind: read = no side effects; write = changes Helios's own data; action = does something on the PC.
TOOLS = {
    "get_system_status": ("mcp__helios__system_health", "read"),
    "get_project_status": ("mcp__helios__project_health", "read"),
    "get_active_projects": ("mcp__helios__list_projects", "read"),
    "get_project_changes": ("mcp__helios__project_changes", "read"),
    "search_memory": ("mcp__helios__recall_memory", "read"),
    "get_recent_memory": ("mcp__helios__list_memories", "read"),
    "get_morning_report": ("mcp__helios__morning_briefing", "read"),
    "search_research": ("mcp__helios__research_findings", "read"),
    "safe_diagnostic": ("mcp__helios__system_health", "read"),
    "save_memory": ("mcp__helios__remember", "write"),
    "create_reminder": ("mcp__helios__set_reminder", "write"),
    "open_app": ("mcp__helios__open_app", "action"),
    "run_project_tests": ("mcp__helios__run_project_checks", "action"),
}
_MAX_OUT = 12000

_policy = None


def _load_policy():
    """The PreToolUse gate module — the one authority for every Helios tool call."""
    global _policy
    if _policy is None:
        spec = importlib.util.spec_from_file_location("helios_pretooluse_public",
                                                      os.path.join(_ROOT, "hooks", "pretooluse.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _policy = mod
    return _policy


def cfg() -> dict:
    return conf._section("mcp_public")


def _short(args: dict) -> str:
    from helios.memory import _redact
    return _redact(", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items()))[:240]


def _guard(tool: str, args: dict) -> str | None:
    """None = go ahead; otherwise the refusal to return to the client."""
    internal, kind = TOOLS[tool]
    c = cfg()
    why = None
    if not c.get("enabled", True):
        why = "the Helios public MCP server is turned off ([mcp_public] enabled = false)"
    elif tool in (c.get("disabled_tools") or []):
        why = f"{tool} is disabled in [mcp_public] disabled_tools"
    elif c.get("read_only", False) and kind != "read":
        why = f"the server is read-only ([mcp_public] read_only = true) — {tool} changes things"
    if why is None:
        try:
            decision, reason = _load_policy().decide(internal, dict(args), "mcp-public")
        except Exception as e:                # the gate failing must never mean "allow"
            decision, reason = "deny", f"permission gate error ({e})"
        if decision != "allow":
            why = f"denied by Helios's permission policy: {reason}"
        conf.log("mcp_public", f"{tool} [{kind}] {decision}: {reason} — {_short(args)}")
    else:
        conf.log("mcp_public", f"{tool} [{kind}] refused: {why} — {_short(args)}")
    return f"Not done: {why}." if why else None


def _cap(text) -> str:
    text = text if isinstance(text, str) else "\n".join(map(str, text))
    return text if len(text) <= _MAX_OUT else text[:_MAX_OUT] + "\n…(truncated)"


# ------------------------------------------------------------------------------ read tools

@mcp.tool()
def get_system_status() -> str:
    """Helios's own status: whether the app is running, its brain, Night Mode's last run, disk
    space, the voice pipeline and the memory vault. Read-only."""
    if (r := _guard("get_system_status", {})):
        return r
    from helios import health
    parts = []
    for name in ("helios_runtime", "helios_voice", "helios_memory"):
        title, fn = health.PROBES[name]
        try:
            pr = fn({})
            parts.append(f"{title}: {pr['status'].upper()}\n  " + "\n  ".join(pr["items"])
                         + ("".join(f"\n  ! {i}" for i in pr["issues"])))
        except Exception as e:
            parts.append(f"{title}: unknown ({e})")
    return _cap("\n".join(parts))


@mcp.tool()
def get_project_status(project: str = "") -> str:
    """PROJECT HEALTH for one of the user's configured projects (or all active ones): verdict,
    build/tests/lint/typecheck, git status, dependencies, potential issues. Read-only (uses the
    last results; run_project_tests refreshes them)."""
    if (r := _guard("get_project_status", {"project": project})):
        return r
    from helios import health
    try:
        return _cap(health.format_report(health.reports(project or None)))
    except KeyError as e:
        return str(e)


@mcp.tool()
def get_active_projects() -> str:
    """The user's configured projects (from their manifests) with status and last check result."""
    if (r := _guard("get_active_projects", {})):
        return r
    from helios import projects
    return _cap(projects.format_list())


@mcp.tool()
def get_project_changes(project: str = "", hours: float = 24) -> str:
    """What changed in the user's projects recently (commits / uncommitted files, or modified
    files for non-git folders). Default: all active projects, last 24 hours."""
    if (r := _guard("get_project_changes", {"project": project, "hours": hours})):
        return r
    from helios import projects
    try:
        return _cap(projects.format_changes(projects.all_changes(max(1.0, min(float(hours), 24 * 31)),
                                                                 project or None)))
    except KeyError as e:
        return str(e)


@mcp.tool()
def search_memory(query: str, category: str = "") -> str:
    """Search what the user asked Helios to remember (active items only; pending lessons and
    secrets are never included). category: user, preferences, projects, decisions, rules,
    skills, research."""
    if (r := _guard("search_memory", {"query": query, "category": category})):
        return r
    from helios import memory_store
    cat = category.strip().lower() or None
    if cat and cat not in memory_store.CATEGORIES:
        return f"Unknown category. Use one of: {', '.join(memory_store.CATEGORIES)}"
    return _cap(memory_store.format_items(memory_store.recall(query, cat, limit=15)))


@mcp.tool()
def get_recent_memory(limit: int = 15) -> str:
    """The most recently remembered items (active only), newest first."""
    if (r := _guard("get_recent_memory", {"limit": limit})):
        return r
    from helios import memory_store
    return _cap(memory_store.format_items(memory_store.recall("", limit=max(1, min(int(limit), 50)))))


@mcp.tool()
def get_morning_report(spoken: bool = False) -> str:
    """Today's Helios morning briefing (last night's Night Mode results, learning, research,
    approvals). spoken=True returns the short read-aloud version."""
    if (r := _guard("get_morning_report", {"spoken": spoken})):
        return r
    from helios import briefing
    return _cap(briefing.latest_text(spoken_version=bool(spoken)))


@mcp.tool()
def search_research(topic: str = "", query: str = "", days: float = 14) -> str:
    """Helios's research library: sourced findings on the user's research topics (kept apart from
    memory). Optional topic, keywords, and age in days."""
    if (r := _guard("search_research", {"topic": topic, "query": query, "days": days})):
        return r
    from helios import research
    return _cap(research.format_findings(research.findings(topic or None, days=days or None,
                                                           query=query, limit=20), verbose=True))


DIAGNOSTICS = ("system", "disk", "network", "helios", "voice", "memory", "errors")


@mcp.tool()
def safe_diagnostic(check: str = "system") -> str:
    """A read-only diagnostic from a fixed list: system (CPU/RAM/GPU/disk/power + findings), disk,
    network (is the PC online), helios (app/brain/Night Mode), voice, memory, errors (error lines
    in Helios's logs in the last 24 h). Nothing is changed or fixed."""
    check = (check or "system").strip().lower()
    if check not in DIAGNOSTICS:
        return f"Unknown diagnostic. Available: {', '.join(DIAGNOSTICS)}"
    if (r := _guard("safe_diagnostic", {"check": check})):
        return r
    import shutil
    from helios import health
    if check == "system":
        from helios import sysdoctor
        return _cap(sysdoctor.report())
    if check == "disk":
        lines = []
        for d in ("C:\\", "D:\\", "E:\\"):
            try:
                u = shutil.disk_usage(d)
                lines.append(f"{d[:2]} {u.free / 1e9:.0f} GB free of {u.total / 1e9:.0f} GB")
            except Exception:
                continue
        return "\n".join(lines) or "no drives readable"
    if check == "network":
        from helios.night_mode.common import online
        return "online" if online() else "OFFLINE (no route to public DNS)"
    if check == "errors":
        out = []
        for name in ("app", "brain", "voice", "night", "scheduler", "antigravity", "health"):
            errs = health._recent_log_errors(conf.LOGS_DIR / f"{name}.log", hours=24)
            if errs:
                out.append(f"{name}.log: {len(errs)} — latest: {errs[-1][:160]}")
        return "\n".join(out) or "no errors logged in the last 24 hours"
    title, fn = health.PROBES[{"helios": "helios_runtime", "voice": "helios_voice",
                               "memory": "helios_memory"}[check]]
    pr = fn({})
    return f"{title}: {pr['status'].upper()}\n" + "\n".join(pr["items"] + [f"! {i}" for i in pr["issues"]])


# ------------------------------------------------------------------------------ write / action tools

@mcp.tool()
def save_memory(text: str, category: str = "auto", project: str = "") -> str:
    """Save something the user wants Helios to remember — a short statement about them
    ("Prefers dark mode"). Passwords, keys and other secrets are refused. Standing rules and
    skills from an outside app wait for the user's approval in Helios."""
    if (r := _guard("save_memory", {"text": text, "category": category, "project": project})):
        return r
    from helios import memory_store
    cat = (category or "auto").strip().lower()
    if cat == "auto" and not project:
        cat = memory_store.classify(text)
    status = "pending" if cat in ("rules", "skills") else "active"
    res = memory_store.remember(text, category, source="mcp client", project=project or None, status=status)
    if res["status"] in ("refused", "rejected"):
        return f"Not saved: {res['reason']}."
    it = res["item"]
    if (it.get("status") or "active") == "pending":
        return f"Proposed to the user for approval [{it['category']}]: {it['text']} (id {it['id']})"
    verb = "Already known (refreshed)" if res["status"] == "duplicate" else "Remembered"
    return f"{verb} [{it['category']}]: {it['text']} (id {it['id']})"


@mcp.tool()
def create_reminder(text: str, in_minutes: float | None = None, at_iso: str | None = None) -> str:
    """Create a Helios reminder (toast + chat when due). Give EITHER in_minutes OR at_iso
    (e.g. '2026-10-02T15:00:00')."""
    if (r := _guard("create_reminder", {"text": text, "in_minutes": in_minutes, "at_iso": at_iso})):
        return r
    from helios import db, sched_util
    text = (text or "").strip()[:300]
    if not text:
        return "Nothing to remind about."
    try:
        due = sched_util.parse_due(in_minutes, at_iso, datetime.now())
    except Exception as e:
        return f"Couldn't understand the time: {e}"
    db.init()
    rid = db.add_reminder(text, due.isoformat(timespec="seconds"))
    return f"Reminder #{rid} set for {due:%Y-%m-%d %H:%M}: {text}"


@mcp.tool()
def open_app(name: str) -> str:
    """Open an application or website on the user's PC by name ('Spotify', 'notepad',
    'github.com'). Visible to the user; subject to Helios's permission policy."""
    if (r := _guard("open_app", {"name": name})):
        return r
    from helios import open_app as _oa
    return _oa.open_app(name)


@mcp.tool()
def run_project_tests(project: str, check: str = "") -> str:
    """Run the health checks the user configured for ONE project (their own test/lint/build
    commands, no shell; anything that pushes/publishes/deploys/deletes is refused), then return
    its PROJECT HEALTH. `check` runs a single named check. Can take minutes."""
    if (r := _guard("run_project_tests", {"project": project, "check": check})):
        return r
    from helios import health, projects
    if not (project or "").strip():
        return "Name the project (see get_active_projects)."
    try:
        if check:
            return _cap(projects.format_checks(projects.run_checks(project, check)))
        return _cap(health.format_report(health.run_all(project)))
    except projects.Busy as e:
        return f"Not started: {e}."
    except KeyError as e:
        return str(e)


def in_helios_brain_session() -> bool:
    """True when an agy session run BY HELIOS started us. Antigravity's global MCP config is shared
    by the IDE and the agy CLI, so once this server is registered there for the IDE, Helios's own
    brain sessions would load it too — next to the internal server, duplicating every tool. Helios
    marks its sessions with HELIOS_AGY_MARKER (agy_cli.MARKER_ENV), which MCP servers inherit."""
    return bool(os.environ.get("HELIOS_AGY_MARKER"))


if __name__ == "__main__":
    if in_helios_brain_session():
        FastMCP("helios-public").run()     # no tools here: the brain already has the internal server
    else:
        mcp.run()
