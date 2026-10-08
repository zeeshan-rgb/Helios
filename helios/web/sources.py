"""Source verification for research findings and leads: does the cited page exist, what does it
really say, and does it support the claim?

    source_url (often a search engine redirect link)
      → resolve the redirect to the real page
      → read it through the pipeline (scraper.get, purpose="crawl": robots.txt, spacing, rendering)
      → provenance: final URL, page title, site, estimated date, fetch time, rendered?, method
      → grounding: how much of the claim's wording the page actually contains (0..1)

status:
  ok       read the page                      (grounding measured)
  blocked  the site refuses automated reading (403/429, bot check, robots.txt) — it EXISTS,
           so the claim isn't penalised, but it couldn't be confirmed directly
  failed   dead / not found / unreachable     (callers drop or down-weight)
When the pipeline can't read a page for a network-level reason, a plain "does the link open"
check (research.check_source) still tells blocked/ok/failed apart — never for robots.txt refusals.
"""

from __future__ import annotations

import re

from .. import memory_store
from . import scraper

_BLOCKED = ("refused", "rate-limited", "needs a login", "bot check", "robots.txt", "legal reasons",
            "access wall")
_HTTP_DEAD = re.compile(r"^HTTP (404|410|5\d\d)")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
STRONG, WEAK = 0.5, 0.25


def resolve(url: str) -> str:
    """Search-engine redirect links → the real page (a plain redirect follow; every hop checked)."""
    from .. import research
    if not research._redirector(url):
        return url
    final, _status = research.check_source(url)
    return final if final and research._valid_url(final) else url


def coverage(claim: str, text: str) -> float:
    """Share of the claim's significant words that appear on the page (0..1)."""
    want = memory_store._tokens(claim or "")
    if not want:
        return 0.0
    have = memory_store._tokens(text or "")
    return round(len(want & have) / len(want), 2)


def grounding_label(cov: float | None) -> str:
    if cov is None:
        return "not checked"
    return "strong" if cov >= STRONG else "partial" if cov >= WEAK else "weak"


def excerpt(text: str, claim: str, limit: int = 300) -> str:
    """The passage that best matches the claim, with phone numbers and email addresses removed."""
    want = memory_store._tokens(claim or "")
    best, score = "", -1
    for para in re.split(r"\n\s*\n|\n", text or ""):
        para = para.strip()
        if len(para) < 30:
            continue
        s = len(want & memory_store._tokens(para))
        if s > score:
            best, score = para, s
    best = _EMAIL.sub("[email removed]", _PHONE.sub("[number removed]", best))
    return best[:limit] + ("…" if len(best) > limit else "")


def check(url: str, claim: str = "", *, getter=None, resolver=None, opener=None) -> dict:
    """Verify one cited source. Always returns a dict with 'status' and 'final_url'."""
    from .. import research
    target = (resolver or resolve)(url)
    r = (getter or scraper.get)(target, purpose="crawl")
    if r.get("error"):
        err = r["error"]
        final = r.get("final_url") or target
        if any(m in err for m in _BLOCKED):
            return {"status": "blocked", "final_url": final, "reason": err, "read": False}
        if _HTTP_DEAD.match(err) or "blocked:" in err:
            return {"status": "failed", "final_url": final, "reason": err, "read": False}
        # network-level trouble reading it — does the link at least open?
        final2, status = (opener or research.check_source)(final)
        return {"status": status, "final_url": final2 or final, "read": False,
                "reason": f"{err}; plain link check: {status}"}
    text = r.get("text") or ""
    if r.get("alt_text"):            # short extraction: judge support on the whole visible page too
        text = f"{text}\n\n{r['alt_text']}"
    cov = coverage(claim, text) if claim else None
    return {"status": "ok", "final_url": r.get("final_url") or target, "read": True,
            "page_title": r.get("title", ""), "site": r.get("sitename") or r.get("hostname", ""),
            "page_date": r.get("date", ""), "fetched_at": r.get("fetched_at", ""),
            "rendered": bool(r.get("rendered")), "method": r.get("method", ""),
            "text_hash": r.get("text_hash", ""), "coverage": cov, "grounding": grounding_label(cov),
            "excerpt": excerpt(text, claim) if claim else "", "text": text}


def normalize(checked, url: str) -> dict:
    """Accept the old verifier shapes too: 'ok' | (final_url, status) | dict."""
    if isinstance(checked, dict):
        return checked
    if isinstance(checked, tuple):
        return {"status": checked[1], "final_url": checked[0] or url, "read": False}
    return {"status": str(checked), "final_url": url, "read": False}
