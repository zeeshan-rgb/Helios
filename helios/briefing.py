"""Morning briefing (blueprint phase 10): "GOOD MORNING" from last night's Night Mode record.

    GOOD MORNING
    OVERNIGHT ACTIVITY      per project: checks (from the health report), changes, potential issues
    LEARNING                new memories, new rules/skills waiting for approval
    RESEARCH                new findings (top few)
    NEEDS YOUR APPROVAL     the pending lessons, with the command to approve each
    then Completed / Observed / Suggested / Failed / Skipped kept apart.

Honesty rule (the blueprint: never claim an action succeeded unless it was verified): a check is
PASS only if it exited 0, a lesson is "learned" only if its note was written, a finding counts only
if it was stored with a source; anything else is reported as observed, skipped or failed. A missed
or stopped night says so, with the reason.

Delivery ([briefing] in settings.toml): prepared once a day at/after `time` (default 08:00) into
<vault>/Briefings/<date>.md, with a toast + dashboard note; with speak_on_wake, the short spoken
version is read aloud the first time Helios is woken after that. Always available on request
(`helios briefing`, the morning_briefing tool).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import conf, memory_store

_MAX_ITEMS = 8


# ------------------------------------------------------------------------------ config / state

def cfg() -> dict:
    return conf._section("briefing")


def enabled() -> bool:
    return bool(cfg().get("enabled", True))


def brief_time(day: date) -> datetime:
    try:
        h, m = (int(x) for x in str(cfg().get("time") or "08:00").split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except Exception:
        h, m = 8, 0
    return datetime(day.year, day.month, day.day, h, m)


def _state_file() -> Path:
    return conf.DATA_DIR / "briefing" / "state.json"


def _state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    from .night_mode.common import safe_write
    keep = dict(sorted(st.items())[-30:])                 # a month of history is plenty
    safe_write(_state_file(), json.dumps(keep, indent=1))


def briefing_path(day: str) -> Path:
    return memory_store.vault() / "Briefings" / f"{day}.md"


# ------------------------------------------------------------------------------ building

def last_night_record(now: datetime | None = None) -> dict | None:
    """The scheduled run (or missed record) for the most recent night window, if it's recent."""
    from .night_mode import scheduler as night
    now = now or datetime.now()
    ws, _ = night.window_for(now)
    for back in (0, 1):
        rec = night.load_run(night.night_key(ws - timedelta(days=back)))
        if rec:
            return rec
    return None


def _task(rec: dict, name: str) -> dict:
    return ((rec or {}).get("tasks") or {}).get(name) or {}


def build(now: datetime | None = None, rec: dict | None = None) -> dict:
    """Assemble the briefing (a JSON-safe dict)."""
    from . import health
    from .night_mode import morning_report
    now = now or datetime.now()
    rec = rec if rec is not None else last_night_record(now)
    b = {"date": now.date().isoformat(), "night": (rec or {}).get("night"),
         "night_status": (rec or {}).get("status") or "none",
         "night_reason": (rec or {}).get("reason", ""), "report": (rec or {}).get("report", ""),
         "window": (rec or {}).get("window"), "projects": [], "learned": [], "pending": [],
         "research": {}, "sections": {}}

    # Projects: tonight's health reports if the night produced them, else the current state.
    reps = (_task(rec, "run_checks").get("data") or {}).get("health")
    b["health_from_night"] = bool(reps)
    if not reps:
        try:
            reps = health.reports()
        except Exception:
            reps = []
    changes = _task(rec, "sync_projects").get("data") or {}
    for r in reps:
        lines = [f"{row['row']}: {row['text']}" for row in r["rows"]
                 if row["row"] in ("Build", "Tests", "Lint", "Typecheck", "Other checks")
                 and row["status"] != "none"]
        c = changes.get(r["project"])
        if c:
            if c.get("git"):
                bits = [f"{c['commits']} commit(s)"] if c.get("commits") else []
                if c.get("uncommitted"):
                    bits.append(f"{c['uncommitted']} uncommitted file(s)")
                lines.append("Changes detected: " + (", ".join(bits) if bits else "none"))
            else:
                lines.append(f"Changes detected: {c.get('files', 0)} file(s)")
        b["projects"].append({"name": r["project"], "verdict": r["verdict"], "lines": lines,
                              "issues": r["issues"]})

    # Learning: what was actually written tonight; approvals from the live pending list.
    b["learned"] = [l for l in (_task(rec, "extract_memories").get("data") or {}).get("learned", [])
                    if l.get("status") == "active"]
    b["new_pending"] = [l for l in (_task(rec, "extract_memories").get("data") or {}).get("learned", [])
                        if l.get("status") == "pending"]
    b["pending"] = [{"id": i["id"], "category": i["category"], "text": i["text"],
                     "seen": int(i.get("seen", 1))} for i in memory_store.pending()]

    research = _task(rec, "research")
    b["research"] = {"status": research.get("status", "not run"), "summary": research.get("summary", ""),
                     **{k: (research.get("data") or {}).get(k) for k in ("new", "duplicates", "top")}}
    lt = _task(rec, "find_leads")
    b["leads"] = {"status": lt.get("status", "not run"), "summary": lt.get("summary", ""),
                  **{k: (lt.get("data") or {}).get(k) for k in ("new", "top")}}

    if rec and rec.get("tasks"):
        b["sections"] = morning_report.collect(rec)
    return b


