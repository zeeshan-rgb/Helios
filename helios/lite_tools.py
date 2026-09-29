"""Curated, permission-gated toolkit for the lite (non-Claude) brain.

The Claude brain gets its tools from MCP servers gated by the PreToolUse hook. The lite brain
has no Claude Code, so this module is its equivalent: a small set of OpenAI function schemas plus
a dispatcher that runs each call through the SAME permission policy as the hook —

    hard-rails (panic / SSRF / ~/.claude)  ->  YOLO check  ->  permissions.classify()  ->
    perms.create()/wait() (the in-UI Approve/Deny) for "ask" verdicts.

A denied call returns a plain string the model can read and adapt to (never raises). This is the
seed of the eventual provider-agnostic agent loop. Backed by existing modules wherever possible
(sysdoctor, memory, db, sched_util) so behaviour matches the Claude path's first-party tools.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime

from . import conf, db, memory, permissions, sched_util, sysdoctor

CREATE_NO_WINDOW = 0x08000000   # don't flash a console when launching apps/shell under pythonw
_RESULT_CAP = 6000              # cap each tool result so a big output can't blow up the context
_FETCH_CAP = 8000              # cap fetched/searched web text


def _cap(s: str, n: int = _RESULT_CAP) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + "\n…(truncated)"


def _strip_html(html: str) -> str:
    """Very small HTML→text: drop scripts/styles/tags, unescape entities, collapse whitespace."""
    import html as _html
    import re
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = _html.unescape(html)
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", html)).strip()


class LiteTools:
    """Schemas + gated execution for the lite brain. One instance per brain; reuses the brain's
    `emit` (unused here — the brain emits the 'tool' event) and the shared `perms` registry."""

    def __init__(self, emit=None, perms=None):
        self.emit = emit
        self.perms = perms
        self.sink = ""   # per-turn permission sink (e.g. "telegram:<chat>"), set by the lite brain
        sc = conf.provider_cfg("search")
        self._search_backend = str(sc.get("backend") or "").strip().lower()
        self._search_key = sc.get("api_key")
        # web_search is only offered when a backend+key is configured; otherwise the model is told
        # (in the lite system note) to ask for a URL and use web_fetch instead.
        self.search_enabled = bool(self._search_backend and self._search_key)

    # ------------------------------------------------------------------ schemas
    def schemas(self) -> list[dict]:
        """OpenAI function-tool schemas for every available tool (web_search included only if a
        search backend is configured)."""
        tools = [
            _fn("read_file", "Read a UTF-8 text file from disk and return its contents.",
                {"path": _str("Absolute or relative path to the file.")}, ["path"]),
            _fn("write_file", "Create or overwrite a UTF-8 text file with the given content.",
                {"path": _str("Path to write."), "content": _str("Full file content to write.")},
                ["path", "content"]),
            _fn("run_shell", "Run a single PowerShell command and return its output. "
                "Read-only/safe commands run automatically; anything that changes the system "
                "asks the user first.",
                {"command": _str("The PowerShell command to run.")}, ["command"]),
            _fn("open_app", "Open an application, file, or URI on the user's computer "
                "(e.g. 'notepad', 'spotify:', 'https://...').",
                {"name": _str("App name, file path, or URI to open.")}, ["name"]),
            _fn("web_fetch", "Fetch a URL and return its readable text content.",
                {"url": _str("The http(s) URL to fetch.")}, ["url"]),
            _fn("system_health", "Get a live snapshot of the PC's health (CPU/RAM/disk/GPU/"
                "battery/top processes) with a short diagnosis.", {}, []),
            _fn("memory_read", "Recall what Helios knows from the Obsidian memory vault, "
                "optionally focused on a query.",
                {"query": _str("Optional topic to focus the recall on.")}, []),
            _fn("memory_append", "Save a durable fact about the user to long-term memory.",
                {"fact": _str("A concise, lasting fact worth remembering.")}, ["fact"]),
            _fn("set_reminder", "Set a one-shot reminder. Give EITHER in_minutes OR at_iso.",
                {"text": _str("What to remind about."),
                 "in_minutes": _num("Minutes from now (e.g. 20)."),
                 "at_iso": _str("Absolute ISO datetime, e.g. 2026-06-28T15:00:00.")}, ["text"]),
            _fn("list_reminders", "List pending reminders.", {}, []),
            _fn("cancel_reminder", "Cancel a pending reminder by its id.",
                {"reminder_id": _int("The reminder id to cancel.")}, ["reminder_id"]),
        ]
        if self.search_enabled:
            tools.insert(5, _fn("web_search",
                "Search the web and return the top results (titles, URLs, snippets).",
                {"query": _str("The search query.")}, ["query"]))
        return tools

    # ------------------------------------------------------------------ dispatch + gate
    def execute(self, name: str, args: dict) -> str:
        """Run a tool call through the permission gate, then dispatch it. Returns a string result
        (or a 'Refused/Denied …' message) — never raises out to the brain loop."""
        try:
            action = getattr(self, f"_do_{name}", None)
            if action is None:
                return f"Unknown tool: {name}"
            # Map the lite tool to a canonical (classify_name, classify_input) for policy + hard-rails.
            cname, cinput = self._classify_key(name, args)
            # --- hard-rails (mirror hooks/pretooluse.py decide(), which lite bypasses) ---
            try:
                if conf.ABORT_FLAG.exists():
                    return "Refused: panic is engaged — stopped until the user sends a new message."
            except Exception:
                pass
            if name == "web_fetch" and permissions.is_internal_url(str(args.get("url", ""))):
                return "Refused: that URL points at a private/loopback address (SSRF blocked)."
            if name == "write_file" and permissions.is_claude_dir(str(args.get("path", ""))):
                return "Refused: Helios does not write to Claude Code's config dir."
            # --- YOLO bypass, else policy classify, else ask the user in the UI ---
            decision = "allow"
            try:
                yolo = conf.YOLO_FLAG.exists()
            except Exception:
                yolo = False
            if not yolo:
                if permissions.classify(cname, cinput) != "allow":
                    if not self.perms:
                        return "Denied: this action needs approval but no approver is connected."
                    rid = self.perms.create(cname, cinput, sink=self.sink)
                    decision = self.perms.wait(rid, 120)
            if decision != "allow":
                return "Denied by the user."
            return _cap(action(args))
        except Exception as e:  # a tool's own failure is a result the model can react to
            conf.log("lite_tools", f"{name} error: {e}")
            return f"Error running {name}: {e}"

    def _classify_key(self, name: str, args: dict) -> tuple[str, dict]:
        """Translate a lite tool into the canonical tool name + input that permissions.classify()
        understands, so the lite brain enforces exactly the same policy as the Claude hook."""
        if name == "read_file":
            return "Read", {"file_path": args.get("path", "")}
        if name == "write_file":
            return "Write", {"file_path": args.get("path", "")}
        if name == "run_shell":
            return "PowerShell", {"command": args.get("command", "")}
        if name == "open_app":
            return "mcp__computer__launch_app", {"app": args.get("name", "")}
        if name == "web_search":
            return "WebSearch", {"query": args.get("query", "")}
        if name == "web_fetch":
            return "WebFetch", {"url": args.get("url", "")}
        # Helios first-party tools: read-only/non-destructive -> classify() returns "allow".
        return f"mcp__helios__{name}", dict(args)

    # ------------------------------------------------------------------ tool bodies
    def _do_read_file(self, args: dict) -> str:
        path = str(args.get("path", "")).strip()
        if not path:
            return "Error: no path given."
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def _do_write_file(self, args: dict) -> str:
        path = str(args.get("path", "")).strip()
        content = args.get("content", "")
        if not path:
            return "Error: no path given."
        p = os.path.abspath(path)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content if isinstance(content, str) else str(content))
        return f"Wrote {len(content)} characters to {p}."

    def _do_run_shell(self, args: dict) -> str:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return "Error: no command given."
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=60, encoding="utf-8",
            errors="replace", creationflags=CREATE_NO_WINDOW, cwd=str(conf.workspace_path()))
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if not parts:
            parts.append(f"(no output; exit code {proc.returncode})")
        return "\n".join(parts)

    def _do_open_app(self, args: dict) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "Error: no app/name given."
        try:
            os.startfile(name)  # type: ignore[attr-defined]  # Windows: apps, files, URIs
            return f"Opened {name}."
        except Exception:
            subprocess.Popen(["cmd", "/c", "start", "", name],
                             creationflags=CREATE_NO_WINDOW)
            return f"Launched {name}."

    def _do_web_search(self, args: dict) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: empty query."
        if not self.search_enabled:
            return "Web search is not configured. Ask the user for a URL and use web_fetch."
        return _cap(self._search(query), _FETCH_CAP)

    def _do_web_fetch(self, args: dict) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return "Error: no url given."
        req = urllib.request.Request(url, headers={"User-Agent": "Helios/1.0 (+local assistant)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(2_000_000)  # cap bytes read
        body = raw.decode("utf-8", errors="replace")
        text = body if "text/plain" in ctype or "json" in ctype else _strip_html(body)
        return _cap(text, _FETCH_CAP)

    def _do_system_health(self, args: dict) -> str:
        return sysdoctor.report()

    def _do_memory_read(self, args: dict) -> str:
        digest = memory.build_digest(str(args.get("query", "")))
        return digest or "(memory is empty so far)"

    def _do_memory_append(self, args: dict) -> str:
        fact = str(args.get("fact", "")).strip()
        if not fact:
            return "Error: empty fact."
        added = memory.append_fact(fact)
        return "Saved to memory." if added else "Already known (not duplicated)."

    def _do_set_reminder(self, args: dict) -> str:
        text = str(args.get("text", "")).strip()
        if not text:
            return "Error: no reminder text."
        in_minutes = args.get("in_minutes")
        at_iso = args.get("at_iso")
        due = sched_util.parse_due(
            float(in_minutes) if in_minutes is not None else None,
            str(at_iso) if at_iso else None, datetime.now())
        rid = db.add_reminder(text, due.isoformat(timespec="seconds"))
        return f"Reminder #{rid} set for {due:%Y-%m-%d %H:%M}: {text}"

    def _do_list_reminders(self, args: dict) -> str:
        rows = db.list_reminders()
        if not rows:
            return "(no pending reminders)"
        return "\n".join(f"#{r['id']} — {r['due_at'][:16].replace('T', ' ')} — {r['text']}"
                         for r in rows)

    def _do_cancel_reminder(self, args: dict) -> str:
        try:
            rid = int(args.get("reminder_id"))
        except Exception:
            return "Error: reminder_id must be a number."
        return f"Cancelled #{rid}" if db.cancel_reminder(rid) else f"No reminder #{rid}."

    # ------------------------------------------------------------------ search backends
    def _search(self, query: str) -> str:
        b = self._search_backend
        try:
            if b == "tavily":
                return self._search_tavily(query)
            if b == "brave":
                return self._search_brave(query)
            if b == "serper":
                return self._search_serper(query)
        except Exception as e:
            return f"Search failed: {e}"
        return f"Unknown search backend '{b}'."

    def _search_tavily(self, query: str) -> str:
        body = json.dumps({"api_key": self._search_key, "query": query,
                           "max_results": 5}).encode("utf-8")
        req = urllib.request.Request("https://api.tavily.com/search", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "\n\n".join(f"{r.get('title','')}\n{r.get('url','')}\n{r.get('content','')}"
                           for r in data.get("results", [])) or "(no results)"

    def _search_brave(self, query: str) -> str:
        url = "https://api.search.brave.com/res/v1/web/search?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"X-Subscription-Token": self._search_key,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = (data.get("web", {}) or {}).get("results", [])[:5]
        return "\n\n".join(f"{r.get('title','')}\n{r.get('url','')}\n{r.get('description','')}"
                           for r in items) or "(no results)"

    def _search_serper(self, query: str) -> str:
        body = json.dumps({"q": query}).encode("utf-8")
        req = urllib.request.Request("https://google.serper.dev/search", data=body,
                                     headers={"X-API-KEY": self._search_key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("organic", [])[:5]
        return "\n\n".join(f"{r.get('title','')}\n{r.get('link','')}\n{r.get('snippet','')}"
                           for r in items) or "(no results)"


# --------------------------------------------------------------------- schema helpers
def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _num(desc: str) -> dict:
    return {"type": "number", "description": desc}


def _int(desc: str) -> dict:
    return {"type": "integer", "description": desc}
