"""Editable quotation / proposal for one prospect — a Markdown note in the vault (Proposals/).

Built only from what Helios verified (audit findings with their evidence, the scoring reasons) and
YOUR rate card: no price is ever invented — a package without a rate-card entry says so. The file is
yours to edit in Obsidian; Helios never overwrites an edited proposal (it writes a new version next
to it instead) and never sends it anywhere.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from .. import memory_store
from . import quote as quoting
from .scoring import PACKAGES, label

_MARK = re.compile(r"\n?<!-- helios:generated sha=([0-9a-f]{12}) -->\s*$")


def folder() -> Path:
    return memory_store.vault() / "Proposals"


def _slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")[:40] or "business"


def _sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def _money(q: dict, item: dict) -> str:
    if item.get("min"):
        sym = quoting.symbol(q.get("currency") or quoting.currency())
        return f"{sym}{item['min']:,}–{sym}{item['max']:,}"
    return "price to be confirmed (not in your rate card yet)"


def render(p: dict) -> str:
    q = p.get("quote") or quoting.build(p)
    reasons = p.get("reasons") or {}
    name = p["name"]
    when = datetime.now().strftime("%d %B %Y")
    pkg = p.get("package") or q.get("package") or ""
    lines = [f"# Proposal — {name}", "",
             f"*Prepared {when} · {label(p.get('category') or '')} · {(p.get('area') or '').split(',')[0]}*", "",
             "> Draft for you to edit. Helios never sends this — you decide what goes to the client.", ""]

    lines += ["## What we noticed", ""]
    found = [*(reasons.get("website") or []), *(reasons.get("ai") or [])]
    if found:
        for r in found:
            ev = r.get("evidence") or ""
            lines.append(f"- **{r['factor']}** — {r.get('detail', '')}" + (f" *(evidence: {ev})*" if ev else ""))
    else:
        lines.append("- (no specific gaps recorded — research this business before sending a proposal)")
    lines.append("")

    if not pkg:
        lines += ["## Recommended package", "", "Not chosen yet — research the business first.", ""]
        return "\n".join(lines)

    lines += ["## Recommended package", "", f"**{PACKAGES.get(pkg, pkg)}**", ""]
    if reasons.get("priority"):
        lines += [f"Why this business: {reasons['priority']}", ""]
    lines += ["## Scope and deliverables", ""] + [f"- {f}" for f in q.get("features") or []] + [""]

    if q.get("optional_addons"):
        lines += ["## Optional integrations", ""]
        for a in q["optional_addons"]:
            lines.append(f"- {a['name']} — {_money(q, a)}")
        if any("WhatsApp" in a["name"] for a in q["optional_addons"]):
            lines.append("- WhatsApp is click-to-chat started by the visitor (opt-in); any automated messaging "
                         "uses the official WhatsApp Business platform and its rules.")
        lines.append("")

    lines += ["## Investment", ""]
    if q.get("status") == "PRICED":
        lines += [f"**{q['display']}** (from your rate card)", ""]
    else:
        key = quoting.PRICE_KEYS.get(pkg, "")
        lines += ["**Price to be confirmed.** This package isn't in your rate card yet "
                  f"(add `{key} = [min, max]` under `[prospecting.pricing]`). Helios never invents a price.", ""]
    lines += ["## Timeline", "", f"About {q.get('timeline', '—')} from the go-ahead and receipt of content.", ""]
    lines += ["## Assumptions", ""] + [f"- {a}" for a in q.get("assumptions") or []] + [""]
    lines += ["## Not included", ""] + [f"- {x}" for x in q.get("exclusions") or []] + [""]

    src = [f"- Listing: {p.get('source_name') or 'source'} — {p.get('source_url')}" if p.get("source_url") else ""]
    if (p.get("audit") or {}).get("url"):
        src.append(f"- Website audited: {p['audit']['url']} ({(p.get('last_site_check') or '')[:16].replace('T', ' ')})")
    lines += ["## Sources", ""] + [s for s in src if s] + [""]
    return "\n".join(lines)


def write(p: dict) -> dict:
    """Write (or refresh) the proposal note. Returns {"path", "text", "version", "status"} where status
    is created | refreshed | unchanged | new_version (the previous file had your edits)."""
    body = render(p)
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    base = f"{_slug(p['name'])}-{p['id']}"
    path, version = d / f"{base}.md", 1
    while path.exists():
        old = path.read_text(encoding="utf-8")
        m = _MARK.search(old)
        if m and _sha(_MARK.sub("", old)) == m.group(1):          # still exactly what Helios wrote
            if _sha(body) == m.group(1):
                return {"path": str(path), "text": body, "version": version, "status": "unchanged"}
            path.write_text(f"{body}\n<!-- helios:generated sha={_sha(body)} -->\n", encoding="utf-8")
            return {"path": str(path), "text": body, "version": version, "status": "refreshed"}
        version += 1                                               # edited by you → keep it, write v2…
        path = d / f"{base}-v{version}.md"
    path.write_text(f"{body}\n<!-- helios:generated sha={_sha(body)} -->\n", encoding="utf-8")
    return {"path": str(path), "text": body, "version": version,
            "status": "created" if version == 1 else "new_version"}