# ------------------------------------------------------------------------------ rendering

def _night_sentence(b: dict) -> str:
    s = b["night_status"]
    if s == "none":
        return "There's no Night Mode report for last night"
    if s == "missed":
        return f"Night Mode didn't run last night — {b['night_reason']}"
    if s.startswith("stopped"):
        return f"Night Mode was {s} before it finished"
    if s in ("crashed", "interrupted"):
        return f"Night Mode {s} part-way through"
    if s == "completed with errors":
        n = len([1 for f in b["sections"].get("failed", []) if "task failed" in f])
        return f"Night Mode finished, but {n or 'some'} task(s) failed"
    if s == "running":
        return "Night Mode is still running"
    return "Night Mode finished without problems"


def render(b: dict) -> str:
    d = datetime.fromisoformat(b["date"]).strftime("%A %d %B %Y")
    out = [f"# Good morning, sir — {d}", "", f"_{_night_sentence(b)}._", ""]
    out += ["## Overnight activity", ""]
    if not b["projects"]:
        out += ["- no projects configured (`helios projects add <folder>`)", ""]
    elif not b["health_from_night"]:
        out += ["_(project health below is the latest known state — the checks didn't run tonight)_", ""]
    for p in b["projects"]:
        out.append(f"### {p['name']} — {p['verdict']}")
        out += [f"- {l}" for l in p["lines"]] or ["- no checks configured"]
        out.append(f"- Potential issues: {len(p['issues'])}" + (": " + "; ".join(p["issues"][:3]) if p["issues"] else ""))
        out.append("")
    out += ["### Learning", f"- New memories: {len(b['learned'])}"]
    out += [f"    - ({l['category']}) {l['text']}" for l in b["learned"][:5]]
    new_rules = [l for l in b["new_pending"] if l.get("category") in ("rules", "skills")]
    out.append(f"- New rules/skills waiting for approval: {len(new_rules)}")
    out.append("")
    r = b["research"]
    out.append("### Research")
    if r.get("status") == "ok":
        out.append(f"- New findings: {r.get('new') or 0}" + (f" (already known: {r['duplicates']})" if r.get("duplicates") else ""))
        out += [f"    - [{t['topic']}] {t['title']} — {t['source_name']}" for t in (r.get("top") or [])[:5]]
    else:
        out.append(f"- {r.get('status', 'not run')}: {r.get('summary') or 'research did not run tonight'}")
    out.append("")
    ld = b.get("leads") or {}
    if ld.get("status") not in (None, "not run"):
        out.append("### Leads")
        if ld.get("status") == "ok":
            out.append(f"- New leads: {ld.get('new') or 0}")
            out += [f"    - {t['title']} — suggest {t['currency']} {t['price_min']:,}–{t['price_max']:,}"
                    f" ({t['service']}, {t['scope']}) · `helios leads show {t['id']}`"
                    for t in (ld.get("top") or [])[:5]]
        else:
            out.append(f"- {ld.get('status')}: {ld.get('summary') or 'did not run'}")
        out.append("")
    out += ["## Needs your approval", ""]
    if b["pending"]:
        out += [f"- {p['category'][:-1]}: {p['text']}" + (f" (said {p['seen']}x)" if p["seen"] > 1 else "")
                + f" — `helios learn approve {p['id']}`" for p in b["pending"][:_MAX_ITEMS]]
    else:
        out.append("- nothing")
    out.append("")
    sec = b["sections"]
    for key, title in (("completed", "Completed"), ("observed", "Observed"), ("suggested", "Suggested"),
                       ("failed", "Failed"), ("skipped", "Skipped")):
        items = sec.get(key) or []
        out.append(f"## {title}")
        out += [f"- {i}" for i in items[:_MAX_ITEMS]] or ["- (none)"]
        if len(items) > _MAX_ITEMS:
            out.append(f"- …and {len(items) - _MAX_ITEMS} more (see the night report)")
        out.append("")
    if b.get("report"):
        out.append(f"Full night report: {b['report']}")
    return "\n".join(out).rstrip() + "\n"


