"""Executive reporting: Helios reports to its owner like a reliable employee (docs/HELIOS_EXECUTIVE_REPORTING.md).

Three parts, all built on what Helios already records — no parallel lead database, scheduler or
notifier:

1. EVENT QUEUE (`exec_events` in helios.db) — subsystems call `emit()` when something matters
   (a high-priority lead, a failing project, a job failing repeatedly, Night Mode finishing,
   Antigravity becoming unreachable). Each event has a severity, a dedupe key (the same unresolved
   problem is one event, counted — never ten), delivery status per channel, and a separate
   acknowledgement (delivered ≠ reviewed).
2. ROUTING (`route()`, every scheduler tick) — CRITICAL goes out at once (dashboard, toast,
   Telegram if configured, a phone call if enabled); HIGH goes out promptly unless it's quiet hours
   or you snoozed reports; NORMAL / LOW wait for the next report's digest. Untrusted content can't
   create events — only Helios's own code paths call emit().
3. REPORTS (`build()`) — midday, end-of-day, Night Mode and weekly reports, assembled from the
   prospect store, research and lead libraries, projects, jobs, memory and the event queue. The
   morning report IS the existing briefing (briefing.py adds a business + decisions section from
   `business()` / `decisions()`). Every number comes from a record; a subsystem that fails is
   reported as failed and the rest of the report is still produced. Completed, attempted, partial,
   failed, skipped, to-verify, suggested and awaiting-approval are kept apart.

Personal phone calls live in calling.py; this module only decides WHEN a call is warranted.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from . import conf, db

SEVERITIES = ("CRITICAL", "HIGH", "NORMAL", "LOW")
KINDS = ("morning", "midday", "eod", "night", "weekly")
_DEFAULTS = {"enabled": True, "midday_report": True, "midday_time": "13:00", "end_of_day_report": True,
             "eod_time": "18:30", "night_mode_report": True, "weekly_review": True, "weekly_day": "mon",
             "weekly_time": "09:00", "skip_quiet_midday": True, "quiet_hours_start": "22:00",
             "quiet_hours_end": "08:00", "channels": ["dashboard", "toast", "telegram"],
             "immediate": ["CRITICAL", "HIGH"], "lead_alert_priority": "A"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_events (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL, last_seen TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL,
  summary TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '',
  related TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '',
  approval INTEGER NOT NULL DEFAULT 0, dedupe_key TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'new',          -- new | delivered | batched | held | reported
  delivery TEXT NOT NULL DEFAULT '{}',        -- {channel: {status, at, detail}}
  retries INTEGER NOT NULL DEFAULT 0,
  acknowledged TEXT                            -- when YOU marked it reviewed
);
CREATE INDEX IF NOT EXISTS exec_events_key ON exec_events(dedupe_key);
CREATE TABLE IF NOT EXISTS exec_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, created TEXT NOT NULL,
  path TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', spoken TEXT NOT NULL DEFAULT '',
  delivery TEXT NOT NULL DEFAULT '{}'
);
"""
_inited: set[str] = set()
_lock = threading.Lock()


def _init() -> None:
    key = str(db.DB_PATH)
    if key in _inited:
        return
    with _lock:
        if key not in _inited:
            db.init()
            with db._conn() as c:
                c.executescript(_SCHEMA)
            _inited.add(key)


