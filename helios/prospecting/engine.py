"""Orchestration: scan configured areas → research (audit + score + quote + draft) → queue.

    [prospecting]
    enabled = true
    areas = [{ name = "Mehdipatnam, Hyderabad", radius_km = 2 }]
    categories = ["salon", "clinic", "dentist", "restaurant", "retail", "repair", "fitness"]
    max_cells_per_run = 4          # OpenStreetMap cells scanned per run (coverage advances nightly)
    max_research_per_run = 12      # businesses audited + scored per run
    search_websites = false        # ask Antigravity (search only) for official sites OSM doesn't list
    currency = "INR"
    [prospecting.pricing]          # your rate card — empty means NEEDS USER PRICING, never a guess

Night Mode runs it (research and drafting only — it never sends anything).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from .. import conf
from . import audit, geo, outreach, quote, scoring, store


def cfg() -> dict:
    v = conf.SETTINGS.get("prospecting")
    return v if isinstance(v, dict) else {}


def enabled() -> bool:
    return bool(cfg().get("enabled", False))


def _int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(cfg().get(key, default))))
    except (TypeError, ValueError):
        return default


def categories(override=None) -> list[str]:
    cats = override or cfg().get("categories") or ["salon", "clinic", "dentist", "restaurant", "retail",
                                                   "repair", "fitness"]
    return [c for c in cats if c in geo.CATEGORIES or c in (cfg().get("categories_extra") or {})]


def areas() -> list[dict]:
    out = []
    for a in cfg().get("areas") or []:
        if isinstance(a, str):
            out.append({"name": a, "radius_km": 2.0})
        elif isinstance(a, dict) and a.get("name"):
            out.append({"name": str(a["name"]), "radius_km": float(a.get("radius_km") or 0) or None,
                        "categories": a.get("categories")})
    return out


# ------------------------------------------------------------------ discovery

def scan_area(name: str, *, radius_km: float | None = 2.0, cats: list[str] | None = None,
              max_cells: int | None = None, geocoder=None, overpass=None, pause: float = 2.0) -> dict:
    """Plan the area's cells (once) and scan the next pending ones. Returns a coverage report."""
    cats = categories(cats)
    rep = {"area": name, "cells_scanned": 0, "candidates": 0, "new": 0, "errors": []}
    place = (geocoder or geo.geocode)(name)
    if not place:
        rep["errors"].append(f"couldn't find '{name}' on OpenStreetMap")
        return rep
    cells = geo.cells_for(geo.area_bbox(place, radius_km), float(cfg().get("cell_km", 1.0)),
                          centre=(place["lat"], place["lon"]))
    store.plan_cells(name, cells, cats)
    for i, cell in enumerate(store.pending_cells(name, cats, max_cells or _int("max_cells_per_run", 4, 1, 50))):
        if i and pause:
            time.sleep(pause)                      # Overpass fair use
        try:
            elements = geo.query_cell(cell["cell"], cats, fetch=overpass)
        except Exception as e:
            store.finish_cell(cell["id"], status="failed", note=str(e)[:200])
            rep["errors"].append(f"cell {cell['cell']}: {e}")
            continue
        found = new = 0
        for el in elements:
            b = geo.to_business(el, name)
            if not b:
                continue
            found += 1
            _row, is_new = store.upsert(b)
            new += is_new
        store.finish_cell(cell["id"], status="done", candidates=found)
        rep["cells_scanned"] += 1
        rep["candidates"] += found
        rep["new"] += new
    conf.log("prospecting", f"scan {name}: {rep['cells_scanned']} cell(s), {rep['candidates']} candidates, "
                            f"{rep['new']} new")
    return rep


# ------------------------------------------------------------------ research one business

