"""Approve & send for outreach emails to potential clients.

The owner reads a draft and clicks Approve & send (dashboard), types `send` (CLI) or says "approve"
and confirms the permission prompt (brain) — only then does Helios send that ONE email from their
Gmail and mark the lead CONTACTED with the Gmail message id as evidence. All Gmail calls are fakes
(conftest also blocks the real Gmail API): no test can send a real email.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios import dashboard, gmail, permissions
from helios.prospecting import discovery, engine, geo, outreach, quote, store

_ROOT = Path(__file__).resolve().parent.parent
SIG = "Best regards,\nSyed\nMaqsusi Web"


@pytest.fixture
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(geo.conf, "DATA_DIR", tmp_path)
    c = {"enabled": True, "areas": [{"name": "Testpur", "radius_km": 1}], "categories": ["salon"], "cell_km": 1.0,
         "max_cells_per_run": 2, "max_research_per_run": 20, "currency": "INR", "signature": SIG, "pricing": {}}
    for mod in (engine, quote, discovery):
        monkeypatch.setattr(mod, "cfg", lambda c=c: c)
    monkeypatch.setattr(geo.conf, "SETTINGS", {"prospecting": c})
    store._inited.clear()
    return c


def _el(i, name):
    return {"type": "node", "id": i, "lat": 17.4, "lon": 78.43, "tags": {"name": name, "shop": "hairdresser"}}


def drafted(channel="email", to="hello@glow.in", body="Hi Glow team,\n\nA quick idea.\n\nBest regards", i=50):
    p, _ = store.upsert(geo.to_business(_el(i, f"Glow Salon {i}"), "Testpur"))
    store.update(p["id"], draft={"channel": channel, "to": to, "subject": "A website idea", "body": body, "claims": []},
                 email=to if channel == "email" else "", phone=to if channel == "phone" else "")
    for s in ("RESEARCHED", "QUALIFIED", "DRAFTED"):
        store.set_status(p["id"], s)
    return p["id"]


def fake_gmail(ok=True, signed=True):
    sent, extras = [], []

    def send(to, subject, body, **kw):
        if not ok:
            raise gmail.GmailError("quota exceeded")
        sent.append((to, subject, body))
        extras.append(kw)
        return {"id": f"msg{len(sent)}", "to": to}
    return SimpleNamespace(signed_in=lambda: signed, send_outreach=send, sent=sent, extras=extras)


# ------------------------------------------------------------------ the send flow

def test_approve_and_send_marks_contacted_with_gmail_evidence(pc):
    pid, gm = drafted(), fake_gmail()
    p = outreach.send_email(pid, by="you (test)", gmail_mod=gm)
    assert p["status"] == "CONTACTED"
    assert gm.sent[0][0] == "hello@glow.in" and gm.sent[0][1] == "A website idea"
    assert gm.sent[0][2].endswith(SIG) and not gm.sent[0][2].rstrip().endswith("\n\nBest regards")   # real sign-off
    events = [e["event"] for e in p["contact_log"]]
    assert events[-2:] == ["approved", "contacted"] and "Gmail sent message msg1" in p["contact_log"][-1]["evidence"]


def test_never_sends_twice(pc):
    pid, gm = drafted(), fake_gmail()
    outreach.send_email(pid, gmail_mod=gm)
    with pytest.raises(store.StatusError, match="CONTACTED"):
        outreach.send_email(pid, gmail_mod=gm)
    assert len(gm.sent) == 1


def test_no_signature_no_send(pc):
    pc["signature"] = ""
    pid, gm = drafted(), fake_gmail()
    with pytest.raises(store.StatusError, match="sign-off"):
        outreach.send_email(pid, gmail_mod=gm)
    assert gm.sent == [] and store.get(pid)["status"] == "DRAFTED"


def test_phone_leads_are_never_emailed(pc):
    pid, gm = drafted(channel="phone", to="+91 90000 11122"), fake_gmail()
    with pytest.raises(store.StatusError, match="call notes"):
        outreach.send_email(pid, gmail_mod=gm)
    assert gm.sent == [] and store.get(pid)["status"] == "DRAFTED"


def test_gmail_not_connected(pc):
    pid, gm = drafted(), fake_gmail(signed=False)
    with pytest.raises(store.StatusError, match="Gmail isn't connected"):
        outreach.send_email(pid, gmail_mod=gm)
    assert store.get(pid)["status"] == "DRAFTED"


def test_failed_send_is_not_claimed(pc):
    pid = drafted()
    with pytest.raises(store.StatusError, match="NOT sent"):
        outreach.send_email(pid, gmail_mod=fake_gmail(ok=False))
    p = store.get(pid)
    assert p["status"] == "APPROVED" and not any(e["event"] == "contacted" for e in p["contact_log"])
    p = outreach.send_email(pid, gmail_mod=fake_gmail())                 # retry from APPROVED works
    assert p["status"] == "CONTACTED"


def test_rejected_or_new_leads_cannot_be_sent(pc):
    pid, gm = drafted(), fake_gmail()
    store.set_status(pid, "REJECTED")
    with pytest.raises(store.StatusError, match="REJECTED"):
        outreach.send_email(pid, gmail_mod=gm)
    assert gm.sent == []


def test_possessive_subject():
    base = {"website_status": "ok", "category": "professional", "area": "X", "reasons": {}}
    assert outreach.build_draft({**base, "name": "Outlook HR Solutions"})["subject"] == "A quick idea for Outlook HR Solutions' website"
    assert outreach.build_draft({**base, "name": "Glow Salon"})["subject"] == "A quick idea for Glow Salon's website"


# ------------------------------------------------------------------ the Gmail send itself

def test_send_outreach_posts_one_message_and_refuses_system_addresses(monkeypatch):
    calls = []
    monkeypatch.setattr(gmail, "profile", lambda: {"emailAddress": "me@gmail.com"})
    monkeypatch.setattr(gmail, "_api", lambda method, path, *a, **k: (calls.append((method, path, k)), {"id": "m9"})[1])
    r = gmail.send_outreach("Glow <hello@glow.in>", "Hi", "Body\nBest regards,\nSyed")
    assert r == {"id": "m9", "to": "hello@glow.in"}
    assert calls[0][:2] == ("POST", "/messages/send") and "raw" in calls[0][2]["body"]
    for bad in ("noreply@glow.in", "no-reply@x.com", "mailer-daemon@x.com", "not-an-email"):
        with pytest.raises(gmail.GmailError):
            gmail.send_outreach(bad, "Hi", "Body")
    assert len(calls) == 1


def test_tests_can_never_reach_real_gmail():
    with pytest.raises(gmail.GmailError, match="network disabled in tests"):
        gmail._api("GET", "/profile")


# ------------------------------------------------------------------ who may trigger a send

def test_prospect_approve_needs_the_owner_and_never_runs_unattended():
    name = "mcp__helios__prospect_approve"
    assert permissions.classify(name, {"prospect_id": 1}) == "ask"
    assert permissions.is_outbound_send(name)          # background agents: blocked outright
    assert permissions.never_yolo(name, {})             # always the Approve/Deny prompt, even in YOLO


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("helios_mcp_server_out", _ROOT / "mcp" / "helios_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mcp_approve_on_call_notes_only_approves(pc, srv):
    pid = drafted(channel="phone", to="+91 90000 11122")
    out = srv.prospect_approve(pid)
    assert "call notes" in out and store.get(pid)["status"] == "APPROVED"


def test_mcp_approve_sends_the_email(pc, srv, monkeypatch):
    pid, gm = drafted(), fake_gmail()
    real = outreach.send_email
    monkeypatch.setattr(outreach, "send_email", lambda i, by="you": real(i, by=by, gmail_mod=gm))
    out = srv.prospect_approve(pid)
    assert out.startswith("Sent") and "hello@glow.in" in out and len(gm.sent) == 1
    assert store.get(pid)["status"] == "CONTACTED"


# ------------------------------------------------------------------ the dashboard queue

def test_outreach_queue_lists_drafts_and_readiness(pc, monkeypatch):
    monkeypatch.setattr(gmail, "signed_in", lambda: True)
    a = drafted(i=51)
    b = drafted(channel="phone", to="+91 90000 11122", i=52)
    q = dashboard.outreach_queue()
    ids = [x["id"] for x in q["items"]]
    assert set(ids) == {a, b} and q["gmail"] is True and q["signature"] is True
    assert {x["channel"] for x in q["items"]} == {"email", "phone"}
    pc["signature"] = ""
    assert dashboard.outreach_queue()["signature"] is False


# ------------------------------------------------------------------ the signature card

from helios import email_signature  # noqa: E402

CARD = {"name": "Test User", "title": "Web & AI Developer", "company": "Helios Web Studio",
        "phone": "+91 98765 43210", "email": "", "linkedin": "https://www.linkedin.com/in/test-user",
        "website": "", "location": "", "image": "", "style": ""}


@pytest.fixture
def sig(monkeypatch, tmp_path):
    c = dict(CARD)
    monkeypatch.setattr(email_signature, "card", lambda: dict(c))
    return c


def test_signature_text_and_validation(sig):
    t = email_signature.text()
    assert t.startswith("Best regards,\nTest User\nWeb & AI Developer · Helios Web Studio")
    assert "Mobile: +91 98765 43210" in t and "LinkedIn: https://www.linkedin.com/in/test-user" in t
    assert email_signature.validate(CARD) == []
    assert email_signature.validate({**CARD, "linkedin": "bit.ly/abc"})            # no shorteners / non-profile links
    assert email_signature.validate({**CARD, "phone": "call me"})
    assert email_signature.validate({**CARD, "name": ""})


def test_card_replaces_bare_sign_off_and_adds_opt_out(pc, sig):
    body = outreach._final_body("Hi Glow team,\n\nA quick idea.\n\nBest regards")
    assert "Test User" in body and body.count("Best regards") == 1
    assert body.rstrip().endswith(email_signature.OPT_OUT)
    assert outreach._final_body(body) == body                                     # idempotent


def test_send_uses_html_card_inline_image_and_real_sender_name(pc, sig, tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(email_signature.conf, "DATA_DIR", tmp_path)
    src = tmp_path / "me.jpg"
    Image.new("RGB", (400, 300), (30, 90, 200)).save(src)
    sig["image"] = email_signature.prepare_image(str(src))
    pid, gm = drafted(), fake_gmail()
    outreach.send_email(pid, gmail_mod=gm)
    kw = gm.extras[0]
    assert kw["from_name"] == "Test User"
    assert 'href="tel:+919876543210"' in kw["html"] and 'href="https://www.linkedin.com/in/test-user"' in kw["html"]
    assert kw["images"][0][0] == "helios-sig" and kw["images"][0][2] == "png" and 'src="cid:helios-sig"' in kw["html"]
    assert email_signature.OPT_OUT.replace('"', "&quot;").replace("'", "&#x27;") in kw["html"]
    assert "<script" not in kw["html"] and "http://" not in kw["html"]


def test_prepare_image_makes_a_small_square_png(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(email_signature.conf, "DATA_DIR", tmp_path)
    src = tmp_path / "logo.jpg"
    Image.new("RGB", (1200, 800), (200, 40, 40)).save(src)
    out = Path(email_signature.prepare_image(str(src)))
    im = Image.open(out)
    assert im.size == (160, 160) and out.suffix == ".png" and out.stat().st_size < 60_000
    with pytest.raises(ValueError):
        email_signature.prepare_image(str(tmp_path / "missing.png"))


def test_gmail_builds_multipart_with_inline_image(monkeypatch, sig, tmp_path):
    import base64
    import email
    from PIL import Image
    monkeypatch.setattr(email_signature.conf, "DATA_DIR", tmp_path)
    src = tmp_path / "me.png"
    Image.new("RGB", (300, 300), (10, 120, 90)).save(src)
    sig["image"] = email_signature.prepare_image(str(src))
    raw = []
    monkeypatch.setattr(gmail, "profile", lambda: {"emailAddress": "me@gmail.com"})
    monkeypatch.setattr(gmail, "_api", lambda m, p, *a, **k: (raw.append(k["body"]["raw"]), {"id": "m1"})[1])
    body = "Hi,\n\nIdea.\n\n" + email_signature.text()
    gmail.send_outreach("hello@glow.in", "Idea", body, html=email_signature.email_html(body),
                        image=email_signature.image_bytes(), from_name="Test User")
    m = email.message_from_bytes(base64.urlsafe_b64decode(raw[0]))
    assert m["From"] == "Test User <me@gmail.com>"
    types = [p.get_content_type() for p in m.walk()]
    assert "text/plain" in types and "text/html" in types and "image/png" in types
    img = next(p for p in m.walk() if p.get_content_type() == "image/png")
    assert img["Content-ID"] == "<helios-sig>" and img.get_content_disposition() == "inline"
    html_part = next(p for p in m.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert 'src="cid:helios-sig"' in html_part


def test_dashboard_preview_shows_exactly_what_is_sent(pc, sig):
    from helios.prospecting import engine as eng
    pid = drafted()
    d = eng.card(store.get(pid))["draft"]
    assert d["preview"].rstrip().endswith(email_signature.OPT_OUT) and "Test User" in d["preview"]
    assert "Test User" in d["preview_html"] and "linkedin.com/in/test-user" in d["preview_html"]


def test_dark_card_is_real_text_with_inline_icons_and_no_logo(sig, tmp_path, monkeypatch):
    monkeypatch.setattr(email_signature.conf, "DATA_DIR", tmp_path)
    sig.update(style="dark", email="me@gmail.com", location="Hyderabad, India", image="")
    imgs = email_signature.inline_images()
    cids = [c for c, _, _ in imgs]
    assert "helios-sig" not in cids                                     # no logo when none is set
    assert cids == ["sig-accent", "sig-i-phone", "sig-i-mail", "sig-i-in", "sig-i-pin"]
    h = email_signature.html()
    for c in cids:
        assert f'src="cid:{c}"' in h
    assert "Test User" in h and 'href="mailto:me@gmail.com"' in h and 'href="tel:+919876543210"' in h
    assert "Hyderabad, India" in h and 'bgcolor="#0b1220"' in h
    p = email_signature.html(preview=True)
    assert "cid:" not in p and p.count("data:image/png;base64,") == len(cids)
    t = email_signature.text()
    assert "Email: me@gmail.com" in t and t.endswith("Hyderabad, India")


def test_style_and_email_validation():
    assert email_signature.validate({**CARD, "style": "neon"})
    assert email_signature.validate({**CARD, "email": "not-an-email"})
    assert email_signature.validate({**CARD, "style": "dark", "email": "me@gmail.com"}) == []


def test_gmail_embeds_every_signature_image(monkeypatch):
    import base64
    import email
    raw = []
    monkeypatch.setattr(gmail, "profile", lambda: {"emailAddress": "me@gmail.com"})
    monkeypatch.setattr(gmail, "_api", lambda m, p, *a, **k: (raw.append(k["body"]["raw"]), {"id": "m2"})[1])
    imgs = [("sig-accent", b"\x89PNG-a", "png"), ("sig-i-phone", b"\x89PNG-b", "png")]
    gmail.send_outreach("hello@glow.in", "Idea", "Body", html='<img src="cid:sig-accent"><img src="cid:sig-i-phone">',
                        images=imgs, from_name="Syed")
    m = email.message_from_bytes(base64.urlsafe_b64decode(raw[0]))
    ids = sorted(p["Content-ID"] for p in m.walk() if p.get_content_maintype() == "image")
    assert ids == ["<sig-accent>", "<sig-i-phone>"]
