"""Explicit, categorized memory items in the Obsidian vault (remember / recall / forget).

memory.py keeps the always-injected Profile + People/Projects notes + Daily logs; this module adds
the things the user asks Helios to remember, one Markdown note per item so each is inspectable and
individually deletable in Obsidian:

    <vault>/User/          facts about the user          <vault>/Decisions/  decisions made
    <vault>/Preferences/   how they like things done     <vault>/Rules/      standing rules
    <vault>/Projects/<P>/  facts about one project       <vault>/Skills/     how-tos that worked
    <vault>/Research/      findings (kept apart from stable memory)

Every item is timestamped, attributed to a source, deduplicated against its category, and never
holds a secret (anything credential-shaped is refused, not just redacted).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

from . import conf

CATEGORIES = {
    "user": "User", "preferences": "Preferences", "projects": "Projects", "decisions": "Decisions",
    "rules": "Rules", "skills": "Skills", "research": "Research",
}
_DUP_JACCARD = 0.8
_SECRET_HINT = re.compile(
    r"\b(pass(word|code|phrase)?|pin|otp|one[- ]time code|api[ _-]?key|secret|token|cvv|"
    r"card number|iban|account number|ssn|social security)\b\s*(is|was|=|:|-)\s*\S", re.I)
_RULE_HINT = re.compile(r"\b(always|never|must|don'?t|do not|require[sd]?|only ever)\b", re.I)
_PREF_HINT = re.compile(r"\b(prefer|like|love|hate|dislike|want|favou?rite|rather)\b", re.I)
_DECISION_HINT = re.compile(r"\b(decided|decision|we will|we'll|chose|going with|agreed)\b", re.I)


def vault() -> Path:
    """The same vault memory.py uses (one source of truth, redirected together in tests)."""
    from . import memory
    return Path(memory.VAULT)


def looks_secret(text: str) -> bool:
    from .memory import _SECRET_RE
    return bool(_SECRET_RE.search(text or "") or _SECRET_HINT.search(text or ""))


def classify(text: str) -> str:
    """Best-effort category for category='auto' (the model usually passes one explicitly)."""
    if _DECISION_HINT.search(text):
        return "decisions"
    if _RULE_HINT.search(text):
        return "rules"
    if _PREF_HINT.search(text):
        return "preferences"
    return "user"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (s or "").lower())).strip()


_STOP = {"the", "and", "for", "you", "your", "are", "was", "with", "that", "this", "what", "about",
          "tell", "have", "has", "had", "from", "not", "but", "all", "any", "can", "our", "out",
          "his", "her", "its", "they", "them", "who", "how", "why", "when", "where", "which", "will",
          "would", "should", "could", "remember", "know", "does", "did", "please", "sir", "into"}
_SUFFIXES = ("ences", "ence", "ings", "ing", "ers", "er", "ed", "es", "s")


def _stem(w: str) -> str:
    """Tiny suffix stripper so 'reporting'/'reports' and 'preference'/'prefers' meet."""
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _tokens(s: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in _STOP}


def _slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:48] or "item"


def _safe_project(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _-]", "", name or "").strip()[:60]


def _folder(category: str, project: str | None = None) -> Path:
    base = vault() / CATEGORIES[category]
    if category == "projects":
        return base / (_safe_project(project) or "General")
    return base


def _parse(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not raw.startswith("---"):
        return None
    head, _, body = raw[3:].partition("\n---")
    meta: dict = {}
    for line in head.strip().splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip()] = v.strip()
    if "id" not in meta or "category" not in meta:
        return None
    meta["text"] = body.strip()
    meta["path"] = str(path)
    try:
        meta["seen"] = int(meta.get("seen", 1))
    except ValueError:
        meta["seen"] = 1
    return meta


_FIELDS = ("id", "category", "project", "created", "updated", "source", "seen",
           "status", "confidence", "evidence")
STATUSES = ("active", "pending", "rejected")


def _one_line(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:300]


def _write(item: dict) -> None:
    lines = ["---"] + [f"{k}: {_one_line(item.get(k, ''))}" for k in _FIELDS]
    lines += ["---", "", item["text"], ""]
    Path(item["path"]).write_text("\n".join(lines), encoding="utf-8")


def is_active(it: dict) -> bool:
    """Items written before statuses existed count as active."""
    return (it.get("status") or "active") == "active"


def items(category: str | None = None, project: str | None = None) -> list[dict]:
    cats = [category] if category else list(CATEGORIES)
    out = []
    for c in cats:
        base = vault() / CATEGORIES[c]
        if not base.exists():
            continue
        pattern = "*/*.md" if c == "projects" else "*.md"
        for f in base.glob(pattern):
            it = _parse(f)
            if it and (not project or it.get("project", "").lower() == _safe_project(project).lower()):
                out.append(it)
    out.sort(key=lambda i: i.get("updated", ""), reverse=True)
    return out


def _find_duplicate(text: str, category: str, project: str | None) -> dict | None:
    n, toks = _norm(text), _tokens(text)
    for it in items(category, project if category == "projects" else None):
        if _norm(it["text"]) == n:
            return it
        other = _tokens(it["text"])
        if toks and other and len(toks & other) / len(toks | other) >= _DUP_JACCARD:
            return it
        # One statement fully containing the other (e.g. the same fact, re-said with more
        # detail) is the same memory; the fuller wording wins in remember().
        small, big = (toks, other) if len(toks) <= len(other) else (other, toks)
        if len(small) >= 3 and small <= big:
            return it
    return None


def remember(text: str, category: str = "auto", *, source: str = "chat",
             project: str | None = None, status: str = "active",
             confidence: float | None = None, evidence: str = "") -> dict:
    """Store one memory. Returns {"status": saved|duplicate|refused|rejected, "item": {...}|None,
    "reason": str}. `status` pending = a learned lesson awaiting the user's approval; an item the
    user once rejected is never re-proposed ("rejected"), and an explicit remember (active)
    promotes a pending duplicate."""
    status = status if status in STATUSES else "active"
    from .memory import _locked_call
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return {"status": "refused", "item": None, "reason": "nothing to remember"}
    if len(text) > 1000:
        return {"status": "refused", "item": None, "reason": "too long — summarize it first"}
    if looks_secret(text):
        return {"status": "refused", "item": None,
                "reason": "that looks like a password, key or other secret — I never store those"}
    category = (category or "auto").strip().lower()
    if category == "auto" or category not in CATEGORIES:
        category = "projects" if project else classify(text)
    if category == "projects" and not _safe_project(project or ""):
        project = "General"
    now = datetime.now().isoformat(timespec="seconds")

    def _do():
        dup = _find_duplicate(text, category, project)
        if dup:
            old_status = dup.get("status") or "active"
            if old_status == "rejected" and status != "active":
                return {"status": "rejected", "item": dup,
                        "reason": "the user rejected this before — not proposing it again"}
            dup["updated"] = now
            dup["seen"] = int(dup.get("seen", 1)) + 1
            if len(text) > len(dup["text"]):
                dup["text"] = text          # keep the more complete wording
            if status == "active":
                dup["status"] = "active"    # an explicit remember confirms a pending lesson
            if confidence is not None:
                try:
                    dup["confidence"] = f"{max(float(dup.get('confidence') or 0), confidence):.2f}"
                except ValueError:
                    dup["confidence"] = f"{confidence:.2f}"
            _write(dup)
            return {"status": "duplicate", "item": dup, "reason": "already known — refreshed"}
        folder = _folder(category, project)
        folder.mkdir(parents=True, exist_ok=True)
        iid = f"{category[:4]}-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}"
        item = {"id": iid, "category": category, "project": _safe_project(project or ""),
                "created": now, "updated": now, "source": source, "seen": 1, "text": text,
                "status": status,
                "confidence": "" if confidence is None else f"{confidence:.2f}",
                "evidence": evidence,
                "path": str(folder / f"{_slug(text)}-{iid[-6:]}.md")}
        _write(item)
        return {"status": "saved", "item": item, "reason": ""}

    res = _locked_call(_do)
    conf.log("memory", f"remember [{category}] {res['status']}: {text[:80]!r}")
    _refresh_index()
    return res


def _refresh_index() -> None:
    try:
        from . import memory
        if Path(vault()).resolve() != Path(memory.VAULT).resolve():
            return   # a redirected vault (tests): never rewrite the real vault's index
        memory._rebuild_index()
    except Exception as e:  # pragma: no cover
        conf.log("memory", f"index refresh failed: {e}")


def set_status(item_id: str, status: str) -> dict | None:
    """Approve (active) / reject / re-queue (pending) one item by id. Returns the item or None."""
    from .memory import _locked_call
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    target = next((it for it in items() if it["id"] == (item_id or "").strip()), None)
    if target is None:
        return None

    def _do():
        target["status"] = status
        target["updated"] = datetime.now().isoformat(timespec="seconds")
        _write(target)
    _locked_call(_do)
    conf.log("memory", f"status {target['id']} -> {status}")
    _refresh_index()
    return target


def pending() -> list[dict]:
    return [it for it in items() if (it.get("status") or "active") == "pending"]


def recall(query: str, category: str | None = None, project: str | None = None,
           limit: int = 8, include_inactive: bool = False) -> list[dict]:
    """Keyword + recency ranked search over ACTIVE items (pending/rejected lessons are excluded
    unless include_inactive). An empty query lists the most recent items."""
    pool = [it for it in items(category, project) if include_inactive or is_active(it)]
    if not (query or "").strip():
        return pool[:limit]
    q = _tokens(query)
    scored = []
    for rank, it in enumerate(pool):                       # pool is newest-first
        hay = _tokens(it["text"]) | _tokens(it.get("project", "")) | {it["category"]}
        overlap = len(q & hay)
        if overlap:
            scored.append((overlap + max(0.0, 0.5 - rank * 0.01), it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def forget(ref: str) -> dict:
    """Delete by exact id, or by a query that matches exactly one item. Several matches -> nothing
    deleted, candidates returned (the caller asks which one)."""
    from .memory import _locked_call
    ref = (ref or "").strip()
    if not ref:
        return {"deleted": [], "candidates": []}
    exact = [it for it in items() if it["id"] == ref]
    matches = exact or recall(ref, limit=5, include_inactive=True)
    if exact or len(matches) == 1:
        target = matches[0]

        def _do():
            Path(target["path"]).unlink(missing_ok=True)
        _locked_call(_do)
        conf.log("memory", f"forget {target['id']}: {target['text'][:80]!r}")
        _refresh_index()
        return {"deleted": [target], "candidates": []}
    return {"deleted": [], "candidates": matches}


def digest_section(message: str) -> str:
    """Remembered items for the per-turn MEMORY block: every rule and preference (they apply
    everywhere) plus the items most relevant to this message from the other categories."""
    lines = []
    for cat in ("rules", "preferences"):
        for it in [i for i in items(cat) if is_active(i)][:25]:
            lines.append(f"- [{cat[:-1]}] {it['text']}")
    seen = {l for l in lines}
    for it in recall(message, limit=8):
        if it["category"] in ("rules", "preferences"):
            continue
        tag = f"project {it['project']}" if it["category"] == "projects" else it["category"]
        line = f"- [{tag}] {it['text']}"
        if line not in seen:
            lines.append(line)
    return "\n".join(lines)


def format_items(found: list[dict]) -> str:
    if not found:
        return "(nothing remembered yet)"
    out = []
    for it in found:
        where = f" · {it['project']}" if it.get("project") else ""
        state = "" if is_active(it) else f" <{it.get('status')}>"
        conf_ = f", confidence {it['confidence']}" if it.get("confidence") else ""
        out.append(f"{it['id']} [{it['category']}{where}]{state} {it['text']} "
                   f"(since {it.get('created', '?')[:10]}, via {it.get('source', '?')}{conf_})")
        if it.get("evidence"):
            out.append(f"    evidence: {it['evidence']}")
    return "\n".join(out)