def research(pid: int, *, auditor=None, fetch=None, robots=None, link_check: bool = True) -> dict:
    """Audit → score → quote → draft. Stops at DRAFTED; never touches APPROVED or later."""
    p = store.get(pid)
    if not p:
        raise store.StatusError("no such prospect")
    if p["status"] not in ("NEW", "RESEARCHED", "QUALIFIED", "DRAFTED"):
        return p
    store.update(pid, **audit.audit_prospect(p, auditor=auditor, fetch=fetch, robots=robots,
                                             link_check=link_check))
    p = store.get(pid)
    sc = scoring.score(p)
    store.update(pid, website_score=sc["website_score"], ai_score=sc["ai_score"], priority=sc["priority"],
                 package=sc["package"], reasons=sc["reasons"], last_researched=store.now())
    if p["status"] == "NEW":
        store.set_status(pid, "RESEARCHED")
    p = store.get(pid)
    if not sc["qualified"]:
        if p["status"] in ("QUALIFIED", "DRAFTED"):
            # The fresh evidence no longer supports a pitch: withdraw it from the approval queue
            # rather than leave an email whose claims don't match the audit any more.
            d = dict(p.get("draft") or {})
            if d:
                d["withdrawn"] = store.now()
                store.update(pid, draft=d)
            store.set_status(pid, "RESEARCHED")
            store.log_event(pid, "no longer qualifies after re-research — draft withdrawn", by="helios")
        return store.get(pid)
    store.update(pid, quote=quote.build(p))
    newly = p["status"] in ("NEW", "RESEARCHED")
    if newly:
        store.set_status(pid, "QUALIFIED")
    p = store.get(pid)
    if p.get("email") or p.get("phone"):
        old = p.get("draft") or {}
        if old.get("edited_by") and not old.get("withdrawn"):
            # Never overwrite the user's own text — flag it instead so they can re-check the claims.
            store.update(pid, draft={**old, "outdated": store.now()})
        else:
            store.update(pid, draft=outreach.build_draft(p) if p.get("email") else outreach.build_call_notes(p))
        if p["status"] == "QUALIFIED":
            store.set_status(pid, "DRAFTED")
    if newly:
        store.add_qualified(p["area"], 1)
        from .. import executive           # report it (HIGH for priority A with a public contact)
        executive.lead_event(store.get(pid))
    return store.get(pid)


def search_enabled() -> bool:
    return bool(cfg().get("search_websites", True))


def verify_presence(*, batches: int | None = None, size: int | None = None, call=None) -> dict:
    """Web-search the official site / contacts of NEW businesses OSM lists without a website
    (Antigravity, search-only). Chains are archived; the rest become researchable."""
    from . import discovery
    rep = {"checked": 0, "websites": 0, "none": 0, "chains": 0, "errors": []}
    if not search_enabled():
        return rep
    size = size or _int("search_batch_size", 8, 1, 12)
    todo = [p for p in store.list_prospects(status="NEW", limit=500, order="new") if discovery.needs_search(p)]
    todo.sort(key=lambda p: (p.get("category") not in (geo.APPOINTMENT | geo.ORDERING), p["id"]))
    for i in range(batches or _int("max_search_batches_per_run", 2, 0, 20)):
        batch = todo[i * size:(i + 1) * size]
        if not batch:
            break
        r = discovery.verify_batch(batch, call=call)
        if r["error"]:
            rep["errors"].append(f"website search: {r['error']}")
            break
        for k in ("checked", "websites", "none", "chains"):
            rep[k] += r[k]
    for p in store.list_prospects(status="NEW", limit=500, order="new"):
        if "large chain" in (p.get("notes") or ""):
            store.set_status(p["id"], "ARCHIVED")
    rep["merged"] = store.merge_duplicates()          # same name + same phone/domain, now that contacts are known
    return rep


def _research_queue(limit: int) -> list[dict]:
    """NEW businesses first — those with a website to audit, then ones whose presence was checked
    (when the web search is on, unchecked ones wait for it) — then unreachable sites due for their
    second check."""
    fresh = [p for p in store.list_prospects(status="NEW", limit=500, order="new")
             if p.get("website") or p.get("searched_at") or not search_enabled()]
    fresh.sort(key=lambda p: (not p.get("website"), p["id"]))
    retry = [p for p in store.list_prospects(status="RESEARCHED", limit=200, order="new")
             if p.get("website_status") == "unreachable" and audit._recheck_due(p)]
    return (fresh + retry)[:limit]


def run(*, area: str | None = None, max_cells: int | None = None, max_research: int | None = None,
        geocoder=None, overpass=None, auditor=None, fetch=None, robots=None, searcher=None,
        search_batches: int | None = None, pause: float = 2.0) -> dict:
    """One prospecting pass: scan configured (or given) areas → verify online presence of
    businesses OSM lists without a site → research the queue."""
    t0 = time.monotonic()
    specs = [a for a in areas() if not area or a["name"].lower() == area.lower()] or \
            ([{"name": area, "radius_km": 2.0}] if area else [])
    report = {"scans": [], "researched": 0, "qualified": 0, "drafted": 0, "errors": [], "search": {}}
    for a in specs:
        rep = scan_area(a["name"], radius_km=a.get("radius_km"), cats=a.get("categories"), max_cells=max_cells,
                        geocoder=geocoder, overpass=overpass, pause=pause)
        report["scans"].append(rep)
        report["errors"] += rep["errors"]
    if max_research != 0:
        report["search"] = verify_presence(batches=search_batches, call=searcher)
        report["errors"] += report["search"].get("errors", [])
    # 0 means "scan only" — `max_research or default` used to turn it into the default (12).
    n = max_research if max_research is not None else _int("max_research_per_run", 12, 1, 100)
    for p in (_research_queue(n) if n > 0 else []):
        try:
            r = research(p["id"], auditor=auditor, fetch=fetch, robots=robots)
        except Exception as e:
            report["errors"].append(f"{p['name']}: {e}")
            continue
        report["researched"] += 1
        report["qualified"] += r["status"] in ("QUALIFIED", "DRAFTED")
        report["drafted"] += r["status"] == "DRAFTED"
    try:
        from ..web import browser
        browser.close_shared()
    except Exception:
        pass
    report["seconds"] = round(time.monotonic() - t0, 1)
    return report