_SPEAK_RULES = [   # (pattern, spoken form, priority) — most important first
    (r"security advisories: (?:(\d+) critical)?.*?(\d+) high", None, 0),
    (r"security advisories", "it has security advisories", 1),
    (r"^(.+) failing$", r"\1 are failing", 2),
    (r"(\d+) commit\(s\) not pushed", r"\1 commits aren't pushed", 3),
    (r"uncommitted work waiting (\d+) days", r"there's uncommitted work from \1 days ago", 4),
    (r"dependencies aren't installed", "its dependencies aren't installed", 5),
    (r"(\d+) dependency major version\(s\) behind", r"\1 dependencies are a major version behind", 6),
    (r"^pip check:", "a Python package is missing a requirement", 7),
    (r"health checks never run", "its checks have never run", 8),
    (r"health checks last run", "its checks are out of date", 8),
    (r"no tests configured", "it has no tests set up", 9),
]


def _speakable(issue: str) -> tuple[int, str]:
    for rx, form, prio in _SPEAK_RULES:
        m = re.search(rx, issue, re.I)
        if not m:
            continue
        if form is None:                       # security advisories with a high count
            crit, high = m.group(1), m.group(2)
            return prio, (f"{crit} critical and " if crit and crit != "0" else "") + \
                f"{high} high-severity security {'advisory' if high == '1' else 'advisories'}"
        return prio, m.expand(form) if "\\" in form else form
    return 10, issue.split(" — ")[0].split(" (")[0].rstrip(". ")


def _top_issue(issues: list[str]) -> str:
    return min((_speakable(i) for i in issues), default=(99, ""))[1]


def spoken(b: dict) -> str:
    """A short version for TTS: no links, ids or markdown; a few sentences."""
    night = re.sub(r" \(.*?\)", "", _night_sentence(b))      # no "(panic stop)" asides aloud
    parts = [f"Good morning, sir. {night}."]
    healthy = [p["name"] for p in b["projects"] if p["verdict"] == "OK"]
    for p in b["projects"]:
        top = _top_issue(p["issues"])
        if p["verdict"] == "FAILING":
            first = next((l for l in p["lines"] if "FAIL" in l), "")
            row = first.split(":")[0]
            what = {"Tests": "the tests fail", "Build": "the build fails", "Lint": "the lint fails",
                    "Typecheck": "the typecheck fails", "Other checks": "some checks fail"
                    }.get(row, top) if first else top
            parts.append(f"{p['name']} is failing: {what}.")
        elif p["verdict"] == "ATTENTION" and top:
            parts.append(f"{p['name']} needs attention: {top}.")
        elif p["verdict"] == "UNKNOWN":
            extra = [s for _, s in sorted(_speakable(i) for i in p["issues"]) if "never run" not in s][:1]
            parts.append(f"{p['name']} hasn't been checked yet" + (f", and {extra[0]}" if extra else "") + ".")
    if healthy:
        parts.append(f"{' and '.join(healthy)} {'looks' if len(healthy) == 1 else 'look'} healthy.")
    n_learn, n_pend = len(b["learned"]), len(b["pending"])
    if n_learn or n_pend:
        s = f"I learned {n_learn} new thing{'s' if n_learn != 1 else ''}" if n_learn else "I learned nothing new"
        if n_pend:
            s += f", and {n_pend} lesson{'s are' if n_pend != 1 else ' is'} waiting for your approval"
        parts.append(s + ".")
    r = b["research"]
    if r.get("status") == "ok" and r.get("new"):
        top = (r.get("top") or [{}])[0].get("title", "")
        parts.append(f"Research turned up {r['new']} new finding{'s' if r['new'] != 1 else ''}"
                     + (f", including: {top}." if top else "."))
    ld = b.get("leads") or {}
    if ld.get("status") == "ok" and ld.get("new"):
        top = (ld.get("top") or [{}])[0]
        best = (f"; the best: {top.get('title')}, worth about {top.get('price_min', 0):,} to "
                f"{top.get('price_max', 0):,} {top.get('currency', 'USD')}") if top.get("title") else ""
        parts.append(f"I found {ld['new']} new lead{'s' if ld['new'] != 1 else ''}{best}.")
    failed = [f for f in b["sections"].get("failed", []) if "task failed" in f]
    if b["night_status"].startswith("completed"):
        parts.append("Nothing failed." if not failed else "The details are in the report.")
    return " ".join(parts)