def cfg() -> dict:
    v = conf._section("executive_reporting")
    out = dict(_DEFAULTS)
    out.update({k: val for k, val in v.items() if not isinstance(val, dict)})
    return out


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hm(s: str, default: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(s or default).split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    h, m = (int(x) for x in default.split(":"))
    return h, m


def in_quiet_hours(now: datetime | None = None, c: dict | None = None) -> bool:
    c = c or cfg()
    now = now or datetime.now()
    s, e = _hm(c.get("quiet_hours_start"), "22:00"), _hm(c.get("quiet_hours_end"), "08:00")
    t, s, e = now.hour * 60 + now.minute, s[0] * 60 + s[1], e[0] * 60 + e[1]
    return (s <= t < e) if s <= e else (t >= s or t < e)


def snoozed_until() -> datetime | None:
    _init()
    v = db.get_state("exec_snooze_until")
    try:
        d = datetime.fromisoformat(v) if v else None
    except ValueError:
        return None
    return d if d and d > datetime.now() else None


def snooze(hours: float) -> datetime:
    """Hold non-critical notifications for `hours` (0 = cancel the snooze). Critical still go out."""
    _init()
    until = datetime.now() + timedelta(hours=max(0.0, min(float(hours), 72.0)))
    db.set_state("exec_snooze_until", until.isoformat(timespec="seconds") if hours > 0 else "")
    return until


# ------------------------------------------------------------------ events

_SAFE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{8,}\d")


def scrub(text: str) -> str:
    """No secrets, emails or phone numbers in notification payloads / spoken text."""
    t = conf.SECRET_RE.sub("[redacted]", str(text or ""))
    return _SAFE.sub("[contact on the dashboard]", t)


def _row(r) -> dict:
    d = dict(r)
    d["delivery"] = json.loads(d.get("delivery") or "{}")
    d["approval"] = bool(d.get("approval"))
    return d


def emit(source: str, etype: str, severity: str, summary: str, *, detail: str = "", ref: str = "",
         related: str = "", action: str = "", approval: bool = False, dedupe_key: str = "") -> dict:
    """Record an event from a Helios subsystem. Returns {"status": new|repeat|off, "event": {...}}.
    The same dedupe key, while unacknowledged and recent (7 days), counts up instead of creating a
    new event — so a failing check that is seen every night notifies once."""
    if not cfg().get("enabled", True):
        return {"status": "off", "event": None}
    _init()
    severity = severity.upper() if severity and severity.upper() in SEVERITIES else "NORMAL"
    key = dedupe_key or f"{source}:{etype}:{related or summary[:80]}"
    now = _now()
    since = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    with db._conn() as c:
        old = c.execute("SELECT * FROM exec_events WHERE dedupe_key=? AND acknowledged IS NULL AND last_seen>=? "
                        "ORDER BY ts DESC LIMIT 1", (key, since)).fetchone()
        if old:
            sev = min(old["severity"], severity, key=SEVERITIES.index)          # escalation sticks
            c.execute("UPDATE exec_events SET count=count+1, last_seen=?, severity=?, summary=?, detail=? WHERE id=?",
                      (now, sev, scrub(summary)[:300], scrub(detail)[:4000], old["id"]))
            row = c.execute("SELECT * FROM exec_events WHERE id=?", (old["id"],)).fetchone()
            return {"status": "repeat", "event": _row(row)}
        eid = f"ev-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}"
        c.execute("INSERT INTO exec_events(id, ts, last_seen, source, type, severity, summary, detail, ref, related, "
                  "action, approval, dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (eid, now, now, source[:40], etype[:40], severity, scrub(summary)[:300], scrub(detail)[:4000],
                   ref[:400], str(related)[:80], scrub(action)[:300], int(bool(approval)), key))
        row = c.execute("SELECT * FROM exec_events WHERE id=?", (eid,)).fetchone()
    conf.log("executive", f"event {eid} [{severity}] {source}/{etype}: {summary[:100]}")
    return {"status": "new", "event": _row(row)}


def events(*, since: str | None = None, until: str | None = None, source: str | None = None,
           severity: str | None = None, unacked: bool = False, limit: int = 200) -> list[dict]:
    _init()
    q, a = "SELECT * FROM exec_events WHERE 1=1", []
    if since:
        q += " AND last_seen>=?"; a.append(since)
    if until:
        q += " AND ts<=?"; a.append(until)
    if source:
        q += " AND source=?"; a.append(source)
    if severity:
        q += " AND severity=?"; a.append(severity.upper())
    if unacked:
        q += " AND acknowledged IS NULL"
    q += " ORDER BY last_seen DESC LIMIT ?"; a.append(int(limit))
    with db._conn() as c:
        return [_row(r) for r in c.execute(q, a).fetchall()]


def acknowledge(ids: list[str] | None = None) -> int:
    """Mark events reviewed (all unacknowledged ones when ids is None)."""
    _init()
    with db._conn() as c:
        if ids is None:
            cur = c.execute("UPDATE exec_events SET acknowledged=? WHERE acknowledged IS NULL", (_now(),))
        else:
            cur = c.execute(f"UPDATE exec_events SET acknowledged=? WHERE acknowledged IS NULL AND id IN "
                            f"({','.join('?' * len(ids))})", (_now(), *ids)) if ids else None
    return cur.rowcount if cur else 0