def night_task(ctx: dict):
    from ..night_mode.common import Result
    if not enabled():
        return Result.skipped("business prospecting is off ([prospecting] enabled = false)")
    if not areas():
        return Result.skipped("no areas configured ([prospecting] areas = [...])")
    if not ctx.get("online"):
        return Result.skipped("offline — prospecting needs the internet")
    rep = run()
    res = Result()
    for s in rep["scans"]:
        res.completed.append(f"searched OpenStreetMap in {s['cells_scanned']} cell(s) of {s['area']}: "
                             f"{s['candidates']} businesses, {s['new']} new")
    srch = rep.get("search") or {}
    if srch.get("checked"):
        res.completed.append(f"web-checked {srch['checked']} business(es) OpenStreetMap lists without a site: "
                             f"{srch['websites']} websites found, {srch['none']} with none, {srch['chains']} chains skipped")
    res.completed.append(f"researched {rep['researched']} business(es): {rep['qualified']} qualified, "
                         f"{rep['drafted']} outreach draft(s) waiting for your approval")
    res.failed += rep["errors"][:10]
    res.summary = (f"{rep['researched']} researched, {rep['qualified']} qualified, {rep['drafted']} drafts "
                   "to approve (nothing sent)")
    res.data = briefing_data(hours=24)
    return res


# ------------------------------------------------------------------ briefing + formatting

def briefing_data(hours: float = 24) -> dict:
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    recent = [p for p in store.list_prospects(limit=500, order="new") if (p.get("last_researched") or "") >= since]
    top = [p for p in store.list_prospects(limit=50) if p["status"] in ("QUALIFIED", "DRAFTED")][:5]
    return {"researched": len(recent),
            "no_or_weak_site": sum(1 for p in recent if (p.get("website_score") is not None and p["website_score"] <= 1)),
            "redesign": sum(1 for p in recent if p.get("website_score") in (2, 3)),
            "strong_ai": sum(1 for p in recent if (p.get("ai_score") or 0) >= 3),
            "qualified": sum(1 for p in recent if p["status"] in ("QUALIFIED", "DRAFTED", "APPROVED")),
            "to_approve": len(store.list_prospects(status="DRAFTED", limit=500)),
            "top": [{"id": p["id"], "name": p["name"], "area": p["area"], "priority": p["priority"],
                     "why": ((p.get("reasons") or {}).get("website") or [{}])[0].get("factor", ""),
                     "website": p.get("website_status"), "ai": p.get("ai_score"),
                     "package": scoring.PACKAGES.get(p.get("package") or "", ""),
                     "price": (p.get("quote") or {}).get("display") or (p.get("quote") or {}).get("status", ""),
                     "source": p.get("source_url")} for p in top]}


_WS = {"none": "no website found", "unlisted": "no website listed", "ok": "audited", "broken": "broken",
       "unreachable": "didn't load (re-check due)", "not_audited": "not audited (robots.txt)",
       "refused": "not audited (bot check)", "unchecked": "not checked yet"}


