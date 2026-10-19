"""Read-only data for the dashboard UI (docs/HELIOS_DASHBOARD_UI.md).

Every number the dashboard shows comes from here, and every value here comes from a real Helios
source: the prospect store, the usage log, the research / lead libraries, projects, Night Mode, the
job scheduler, settings, and the voice daemon's own state pushes. Nothing is estimated or invented.
When a source doesn't expose something (the provider's quota, a model's context window), the field
is None with a reason, and the UI says "not exposed".

All functions are cheap and never raise. The only slow one (`agy models`, which checks that
Antigravity is reachable) runs in a background thread and is cached.
"""

from __future__ import annotations

import importlib.util
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import conf

ENGINE_LABEL = {"antigravity": "Antigravity CLI (agy)", "claude": "Claude Code CLI",
                "gemini": "Gemini CLI", "lite": "Lite (OpenAI-compatible API)"}
QUOTA_WHY = {
    "antigravity": "The agy CLI has no usage or quota command (subcommands: agent, models, mcp, "
                   "plugin, update…), and Helios never reads private APIs or tokens.",
    "claude": "Claude Code's print mode reports per-turn tokens, not your plan's limits.",
    "gemini": "The Gemini CLI doesn't report plan quota to other programs.",
    "lite": "The configured API doesn't report a quota to Helios.",
}


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:
        conf.log("dashboard", f"{getattr(fn, '__name__', 'call')} failed: {type(e).__name__}: {e}")
        return default


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ voice (pushed by the daemon)

_voice: dict = {"state": None, "at": None, "followup": False, "wake": None, "wake_reason": ""}
_voice_lock = threading.Lock()


def note_voice(data: dict) -> None:
    """Remember the latest voice-daemon state (server.py calls this for every /voice/state push), so
    a freshly opened dashboard shows the real state instead of waiting for the next event."""
    with _voice_lock:
        st = str(data.get("state") or "")
        if st:
            _voice["state"] = st
            _voice["at"] = _now()
            _voice["followup"] = bool(data.get("followup")) if st == "listening" else False
        for k in ("wake", "wake_reason"):
            if k in data:
                _voice[k] = data[k]


def voice_status() -> dict:
    from . import health
    cfg = conf.voice_cfg()
    with _voice_lock:
        v = dict(_voice)
    running = _safe(health._voice_daemon_running, False)
    return {"enabled": bool(cfg.get("enabled", False)), "daemon": running, "state": v["state"] if running else None,
            "followup": v["followup"], "at": v["at"],
            "wake_word": v["wake"] if v["wake"] is not None else (str(cfg.get("wake_word") or "") or None),
            "wake_ready": None if v["wake"] is None else not v["wake_reason"],
            "wake_reason": v["wake_reason"], "stt": str(cfg.get("stt_model") or ""),
            "engine": str((cfg.get("stt") or {}).get("engine", "") if isinstance(cfg.get("stt"), dict) else "")}


# ------------------------------------------------------------------ engine + model limits

_probe: dict = {"at": None, "ok": None, "detail": "not checked yet", "models": [], "running": False}
_probe_lock = threading.Lock()
PROBE_TTL = 600


