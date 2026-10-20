"""Personalised outreach drafts + the approval queue. Helios never sends on its own.

The email is built from a template using only verified findings — each factual sentence is kept
with its evidence (claims[]), so nothing in it is invented. The price line appears only when the
quote is PRICED from your rate card.

Flow: DRAFTED → you read it and click Approve & send (or say "approve" and confirm the prompt) →
Helios sends it from your Gmail → CONTACTED with the Gmail message id as evidence. Or approve only,
put it in Gmail drafts and send it yourself (Helios sees it in Sent → CONTACTED). Phone-only leads get
call notes for YOU. Never sent without your explicit action, never during Night Mode, never by a
background agent, never to system/no-reply addresses. WhatsApp:
recommended as an opt-in feature for the business's own website — Helios never messages anyone
on WhatsApp.
"""

from __future__ import annotations

from .. import conf
from . import quote as quoting
from . import store
from .scoring import PACKAGES

_FINDING = {
    "no website discovered": ("I couldn't find a website for {name}", "people searching online for a {cat} "
                              "nearby can't see your services, hours or how to reach you"),
    "poor mobile layout": ("your website doesn't fit a phone screen", "most visitors are on their phones and "
                           "have to pinch and scroll sideways to read it"),
    "no HTTPS": ("your website loads without HTTPS", "browsers label it 'Not secure', which puts some visitors off"),
    "very slow": ("your website is slow to load", "visitors often leave before it appears"),
    "hard to contact": ("I couldn't find a quick way to contact you on your website",
                        "an interested customer has nowhere obvious to reach out"),
    "looks outdated": ("your website looks like it hasn't been updated in a while", "new customers may "
                       "wonder whether the details are still current"),
    "website broken": ("your website didn't load when I tried it on two different days",
                       "anyone clicking through from a search gets an error"),
}
# How each package reads in a sentence ("I build ... for local businesses").
_OFFER = {"A": "simple, mobile-friendly websites", "B": "modern, responsive websites",
          "C": "website redesigns (with SEO and analytics)", "D": "websites with WhatsApp enquiry buttons",
          "E": "websites with an AI assistant for common questions",
          "F": "websites with an AI assistant that captures enquiries and hands them over on WhatsApp",
          "G": "online stores", "H": "WhatsApp and AI-assistant add-ons for existing websites"}

_AI_PITCH = {
    "appointment business without online booking": "customers could request appointments online at any hour, "
                                                    "with an assistant that collects the details for you",
    "takes orders without an online ordering path": "customers could place orders online or through WhatsApp "
                                                    "after hours",
    "enquiries only by phone": "enquiries that come in after hours would be captured instead of missed",
    "no online enquiry or admission form": "families could send an enquiry or book a visit online, and common "
                                           "admission questions would be answered from your own approved information",
    "no WhatsApp option": "visitors could start a WhatsApp chat with you in one tap (they choose to)",
    "many services / FAQs": "common questions could be answered instantly from your own approved information",
}


def signature() -> str:
    from .. import email_signature
    if email_signature.configured():                 # the full card: name, role, mobile, LinkedIn
        return email_signature.text()
    sig = str(quoting.cfg().get("signature") or "").strip()
    if not sig:
        g = conf.SETTINGS.get("gmail")
        sig = str((g or {}).get("signature") or "").strip() if isinstance(g, dict) else ""
    return sig or "Best regards"


