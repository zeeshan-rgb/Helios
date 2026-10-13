"""Deterministic scores with stated reasons — never an unexplained "AI score".

website_score  0 = no website discovered · 1 = broken / severe problems · 2 = significant
               improvement opportunity · 3 = moderate · 4 = good but improvable · 5 = strong
ai_score       0 = no clear need … 5 = obvious business use case — every point needs a concrete
               customer-journey reason (category + what the site lacks), not "AI is available"
priority       A / B / C (A needs a usable public contact)
package        A basic site · B modern responsive site · C redesign + SEO/analytics ·
               D site + WhatsApp enquiries · E site + AI chatbot · F site + AI lead capture +
               WhatsApp · G custom / e-commerce · H add-ons only (good site, AI/WhatsApp gap)
"""

from __future__ import annotations

from . import geo

PACKAGES = {
    "A": "Basic business website",
    "B": "Modern responsive website",
    "C": "Website redesign + SEO / basic analytics",
    "D": "Website + WhatsApp enquiry system",
    "E": "Website + AI chatbot",
    "F": "Website + AI lead capture + WhatsApp workflow",
    "G": "Custom / e-commerce solution",
    "H": "Add-ons for an existing site (WhatsApp / AI assistant)",
}
PACKAGE_FEATURES = {
    "A": ["3-5 page mobile-friendly site", "contact details, map and hours", "basic SEO setup"],
    "B": ["responsive design that works on phones", "clear calls to action", "contact form", "basic SEO"],
    "C": ["redesign of the existing site", "mobile layout fixes", "speed improvements", "SEO metadata + analytics"],
    "D": ["mobile-friendly site", "WhatsApp click-to-chat buttons (visitor starts the chat)", "enquiry form"],
    "E": ["mobile-friendly site", "AI assistant answering FAQs from approved business information",
          "hand-off to a human"],
    "F": ["mobile-friendly site", "AI assistant that collects enquiry/booking details",
          "WhatsApp hand-off after the visitor opts in", "lead notifications to the business"],
    "G": ["online catalogue / store", "cart and checkout", "order notifications"],
    "H": ["WhatsApp click-to-chat (opt-in)", "AI FAQ / enquiry assistant on the existing site"],
}


_LABELS = {"salon": ("salon", "salons"), "clinic": ("clinic", "clinics"), "dentist": ("dental clinic", "dental clinics"),
           "restaurant": ("restaurant", "restaurants"), "bakery": ("bakery", "bakeries"), "retail": ("shop", "shops"),
           "repair": ("repair shop", "repair shops"), "fitness": ("fitness centre", "fitness centres"),
           "education": ("training centre", "training centres"), "professional": ("professional office", "professional offices"),
           "hospitality": ("guest house", "guest houses"), "workshop": ("workshop", "workshops")}


# Third-party booking / delivery platforms — a business listed there has an online channel, just not
# its own (changes the AI reason from "can't order online" to "pays a platform for it").
PLATFORMS = ("zomato.com", "swiggy.com", "ubereats", "magicpin", "practo.com", "fresha.com", "booksy.com",
             "urbancompany.com", "justdial.com/order", "dunzo")


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    h = urlsplit(url).hostname or url
    return h[4:] if h.startswith("www.") else h


def label(cat: str, plural: bool = False, article: bool = False) -> str:
    one, many = _LABELS.get(cat or "", ("business", "businesses"))
    if plural:
        return many
    return (("an " if one[0] in "aeiou" else "a ") + one) if article else one


def _r(factor: str, detail: str, evidence: str = "") -> dict:
    return {"factor": factor, "detail": detail, "evidence": evidence}


def website_score(p: dict) -> tuple[int, list[dict]]:
    st = p.get("website_status")
    a = p.get("audit") or {}
    if st == "none":
        return 0, [_r("no website discovered", "OpenStreetMap lists none and a web search found none",
                      p.get("source_url", ""))]
    if st in ("unlisted", "unchecked") and not p.get("website"):
        # one source saying "no website" isn't enough to pitch a website
        return -1, [_r("website not verified", "OpenStreetMap lists none — not yet confirmed by a web search",
                       p.get("source_url", ""))]
    if st == "broken":
        return 1, [_r("website broken", a.get("note") or "failed on two separate checks", a.get("url", ""))]
    if st in ("not_audited", "refused", "unreachable"):
        return -1, [_r("website not audited", a.get("note") or st, a.get("url", p.get("website", "")))]
    score, reasons = 5.0, []

    def minus(pts, factor, detail, ev=""):
        nonlocal score
        score -= pts
        reasons.append(_r(factor, detail, ev))
    if not a.get("https"):
        minus(1, "no HTTPS", "the site loads over plain http — browsers mark it 'Not secure'", a.get("url", ""))
    if a.get("mobile_overflow") or not a.get("has_viewport"):
        minus(1, "poor mobile layout", "the page doesn't fit a phone screen (no responsive viewport / sideways scrolling)")
    ms = a.get("load_ms") or 0
    if ms > 8000:
        minus(1.5, "very slow", f"took {ms / 1000:.1f}s to load (without images)")
    elif ms > 4500:
        minus(0.5, "slow", f"took {ms / 1000:.1f}s to load (without images)")
    if not (a.get("phones") or a.get("emails") or a.get("contact_form") or a.get("whatsapp")):
        minus(1, "hard to contact", "no phone, email, form or WhatsApp found on the page")
    if a.get("outdated"):
        minus(1, "looks outdated", f"copyright year {a.get('copyright_year')}")
    if not a.get("title") or not a.get("description"):
        minus(0.5, "weak SEO basics", "missing page title or meta description")
    if len(a.get("broken_links") or []) >= 2:
        minus(0.5, "broken links", ", ".join((a.get("broken_links") or [])[:2]))
    if (a.get("console_errors") or 0) >= 3:
        minus(0.5, "script errors", f"{a['console_errors']} JavaScript errors while loading")
    if not reasons:
        reasons.append(_r("strong website", "no significant problems found in the audit", a.get("url", "")))
    return max(1, min(5, round(score))), reasons