def format_prospect(p: dict, *, verbose: bool = False) -> str:
    q = p.get("quote") or {}
    price = q.get("display") or q.get("status") or "—"
    head = (f"#{p['id']} [{p['status']}] {p['name']} ({p.get('category') or '?'}, {p.get('area', '')}) — "
            f"priority {p.get('priority') or '?'} · website {p.get('website_score') if p.get('website_score') is not None else '?'}/5 "
            f"({_WS.get(p.get('website_status'), p.get('website_status'))}) · AI {p.get('ai_score') or 0}/5 · "
            f"{scoring.PACKAGES.get(p.get('package') or '', 'no package')} · {price}")
    if not verbose:
        return head
    r = p.get("reasons") or {}
    lines = [head, f"  source: {p.get('source_name')} — {p.get('source_url')} (found {p.get('discovered_at', '')[:10]}, "
                   f"{p.get('verification')})"]
    if p.get("website"):
        lines.append(f"  website: {p['website']}")
    if p.get("email") or p.get("phone"):
        lines.append(f"  public contact: {p.get('email') or ''}{' (' + p['email_source'] + ')' if p.get('email') else ''}"
                     f"{'  ' + p['phone'] if p.get('phone') else ''}")
    for k, title in (("website", "Website"), ("ai", "AI opportunity")):
        for x in r.get(k) or []:
            lines.append(f"  {title}: {x['factor']} — {x['detail']}")
    if r.get("priority"):
        lines.append(f"  Priority: {r['priority']}")
    if q.get("package"):
        lines.append(f"  Quote: {q['name']} · {price} · {q.get('timeline', '')} · features: {', '.join(q.get('features', []))}")
        if q.get("status") == "NEEDS USER PRICING":
            lines.append(f"    {q.get('note')}")
    d = p.get("draft") or {}
    if d.get("body"):
        lines += [f"  Draft to {d.get('to')} ({d.get('to_source')}) — subject: {d.get('subject')}", "",
                  *["    " + ln for ln in d["body"].splitlines()], "",
                  "  Evidence: " + "; ".join(f"{c['text']} ← {c['evidence']}" for c in d.get("claims", []))]
    if p.get("contact_log"):
        lines.append("  History: " + "; ".join(f"{e['at'][:16]} {e['event']}" + (f" ({e.get('evidence')})" if e.get("evidence") else "")
                                                for e in p["contact_log"]))
    return "\n".join(lines)


def card(p: dict) -> dict:
    """Compact view of one prospect for the dashboard's approval queue."""
    a = p.get("audit") or {}
    q = p.get("quote") or {}
    d = p.get("draft") or {}
    has_site = p.get("website_status") == "ok"
    return {"id": p["id"], "name": p["name"], "category": scoring.label(p.get("category") or ""),
            "area": (p.get("area") or "").split(",")[0], "status": p["status"], "priority": p.get("priority") or "",
            "website_status": _WS.get(p.get("website_status"), p.get("website_status")),
            "website_score": p.get("website_score"), "ai_score": p.get("ai_score") or 0,
            "mobile": (None if not has_site else ("Poor" if a.get("mobile_overflow") or not a.get("has_viewport") else "OK")),
            "booking": None if not has_site else ("Yes" if a.get("booking") else "Missing"),
            "whatsapp": None if not has_site else ("Yes" if a.get("whatsapp") else "Missing"),
            "package": scoring.PACKAGES.get(p.get("package") or "", ""),
            "price": q.get("display") or q.get("status") or "", "timeline": q.get("timeline", ""),
            "features": q.get("features") or [], "source_name": p.get("source_name"), "source_url": p.get("source_url"),
            "website": p.get("website") or "",
            "reasons": [{"kind": k, **r} for k in ("website", "ai") for r in ((p.get("reasons") or {}).get(k) or [])],
            "priority_reason": (p.get("reasons") or {}).get("priority", ""),
            "draft": {"channel": d.get("channel", "email"), "to": d.get("to", ""), "to_source": d.get("to_source", ""),
                      "subject": d.get("subject", ""), "body": d.get("body", ""), "claims": d.get("claims") or []},
            "history": p.get("contact_log") or []}


def needs_pricing(limit: int = 200) -> list[dict]:
    """NEEDS_PRICING view: live prospects whose recommended package has no price in your rate card.
    A derived state (the quote says NEEDS USER PRICING), not a separate status — the draft still
    goes ahead without a figure, so the research/approval flow isn't blocked on pricing."""
    live = ("QUALIFIED", "DRAFTED", "APPROVED")
    return [p for p in store.list_prospects(limit=limit)
            if p["status"] in live and (p.get("quote") or {}).get("status") == "NEEDS USER PRICING"]


def format_list(items: list[dict]) -> str:
    return "\n".join(format_prospect(p) for p in items) or "No prospects yet."


def format_coverage(rows: list[dict]) -> str:
    if not rows:
        return "No areas scanned yet."
    return "\n".join(f"{r['area']} [{r['categories']}]: searched OpenStreetMap in {r['done'] or 0}/{r['cells']} cells"
                     f"{' (' + str(r['failed']) + ' failed)' if r['failed'] else ''} · {r['candidates'] or 0} businesses · "
                     f"{r['qualified'] or 0} qualified · last {str(r['last'] or 'never')[:16]}" for r in rows) + \
        f"\n(Source: {geo.ATTRIBUTION}. OpenStreetMap doesn't list every business.)"