# ------------------------------------------------------------------------------ delivery

def prepare(now: datetime | None = None) -> dict:
    """Build + write today's briefing file. Returns {'briefing', 'text', 'spoken', 'path'}."""
    from .night_mode.common import safe_write
    now = now or datetime.now()
    b = build(now)
    text = render(b)
    path = safe_write(briefing_path(b["date"]), text)
    conf.log("briefing", f"prepared {b['date']} (night {b['night']}: {b['night_status']})")
    return {"briefing": b, "text": text, "spoken": spoken(b), "path": str(path)}


def tick(now: datetime | None = None, emit=None) -> str:
    """App scheduler hook: prepare today's briefing once, at/after the configured time."""
    now = now or datetime.now()
    if not enabled():
        return "disabled"
    if now < brief_time(now.date()):
        return "early"
    today = now.date().isoformat()
    st = _state()
    if st.get(today, {}).get("prepared"):
        return "done"
    from .night_mode import scheduler as night
    if night.is_running() and now < brief_time(now.date()) + timedelta(hours=2):
        return "waiting"                      # let tonight's run finish first (up to 2 h)
    out = prepare(now)
    st[today] = {"prepared": now.isoformat(timespec="seconds"), "night": out["briefing"]["night"],
                 "spoken_text": out["spoken"], "spoken": False, "path": out["path"]}
    _save_state(st)
    try:
        from . import jobs
        jobs.record_system_run("morning_briefing", "ok",
                               f"prepared (night {out['briefing']['night'] or 'none'}: "
                               f"{out['briefing']['night_status']})", now, datetime.now())
    except Exception as e:  # pragma: no cover
        conf.log("briefing", f"job history record failed: {e}")
    if cfg().get("notify", True):
        msg = "☀ Good morning, sir — your briefing is ready. Say “good morning” or open it in the vault."
        try:
            if emit:
                emit("status", msg)
            from . import notify
            notify.toast("Helios — Good morning", out["spoken"][:200])
        except Exception:
            pass
    return "prepared"


def on_wake(publish, now: datetime | None = None) -> bool:
    """Called when Helios is woken (clap / tray / hotkey). The first wake after the briefing is
    ready reads the spoken version aloud (speak_on_wake). Returns True if it was announced."""
    now = now or datetime.now()
    if not enabled() or not cfg().get("speak_on_wake", True) or now < brief_time(now.date()):
        return False
    today = now.date().isoformat()
    st = _state()
    entry = st.get(today) or {}
    if entry.get("spoken"):
        return False
    if not entry.get("prepared"):
        tick(now)
        st = _state()
        entry = st.get(today) or {}
        if not entry.get("prepared"):
            return False
    entry["spoken"] = True
    st[today] = entry
    _save_state(st)
    try:
        publish("announce", {"text": entry.get("spoken_text", ""), "kind": "briefing"})
    except Exception as e:
        conf.log("briefing", f"announce failed: {e}")
        return False
    conf.log("briefing", f"read aloud on wake ({today})")
    return True


def latest_text(day: str | None = None, *, spoken_version: bool = False) -> str:
    """Today's (or a given day's) briefing — prepared on the spot if it doesn't exist yet."""
    day = day or date.today().isoformat()
    f = briefing_path(day)
    if spoken_version:
        entry = _state().get(day) or {}
        return entry.get("spoken_text") or prepare()["spoken"]
    if f.exists():
        return f.read_text(encoding="utf-8")
    if day != date.today().isoformat():
        return f"No briefing for {day}."
    return prepare()["text"]
