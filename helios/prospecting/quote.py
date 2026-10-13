"""Quotation from YOUR rate card — Helios never invents a price.

[prospecting.pricing] in settings.toml, one [min, max] per package or add-on, e.g.
    basic_website = [15000, 25000]
If the package (or a needed add-on) has no price there, the quote's status is NEEDS USER PRICING
and no figure is shown anywhere (draft email included).
"""

from __future__ import annotations

from .. import conf
from .scoring import PACKAGE_FEATURES, PACKAGES

PRICE_KEYS = {"A": "basic_website", "B": "modern_website", "C": "redesign_seo", "D": "website_whatsapp",
              "E": "website_ai_chatbot", "F": "website_ai_leads_whatsapp", "G": "ecommerce",
              "H": "addons_whatsapp_ai"}
# Effort estimates (not prices) — edit with [prospecting.timeline] if yours differ.
TIMELINE = {"A": "1-2 weeks", "B": "2-3 weeks", "C": "2-4 weeks", "D": "2-3 weeks", "E": "3-4 weeks",
            "F": "4-6 weeks", "G": "5-8 weeks", "H": "1-2 weeks"}
ADDONS = {"online_booking": "Online booking", "ai_chatbot": "AI FAQ / enquiry assistant",
          "whatsapp_integration": "WhatsApp click-to-chat (opt-in)", "seo_basic": "Basic SEO + analytics"}


def cfg() -> dict:
    v = conf.SETTINGS.get("prospecting")
    return v if isinstance(v, dict) else {}


def currency() -> str:
    return str(cfg().get("currency") or "INR")


def symbol(cur: str) -> str:
    return {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}.get(cur, cur + " ")


def _price(key: str) -> tuple[int, int] | None:
    v = (cfg().get("pricing") or {}).get(key)
    try:
        lo, hi = int(v[0]), int(v[1])
        return (lo, hi) if 0 < lo <= hi else None
    except (TypeError, ValueError, IndexError):
        return None


def build(p: dict) -> dict:
    pkg = p.get("package") or ""
    if not pkg:
        return {"status": "NO PACKAGE"}
    cur = currency()
    price = _price(PRICE_KEYS[pkg])
    a = p.get("audit") or {}
    addons = []
    if p.get("category") in ("salon", "clinic", "dentist", "fitness") and pkg in ("B", "C") and not a.get("booking"):
        addons.append("online_booking")
    if pkg in ("B", "C") and not a.get("whatsapp"):
        addons.append("whatsapp_integration")
    addon_rows = [{"key": k, "name": ADDONS[k], "price": _price(k)} for k in addons]
    timeline = (cfg().get("timeline") or {}).get(pkg) or TIMELINE[pkg]
    q = {"package": pkg, "name": PACKAGES[pkg], "features": PACKAGE_FEATURES[pkg], "timeline": timeline,
         "currency": cur,
         "assumptions": ["the business provides its logo, photos, texts and service list",
                         "domain and hosting fees are billed at cost",
                         "AI assistants answer only from information the business approves"],
         "exclusions": ["paid advertising budget", "third-party subscriptions (booking / WhatsApp API fees)",
                        "content writing beyond the agreed pages"],
         "optional_addons": [{"name": r["name"], **({"min": r["price"][0], "max": r["price"][1]} if r["price"] else
                                                    {"status": "NEEDS USER PRICING"})} for r in addon_rows]}
    if not price:
        q.update(status="NEEDS USER PRICING",
                 note=f"add {PRICE_KEYS[pkg]} = [min, max] under [prospecting.pricing] in settings.toml")
        return q
    q.update(status="PRICED", min=price[0], max=price[1],
             display=f"{symbol(cur)}{price[0]:,}–{symbol(cur)}{price[1]:,}")
    return q