def _probe_agy() -> None:
    from . import agy_cli
    ok, detail, models = False, "", []
    cmd = agy_cli.command()
    if not cmd:
        detail = "agy not found (set [antigravity].bin)"
    else:
        try:
            r = subprocess.run([*cmd, "models"], capture_output=True, text=True, timeout=45,
                               encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (r.stdout or "") + (r.stderr or "")
            for line in out.splitlines():
                slug, sep, name = line.partition("\t")
                if sep and slug.strip() and " " not in slug.strip():
                    models.append({"id": slug.strip(), "name": name.strip()})
            if models:
                ok, detail = True, f"{len(models)} models available"
            elif agy_cli.looks_unauthenticated(out):
                detail = "not signed in — run agy once in a terminal and sign in"
            else:
                detail = (out.strip().splitlines() or ["no models listed"])[-1][:160]
        except subprocess.TimeoutExpired:
            detail = "agy didn't answer within 45 s"
        except Exception as e:
            detail = f"couldn't run agy: {e}"[:160]
    with _probe_lock:
        _probe.update(at=_now(), ok=ok, detail=detail, models=models, running=False)
    if not ok:
        try:
            from . import executive
            executive.emit("antigravity", "engine_unavailable", "HIGH", f"Antigravity unavailable: {detail}",
                           action="run `agy` once in a terminal (sign-in) or check [antigravity].bin",
                           dedupe_key="agy:unavailable")
        except Exception:
            pass


def engine_health(force: bool = False) -> dict:
    """Is the brain reachable? For Antigravity: a cached `agy models` run (background thread)."""
    eng = conf.brain_engine()
    if eng != "antigravity":
        return {"engine": eng, "ok": None, "detail": "not checked for this engine", "at": None, "models": []}
    with _probe_lock:
        stale = not _probe["at"] or (datetime.now() - datetime.fromisoformat(_probe["at"])).total_seconds() > PROBE_TTL
        if (force or stale) and not _probe["running"]:
            _probe["running"] = True
            threading.Thread(target=_probe_agy, daemon=True, name="dash-agy-probe").start()
        snap = dict(_probe)
    return {"engine": eng, "ok": snap["ok"], "detail": snap["detail"] if not snap["running"] or snap["at"] else "checking…",
            "at": snap["at"], "models": snap["models"], "checking": snap["running"]}


def _configured_models(eng: str) -> list[dict]:
    if eng == "antigravity":
        from . import agy_cli
        c = agy_cli.cfg()
        out = [{"role": "Live chat", "model": str(c.get("model") or "") or "agy default"}]
        for tier in ("light", "medium", "heavy"):
            out.append({"role": f"Background ({tier})", "model": agy_cli.model_for(tier) or "agy default"})
        return out
    sec = conf._section(eng if eng in ("gemini", "claude") else "brain")
    return [{"role": "Configured", "model": str(sec.get("model") or "default")}]


def _usage_rows(days: int) -> list[dict]:
    from . import usage
    return usage._rows(days)


def limits(force: bool = False) -> dict:
    """The Model Limits panel. Provider quota fields are None unless a supported source exposes
    them (none does today); local usage is what Helios itself measured."""
    from . import usage
    eng = conf.brain_engine()
    rows = _safe(lambda: _usage_rows(7), [])
    today = datetime.now().date().isoformat()

    def tot(rs):
        return {"calls": len(rs), "in": sum(int(r.get("in", 0)) for r in rs),
                "out": sum(int(r.get("out", 0)) for r in rs)}
    by: dict = {}
    for r in rows:
        if r.get("ts", "")[:10] == today:
            d = by.setdefault(usage.purpose(r.get("label", "")), {"calls": 0, "tokens": 0})
            d["calls"] += 1
            d["tokens"] += int(r.get("in", 0)) + int(r.get("out", 0))
    health = engine_health(force)
    models = _safe(lambda: _configured_models(eng), [])
    offered = {m["id"] for m in health.get("models") or []}
    for m in models:   # a configured slug the account doesn't offer would fail at run time — say so
        m["offered"] = None if not offered or m["model"] == "agy default" else m["model"] in offered
    return {
        "backend": ENGINE_LABEL.get(eng, eng), "engine": eng,
        "models": models,
        "health": health,
        "provider": {"session": None, "weekly": None, "reset": None, "context_window": None,
                     "why": QUOTA_WHY.get(eng, "Not exposed by the current interface.")},
        "local": {"today": tot([r for r in rows if r.get("ts", "")[:10] == today]), "week": tot(rows),
                  "by_purpose": sorted(({"purpose": k, **v} for k, v in by.items()), key=lambda x: -x["tokens"])[:6],
                  "last_call": max((r.get("ts", "") for r in rows), default=None),
                  "source": "data/usage.jsonl — every AI call Helios makes"},
        "updated": _now(),
    }


# ------------------------------------------------------------------ module status (nav indicators)

def _mod(id_: str, label: str, state: str, detail: str) -> dict:
    return {"id": id_, "label": label, "state": state, "detail": detail}


def modules() -> list[dict]:
    """One truthful indicator per nav item: ok | warn (needs setup / not running) | off | err."""
    out = []
    v = voice_status()
    if not v["enabled"]:
        out.append(_mod("voice", "Voice", "off", "voice is off in settings"))
    elif not v["daemon"]:
        out.append(_mod("voice", "Voice", "warn", "voice daemon not running"))
    elif v["state"] == "off":
        out.append(_mod("voice", "Voice", "warn", "microphone muted"))
    elif v["state"] == "disabled":
        out.append(_mod("voice", "Voice", "err", "no microphone found"))
    elif v["state"] == "dormant":
        out.append(_mod("voice", "Voice", "ok", "asleep — a double clap wakes Helios"
                        + ("" if v.get("wake_ready") else " (wake word not available)")))
    else:
        out.append(_mod("voice", "Voice", "ok", "listening for the wake word" if v.get("wake_ready") else "running"))

    drv = str(conf._section("computer").get("driver_bin") or "")
    out.append(_mod("computer", "Screen & PC Control", "ok" if drv and Path(drv).exists() else "warn",
                    "cua-driver ready" if drv and Path(drv).exists() else "cua-driver not found"))
    pw = importlib.util.find_spec("playwright") is not None
    out.append(_mod("browser", "Browser", "ok" if pw else "warn",
                    "hidden Edge/Chrome via Playwright" if pw else "Playwright not installed"))

    r_on = bool(conf._section("research").get("enabled", False))
    out.append(_mod("research", "Research", "ok" if r_on else "off", "nightly research on" if r_on else "research off"))

    p = conf._section("prospecting")
    if p.get("enabled") and p.get("areas"):
        out.append(_mod("leads", "Leads & Business", "ok", f"prospecting on · {len(p.get('areas') or [])} area(s)"))
    else:
        out.append(_mod("leads", "Leads & Business", "warn",
                        "prospecting off — set [prospecting] enabled + areas" if not p.get("enabled") else "no areas set"))

    def _projects():
        from . import projects
        items, errs = projects.load_all()
        act = [x for x in items if x.get("usable", True) and x.get("active", True)]
        bad = sum(1 for x in act if projects.failing(x))
        if not items:
            return _mod("projects", "Projects", "off", "no projects registered")
        if errs or bad:
            return _mod("projects", "Projects", "warn", f"{bad} failing check(s)" + (f", {len(errs)} manifest error(s)" if errs else ""))
        return _mod("projects", "Projects", "ok", f"{len(act)} active")
    out.append(_safe(_projects, _mod("projects", "Projects", "err", "couldn't read projects")))

    def _memory():
        from . import memory_store
        return _mod("memory", "Memory", "ok" if memory_store.vault().is_dir() else "warn",
                    "Obsidian vault connected" if memory_store.vault().is_dir() else "vault folder missing")
    out.append(_safe(_memory, _mod("memory", "Memory", "err", "couldn't read the vault")))

    def _night():
        from .night_mode import scheduler as night
        if not night.enabled():
            return _mod("night", "Night Mode", "off", "off in settings")
        if night.is_running():
            return _mod("night", "Night Mode", "ok", "running now")
        recent = night.runs(10)
        last = next((r for r in recent if r.get("kind") != "manual"), None)     # the last real NIGHT
        manual = next((r for r in recent if r.get("kind") == "manual"), None)
        newer = manual and (not last or (manual.get("finished") or "") > (last.get("finished") or ""))
        extra = f" · manual run: {manual.get('status')}" if newer else ""
        if last and str(last.get("status", "")).startswith(("failed", "missed", "crashed", "interrupted")):
            return _mod("night", "Night Mode", "warn", f"last night: {last.get('status')}{extra}")
        tasks = (last or {}).get("tasks") or {}
        skipped = sum(1 for t in tasks.values() if t.get("status") == "skipped")
        note = f" ({skipped} of {len(tasks)} tasks skipped)" if skipped else ""
        return _mod("night", "Night Mode", "warn" if skipped else "ok",
                    (f"last night: {last.get('status')}{note}" if last else "no nights yet") + extra)
    out.append(_safe(_night, _mod("night", "Night Mode", "err", "couldn't read Night Mode")))

    def _gmail():
        from . import gmail
        if not conf._section("gmail").get("enabled", False):
            return _mod("gmail", "Gmail", "off", "off in settings")
        return _mod("gmail", "Gmail", "ok", "signed in") if gmail.signed_in() else \
            _mod("gmail", "Gmail", "warn", "not signed in (helios gmail login)")
    out.append(_safe(_gmail, _mod("gmail", "Gmail", "err", "couldn't read Gmail settings")))

    def _polarion():
        from . import polarion
        ok = [i for i in polarion.INSTANCES if polarion.configured(i)[0]]
        return _mod("polarion", "Polarion", "ok" if ok else "warn",
                    f"token set: {', '.join(ok)} (read-only)" if ok else "no token yet (helios polarion token)")
    out.append(_safe(_polarion, _mod("polarion", "Polarion", "err", "couldn't read Polarion settings")))

    internal = (conf.ROOT / "mcp" / "helios_server.py").exists()
    pub = bool(conf._section("mcp_public").get("enabled", False))
    out.append(_mod("mcp", "MCP Server", "ok" if internal else "err",
                    ("internal tools ready" if internal else "internal server missing") + (" · public server on" if pub else "")))
    return out


# ------------------------------------------------------------------ system status

_cpu_primed = False


def system() -> dict:
    global _cpu_primed
    out: dict = {"version": conf.VERSION, "cpu": None, "ram": None, "disks": [], "battery": None}
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        out["cpu"] = cpu if _cpu_primed else None    # the first reading is meaningless (0.0)
        _cpu_primed = True
        m = psutil.virtual_memory()
        out["ram"] = {"pct": m.percent, "used_gb": round(m.used / 2**30, 1), "total_gb": round(m.total / 2**30, 1)}
        for d in ("C:\\", "D:\\"):
            try:
                u = psutil.disk_usage(d)
                out["disks"].append({"drive": d[:2], "free_gb": round(u.free / 2**30, 1), "pct": u.percent})
            except Exception:
                pass
        b = psutil.sensors_battery()
        if b:
            out["battery"] = {"pct": round(b.percent), "plugged": bool(b.power_plugged)}
    except Exception as e:
        conf.log("dashboard", f"system stats failed: {e}")
    return out


# ------------------------------------------------------------------ metrics + business data

_ACTIVE_OUT = ("ARCHIVED", "REJECTED")
_CONTACTED = ("CONTACTED", "REPLIED", "INTERESTED", "NOT_INTERESTED", "CLOSED")


def metrics() -> dict:
    from . import leads, research
    from .prospecting import store
    c = _safe(store.counts, {})
    week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    new_week = _safe(lambda: sum(store.counts(since=week).values()), 0)
    rows = _safe(lambda: _usage_rows(1), [])
    today = datetime.now().date().isoformat()
    return {
        "businesses": sum(n for s, n in c.items() if s not in _ACTIVE_OUT),
        "businesses_week": new_week,
        "qualified": c.get("QUALIFIED", 0) + c.get("DRAFTED", 0),
        "to_approve": c.get("DRAFTED", 0),
        "approved": c.get("APPROVED", 0),
        "contacted": sum(c.get(s, 0) for s in _CONTACTED),
        "research_week": len(_safe(lambda: research.findings(days=7, limit=1000), [])),
        "leads_week": len(_safe(lambda: leads.all_leads(days=7), [])),
        "tokens_today": sum(int(r.get("in", 0)) + int(r.get("out", 0)) for r in rows if r.get("ts", "")[:10] == today),
    }


def _row(p: dict) -> dict:
    from .prospecting import engine, scoring
    q = p.get("quote") or {}
    return {"id": p["id"], "name": p["name"], "category": scoring.label(p.get("category") or ""),
            "area": (p.get("area") or "").split(",")[0], "status": p["status"], "priority": p.get("priority") or "",
            "website_status": engine._WS.get(p.get("website_status"), p.get("website_status")),
            "website": p.get("website") or "", "website_score": p.get("website_score"),
            "ai_score": p.get("ai_score") or 0, "package": p.get("package") or "",
            "package_name": scoring.PACKAGES.get(p.get("package") or "", ""),
            "price": q.get("display") or q.get("status") or "",
            "source_name": p.get("source_name") or "", "source_url": p.get("source_url") or "",
            "lat": p.get("lat"), "lon": p.get("lon"), "discovered": (p.get("discovered_at") or "")[:10],
            "contact": "email" if p.get("email") else ("phone" if p.get("phone") else "")}


def business(limit: int = 500) -> list[dict]:
    from .prospecting import store
    return [_row(p) for p in _safe(lambda: store.list_prospects(limit=limit), [])]


def map_data() -> dict:
    """Plotted businesses + the OSM cells actually searched. No basemap tiles are fetched; the UI
    draws a schematic plot, so it never looks like a complete map of an area."""
    from .prospecting import geo, store
    from . import db
    pts = [r for r in business() if r["lat"] is not None and r["lon"] is not None and r["status"] not in _ACTIVE_OUT]
    cells = []

    def _cells():
        store._init()
        with db._conn() as c:
            return [dict(r) for r in c.execute("SELECT area, cell, status, candidates FROM prospect_cells")]
    for r in _safe(_cells, []):
        try:
            s, w, n, e = (float(x) for x in r["cell"].split(","))
            cells.append({"area": r["area"].split(",")[0], "s": s, "w": w, "n": n, "e": e,
                          "status": r["status"], "candidates": r["candidates"]})
        except (ValueError, AttributeError):
            continue
    return {"points": [{k: r[k] for k in ("id", "name", "lat", "lon", "priority", "status", "category")} for r in pts],
            "cells": cells, "attribution": geo.ATTRIBUTION,
            "note": "Only businesses OpenStreetMap lists in the cells searched so far. OSM doesn't list every business."}


def coverage() -> list[dict]:
    from .prospecting import store
    return [{"area": r["area"], "categories": r["categories"], "cells": r["cells"] or 0, "done": r["done"] or 0,
             "failed": r["failed"] or 0, "candidates": r["candidates"] or 0, "qualified": r["qualified"] or 0,
             "last": r["last"]} for r in _safe(store.coverage, [])]


def research_items(limit: int = 8) -> list[dict]:
    from . import research
    return [{"id": f["id"], "title": f.get("title", ""), "topic": f.get("topic", ""),
             "found": f.get("last_seen") or f.get("found", ""), "source_name": f.get("source_name", ""),
             "source_url": f.get("source_url", ""), "confidence": f.get("confidence", 0),
             "summary": (f.get("summary") or "")[:240]}
            for f in _safe(lambda: research.findings(limit=limit), [])]


def activity(limit: int = 20) -> list[dict]:
    """A merged, newest-first timeline of things Helios actually did."""
    from . import jobs, leads, research
    from .night_mode import scheduler as night
    from .prospecting import store
    items: list[dict] = []
    for r in _safe(lambda: night.runs(5), []):
        items.append({"at": r.get("finished") or r.get("started") or "", "kind": "night",
                      "text": f"Night Mode {r.get('night', '')}: {r.get('status', '?')}"})
    for r in _safe(lambda: jobs.history(limit=10), []):
        if r.get("name") == "night_mode":
            continue
        items.append({"at": r.get("finished_at") or r.get("started_at") or "", "kind": "job",
                      "text": f"{r.get('name')}: {r.get('status')}" + (f" — {str(r.get('summary'))[:90]}" if r.get("summary") else "")})
    for p in _safe(lambda: store.list_prospects(limit=200, order="new"), []):
        for e in (p.get("contact_log") or [])[-3:]:
            items.append({"at": e.get("at", ""), "kind": "business", "text": f"{p['name']}: {e.get('event')}"})
        if p.get("last_researched"):
            items.append({"at": p["last_researched"], "kind": "business",
                          "text": f"Researched {p['name']} ({(p.get('area') or '').split(',')[0]})"})
    for f in _safe(lambda: research.findings(days=14, limit=10), []):
        items.append({"at": f.get("found", ""), "kind": "research", "text": f"Research: {f.get('title', '')}"})
    for ld in _safe(lambda: leads.all_leads(days=14)[:10], []):
        items.append({"at": ld.get("found", ""), "kind": "lead", "text": f"Lead: {ld.get('title', '')}"})
    items = [i for i in items if i["at"]]
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]