def build_draft(p: dict) -> dict:
    """Subject + body + claims (each factual sentence with its evidence). Pure function."""
    from .scoring import label
    name = p["name"]
    cat = p.get("category") or ""
    cats = label(cat, plural=True)
    reasons = p.get("reasons") or {}
    claims = []
    web = reasons.get("website") or []
    finding = next((r for r in web if r["factor"] in _FINDING), None)
    lines = [f"Hi {name} team,", ""]
    where = f" in {p['area'].split(',')[0]}" if p.get("area") else ""
    if finding:
        what, why = _FINDING[finding["factor"]]
        sent = f"I was looking at {cats}{where} and noticed {what.format(name=name)} — {why.format(cat=label(cat))}."
        claims.append({"text": what.format(name=name), "evidence": f"{finding['detail']} ({finding.get('evidence') or p.get('source_url')})"})
        lines.append(sent)
    else:
        lines.append(f"I was looking at {cats}{where} and came across {name}.")
    ai = (reasons.get("ai") or [])[:2]
    benefits = [_AI_PITCH[r["factor"]] for r in ai if r["factor"] in _AI_PITCH]
    for r in ai:
        if r["factor"] in _AI_PITCH:
            claims.append({"text": r["factor"], "evidence": r["detail"]})
    pkg = p.get("package") or ""
    q = p.get("quote") or {}
    lines.append("")
    offer = f"I build {_OFFER.get(pkg, 'websites')} for local businesses"
    if benefits:
        offer += " — with it, " + "; and ".join(benefits)
    lines.append(offer + ".")
    if q.get("status") == "PRICED":
        lines.append(f"A package like this typically runs {q['display']}, ready in about {q['timeline']}.")
    lines += ["", "Would you be open to a quick 10-minute call this week? Happy to share a few examples first.",
              "", signature()]
    poss = name + ("'" if name.endswith("s") else "'s")
    subject = f"A website idea for {name}" if p.get("website_status") in ("none", "unlisted") else \
        f"A quick idea for {poss} website"
    return {"channel": "email", "subject": subject[:120], "body": "\n".join(lines), "claims": claims,
            "to": p.get("email") or "", "to_source": p.get("email_source") or "",
            "built_at": store.now(), "priced": q.get("status") == "PRICED"}


def build_call_notes(p: dict) -> dict:
    """For businesses with only a public phone number (common for small Indian shops): talking
    points for YOU to use on a call or visit — same evidence rules. Helios never calls or messages."""
    from .scoring import label
    email_like = build_draft({**p, "email": ""})          # reuse the finding/benefit sentences + claims
    reasons = p.get("reasons") or {}
    q = p.get("quote") or {}
    finding = next((r for r in (reasons.get("website") or []) if r["factor"] in _FINDING), None)
    benefits = [_AI_PITCH[r["factor"]] for r in (reasons.get("ai") or [])[:2] if r["factor"] in _AI_PITCH]
    pkg = p.get("package") or ""
    lines = [f"Call notes — {p['name']} ({p.get('phone')}, public business number)", "",
             f"- Who you are: you build {_OFFER.get(pkg, 'websites')} for local "
             f"{label(p.get('category') or '', plural=True)}."]
    if finding:
        what, why = _FINDING[finding["factor"]]
        lines.append(f"- What you noticed: {what.format(name=p['name'])} — {why.format(cat=label(p.get('category') or ''))}.")
    if benefits:
        lines.append("- What it would do for them: " + "; ".join(benefits) + ".")
    lines.append("- Price guide (your rate card): " + (q["display"] if q.get("status") == "PRICED" else
                                                      "not priced yet — add it to [prospecting.pricing] first"))
    lines += ["- Ask: \"Would a quick 10-minute visit or call this week work?\"",
              "- Not interested? Thank them, and mark it NOT_INTERESTED."]
    return {"channel": "phone", "subject": f"Call notes for {p['name']}", "body": "\n".join(lines),
            "claims": email_like["claims"], "to": p.get("phone") or "", "to_source": "public business phone",
            "built_at": store.now(), "priced": q.get("status") == "PRICED"}


# ------------------------------------------------------------------ Gmail (drafts only, after approval)

def to_gmail_draft(pid: int, *, gmail_mod=None) -> dict:
    """Put an APPROVED prospect's email into the user's Gmail as a DRAFT (never sends)."""
    p = store.get(pid)
    if not p or p["status"] != "APPROVED":
        raise store.StatusError("only an APPROVED prospect can be put in Gmail drafts")
    d = p.get("draft") or {}
    if d.get("channel", "email") != "email":
        raise store.StatusError("this one is phone outreach (call notes) — there's no email to draft")
    g = gmail_mod
    if g is None:
        from .. import gmail as g
    if not g.signed_in():
        raise store.StatusError("Gmail isn't connected — copy the email from `helios prospects show` "
                                "and send it yourself, then `helios prospects sent <id>`")
    res = g.create_outreach_draft(d["to"], d["subject"], d["body"])
    draft = {**d, "gmail_draft_id": res.get("draft_id", ""), "gmail_drafted_at": store.now()}
    store.update(pid, draft=draft)
    return draft