def _set(eid: str, **fields) -> None:
    if "delivery" in fields:
        fields["delivery"] = json.dumps(fields["delivery"])
    with db._conn() as c:
        c.execute(f"UPDATE exec_events SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), eid))


# ------------------------------------------------------------------ channels + routing

def _deliver(text: str, title: str, channels: list[str], publish=None) -> dict:
    """Send one message on each channel; returns per-channel status (never raises)."""
    out = {}
    for ch in channels:
        at = _now()
        try:
            if ch == "dashboard":
                if publish:
                    publish("status", f"{title}: {text}"[:600])
                    publish("dashboard", {"event": "exec", "title": title, "text": text[:600]})
                    out[ch] = {"status": "delivered", "at": at}
                else:
                    out[ch] = {"status": "unavailable", "at": at, "detail": "dashboard not connected"}
            elif ch == "toast":
                from . import notify
                notify.toast(title, text[:250])
                out[ch] = {"status": "delivered", "at": at}
            elif ch == "telegram":
                from . import channels as chans
                ok, detail = chans.telegram_send(f"{title}\n{text}")
                out[ch] = {"status": "delivered" if ok else ("unavailable" if "isn't configured" in detail else "failed"),
                           "at": at, **({} if ok else {"detail": detail[:160]})}
        except Exception as e:
            out[ch] = {"status": "failed", "at": at, "detail": str(e)[:160]}
    return out


def route(now: datetime | None = None, publish=None) -> dict:
    """Deliver what's due. Called every scheduler tick. Returns counts (for tests / logs)."""
    c = cfg()
    if not c.get("enabled", True):
        return {"off": True}
    _init()
    now = now or datetime.now()
    quiet, snz = in_quiet_hours(now, c), snoozed_until()
    immediate = {s.upper() for s in (c.get("immediate") or ["CRITICAL", "HIGH"])}
    chans = [ch for ch in (c.get("channels") or ["dashboard"]) if ch in ("dashboard", "toast", "telegram")]
    done = {"delivered": 0, "batched": 0, "held": 0, "calls": 0}
    with db._conn() as cx:
        pending = [_row(r) for r in cx.execute(
            "SELECT * FROM exec_events WHERE state IN ('new','held') AND acknowledged IS NULL ORDER BY ts").fetchall()]
    for ev in pending:
        sev = ev["severity"]
        if sev not in immediate:
            _set(ev["id"], state="batched")
            done["batched"] += 1
            continue
        if sev != "CRITICAL" and (quiet or snz):
            if ev["state"] != "held":
                _set(ev["id"], state="held")
            done["held"] += 1
            continue
        text = ev["summary"] + (f" — {ev['action']}" if ev["action"] else "")
        delivery = _deliver(text, f"Helios · {sev.title()}", chans, publish)
        _set(ev["id"], state="delivered", delivery={**ev["delivery"], **delivery})
        done["delivered"] += 1
        try:                                   # SMS: recipients who want this category; owner fallback
            from . import sms                  # when no remote channel (Telegram) reached them
            remote_ok = (delivery.get("telegram") or {}).get("status") == "delivered"
            sms.notify_event(ev, other_channels_failed=not remote_ok)
        except Exception as e:  # pragma: no cover
            conf.log("executive", f"sms routing failed: {e}")
        from . import calling
        if calling.wants_call_for(ev):
            res = calling.schedule(reason=ev["summary"], event_id=ev["id"], critical=sev == "CRITICAL")
            done["calls"] += res.get("status") == "scheduled"
    return done


# ------------------------------------------------------------------ data collectors

def _period(kind: str, now: datetime) -> tuple[datetime, datetime]:
    if kind == "weekly":
        return now - timedelta(days=7), now
    if kind == "night":
        from .briefing import last_night_record
        rec = last_night_record(now) or {}
        try:
            return datetime.fromisoformat(rec["window"][0]), now
        except Exception:
            return now - timedelta(hours=12), now
    if kind == "midday":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    if kind == "eod":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    return now - timedelta(hours=24), now


def business(since: datetime, until: datetime | None = None) -> dict:
    """The business pipeline in a period, from the prospect store (the existing lead database)."""
    from .prospecting import scoring, store
    s = since.isoformat(timespec="seconds")
    u = (until or datetime.now()).isoformat(timespec="seconds")
    rows = store.list_prospects(limit=5000, order="new")

    def inp(v):
        return bool(v) and s <= str(v) <= u
    disc = [p for p in rows if inp(p.get("discovered_at"))]
    res = [p for p in rows if inp(p.get("last_researched"))]
    live = ("QUALIFIED", "DRAFTED", "APPROVED")
    qual = [p for p in res if p["status"] in live or p["status"] in ("CONTACTED", "REPLIED", "INTERESTED")]
    events_in = [(p, e) for p in rows for e in (p.get("contact_log") or []) if inp(e.get("at"))]
    proposals = 0
    try:
        from .prospecting import proposal
        pdir = proposal.folder()
        proposals = sum(1 for f in pdir.glob("*.md") if inp(datetime.fromtimestamp(f.stat().st_mtime).isoformat())) \
            if pdir.exists() else 0
    except Exception:
        pass
    top = sorted([p for p in qual if p["status"] in live],
                 key=lambda p: ({"A": 0, "B": 1, "C": 2}.get(p.get("priority") or "C", 3), -(p.get("ai_score") or 0)))[:5]
    return {
        "discovered": len(disc), "researched": len(res), "qualified": len(qual),
        "no_website_verified": sum(1 for p in res if p.get("website_status") == "none"),
        "no_website_unverified": sum(1 for p in disc if p.get("website_status") in ("unlisted", "unchecked")
                                     and not p.get("website")),
        "redesign": sum(1 for p in res if p.get("website_score") in (2, 3)),
        "strong_ai": sum(1 for p in res if (p.get("ai_score") or 0) >= 3),
        "audits": sum(1 for p in rows if inp(p.get("last_site_check")) and p.get("website_status") == "ok"),
        "drafted": sum(1 for p in rows if p["status"] == "DRAFTED"),
        "needs_pricing": sum(1 for p in rows if p["status"] in live
                             and (p.get("quote") or {}).get("status") == "NEEDS USER PRICING"),
        "approved_in_period": sum(1 for _, e in events_in if e.get("event") == "approved"),
        "contacted_in_period": sum(1 for _, e in events_in if e.get("event") == "contacted"),
        "replies": sum(1 for p in rows if p["status"] in ("REPLIED", "INTERESTED", "PROPOSAL_ACCEPTED", "WON")
                       and inp(p.get("updated_at"))),
        "won": sum(1 for p in rows if p["status"] == "WON" and inp(p.get("updated_at"))),
        "rejected": sum(1 for p in rows if p["status"] in ("REJECTED", "ARCHIVED") and inp(p.get("updated_at"))),
        "proposals_written": proposals,
        "top": [{"id": p["id"], "name": p["name"], "category": scoring.label(p.get("category") or ""),
                 "area": (p.get("area") or "").split(",")[0], "priority": p.get("priority") or "",
                 "why": ((p.get("reasons") or {}).get("website") or (p.get("reasons") or {}).get("ai") or [{}])[0].get("factor", ""),
                 "evidence": ((p.get("reasons") or {}).get("website") or [{}])[0].get("evidence", "") or p.get("source_url", ""),
                 "package": scoring.PACKAGES.get(p.get("package") or "", ""),
                 "price": (p.get("quote") or {}).get("display") or (p.get("quote") or {}).get("status", ""),
                 "status": p["status"]} for p in top],
    }


def decisions() -> list[dict]:
    """Everything waiting for the owner: drafts to approve, quotes needing a price, lessons to review,
    acknowledged-but-unresolved approvals from the event queue."""
    out = []
    try:
        from .prospecting import engine, store
        n = len(store.list_prospects(status="DRAFTED", limit=1000))
        if n:
            out.append({"what": f"{n} outreach draft{'s' if n != 1 else ''} to approve or reject",
                        "how": "dashboard → Leads & Business, or `helios prospects list DRAFTED`"})
        np_ = len(engine.needs_pricing())
        if np_:
            out.append({"what": f"{np_} quote{'s' if np_ != 1 else ''} need a price in your rate card",
                        "how": "[prospecting.pricing] in settings.toml"})
    except Exception as e:
        out.append({"what": f"couldn't read the prospect queue ({e})", "how": ""})
    try:
        from . import memory_store
        n = len(memory_store.pending())
        if n:
            out.append({"what": f"{n} learned rule/skill{'s' if n != 1 else ''} waiting for approval",
                        "how": "`helios learn review`"})
    except Exception:
        pass
    for ev in events(unacked=True, limit=50):
        if ev["approval"]:
            out.append({"what": ev["summary"], "how": ev["action"] or "", "event": ev["id"]})
    return out


def _safe(name: str, fn, rep: dict):
    """Run one collector; a failure is reported (never hidden) and the report continues."""
    try:
        return fn()
    except Exception as e:
        rep["sections"]["failed"].append(f"couldn't read {name}: {type(e).__name__}: {e}"[:200])
        conf.log("executive", f"collector {name} failed: {e}")
        return None


def build(kind: str, now: datetime | None = None) -> dict:
    """Assemble a report (midday | eod | night | weekly; morning = the briefing + business)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    now = now or datetime.now()
    since, until = _period(kind, now)
    rep = {"kind": kind, "since": since.isoformat(timespec="minutes"), "until": until.isoformat(timespec="minutes"),
           "sections": {k: [] for k in ("completed", "attempted", "partial", "failed", "skipped", "verify",
                                        "suggested", "approval")}, "metrics": {}}
    sec = rep["sections"]
    b = _safe("the business pipeline", lambda: business(since, until), rep) or {}
    rep["business"] = b
    if b:
        if b["discovered"]:
            sec["completed"].append(f"discovered {b['discovered']} businesses (OpenStreetMap)")
        if b["researched"]:
            sec["completed"].append(f"researched {b['researched']} — {b['qualified']} qualified")
        if b["audits"]:
            sec["completed"].append(f"audited {b['audits']} websites")
        if b["proposals_written"]:
            sec["completed"].append(f"wrote {b['proposals_written']} proposal(s) to the vault")
        if b["no_website_unverified"]:
            sec["verify"].append(f"{b['no_website_unverified']} businesses list no website in OSM — not yet "
                                 "confirmed by a web search, so not counted as 'no website'")
        if b["needs_pricing"]:
            sec["approval"].append(f"{b['needs_pricing']} quote(s) need pricing")
        if b["drafted"]:
            sec["approval"].append(f"{b['drafted']} outreach draft(s) await your approval — nothing has been sent")
    research = _safe("the research library", lambda: _research(since, until), rep) or {}
    rep["research"] = research
    if research.get("new"):
        sec["completed"].append(f"{research['new']} new research finding(s) with sources")
    leads = _safe("the freelance lead library", lambda: _leads(since, until), rep) or {}
    rep["leads"] = leads
    if leads.get("new"):
        sec["completed"].append(f"{leads['new']} freelance lead(s) found (pitch drafts, not sent)")
    jobs_ = _safe("job history", lambda: _jobs(since, until), rep) or {}
    rep["jobs"] = jobs_
    for j in jobs_.get("ok", []):
        sec["completed"].append(f"job {j}")
    for j in jobs_.get("failed", []):
        sec["failed"].append(f"job {j}")
    for j in jobs_.get("skipped", []):
        sec["skipped"].append(f"job {j}")
    if kind in ("night", "morning", "weekly", "eod"):
        night = _safe("the Night Mode record", lambda: _night(now), rep) or {}
        rep["night"] = night
        for t in night.get("ok", []):
            sec["completed"].append(f"Night Mode: {t}")
        for t in night.get("failed", []):
            sec["failed"].append(f"Night Mode: {t}")
        for t in night.get("skipped", []):
            sec["skipped"].append(f"Night Mode: {t}")
        for t in night.get("partial", []):
            sec["partial"].append(f"Night Mode: {t}")
    if kind in ("eod", "weekly", "night"):
        projects = _safe("project health", _projects, rep) or []
        rep["projects"] = projects
        for p in projects:
            if p["verdict"] == "FAILING":
                sec["failed"].append(f"project {p['project']}: failing — {', '.join(p['issues'][:2])}")
            elif p["verdict"] == "ATTENTION":
                sec["suggested"].append(f"project {p['project']} needs attention: {', '.join(p['issues'][:2])}")
    mem = _safe("memory", lambda: _memory(since, until), rep) or {}
    rep["memory"] = mem
    if mem.get("new"):
        sec["completed"].append(f"saved {mem['new']} new memory item(s)")
    for d in _safe("pending decisions", decisions, rep) or []:
        if d["what"] not in " ".join(sec["approval"]):
            sec["approval"].append(d["what"] + (f" ({d['how']})" if d.get("how") else ""))
    ev = [e for e in events(since=rep["since"], limit=200) if e["state"] in ("batched", "held", "delivered")]
    rep["digest"] = [{"id": e["id"], "severity": e["severity"], "summary": e["summary"], "count": e["count"],
                      "source": e["source"]} for e in ev if e["severity"] in ("NORMAL", "LOW", "HIGH")][:20]
    for k, svc in (("gmail", _gmail), ("polarion", _polarion)):
        msg = _safe(k, svc, rep)
        if msg:
            sec["skipped" if msg.startswith("not") else "completed"].append(f"{k}: {msg}")
    if kind == "weekly":
        rep["metrics"] = _weekly_metrics(b, research, rep)
        sec["suggested"] += _recommendations(b, rep)
    rep["meaningful"] = any(sec[k] for k in ("completed", "failed", "partial", "approval")) or bool(rep["digest"])
    rep["text"] = render(rep)
    rep["spoken"] = spoken(rep)
    return rep


def _research(since, until) -> dict:
    from . import research
    s, u = since.isoformat(), until.isoformat()
    f = [x for x in research.findings(limit=500) if s <= (x.get("found") or "") <= u]
    return {"new": len(f), "top": [{"title": x.get("title", ""), "topic": x.get("topic", ""),
                                    "source": x.get("source_url", "")} for x in f[:5]]}


def _leads(since, until) -> dict:
    from . import leads
    s, u = since.isoformat(), until.isoformat()
    f = [x for x in leads.all_leads() if s <= (x.get("found") or "") <= u]
    return {"new": len(f), "top": [{"title": x.get("title", ""), "source": x.get("source_url", "")} for x in f[:5]]}


def _jobs(since, until) -> dict:
    from . import jobs
    s, u = since.isoformat(), until.isoformat()
    out = {"ok": [], "failed": [], "skipped": []}
    for r in jobs.history(limit=200):
        if not (s <= (r.get("started_at") or "") <= u) or r.get("name") == "night_mode":
            continue
        st = r.get("status") or ""
        line = f"{r.get('name')}: {(r.get('summary') or st)[:120]}"
        (out["ok"] if st == "ok" else out["failed"] if st in ("failed", "interrupted") else out["skipped"]).append(line)
    return out


def _night(now) -> dict:
    from .briefing import last_night_record
    rec = last_night_record(now) or {}
    out = {"status": rec.get("status") or "none", "ok": [], "failed": [], "skipped": [], "partial": [],
           "report": rec.get("report", "")}
    if rec.get("status") == "missed":
        out["failed"].append(f"didn't run — {rec.get('reason', '')}")
    for name, t in (rec.get("tasks") or {}).items():
        line = f"{name.replace('_', ' ')} — {t.get('summary', '')}"[:200]
        st = t.get("status")
        (out["ok"] if st == "ok" else out["failed"] if st == "failed" else out["skipped"]).append(line)
    for a, z, n in rec.get("slept") or []:
        out["partial"].append(f"{n.replace('_', ' ')}: the PC was asleep {a[11:16]}–{z[11:16]}")
    return out


def _projects() -> list[dict]:
    from . import health
    return [{"project": r["project"], "verdict": r["verdict"], "issues": r["issues"][:3]} for r in health.reports(None)]


def _memory(since, until) -> dict:
    from . import memory_store
    s, u = since.isoformat(), until.isoformat()
    items = [i for i in memory_store.items() if s <= (i.get("created") or "") <= u and memory_store.is_active(i)]
    return {"new": len(items), "items": [f"[{i['category']}] {i['text'][:120]}" for i in items[:5]]}


def _gmail() -> str:
    from . import gmail
    if not gmail.signed_in():
        return "not connected (helios gmail login)"
    return "connected"


def _polarion() -> str:
    from . import polarion
    ok = [i for i in polarion.INSTANCES if polarion.configured(i)[0]]
    return f"read-only access: {', '.join(ok)}" if ok else "not configured (helios polarion token local)"


def _weekly_metrics(b: dict, research: dict, rep: dict) -> dict:
    cov = []
    try:
        from .prospecting import store
        cov = [{"area": r["area"].split(",")[0], "done": r["done"] or 0, "cells": r["cells"] or 0}
               for r in store.coverage()]
    except Exception:
        pass
    return {"businesses_discovered": b.get("discovered", 0), "researched": b.get("researched", 0),
            "qualified": b.get("qualified", 0), "no_website_verified": b.get("no_website_verified", 0),
            "redesign_opportunities": b.get("redesign", 0), "strong_ai": b.get("strong_ai", 0),
            "proposals_written": b.get("proposals_written", 0), "approved": b.get("approved_in_period", 0),
            "contacted": b.get("contacted_in_period", 0), "replies": b.get("replies", 0), "won": b.get("won", 0),
            "rejected_or_archived": b.get("rejected", 0), "research_findings": research.get("new", 0),
            "coverage": cov,
            "revenue": "not tracked — no verified sales records are configured (leads and proposals are not revenue)"}


def _recommendations(b: dict, rep: dict) -> list[str]:
    out = []
    if b.get("needs_pricing"):
        out.append("add prices for the packages your quotes keep needing ([prospecting.pricing])")
    if b.get("drafted", 0) >= 3:
        out.append(f"review the {b['drafted']} drafts waiting — they go stale as businesses change")
    for c in (rep.get("metrics") or {}).get("coverage", []):
        if c["cells"] and c["done"] < c["cells"]:
            out.append(f"{c['area']}: {c['cells'] - c['done']} of {c['cells']} OSM cells still unscanned")
    night = rep.get("night") or {}
    if any("battery" in s for s in night.get("skipped", [])):
        out.append("keep the laptop plugged in overnight — heavy Night Mode tasks were skipped on battery")
    return out


# ------------------------------------------------------------------ rendering

_TITLES = {"morning": "Morning briefing", "midday": "Midday progress report", "eod": "End-of-day report",
           "night": "Night Mode report", "weekly": "Weekly business review"}


def render(rep: dict) -> str:
    b = rep.get("business") or {}
    out = [f"# {_TITLES[rep['kind']]} — {rep['until'][:16].replace('T', ' ')}", "",
           f"_Period: {rep['since'][:16].replace('T', ' ')} → {rep['until'][:16].replace('T', ' ')}. Every number below "
           "comes from Helios's records; nothing has been sent to anyone._", ""]
    if b:
        out += ["## Business pipeline", "",
                f"- Discovered: {b['discovered']} · researched: {b['researched']} · qualified: {b['qualified']}",
                f"- No website (verified by OSM + web search): {b['no_website_verified']} · redesign opportunities: "
                f"{b['redesign']} · strong AI opportunities: {b['strong_ai']}",
                f"- Drafts awaiting approval: {b['drafted']} · quotes needing pricing: {b['needs_pricing']} · "
                f"proposals written: {b['proposals_written']}",
                f"- Approved: {b['approved_in_period']} · contacted (verified): {b['contacted_in_period']} · "
                f"replies: {b['replies']}", ""]
        if b.get("top"):
            out += ["### Best opportunities", ""]
            out += [f"- **{t['name']}** ({t['category']}, {t['area']}) — priority {t['priority'] or '?'}: {t['why'] or '—'}; "
                    f"{t['package'] or 'no package'}; {t['price'] or '—'} · evidence: {t['evidence'] or '—'} · "
                    f"`helios prospects show {t['id']}`" for t in b["top"]]
            out.append("")
    if rep.get("metrics"):
        m = rep["metrics"]
        out += ["## Week in numbers", ""] + [f"- {k.replace('_', ' ')}: {v}" for k, v in m.items() if k != "coverage"]
        out += [f"- coverage {c['area']}: {c['done']}/{c['cells']} cells" for c in m.get("coverage", [])] + [""]
    titles = {"completed": "Completed", "attempted": "Attempted", "partial": "Partly done", "failed": "Failed",
              "skipped": "Skipped (missing configuration or conditions)", "verify": "Needs verification",
              "suggested": "Suggested", "approval": "Waiting for your decision"}
    for k, t in titles.items():
        items = rep["sections"].get(k) or []
        if items:
            out += [f"## {t}", ""] + [f"- {i}" for i in items[:15]] + [""]
    if rep.get("digest"):
        out += ["## Events since the last report", ""]
        out += [f"- [{d['severity']}] {d['summary']}" + (f" (×{d['count']})" if d["count"] > 1 else "") for d in rep["digest"]]
        out.append("")
    if not rep.get("meaningful"):
        out += ["_Nothing meaningful changed in this period._", ""]
    return "\n".join(out)


def spoken(rep: dict) -> str:
    """Executive-summary voice version: the headline numbers, the problems, the decisions, then a
    question — no ids, links, emails or phone numbers."""
    b, sec = rep.get("business") or {}, rep["sections"]
    lead = {"midday": "Here's your midday update", "eod": "Here's your end-of-day report",
            "night": "Night Mode has finished", "weekly": "Here's your weekly business review",
            "morning": "Good morning. Here's your Helios briefing"}[rep["kind"]]
    parts = [f"{lead}, sir."]
    did = []
    if b.get("discovered"):
        did.append(f"found {b['discovered']} businesses")
    if b.get("qualified"):
        did.append(f"qualified {b['qualified']}")
    if b.get("audits"):
        did.append(f"audited {b['audits']} websites")
    r = rep.get("research") or {}
    if r.get("new"):
        did.append(f"added {r['new']} research findings")
    if did:
        parts.append("I " + ", ".join(did[:-1]) + (" and " if len(did) > 1 else "") + did[-1] + ".")
    elif not rep.get("meaningful"):
        parts.append("Nothing meaningful changed since the last report.")
    if b.get("top"):
        t = b["top"][0]
        parts.append(f"The strongest opportunity is {t['name']}, a {t['category']}: {t['why'] or 'a clear gap'}.")
    if b.get("drafted"):
        parts.append(f"{b['drafted']} draft{'s are' if b['drafted'] != 1 else ' is'} ready for your review. Nothing has been sent.")
    if sec["failed"]:
        parts.append(f"{len(sec['failed'])} thing{'s' if len(sec['failed']) != 1 else ''} failed, "
                     f"including {sec['failed'][0].split(' — ')[0]}.")
    if sec["skipped"]:
        parts.append(f"{len(sec['skipped'])} task{'s were' if len(sec['skipped']) != 1 else ' was'} skipped.")
    n_dec = len(sec["approval"])
    if n_dec:
        parts.append(f"I need your decision on {n_dec} item{'s' if n_dec != 1 else ''}.")
    if b.get("top") and (sec["failed"] or n_dec):
        parts.append("Would you like the business report or the problems first?")
    return scrub(" ".join(parts))


# ------------------------------------------------------------------ saving, delivery, schedule

def folder() -> Path:
    from . import memory_store
    return memory_store.vault() / "Executive Reports"


def save(rep: dict) -> dict:
    from .night_mode.common import safe_write
    _init()
    path = safe_write(folder() / f"{rep['until'][:10]}-{rep['kind']}.md", rep["text"])
    with db._conn() as c:
        cur = c.execute("INSERT INTO exec_reports(kind, period_start, period_end, created, path, summary, spoken) "
                        "VALUES(?,?,?,?,?,?,?)", (rep["kind"], rep["since"], rep["until"], _now(), str(path),
                                                   rep["spoken"][:400], rep["spoken"]))
        rid = cur.lastrowid
        if rep.get("digest"):
            ids = [d["id"] for d in rep["digest"]]
            c.execute(f"UPDATE exec_events SET state='reported' WHERE id IN ({','.join('?' * len(ids))})"
                      f" AND state IN ('batched','held')", ids)
    return {"id": rid, "path": str(path)}


def record(kind: str, since: datetime, until: datetime, path: str, spoken_text: str) -> int:
    """Register a report produced elsewhere (the morning briefing) so it shows in the history."""
    _init()
    with db._conn() as c:
        cur = c.execute("INSERT INTO exec_reports(kind, period_start, period_end, created, path, summary, spoken, "
                        "delivery) VALUES(?,?,?,?,?,?,?,?)",
                        (kind, since.isoformat(timespec="minutes"), until.isoformat(timespec="minutes"), _now(), path,
                         scrub(spoken_text)[:400], scrub(spoken_text),
                         json.dumps({"dashboard": {"status": "delivered", "at": _now()}})))
    return cur.lastrowid


def reports(*, since: str | None = None, limit: int = 30) -> list[dict]:
    _init()
    with db._conn() as c:
        q, a = "SELECT * FROM exec_reports", []
        if since:
            q += " WHERE created>=?"; a.append(since)
        rows = c.execute(q + " ORDER BY id DESC LIMIT ?", (*a, int(limit))).fetchall()
    return [{**dict(r), "delivery": json.loads(r["delivery"] or "{}")} for r in rows]


def deliver_report(kind: str, now: datetime | None = None, publish=None, *, build_fn=None) -> dict:
    """Build, save and deliver one report on the configured channels (+ a call if enabled for it)."""
    rep = (build_fn or build)(kind, now)
    c = cfg()
    if kind == "midday" and c.get("skip_quiet_midday", True) and not rep["meaningful"]:
        conf.log("executive", "midday report skipped — nothing meaningful changed")
        return {"status": "skipped", "report": rep}
    saved = save(rep)
    chans = [ch for ch in (c.get("channels") or ["dashboard"]) if ch in ("dashboard", "toast", "telegram")]
    delivery = _deliver(rep["spoken"], f"Helios · {_TITLES[kind]}", chans, publish)
    with db._conn() as cx:
        cx.execute("UPDATE exec_reports SET delivery=? WHERE id=?", (json.dumps(delivery), saved["id"]))
    from . import calling
    call = calling.schedule(reason=_TITLES[kind], report_kind=kind) if calling.wants_call_for_report(kind) else None
    try:                                       # short SMS notice, counts taken from the report's own sections
        from . import sms
        sec = rep["sections"]
        if kind == "night":
            sms.notify("night_mode", f"{len(sec['completed'])} completed, {len(sec['failed'])} failed, "
                                     f"{len(sec['skipped'])} skipped", dedupe_key=f"report:night:{rep['until'][:10]}")
        else:
            sms.notify("reports", _TITLES[kind].lower(), dedupe_key=f"report:{kind}:{rep['until'][:13]}")
    except Exception as e:  # pragma: no cover
        conf.log("executive", f"sms report notice failed: {e}")
    return {"status": "delivered", "report": rep, "saved": saved, "delivery": delivery, "call": call}


def _due_today(key: str, at: tuple[int, int], now: datetime) -> bool:
    if now < now.replace(hour=at[0], minute=at[1], second=0, microsecond=0):
        return False
    return db.get_state(f"exec_done_{key}") != now.date().isoformat()


def tick(now: datetime | None = None, publish=None) -> list[str]:
    """Scheduler hook (every tick): route events, then deliver any report that's due — once per day
    (weekly: once per week), catching up after sleep but never backfilling old days."""
    now = now or datetime.now()
    c = cfg()
    done = []
    if not c.get("enabled", True):
        return done
    _init()
    try:
        route(now, publish)
    except Exception as e:  # pragma: no cover
        conf.log("executive", f"route failed: {e}")
    plan = []
    if c.get("midday_report", True):
        plan.append(("midday", _hm(c.get("midday_time"), "13:00")))
    if c.get("end_of_day_report", True):
        plan.append(("eod", _hm(c.get("eod_time"), "18:30")))
    for kind, at in plan:
        late = now - now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        if _due_today(kind, at, now) and late < timedelta(hours=4):      # don't send a midday report at night
            db.set_state(f"exec_done_{kind}", now.date().isoformat())
            deliver_report(kind, now, publish)
            done.append(kind)
    if c.get("weekly_review", True):
        day = str(c.get("weekly_day") or "mon").lower()[:3]
        at = _hm(c.get("weekly_time"), "09:00")
        week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]}"
        if now.strftime("%a").lower() == day and now >= now.replace(hour=at[0], minute=at[1]) \
                and db.get_state("exec_done_weekly") != week:
            db.set_state("exec_done_weekly", week)
            deliver_report("weekly", now, publish)
            done.append("weekly")
    try:
        from . import calling
        calling.tick(now, publish)
    except Exception as e:  # pragma: no cover
        conf.log("executive", f"calling tick failed: {e}")
    try:
        from . import sms
        sms.tick(now)
    except Exception as e:  # pragma: no cover
        conf.log("executive", f"sms tick failed: {e}")
    return done


def on_night_finished(rec: dict, publish=None) -> None:
    """Night Mode calls this when its run ends: one event (HIGH if something failed) + the report."""
    tasks = rec.get("tasks") or {}
    failed = [n for n, t in tasks.items() if t.get("status") == "failed"]
    skipped = [n for n, t in tasks.items() if t.get("status") == "skipped"]
    sev = "HIGH" if failed or rec.get("status") in ("crashed", "missed") else "NORMAL"
    summary = (f"Night Mode {rec.get('status')}: {len(tasks)} task(s)" + (f", {len(failed)} failed" if failed else "")
               + (f", {len(skipped)} skipped" if skipped else ""))
    emit("night_mode", "night_finished", sev, summary, ref=str(rec.get("report", "")),
         related=str(rec.get("night", "")), dedupe_key=f"night:{rec.get('night')}")
    if cfg().get("night_mode_report", True) and rec.get("kind") == "scheduled":
        try:
            deliver_report("night", publish=publish)
        except Exception as e:  # pragma: no cover
            conf.log("executive", f"night report failed: {e}")


def lead_event(p: dict) -> None:
    """The prospecting engine calls this when a business qualifies. Priority A with a verified public
    contact → HIGH (prompt notification); others → NORMAL (next digest). Never authorises contact."""
    try:
        from .prospecting import scoring
        q = p.get("quote") or {}
        why = ((p.get("reasons") or {}).get("website") or (p.get("reasons") or {}).get("ai") or [{}])[0]
        high = (p.get("priority") or "") == str(cfg().get("lead_alert_priority") or "A") and \
            bool(p.get("email") or p.get("phone"))
        price = q.get("display") or ("needs pricing" if q.get("status") == "NEEDS USER PRICING" else "—")
        summary = (f"Lead: {p['name']} ({scoring.label(p.get('category') or '')}, {(p.get('area') or '').split(',')[0]}) — "
                   f"{why.get('factor', 'opportunity')}; {scoring.PACKAGES.get(p.get('package') or '', 'no package')}; {price}")
        emit("prospecting", "lead_qualified", "HIGH" if high else "NORMAL", summary,
             detail=f"why: {why.get('detail', '')}\nevidence: {why.get('evidence') or p.get('source_url', '')}",
             related=f"prospect:{p['id']}", approval=p.get("status") == "DRAFTED",
             action=("review and approve the draft (nothing is sent)" if p.get("status") == "DRAFTED" else "review")
             + ("; add a price for this package" if q.get("status") == "NEEDS USER PRICING" else ""),
             dedupe_key=f"lead:{p['id']}")
    except Exception as e:  # pragma: no cover
        conf.log("executive", f"lead event failed: {e}")