def upcoming(limit: int = 8) -> list[dict]:
    """Scheduled work that will really run: Night Mode tasks, enabled jobs, scheduled workflows,
    plus drafts waiting for the user's approval."""
    from . import db, jobs
    from .night_mode import scheduler as night
    from .prospecting import store
    out: list[dict] = []
    now = datetime.now()

    def _night():
        if not night.enabled():
            return []
        ws, _ = night.next_window(now)
        tasks, _ = night.plan(ws)
        return [{"at": t.isoformat(timespec="minutes"), "title": name.replace("_", " ").capitalize(),
                 "kind": "night", "detail": "Night Mode"} for name, t in tasks[:6]]
    out += _safe(_night, [])
    for j in _safe(jobs.all_jobs, []):
        if j.get("enabled") and j.get("next_run") and j.get("name") != "night_mode":
            out.append({"at": j["next_run"][:16], "title": j["name"].replace("_", " ").capitalize(),
                        "kind": "job", "detail": j.get("schedule", "")})
    for w in _safe(db.list_workflows, []):
        if w.get("enabled") and w.get("next_run"):
            out.append({"at": w["next_run"][:16], "title": w["name"], "kind": "workflow", "detail": w.get("schedule", "")})
    out.sort(key=lambda x: x["at"])
    n = _safe(lambda: len(store.list_prospects(status="DRAFTED", limit=500)), 0)
    if n:
        out.insert(0, {"at": "", "title": f"{n} outreach draft{'s' if n != 1 else ''} to approve",
                       "kind": "approval", "detail": "Helios never sends — you approve, then send it yourself"})
    return out[:limit]


