"""Website status + audit for one prospect.

website_status:
  none        no website discovered (OSM lists none; the web search, when enabled, found none)
  unlisted    OSM lists none — not yet searched elsewhere (not claimed as "no website")
  ok          loaded and audited
  not_audited robots.txt asks automated visitors not to crawl it (exists; not audited)
  refused     the site refuses automated visitors / shows a bot check (exists; not audited)
  unreachable failed once — re-checked on a later run before anything is concluded
  broken      failed on 2+ separate checks at least 6 hours apart
Never "no website" or "broken" from a single failed request.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .. import conf
from ..web import policy, scraper
from . import geo, store

BOOKING_HINTS = re.compile(r"\b(book (now|online|an appointment)|book appointment|schedule (a|an|your) "
                           r"(visit|appointment|consultation)|appointment|reserve (a )?table|reservations?)\b", re.I)
BOOKING_DOMAINS = ("calendly.com", "fresha.com", "booksy.com", "setmore.com", "practo.com", "zocdoc.com",
                   "simplybook", "square.site", "acuityscheduling", "youcanbook.me", "vagaro.com", "mindbodyonline")
ORDER_HINTS = re.compile(r"\b(order online|order now|add to cart|checkout|buy now|shop now|cart)\b", re.I)
ORDER_DOMAINS = ("swiggy.com", "zomato.com", "ubereats.com", "doordash.com", "deliveroo", "dunzo", "shopify")
REVIEW_HINTS = re.compile(r"\b(testimonials?|reviews?|what our (clients|customers|patients) say|rated \d|★)\b", re.I)
HOURS_HINTS = re.compile(r"\b(opening hours|business hours|open (daily|mon|monday|from)|mon(day)?\s*[-–]\s*(sat|sun|fri)|\d{1,2}(:\d{2})?\s*(am|pm)\s*[-–])", re.I)
CHATBOT_SCRIPTS = ("tawk.to", "intercom", "crisp.chat", "tidio", "drift.com", "livechatinc", "zendesk",
                   "freshchat", "hubspot.com/conversations", "botpress", "chatbase", "wati.io", "interakt")
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "youtube.com", "x.com", "twitter.com")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NOT_CONTACT = re.compile(r"^(no-?reply|do-?not-?reply|mailer|postmaster|abuse|privacy|security|password|"
                          r"auth|accounts?|verify|bounce|unsubscribe)[@.+-]", re.I)
# No dots inside (versions / dates), but a sentence-ending full stop after it is fine.
_PHONE = re.compile(r"(?<!\d)(?<!\d\.)\+?\d[\d\s()-]{8,16}\d(?!\d|\.\d)")


def phones_in(text: str) -> list[str]:
    """Phone-number-shaped strings: 10-13 digits in at most 4 groups (a lone 1-3 digit country code
    after '+' allowed), at least one group of 4+ digits, no one-digit groups, not a date. (Live:
    python.org's code samples and event dates matched a looser pattern.)"""
    out = []
    for m in _PHONE.findall(text or ""):
        s = m.strip()
        digits = re.sub(r"\D", "", s)
        if not 10 <= len(digits) <= 13 or len(set(digits)) <= 2 or re.search(r"\d{4}-\d{2}-\d{2}", s):
            continue
        groups = re.findall(r"\d+", s)
        if s.startswith("+") and len(groups[0]) <= 3:
            groups = groups[1:]
        if not groups or len(groups) > 4 or min(len(g) for g in groups) < 2 or max(len(g) for g in groups) < 4:
            continue
        out.append(re.sub(r"\s+", " ", s))
    return list(dict.fromkeys(out))


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def business_emails(candidates: list[tuple[str, str]], domain: str) -> list[tuple[str, str]]:
    """(email, source) — public business contacts only: no system/no-reply addresses; addresses on
    the business's own domain first."""
    out, seen = [], set()
    for email, source in candidates:
        e = email.strip().strip(".").lower()
        if not _EMAIL.fullmatch(e) or e in seen or _NOT_CONTACT.match(e):
            continue
        if re.search(r"\.(png|jpe?g|gif|webp|svg)$", e):        # image file names that look like emails
            continue
        seen.add(e)
        out.append((e, source))
    out.sort(key=lambda x: (not (domain and x[0].endswith("@" + domain)), x[0]))
    return out


def analyse(facts: dict, *, website: str) -> dict:
    """Raw page facts -> business-relevant findings (each a plain fact with evidence)."""
    text = facts.get("text") or ""
    links = facts.get("links") or []
    hrefs = [l.get("href", "") for l in links]
    final = facts.get("final_url") or website
    domain = store.domain_of(final)
    scripts = " ".join(facts.get("scripts") or []).lower()
    forms = facts.get("forms") or []
    year = datetime.now().year
    years = [int(y) for y in re.findall(r"(?:©|copyright|\(c\))\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", text, re.I)]
    mailtos = [(h.split(":", 1)[1].split("?")[0], "its website (mailto link)") for h in hrefs if h.lower().startswith("mailto:")]
    text_emails = [(e, "its website") for e in _EMAIL.findall(text)]
    wa = [h for h in hrefs if re.search(r"(wa\.me/|api\.whatsapp\.com|whatsapp://|web\.whatsapp)", h, re.I)]
    booking_links = [h for h in hrefs if any(d in h.lower() for d in BOOKING_DOMAINS)]
    order_links = [h for h in hrefs if any(d in h.lower() for d in ORDER_DOMAINS)]
    contact_forms = [f for f in forms if f.get("textarea") or f.get("email") or f.get("tel")]
    out = {
        "url": final, "status": facts.get("status"), "https": final.lower().startswith("https://"),
        "redirects": facts.get("redirects") or [], "load_ms": facts.get("load_ms"),
        "title": (facts.get("title") or "")[:120], "description": (facts.get("description") or "")[:200],
        "has_viewport": "width=device-width" in (facts.get("viewport") or "").replace(" ", ""),
        "mobile_overflow": bool(facts.get("mobile_overflow")),
        "console_errors": len(facts.get("console_errors") or []),
        "h1": facts.get("h1", 0), "json_ld": facts.get("json_ld") or [],
        "local_business_schema": any("business" in t.lower() or t in ("Restaurant", "Dentist", "MedicalClinic",
                                     "HairSalon", "BeautySalon", "Store") for t in (facts.get("json_ld") or [])),
        "copyright_year": max(years) if years else None,
        "outdated": bool(years) and max(years) <= year - 3,
        "phones": list(dict.fromkeys([h.split(":", 1)[1] for h in hrefs if h.lower().startswith("tel:")][:2]
                                     + phones_in(text)[:3]))[:4],
        "emails": business_emails(mailtos + text_emails, domain)[:3],
        "contact_form": bool(contact_forms),
        "whatsapp": bool(wa) or bool(re.search(r"\bwhatsapp\b", text, re.I)),
        "booking": bool(booking_links) or bool(BOOKING_HINTS.search(text)),
        "booking_evidence": (booking_links[:1] or [m.group(0) for m in [BOOKING_HINTS.search(text)] if m])[:1],
        "ordering": bool(order_links) or bool(ORDER_HINTS.search(text)),
        "reviews": bool(REVIEW_HINTS.search(text)),
        "hours": bool(HOURS_HINTS.search(text)),
        "map": any(("google.com/maps" in f or "openstreetmap" in f or "maps.google" in f) for f in (facts.get("iframes") or [])),
        "address": bool(re.search(r"\b(road|rd\.?|street|st\.?|nagar|colony|lane|sector|floor|plot|"
                                  r"near|opp\.?|\d{6})\b", text, re.I)),
        "social": sorted({urlsplit(h).hostname.replace("www.", "") for h in hrefs
                          if urlsplit(h).hostname and any(s in urlsplit(h).hostname for s in SOCIAL_HOSTS)}),
        "chatbot": next((c for c in CHATBOT_SCRIPTS if c in scripts), ""),
        "nav_links": facts.get("nav_links", 0), "words": len(text.split()),
        "service_mentions": len(re.findall(r"\b(services?|treatments?|packages?|menu|courses?|products?)\b", text, re.I)),
        "images_without_alt": facts.get("images_without_alt", 0),
    }
    out["internal_links"] = [h for h in hrefs if store.domain_of(h) == domain and h.startswith("http")
                             and not re.search(r"\.(pdf|jpe?g|png|zip)$", h, re.I)][:40]
    return out


def check_links(found: dict, *, fetch=None, limit: int = 5) -> dict:
    """A few internal links + robots.txt / sitemap.xml (polite: same spacing as crawling)."""
    fetch = fetch or scraper.fetch_http
    broken = []
    for h in list(dict.fromkeys(found.get("internal_links") or []))[1:limit + 1]:
        scraper._space_out(h)
        p = fetch(h)
        if p.status and p.status >= 400 and p.status not in (401, 403, 429):
            broken.append(f"{h} (HTTP {p.status})")
    root = "{0.scheme}://{0.netloc}".format(urlsplit(found["url"]))
    sm = fetch(root + "/sitemap.xml")
    return {"broken_links": broken, "sitemap": bool(sm.status and sm.status < 400 and "xml" in (sm.html[:200] + sm.content_type)),
            "links_checked": min(limit, max(0, len(found.get("internal_links") or []) - 1))}


def _recheck_due(p: dict) -> bool:
    if not p.get("last_site_check"):
        return True
    try:
        return datetime.now() - datetime.fromisoformat(p["last_site_check"]) >= timedelta(hours=6)
    except ValueError:
        return True


def audit_prospect(p: dict, *, auditor=None, fetch=None, robots=None, link_check=True) -> dict:
    """Decide website_status and (when allowed) audit it. Returns fields to store."""
    url = normalize_url(p.get("website", ""))
    now = store.now()
    if not url:
        return {"website_status": p.get("website_status") if p.get("website_status") == "none" else "unlisted",
                "audit": {}, "last_site_check": now}
    if geo.forbidden_source(url):
        return {"website_status": "unlisted", "website": "", "domain": "", "last_site_check": now,
                "audit": {"note": geo.forbidden_source(url)}}
    why = policy.check_url(url)
    if why:
        return {"website_status": "unreachable", "audit": {"note": f"not checked: {why}"}, "last_site_check": now}
    if not (robots or scraper.robots_allows)(url):
        return {"website_status": "not_audited", "audit": {"url": url, "note": "robots.txt asks bots not to crawl this site"},
                "last_site_check": now}
    from ..web import browser
    try:
        facts = (auditor or browser.shared().audit)(url)
    except Exception as e:
        failures = int(p.get("site_failures") or 0) + (1 if _recheck_due(p) else 0)
        status = "broken" if failures >= 2 else "unreachable"
        return {"website_status": status, "site_failures": failures, "last_site_check": now,
                "audit": {"url": url, "error": str(e)[:200],
                          "note": "failed on 2 separate checks" if status == "broken" else "failed once — will re-check later"}}
    text_head = (facts.get("title", "") + " " + (facts.get("text") or "")[:600])
    if scraper._BOT_WALL.search(text_head) and len(facts.get("text") or "") < 1500:
        return {"website_status": "refused", "last_site_check": now,
                "audit": {"url": url, "note": "the site shows a bot check — not audited (not bypassed)"}}
    if (facts.get("status") or 0) >= 500:
        failures = int(p.get("site_failures") or 0) + (1 if _recheck_due(p) else 0)
        return {"website_status": "broken" if failures >= 2 else "unreachable", "site_failures": failures,
                "last_site_check": now, "audit": {"url": url, "error": f"HTTP {facts.get('status')}"}}
    found = analyse(facts, website=url)
    if link_check:
        try:
            found.update(check_links(found, fetch=fetch))
        except Exception as e:  # pragma: no cover - network trouble on extras isn't fatal
            conf.log("prospecting", f"link check failed for {url}: {e}")
    fields = {"website_status": "ok", "site_failures": 0, "audit": found, "last_site_check": now,
              "domain": store.domain_of(found["url"])}
    if not p.get("email") and found["emails"]:
        fields["email"], fields["email_source"] = found["emails"][0]
    if not p.get("phone") and found["phones"]:
        fields["phone"] = found["phones"][0]
    return fields