_DEFAULT_SIG = "Best regards"


def has_signature() -> bool:
    """A real sign-off (your name / business) is set — an email never goes out ending in a bare
    'Best regards'."""
    return signature().strip() != _DEFAULT_SIG


def _final_body(body: str) -> str:
    """What actually goes out: drafts built before you set a signature end in the bare default, so
    the real one is swapped in; with a signature card, the polite opt-out line is added too."""
    from .. import email_signature
    b = (body or "").rstrip()
    if b.endswith(_DEFAULT_SIG) and has_signature():
        b = b[: -len(_DEFAULT_SIG)].rstrip() + "\n\n" + signature()
    if email_signature.configured() and email_signature.OPT_OUT not in b:
        b += "\n\n— \n" + email_signature.OPT_OUT
    return b


def send_email(pid: int, *, by: str = "you", gmail_mod=None) -> dict:
    """APPROVE AND SEND one outreach email — only from your explicit action after reading it (the
    dashboard's Approve & send, `helios prospects send`, or saying "approve" and clicking Approve at
    the permission prompt). DRAFTED → APPROVED → sent from your Gmail → CONTACTED, with the Gmail
    message id as the evidence. If the send fails it stays APPROVED (nothing claimed)."""
    p = store.get(pid)
    if not p:
        raise store.StatusError("no such prospect")
    d = p.get("draft") or {}
    if d.get("channel", "email") != "email":
        raise store.StatusError(f"{p['name']} has no public email — these are call notes for YOU to call "
                                f"({d.get('to') or 'no number'}); approve them, then mark it contacted after the call")
    if p["status"] not in ("DRAFTED", "APPROVED"):
        raise store.StatusError(f"only a draft waiting for approval can be sent (this one is {p['status']})")
    if not d.get("to") or not d.get("body"):
        raise store.StatusError("nothing to send (no recipient or no text)")
    if not has_signature():
        raise store.StatusError("set your email sign-off first so the email doesn't end with a bare 'Best regards' — "
                                "[prospecting] signature in settings.toml, e.g. \"Best regards,\\nYour Name\\nYour business\"")
    g = gmail_mod
    if g is None:
        from .. import gmail as g
    if not g.signed_in():
        raise store.StatusError("Gmail isn't connected — run `helios gmail login`")
    if p["status"] == "DRAFTED":
        p = store.approve(pid, by=by)
    body = _final_body(d["body"])
    from .. import email_signature as esig
    extra = {}
    if esig.configured():                            # HTML card + inline photo/logo + your name as sender
        extra = {"html": esig.email_html(body), "images": esig.inline_images(), "from_name": esig.card()["name"]}
    try:
        res = g.send_outreach(d["to"], d["subject"], body, **extra)
    except Exception as e:
        raise store.StatusError(f"approved, but the email was NOT sent ({e}) — it's still waiting; try again") from None
    if body != d["body"]:
        store.update(pid, draft={**d, "body": body})
    return store.record_contact(pid, channel="email (Gmail, sent by Helios on your approval)",
                                evidence=f"Gmail sent message {res['id']}", by=by)


def sync_sent(*, gmail_mod=None) -> list[dict]:
    """APPROVED prospects whose email you sent from Gmail → CONTACTED (evidence: the sent message)."""
    g = gmail_mod
    if g is None:
        from .. import gmail as g
    if not g.signed_in():
        return []
    done = []
    for p in store.list_prospects(status="APPROVED", limit=200):
        d = p.get("draft") or {}
        if not d.get("to") or d.get("channel", "email") != "email":
            continue
        hits = g.search(f'in:sent to:{d["to"]} subject:"{d["subject"][:60]}"', limit=1)
        if hits:
            done.append(store.record_contact(p["id"], channel="email (Gmail)",
                                             evidence=f"Gmail sent message {hits[0]['id']}", by="gmail sent folder"))
    return done