def _audit_view(p: dict) -> dict | None:
    a = p.get("audit") or {}
    if p.get("website_status") != "ok" or not a.get("url"):
        return {"audited": False, "note": a.get("note") or "", "checked": p.get("last_site_check")}
    contact = []
    if a.get("phones"):
        contact.append("phone")
    if a.get("emails"):
        contact.append("email")
    if a.get("contact_form"):
        contact.append("form")
    return {"audited": True, "url": a.get("url"), "checked": p.get("last_site_check"),
            "rows": [
                {"k": "HTTPS", "ok": bool(a.get("https")), "v": "Yes" if a.get("https") else "No"},
                {"k": "Load time", "ok": (a.get("load_ms") or 0) < 4000 if a.get("load_ms") else None,
                 "v": f"{a['load_ms'] / 1000:.1f}s" if a.get("load_ms") else "—"},
                {"k": "Mobile layout", "ok": a.get("has_viewport") and not a.get("mobile_overflow"),
                 "v": "OK" if a.get("has_viewport") and not a.get("mobile_overflow") else "Poor"},
                {"k": "Contact routes", "ok": bool(contact), "v": ", ".join(contact) or "None found"},
                {"k": "Online booking", "ok": bool(a.get("booking")), "v": "Yes" if a.get("booking") else "Missing"},
                {"k": "WhatsApp", "ok": bool(a.get("whatsapp")), "v": "Yes" if a.get("whatsapp") else "Missing"},
                {"k": "SEO basics", "ok": bool(a.get("title") and a.get("description") and a.get("h1")),
                 "v": "OK" if a.get("title") and a.get("description") and a.get("h1") else "Weak"},
                {"k": "Up to date", "ok": not a.get("outdated"),
                 "v": f"© {a['copyright_year']}" if a.get("copyright_year") else "no year shown"},
                {"k": "Broken links", "ok": not a.get("broken_links"), "v": str(len(a.get("broken_links") or []))},
                {"k": "Chat widget", "ok": None, "v": a.get("chatbot") or "none"},
            ]}


