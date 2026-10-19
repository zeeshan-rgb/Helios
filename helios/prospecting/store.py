"""Prospect database (SQLite, helios.db): businesses, coverage cells, status machine.

Statuses (in order): NEW → RESEARCHED → QUALIFIED → DRAFTED → APPROVED → CONTACTED → REPLIED →
INTERESTED | NOT_INTERESTED → CLOSED, plus REJECTED / ARCHIVED from anywhere before CONTACTED.

* APPROVED only by approve() — the user's explicit yes (CLI, or the MCP tool behind the permission
  prompt).
* CONTACTED only by record_contact() with evidence of an approved send (a Gmail sent-message id,
  or the user's own "I sent it" in the CLI). There is no other way to set it.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from datetime import datetime

from .. import db

STATUSES = ("NEW", "RESEARCHED", "QUALIFIED", "DRAFTED", "APPROVED", "CONTACTED", "REPLIED",
            "INTERESTED", "NOT_INTERESTED", "CLOSED", "REJECTED", "ARCHIVED")
_AFTER_CONTACT = ("REPLIED", "INTERESTED", "NOT_INTERESTED", "CLOSED")
_JSON_COLS = ("socials", "audit", "reasons", "quote", "draft", "contact_log", "sources")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT NOT NULL UNIQUE,          -- osm:node/123, else name+domain / name+place
  name TEXT NOT NULL,
  norm_name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '',
  area TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL DEFAULT '',
  lat REAL, lon REAL,
  website TEXT NOT NULL DEFAULT '',
  domain TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',           -- public BUSINESS contact only (with its source)
  email_source TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',           -- public business phone (OSM / its own site)
  opening_hours TEXT NOT NULL DEFAULT '',
  socials TEXT NOT NULL DEFAULT '[]',
  source_name TEXT NOT NULL,                -- e.g. "OpenStreetMap (Overpass API)"
  source_url TEXT NOT NULL,
  sources TEXT NOT NULL DEFAULT '[]',       -- every source this business was seen in
  discovered_at TEXT NOT NULL,
  verification TEXT NOT NULL DEFAULT 'listed in OpenStreetMap',
  status TEXT NOT NULL DEFAULT 'NEW',
  website_status TEXT NOT NULL DEFAULT 'unchecked',
  site_failures INTEGER NOT NULL DEFAULT 0,
  last_site_check TEXT,
  audit TEXT NOT NULL DEFAULT '{}',
  website_score INTEGER,
  ai_score INTEGER,
  priority TEXT NOT NULL DEFAULT '',
  reasons TEXT NOT NULL DEFAULT '{}',
  package TEXT NOT NULL DEFAULT '',
  quote TEXT NOT NULL DEFAULT '{}',
  draft TEXT NOT NULL DEFAULT '{}',
  approved_at TEXT,
  contact_log TEXT NOT NULL DEFAULT '[]',
  last_researched TEXT,
  searched_at TEXT,                         -- web search for the official site/contacts done
  notes TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS prospects_area ON prospects(area);
CREATE TABLE IF NOT EXISTS prospect_cells (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  area TEXT NOT NULL,
  cell TEXT NOT NULL,                       -- "south,west,north,east" (rounded)
  categories TEXT NOT NULL,                 -- comma list scanned in this cell
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
  source TEXT NOT NULL DEFAULT 'OpenStreetMap (Overpass API)',
  scanned_at TEXT,
  candidates INTEGER NOT NULL DEFAULT 0,
  qualified INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  UNIQUE(area, cell, categories)
);
"""

_inited: set[str] = set()
_lock = threading.Lock()


class StatusError(ValueError):
    pass


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


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ normalization / dedupe

