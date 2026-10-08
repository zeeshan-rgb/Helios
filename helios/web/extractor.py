"""Clean content + metadata from a page's HTML (Trafilatura), with a visible-text fallback.

Trafilatura is built for articles; on forum threads, listings and app-like pages it can return
too little, so short results fall back to the page's visible text (the rendered text when the
hidden browser produced the HTML). Dates and authors are Trafilatura's best guess — reported as
"estimated". Secret-shaped strings are redacted; the result never contains raw HTML.
"""

from __future__ import annotations

import hashlib
import re

from .. import conf

MIN_ARTICLE_CHARS = 200
SHORT_ARTICLE = 1200          # below this, also keep the page's visible text (alt_text) for checks
MAX_TEXT = 60000
# Site chrome Trafilatura sometimes returns AS the article (live: a GitHub release page came back
# as only "You signed in with another tab or window. Reload to refresh your session.").
_BOILERPLATE = re.compile(
    r"(you signed (in|out) with another tab or window\.?|you switched accounts on another tab or window\.?|"
    r"reload to refresh your session\.?|dismiss alert|skip to (main )?content|"
    r"we use cookies[^.\n]*\.?|accept (all )?cookies|sign in to (view|continue)[^.\n]*\.?)", re.I)


def strip_boilerplate(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", _BOILERPLATE.sub(" ", text or "")).strip()
_JS_SHELL = re.compile(r"(enable javascript|javascript (is )?(required|disabled)|you need to enable "
                       r"javascript|please turn on javascript|this app works best with javascript)", re.I)


def _clean(text: str, limit: int = MAX_TEXT) -> str:
    t = conf.SECRET_RE.sub("[redacted]", text or "")
    t = re.sub(r"[ \t ]+", " ", re.sub(r"\r\n?", "\n", t))
    t = re.sub(r"\n[ \t]*\n[\s]*", "\n\n", t).strip()
    return t[:limit]


def visible_text(html: str) -> str:
    """Text a reader would see: body text without scripts, styles, templates or hidden noscript."""
    try:
        import lxml.html
        root = lxml.html.fromstring(html)
        for bad in root.xpath("//script|//style|//noscript|//template|//svg|//head"):
            bad.drop_tree()
        return root.text_content()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


def looks_like_js_shell(html: str, text: str) -> bool:
    """A page that needs JavaScript to show its content: tiny visible text, lots of script, or an
    explicit 'enable JavaScript' notice."""
    if not html:
        return False
    if _JS_SHELL.search(text or "") or (len((text or "").strip()) < 300 and len(html) > 20000):
        return True
    return False


def extract(html: str, url: str, *, rendered_text: str = "") -> dict:
    """{title, author, date, sitename, description, language, hostname, tags, pagetype, image,
    text, method, text_hash} — method is 'trafilatura' or 'visible-text'."""
    meta: dict = {}
    text = ""
    method = "trafilatura"
    try:
        import trafilatura
        # NOT deduplicate=True: that remembers text ACROSS calls, so re-reading the same page
        # (fresh=True, a second visit) silently discarded the article.
        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=True,
                                          include_tables=True, favor_recall=True)
        if doc is not None:
            d = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
            text = d.get("text") or ""
            meta = {k: d.get(k) for k in ("title", "author", "date", "sitename", "description",
                                          "language", "hostname", "pagetype", "image")}
            tags = d.get("tags") or d.get("categories") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in re.split(r"[,;]", tags.strip("[]'\" ")) if t.strip()]
            meta["tags"] = [str(t)[:40] for t in tags][:15]
    except Exception as e:  # pragma: no cover - library missing or broken input
        conf.log("web", f"trafilatura failed on {url[:120]}: {e}")
    text = strip_boilerplate(text)
    visible = None
    if len(text.strip()) < MIN_ARTICLE_CHARS:
        visible = strip_boilerplate(rendered_text or visible_text(html))
        if len(visible.strip()) > len(text.strip()):
            text, method = visible, "visible-text"
    if not meta.get("title"):
        m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
        meta["title"] = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    text = _clean(text)
    out = {k: (_clean(str(v), 300) if isinstance(v, str) else v) for k, v in meta.items() if v}
    out.update(text=text, method=method, chars=len(text),
               text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
    if method == "trafilatura" and len(text) < SHORT_ARTICLE:
        # a short "article" may be the wrong block — keep the whole visible text for source checks
        visible = visible if visible is not None else strip_boilerplate(rendered_text or visible_text(html))
        if len(visible) > 2 * len(text):
            out["alt_text"] = _clean(visible, 20000)
    return out