def prospect(pid: int) -> dict | None:
    from .prospecting import engine, store
    p = store.get(pid)
    if not p:
        return None
    return {**engine.card(p), "audit_view": _audit_view(p), "lat": p.get("lat"), "lon": p.get("lon"),
            "phone": p.get("phone") or "", "email": p.get("email") or "",
            "discovered": p.get("discovered_at"), "verification": p.get("verification") or ""}


def featured() -> int | None:
    """The prospect the right panel opens on: the best one waiting for approval, else qualified."""
    from .prospecting import store
    for st in ("DRAFTED", "QUALIFIED", "APPROVED"):
        items = _safe(lambda: store.list_prospects(status=st, limit=1), [])
        if items:
            return items[0]["id"]
    return None


def night_report() -> str:
    from .night_mode import scheduler as night
    return _safe(night.latest_report, "No night report yet.")


def projects_view() -> list[dict]:
    """Per active project: the health verdict from the last stored check results + live git state."""
    from . import health
    return [{"project": r["project"], "verdict": r["verdict"], "issues": r["issues"][:5], "at": r.get("at")}
            for r in _safe(lambda: health.reports(None), [])]


_SETUP = {
    "voice": "Settings → Voice. Wake word, follow-up time and the microphone live in [voice].",
    "computer": "PC control uses cua-driver ([computer].driver_bin). Clicks and typing go through the "
                "permission prompt; one agent drives the mouse at a time.",
    "browser": "Helios's own hidden Edge/Chrome (fresh profile, never your logins). Forms, sending and "
               "buying need your approval; no passwords or card numbers, ever.",
    "memory": "Long-term memory lives in your Obsidian vault ([paths].vault).",
    "gmail": "Run `helios gmail setup <client.json>` then `helios gmail login` in your own terminal. "
             "Helios drafts replies to business contacts only and never sends without your approval.",
    "polarion": "Run `helios polarion token local` (or server) in your own terminal. Access is read-only.",
    "mcp": "Internal tools run for every brain turn. The public server ([mcp_public]) lets other MCP "
           "clients use a curated, permission-gated tool set — see docs/HELIOS_MCP.md.",
}