def ai_score(p: dict) -> tuple[int, list[dict]]:
    cat = p.get("category") or ""
    a = p.get("audit") or {}
    has_site = p.get("website_status") == "ok"
    pts, reasons = 0, []

    def add(n, factor, detail, ev=""):
        nonlocal pts
        pts += n
        reasons.append(_r(factor, detail, ev))
    if a.get("chatbot"):
        return 0, [_r("already has a chat assistant", f"uses {a['chatbot']}")]
    platforms = [s for s in (p.get("socials") or []) if any(d in s.lower() for d in PLATFORMS)]
    ev = p.get("website") or p.get("source_url", "")
    if cat in geo.APPOINTMENT and not (has_site and a.get("booking")):
        if platforms and not has_site:
            add(1, "bookings only through a third-party platform",
                f"bookings go through a listing platform ({_host(platforms[0])}) rather than its own channel — a "
                "direct booking assistant would keep those customers and avoid the platform's fees", platforms[0])
        elif has_site:
            add(2, "appointment business without online booking",
                f"{label(cat, article=True)} works by appointment, but its website has no way to book — an AI "
                "booking assistant could collect requests 24/7 and pass them to the business", ev)
        else:
            add(2, "appointment business without online booking",
                f"{label(cat, article=True)} works by appointment but has no website of its own, so customers can't "
                "book directly online — an AI booking assistant could collect requests 24/7", ev)
    if cat in geo.ORDERING and not (has_site and a.get("ordering")):
        if platforms and not has_site:
            add(1, "orders only through delivery apps",
                f"orders go through {_host(platforms[0])} rather than its own channel — direct ordering (website / "
                "WhatsApp, visitor opts in) would avoid the app's commission", platforms[0])
        elif has_site:
            add(2, "takes orders without an online ordering path",
                f"{label(cat, article=True)} gets order enquiries, but its website has no way to order — an AI/"
                "WhatsApp order assistant could take them after hours", ev)
        else:
            add(2, "takes orders without an online ordering path",
                f"{label(cat, article=True)} gets order enquiries but has no website of its own, so customers can't "
                "order directly online — an AI/WhatsApp order assistant could take them after hours", ev)
    contact_routes = has_site and (a.get("contact_form") or a.get("emails") or a.get("whatsapp"))
    if (p.get("phone") or (a.get("phones") if has_site else None)) and not contact_routes:
        add(1, "enquiries only by phone",
            "the only contact route is a phone number, so calls outside opening hours are missed — an AI "
            "enquiry assistant could capture them")
    if has_site and not a.get("whatsapp") and cat in (geo.APPOINTMENT | geo.ORDERING | geo.RETAIL):
        add(1, "no WhatsApp option", "customers can't start a WhatsApp chat from the site (opt-in click-to-chat)")
    if has_site and (a.get("service_mentions") or 0) >= 8 and (a.get("words") or 0) > 600:
        add(1, "many services / FAQs", "the site lists many services — an AI FAQ assistant could answer "
            "repeat questions from the business's own approved information")
    return min(5, pts), reasons


def priority(p: dict, ws: int, ai: int) -> tuple[str, str]:
    contact = bool(p.get("email") or p.get("phone"))
    if ws == -1:
        return "C", ("website not verified yet" if p.get("website_status") in ("unlisted", "unchecked")
                     else "website exists but couldn't be audited")
    if contact and (ws <= 2 or ai >= 4):
        return "A", "clear website/AI gap and a public contact"
    if ws <= 3 or ai >= 3:
        return "B", "moderate opportunity" + ("" if contact else " (no public contact found)")
    return "C", "strong website, small opportunity"


def package_for(p: dict, ws: int, ai: int) -> str:
    cat = p.get("category") or ""
    a = p.get("audit") or {}
    if ws == 0:
        if cat in geo.RETAIL:
            return "G" if ai >= 3 else "D"
        if cat in (geo.APPOINTMENT | geo.ORDERING):
            return "F" if ai >= 3 else "D"
        return "A"
    if ws == -1:
        return ""
    if ws <= 2:
        return "F" if ai >= 4 else "C"
    if ws == 3:
        return "B" if (a.get("mobile_overflow") or not a.get("has_viewport")) else ("E" if ai >= 3 else "C")
    return "H" if ai >= 2 else ""


def score(p: dict) -> dict:
    """All scores + reasons + package for a prospect (pure function of its stored facts)."""
    ws, wr = website_score(p)
    ai, ar = ai_score(p)
    pr, why = priority(p, ws, ai)
    pkg = package_for(p, ws, ai)
    qualified = bool(pkg) and pr in ("A", "B")
    return {"website_score": ws if ws >= 0 else None, "ai_score": ai, "priority": pr, "package": pkg,
            "qualified": qualified,
            "reasons": {"website": wr, "ai": ar, "priority": why,
                        "package": f"{pkg}: {PACKAGES[pkg]}" if pkg else "no package — not a strong fit"}}