_SUFFIXES = re.compile(r"\b(pvt|private|ltd|limited|llp|inc|co|company|the|and|&)\b")


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['’`]", "", s)               # "Reddy's" and "Reddys" are the same name
    s = _SUFFIXES.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def domain_of(url: str) -> str:
    from urllib.parse import urlsplit
    h = (urlsplit(url if "://" in (url or "") else f"https://{url}").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def _row(r) -> dict:
    d = dict(r)
    for k in _JSON_COLS:
        try:
            d[k] = json.loads(d.get(k) or ("[]" if k in ("socials", "contact_log", "sources") else "{}"))
        except (TypeError, ValueError):
            d[k] = [] if k in ("socials", "contact_log", "sources") else {}
    return d


def find_duplicate(p: dict) -> dict | None:
    """Same business seen before? OSM id, else normalized name + (domain | phone | ~100 m)."""
    _init()
    with db._conn() as c:
        if p.get("dedupe_key"):
            r = c.execute("SELECT * FROM prospects WHERE dedupe_key=?", (p["dedupe_key"],)).fetchone()
            if r:
                return _row(r)
        nn = norm_name(p.get("name", ""))
        if not nn:
            return None
        for r in c.execute("SELECT * FROM prospects WHERE norm_name=?", (nn,)).fetchall():
            d = _row(r)
            if p.get("domain") and d["domain"] and p["domain"] == d["domain"]:
                return d
            if p.get("phone") and d["phone"] and norm_phone(p["phone"]) == norm_phone(d["phone"]):
                return d
            if p.get("lat") is not None and d["lat"] is not None and \
                    abs(p["lat"] - d["lat"]) < 0.001 and abs(p["lon"] - d["lon"]) < 0.001:
                return d
    return None


def upsert(p: dict) -> tuple[dict, bool]:
    """Insert a discovered business, or merge a re-sighting (keeps provenance). (row, is_new)."""
    _init()
    dup = find_duplicate(p)
    src = {"name": p["source_name"], "url": p["source_url"], "seen": now()}
    if dup:
        sources = dup.get("sources") or []
        if not any(s.get("url") == src["url"] for s in sources):
            sources.append(src)
        fill = {k: p[k] for k in ("website", "domain", "email", "email_source", "phone", "address", "city")
                if p.get(k) and not dup.get(k)}
        update(dup["id"], sources=sources, **fill)
        return get(dup["id"]), False
    p = {**p, "norm_name": norm_name(p["name"]), "sources": [src], "discovered_at": now(), "updated_at": now()}
    cols = [k for k in p if k in _columns()]
    with db._conn() as c:
        cur = c.execute(f"INSERT INTO prospects({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [json.dumps(p[k]) if k in _JSON_COLS else p[k] for k in cols])
        pid = cur.lastrowid
    return get(pid), True


_cols_cache: set[str] = set()


def _columns() -> set[str]:
    if not _cols_cache:
        _init()
        with db._conn() as c:
            _cols_cache.update(r[1] for r in c.execute("PRAGMA table_info(prospects)").fetchall())
    return _cols_cache


def get(pid: int) -> dict | None:
    _init()
    with db._conn() as c:
        r = c.execute("SELECT * FROM prospects WHERE id=?", (int(pid),)).fetchone()
    return _row(r) if r else None


def update(pid: int, **fields) -> None:
    """Update research fields. Status changes go through set_status / approve / record_contact."""
    if "status" in fields or "approved_at" in fields or "contact_log" in fields:
        raise StatusError("use set_status / approve / record_contact for status changes")
    _write(pid, fields)


def _write(pid: int, fields: dict) -> None:
    _init()
    fields = {k: v for k, v in fields.items() if k in _columns()}
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = [json.dumps(v) if k in _JSON_COLS else v for k, v in fields.items()]
    with db._conn() as c:
        c.execute(f"UPDATE prospects SET {sets} WHERE id=?", [*vals, int(pid)])


def list_prospects(*, status: str | None = None, area: str | None = None, priority: str | None = None,
                   limit: int = 50, order: str = "score") -> list[dict]:
    _init()
    q, args = "SELECT * FROM prospects WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status.upper())
    if area:
        q += " AND area LIKE ?"
        args.append(f"%{area}%")
    if priority:
        q += " AND priority=?"
        args.append(priority.upper())
    q += {"score": " ORDER BY CASE priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END, "
                   "COALESCE(ai_score,0) + (5 - COALESCE(website_score,5)) DESC, id DESC",
          "new": " ORDER BY id DESC"}.get(order, " ORDER BY id DESC")
    q += " LIMIT ?"
    args.append(int(limit))
    with db._conn() as c:
        return [_row(r) for r in c.execute(q, args).fetchall()]


def counts(since: str | None = None) -> dict:
    _init()
    q = "SELECT status, COUNT(*) n FROM prospects" + (" WHERE discovered_at>=?" if since else "") + " GROUP BY status"
    with db._conn() as c:
        return {r["status"]: r["n"] for r in c.execute(q, (since,) if since else ()).fetchall()}


# ------------------------------------------------------------------ status machine

_RESEARCH_FLOW = ("NEW", "RESEARCHED", "QUALIFIED", "DRAFTED")


def set_status(pid: int, status: str) -> dict:
    """Research-flow and after-contact statuses only. APPROVED → approve(); CONTACTED →
    record_contact()."""
    status = status.upper()
    if status not in STATUSES:
        raise StatusError(f"unknown status {status}")
    if status in ("APPROVED", "CONTACTED"):
        raise StatusError(f"{status} can't be set directly — use approve() / record_contact()")
    p = get(pid)
    if not p:
        raise StatusError("no such prospect")
    cur = p["status"]
    if status in _AFTER_CONTACT and cur not in ("CONTACTED", *_AFTER_CONTACT):
        raise StatusError(f"{status} only after the business was actually contacted (now {cur})")
    if status == "REJECTED" and cur in ("CONTACTED", *_AFTER_CONTACT):
        raise StatusError("already contacted — use NOT_INTERESTED or CLOSED")
    if status in _RESEARCH_FLOW and cur in ("APPROVED", "CONTACTED", *_AFTER_CONTACT):
        raise StatusError(f"can't move back to {status} from {cur}")
    _write(pid, {"status": status})
    return get(pid)


def approve(pid: int, *, by: str = "user") -> dict:
    """The user's explicit approval of the drafted outreach (email + quotation)."""
    p = get(pid)
    if not p:
        raise StatusError("no such prospect")
    if p["status"] != "DRAFTED":
        raise StatusError(f"only a DRAFTED prospect can be approved (this one is {p['status']})")
    d = p.get("draft") or {}
    if not d.get("body"):
        raise StatusError("nothing drafted to approve")
    if not d.get("to"):
        raise StatusError("no public business contact to use")
    _write(pid, {"status": "APPROVED", "approved_at": now()})
    log = p.get("contact_log") or []
    log.append({"at": now(), "event": "approved", "by": by})
    _write(pid, {"contact_log": log})
    return get(pid)


def log_event(pid: int, event: str, by: str = "helios") -> None:
    """Append a non-status event to the prospect's history (status changes have their own paths)."""
    p = get(pid)
    if not p:
        return
    log = p.get("contact_log") or []
    log.append({"at": now(), "event": event[:200], "by": by})
    _write(pid, {"contact_log": log})


def edit_draft(pid: int, *, subject: str, body: str, by: str = "user") -> dict:
    """The user's own edit of the outreach text — only while it's DRAFTED (before approval), so an
    approved message can't change afterwards. The evidence list is kept; the edit is logged."""
    p = get(pid)
    if not p:
        raise StatusError("no such prospect")
    if p["status"] != "DRAFTED":
        raise StatusError(f"only a DRAFTED prospect's text can be edited (this one is {p['status']})")
    body = (body or "").strip()
    if not body:
        raise StatusError("the draft can't be empty")
    d = dict(p.get("draft") or {})
    d.update(subject=(subject or d.get("subject") or "").strip()[:200], body=body[:8000],
             edited_by=by, edited_at=now())
    log = p.get("contact_log") or []
    log.append({"at": now(), "event": "draft edited", "by": by})
    _write(pid, {"draft": d, "contact_log": log})
    return get(pid)


def record_contact(pid: int, *, channel: str, evidence: str, by: str = "user") -> dict:
    """Mark CONTACTED — only for an APPROVED prospect, only with evidence of the send (Gmail sent
    message id, or the user's own confirmation)."""
    p = get(pid)
    if not p:
        raise StatusError("no such prospect")
    if p["status"] != "APPROVED":
        raise StatusError(f"a prospect must be APPROVED before it can be marked contacted (it's {p['status']})")
    if not (evidence or "").strip():
        raise StatusError("evidence of the send is required")
    log = p.get("contact_log") or []
    log.append({"at": now(), "event": "contacted", "channel": channel, "evidence": evidence[:300], "by": by})
    _write(pid, {"status": "CONTACTED", "contact_log": log})
    return get(pid)


def merge_duplicates() -> int:
    """Same business listed twice (e.g. two OSM points) that only shows up once contacts are known:
    same normalized name + same phone or website domain → keep the oldest, archive the rest."""
    _init()
    with db._conn() as c:
        rows = [_row(r) for r in c.execute(
            "SELECT * FROM prospects WHERE status NOT IN ('ARCHIVED','REJECTED') ORDER BY id").fetchall()]
    keep: dict[tuple, dict] = {}
    merged = 0
    for p in rows:
        keys = [k for k in ((p["norm_name"], "tel", norm_phone(p["phone"])) if p["phone"] else None,
                            (p["norm_name"], "web", p["domain"]) if p["domain"] else None) if k]
        first = next((keep[k] for k in keys if k in keep), None)
        if first is None:
            for k in keys:
                keep[k] = p
            continue
        if p["status"] in _RESEARCH_FLOW:
            sources = first.get("sources") or []
            sources += [s for s in (p.get("sources") or []) if s not in sources]
            _write(first["id"], {"sources": sources})
            _write(p["id"], {"status": "ARCHIVED", "notes": f"duplicate of #{first['id']}"})
            merged += 1
    return merged


# ------------------------------------------------------------------ coverage cells

def plan_cells(area: str, cells: list[str], categories: list[str]) -> int:
    _init()
    cats = ",".join(sorted(categories))
    with db._conn() as c:
        n = 0
        for cell in cells:
            cur = c.execute("INSERT OR IGNORE INTO prospect_cells(area, cell, categories) VALUES(?,?,?)",
                            (area, cell, cats))
            n += cur.rowcount
    return n


def pending_cells(area: str, categories: list[str], limit: int) -> list[dict]:
    _init()
    with db._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM prospect_cells WHERE area=? AND categories=? AND status!='done' ORDER BY id LIMIT ?",
            (area, ",".join(sorted(categories)), int(limit))).fetchall()]


def finish_cell(cell_id: int, *, status: str, candidates: int = 0, qualified: int = 0, note: str = "") -> None:
    with db._conn() as c:
        c.execute("UPDATE prospect_cells SET status=?, scanned_at=?, candidates=?, qualified=?, note=? WHERE id=?",
                  (status, now(), candidates, qualified, note[:300], cell_id))


def add_qualified(area: str, n: int) -> None:
    """Bump the qualified count on the most recently scanned cell of an area (coverage report)."""
    with db._conn() as c:
        c.execute("UPDATE prospect_cells SET qualified=qualified+? WHERE id=(SELECT id FROM prospect_cells "
                  "WHERE area=? AND status='done' ORDER BY scanned_at DESC LIMIT 1)", (n, area))


def coverage(area: str | None = None) -> list[dict]:
    _init()
    q = ("SELECT area, categories, COUNT(*) cells, SUM(status='done') done, SUM(status='failed') failed, "
         "SUM(candidates) candidates, SUM(qualified) qualified, MAX(scanned_at) last FROM prospect_cells")
    args = ()
    if area:
        q += " WHERE area=?"
        args = (area,)
    q += " GROUP BY area, categories ORDER BY area"
    with db._conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]