def module_detail(mid: str) -> dict | None:
    mods = {m["id"]: m for m in modules()}
    if mid not in mods:
        return None
    m = mods[mid]
    lines: list[str] = [m["detail"]]
    if mid == "voice":
        v = voice_status()
        lines = [f"Voice: {'on' if v['enabled'] else 'off'} in settings · daemon {'running' if v['daemon'] else 'not running'}",
                 f"State: {v['state'] or 'unknown'}" + (f" (since {v['at'][11:16]})" if v.get("at") else ""),
                 f"Wake word: {v['wake_word'] or 'none'}" + (
                     "" if v["wake_ready"] is None else (" — ready" if v["wake_ready"] else f" — unavailable: {v['wake_reason']}")),
                 f"Speech-to-text model: {v['stt'] or 'default'}"]
    elif mid == "night":
        from .night_mode import scheduler as night
        lines = _safe(night.status_text, m["detail"]).splitlines()
    return {**m, "lines": lines, "help": _SETUP.get(mid, "")}


def summary() -> dict:
    return {"metrics": metrics(), "modules": modules(), "system": system(), "voice": voice_status(),
            "engine": {"label": ENGINE_LABEL.get(conf.brain_engine(), conf.brain_engine()),
                       **{k: v for k, v in engine_health().items() if k != "models"}},
            "featured": featured(), "updated": _now()}


# ------------------------------------------------------------------ executive overview / reports

_EXEC_CATS = ("all", "business", "research", "projects", "night", "jobs", "system", "approvals", "failures", "calls")
_CAT_SOURCES = {"business": {"prospecting"}, "research": {"research", "leads"}, "projects": {"projects"},
                "night": {"night_mode"}, "jobs": {"jobs"}, "system": {"system", "antigravity", "calling"}}


def exec_range(rng: str, frm: str = "", to: str = "") -> tuple[datetime, datetime]:
    now = datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if rng == "yesterday":
        return day0 - timedelta(days=1), day0
    if rng == "7d":
        return now - timedelta(days=7), now
    if rng == "custom":
        try:
            a = datetime.fromisoformat(frm)
            b = datetime.fromisoformat(to) + (timedelta(days=1) if len(to) == 10 else timedelta(0))
            return a, b
        except ValueError:
            pass
    return day0, now


def executive_view(rng: str = "today", frm: str = "", to: str = "", cat: str = "all") -> dict:
    """The Reports / Executive Overview pane: pipeline, decisions, events, reports, calls."""
    from . import calling, executive
    a, b = exec_range(rng, frm, to)
    s, u = a.isoformat(timespec="seconds"), b.isoformat(timespec="seconds")
    cat = cat if cat in _EXEC_CATS else "all"
    evs = _safe(lambda: executive.events(since=s, until=u, limit=300), [])
    snz = _safe(executive.snoozed_until, None)
    if cat == "approvals":
        evs = [e for e in evs if e["approval"]]
    elif cat == "failures":
        evs = [e for e in evs if e["severity"] in ("CRITICAL", "HIGH") and "lead" not in e["type"]]
    elif cat in _CAT_SOURCES:
        evs = [e for e in evs if e["source"] in _CAT_SOURCES[cat]]
    elif cat == "calls":
        evs = [e for e in evs if e["source"] == "calling"]
    return {"range": {"from": s, "to": u, "name": rng}, "category": cat,
            "pipeline": _safe(lambda: executive.business(a, b), {}),
            "decisions": _safe(executive.decisions, []),
            "events": evs[:150],
            "unacked": _safe(lambda: len(executive.events(unacked=True, limit=500)), 0),
            "snoozed_until": snz.isoformat(timespec="minutes") if snz else "",
            "reports": _safe(lambda: [r for r in executive.reports(limit=200) if s <= r["created"] <= u][:30], []),
            "calls": _safe(lambda: [{k: c[k] for k in ("id", "due", "status", "reason", "to_masked", "answered",
                                                         "duration", "detail")} for c in calling.calls(30)], []),
            "calling": _safe(calling.status, {}),
            "schedule": _safe(lambda: {k: executive.cfg().get(k) for k in
                                       ("midday_time", "eod_time", "weekly_day", "weekly_time", "quiet_hours_start",
                                        "quiet_hours_end", "channels")}, {})}


def exec_report_text(rid: int) -> str | None:
    from . import executive
    r = next((x for x in executive.reports(limit=500) if x["id"] == rid), None)
    if not r:
        return None
    try:
        return Path(r["path"]).read_text(encoding="utf-8") if r["path"] else r["spoken"]
    except OSError:
        return r["spoken"] or "(the report file is no longer there)"


def exec_action(body: dict, publish=None) -> dict:
    """The user's own clicks in the Reports pane. None of these contact anyone."""
    from . import calling, executive
    act = str(body.get("action") or "")
    if act == "ack":
        ids = [str(i) for i in (body.get("ids") or [])][:200]
        return {"ok": True, "count": executive.acknowledge(ids)}
    if act == "ack_all":
        return {"ok": True, "count": executive.acknowledge(None)}
    if act == "snooze":
        h = float(body.get("hours") or 0)
        until = executive.snooze(h)
        return {"ok": True, "until": until.isoformat(timespec="minutes") if h > 0 else ""}
    if act == "cancel_call":
        cid = body.get("id")
        return {"ok": True, "count": calling.cancel(int(cid) if cid else None)}
    if act == "build":
        kind = str(body.get("kind") or "")
        if kind not in ("midday", "eod", "night", "weekly"):
            return {"ok": False, "message": "unknown report"}
        res = executive.deliver_report(kind, publish=publish) if body.get("deliver") else \
            {"status": "built", "report": executive.build(kind)}
        return {"ok": True, "status": res["status"], "text": res["report"]["text"]}
    return {"ok": False, "message": "unknown action"}


# ------------------------------------------------------------------ actions (background, user-clicked)

_running: dict[str, float] = {}


def run_action(name: str, emit=None) -> dict:
    """Quick actions that start a real Helios job in the background. Both are safe by design:
    Night Mode only researches/drafts; project checks run exactly the manifest's own commands."""
    if name not in ("night_mode", "project_health"):
        return {"ok": False, "message": "unknown action"}
    if name in _running and time.time() - _running[name] < 6 * 3600:
        return {"ok": False, "message": "already running"}

    def work():
        msg = ""
        try:
            if name == "night_mode":
                from . import jobs
                r = jobs.run_now("night_mode")
                msg = f"Night Mode finished: {r.get('status')} — {str(r.get('summary') or '')[:160]}"
            else:
                from . import health
                reps = health.run_all(None)
                bad = [r["project"] for r in reps if r.get("verdict") in ("FAILING", "ATTENTION")]
                msg = f"Project checks done: {len(reps)} project(s)" + (f", attention: {', '.join(bad)}" if bad else ", all OK")
        except Exception as e:
            msg = f"{name.replace('_', ' ')} didn't run: {e}"
        finally:
            _running.pop(name, None)
        conf.log("dashboard", msg)
        if emit:
            try:
                emit({"event": "action_done", "action": name, "text": msg})
            except Exception:
                pass
    _running[name] = time.time()
    threading.Thread(target=work, daemon=True, name=f"dash-{name}").start()
    return {"ok": True, "message": "Night Mode started" if name == "night_mode" else "Project checks started"}
