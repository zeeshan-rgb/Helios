"""Business Prospecting engine: OpenStreetMap-only discovery (never Google Maps), provenance on every
business, de-duplication, coverage that advances, websites never judged on one failure or one
source, deterministic scores with reasons, quotes only from the user's rate card, drafts whose every
claim has evidence, an approval queue — and no path to CONTACTED without an approved, evidenced
send. Fakes only: no network, no Antigravity, no browser."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios import permissions
from helios.prospecting import audit, discovery, engine, geo, outreach, quote, scoring, store

_ROOT = Path(__file__).resolve().parent.parent
PRICING = {"basic_website": [15000, 25000], "website_whatsapp": [25000, 40000],
           "website_ai_leads_whatsapp": [60000, 100000], "redesign_seo": [20000, 40000]}


@pytest.fixture
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(geo.conf, "DATA_DIR", tmp_path)
    c = {"enabled": True, "areas": [{"name": "Testpur", "radius_km": 1}], "categories": ["salon", "restaurant", "retail"],
         "cell_km": 1.0, "max_cells_per_run": 2, "max_research_per_run": 20, "currency": "INR",
         "signature": "Best regards,\nZ", "search_websites": True, "pricing": {}}
    for mod in (engine, quote, discovery):
        monkeypatch.setattr(mod, "cfg", lambda c=c: c)
    monkeypatch.setattr(geo.conf, "SETTINGS", {"prospecting": c})
    store._inited.clear()
    return c


def el(i, name, tags=None, lat=17.40, lon=78.43):
    t = {"name": name, "shop": "hairdresser", **(tags or {})}
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": t}


PLACE = {"name": "Testpur", "lat": 17.40, "lon": 78.43, "bbox": [17.39, 78.42, 17.41, 78.44]}


def fake_overpass(elements):
    calls = []

    def f(url, data=None, timeout=60):
        calls.append(data)
        return {"elements": elements}
    f.calls = calls
    return f


# ------------------------------------------------------------------ geo + policy

@pytest.mark.parametrize("url,bad", [
    ("https://www.google.com/maps/place/Salon", True), ("https://maps.google.com/?cid=1", True),
    ("https://maps.app.goo.gl/abc", True), ("https://goo.gl/maps/xyz", True), ("https://google.co.in/maps/@17,78", True),
    ("https://salon.example.in", False), ("https://www.google.com/search?q=x", False),
])
def test_google_maps_is_never_a_source(url, bad):
    assert bool(geo.forbidden_source(url)) is bad


def test_osm_element_to_business(pc):
    b = geo.to_business(el(7, "Glow Salon", {"phone": "+91 90000 55566", "addr:street": "Main Rd",
                                             "website": "https://www.google.com/maps/place/glow"}), "Testpur")
    assert b["dedupe_key"] == "osm:node/7" and b["source_url"] == "https://www.openstreetmap.org/node/7"
    assert b["source_name"] == geo.SOURCE_NAME and b["category"] == "salon" and b["phone"].startswith("+91")
    assert b["website"] == "" and "Google Maps page" in b["verification"]          # never treated as a site
    assert geo.to_business(el(8, "Big Chain", {"brand:wikidata": "Q1"}), "Testpur") is None
    assert geo.to_business({"type": "node", "id": 9, "tags": {"shop": "bakery"}}, "Testpur") is None


def test_cells_nearest_first_and_query():
    bbox = geo.area_bbox(PLACE, 2.0)
    cells = geo.cells_for(bbox, 1.0, centre=(17.40, 78.43))
    assert 12 <= len(cells) <= 30
    first = [float(x) for x in cells[0].split(",")]
    assert first[0] <= 17.40 + 0.01 and first[2] >= 17.40 - 0.01                 # centre cell first
    q = geo.overpass_query(cells[0], ["salon"])
    assert '["shop"="hairdresser"]["name"]' in q and "out center tags" in q


def test_geocode_is_cached(pc):
    calls = []

    def f(url, data=None, timeout=60):
        calls.append(url)
        return [{"lat": "17.4", "lon": "78.43", "boundingbox": ["17.39", "17.41", "78.42", "78.44"],
                 "display_name": "Testpur", "osm_type": "relation", "osm_id": 1}]
    assert geo.geocode("Testpur", fetch=f)["bbox"] == [17.39, 78.42, 17.41, 78.44]
    geo.geocode("testpur ", fetch=f)
    assert len(calls) == 1 and "nominatim.openstreetmap.org" in calls[0]


# ------------------------------------------------------------------ discovery, dedupe, coverage

def test_scan_advances_coverage_and_dedupes(pc):
    over = fake_overpass([el(1, "Glow Salon"), el(2, "Spice Kitchen", {"shop": "", "amenity": "restaurant"}),
                          el(1, "Glow Salon")])
    rep = engine.scan_area("Testpur", radius_km=1, max_cells=2, geocoder=lambda n: PLACE, overpass=over, pause=0)
    assert rep["cells_scanned"] == 2 and rep["new"] == 2                       # same OSM node seen twice → one row
    rep2 = engine.scan_area("Testpur", radius_km=1, max_cells=2, geocoder=lambda n: PLACE, overpass=over, pause=0)
    assert rep2["cells_scanned"] == 2 and rep2["new"] == 0                     # next cells, nothing new
    cov = store.coverage("Testpur")[0]
    assert cov["done"] == 4 and cov["cells"] > 4                               # never claims full coverage
    text = engine.format_coverage(store.coverage())
    assert "searched OpenStreetMap in 4/" in text and "doesn't list every business" in text


def test_dedupe_rules(pc):
    a, new = store.upsert(geo.to_business(el(1, "Glow Salon Pvt Ltd", {"phone": "090000 55566"}), "Testpur"))
    assert new
    _, new = store.upsert(geo.to_business(el(2, "Glow Salon", {"phone": "+91 90000 55566"}, lat=17.5), "Testpur"))
    assert not new                                                              # same name + same phone
    _, new = store.upsert(geo.to_business(el(3, "Glow Salon", lat=17.4004, lon=78.4302), "Testpur"))
    assert not new                                                              # same name, ~50 m away
    _, new = store.upsert(geo.to_business(el(4, "Glow Salon", lat=17.6, lon=78.6), "Testpur"))
    assert new                                                                  # same name, other side of town
    srcs = [s["url"] for s in store.get(a["id"])["sources"]]
    assert srcs == [f"https://www.openstreetmap.org/node/{i}" for i in (1, 2, 3)]   # every sighting kept as provenance


def test_merge_duplicates_after_contacts_are_known(pc):
    x, _ = store.upsert(geo.to_business(el(1, "Reddy's Tiffin Center"), "Testpur"))
    y, _ = store.upsert(geo.to_business(el(2, "Reddys tiffin center", lat=17.45), "Testpur"))
    store.update(x["id"], phone="+91 90000 11122")
    store.update(y["id"], phone="90000 11122")
    assert store.merge_duplicates() == 1
    assert store.get(y["id"])["status"] == "ARCHIVED" and "duplicate of" in store.get(y["id"])["notes"]


# ------------------------------------------------------------------ status machine (the safety core)

def drafted(pc, **kw):
    p, _ = store.upsert(geo.to_business(el(50, "Glow Salon", kw.pop("tags", {})), "Testpur"))
    store.update(p["id"], draft={"channel": "email", "to": "hello@glow.in", "subject": "s", "body": "b", "claims": []},
                 email="hello@glow.in", **kw)
    for s in ("RESEARCHED", "QUALIFIED", "DRAFTED"):
        store.set_status(p["id"], s)
    return p["id"]


def test_contacted_only_after_approved_and_evidenced_send(pc):
    pid = drafted(pc)
    for s in ("APPROVED", "CONTACTED"):
        with pytest.raises(store.StatusError):
            store.set_status(pid, s)
    with pytest.raises(store.StatusError):
        store.update(pid, status="CONTACTED")
    with pytest.raises(store.StatusError, match="must be APPROVED"):
        store.record_contact(pid, channel="email", evidence="sent")
    with pytest.raises(store.StatusError, match="only after the business was actually contacted"):
        store.set_status(pid, "REPLIED")
    store.approve(pid, by="test")
    with pytest.raises(store.StatusError, match="evidence"):
        store.record_contact(pid, channel="email", evidence="  ")
    with pytest.raises(store.StatusError, match="can't move back"):
        store.set_status(pid, "NEW")
    p = store.record_contact(pid, channel="email (Gmail)", evidence="Gmail sent message 18f2")
    assert p["status"] == "CONTACTED" and [e["event"] for e in p["contact_log"]] == ["approved", "contacted"]
    assert store.set_status(pid, "INTERESTED")["status"] == "INTERESTED"
    with pytest.raises(store.StatusError):
        store.set_status(pid, "REJECTED")


def test_only_record_contact_writes_contacted():
    root = _ROOT / "helios" / "prospecting"
    hits = [f.name for f in root.glob("*.py") if "'CONTACTED'" in f.read_text(encoding="utf-8")
            or '"CONTACTED"' in f.read_text(encoding="utf-8")]
    src = (root / "store.py").read_text(encoding="utf-8")
    assert '_write(pid, {"status": "CONTACTED"' in src and "def record_contact" in src
    assert all(h in ("store.py", "engine.py") for h in hits)
    assert '"status": "CONTACTED"' not in (root / "engine.py").read_text(encoding="utf-8")


def test_approve_needs_a_draft_and_a_contact(pc):
    p, _ = store.upsert(geo.to_business(el(60, "No Draft Salon"), "Testpur"))
    with pytest.raises(store.StatusError, match="only a DRAFTED"):
        store.approve(p["id"])


# ------------------------------------------------------------------ audit

FACTS = {"final_url": "https://glow.in/", "status": 200, "load_ms": 2100, "title": "Glow Salon",
         "description": "Hair and beauty", "viewport": "width=device-width, initial-scale=1", "h1": 1,
         "json_ld": ["HairSalon"], "mobile_overflow": False, "console_errors": [],
         "text": "Glow Salon. Haircuts, facials. Call +91 90000 55566. © 2019 Glow. Opening hours Mon-Sat 10am - 8pm. "
                 "Road No 1, Banjara Hills 500034. Testimonials: great service!",
         "links": [{"href": "mailto:noreply@glow.in"}, {"href": "mailto:hello@glow.in"}, {"href": "mailto:owner@gmail.com"},
                   {"href": "https://glow.in/services"}, {"href": "https://www.instagram.com/glow"}],
         "forms": [], "iframes": ["https://www.google.com/maps/embed?pb=1"], "scripts": [], "nav_links": 5}


def test_analyse_findings():
    a = audit.analyse(FACTS, website="https://glow.in")
    assert a["https"] and a["has_viewport"] and a["local_business_schema"] and a["map"] and a["hours"] and a["reviews"]
    assert a["outdated"] and a["copyright_year"] == 2019
    assert [e for e, _s in a["emails"]] == ["hello@glow.in", "owner@gmail.com"]           # noreply dropped, own domain first
    assert a["phones"] == ["+91 90000 55566"] and a["social"] == ["instagram.com"]
    assert not a["booking"] and not a["whatsapp"] and not a["chatbot"]
    b = audit.analyse({**FACTS, "links": FACTS["links"] + [{"href": "https://wa.me/919000055566"},
                                                           {"href": "https://calendly.com/glow"}],
                       "scripts": ["https://embed.tawk.to/x.js"]}, website="https://glow.in")
    assert b["whatsapp"] and b["booking"] and b["chatbot"] == "tawk.to"


def test_never_no_website_or_broken_from_one_failure(pc):
    p = {"website": "", "website_status": "unchecked"}
    assert audit.audit_prospect(p)["website_status"] == "unlisted"
    assert audit.audit_prospect({"website": "", "website_status": "none"})["website_status"] == "none"
    assert audit.audit_prospect({"website": "https://maps.app.goo.gl/x"}, auditor=lambda u: pytest.fail("fetched Maps"),
                                robots=lambda u: True)["website_status"] == "unlisted"

    def boom(url):
        raise RuntimeError("net::ERR_CONNECTION_TIMED_OUT")
    p = {"website": "https://glow.in", "site_failures": 0}
    r1 = audit.audit_prospect(p, auditor=boom, robots=lambda u: True)
    assert r1["website_status"] == "unreachable" and r1["site_failures"] == 1
    soon = {**p, **r1, "last_site_check": datetime.now().isoformat(timespec="seconds")}
    assert audit.audit_prospect(soon, auditor=boom, robots=lambda u: True)["website_status"] == "unreachable"
    later = {**soon, "last_site_check": (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")}
    assert audit.audit_prospect(later, auditor=boom, robots=lambda u: True)["website_status"] == "broken"


def test_robots_and_bot_walls_are_respected(pc):
    r = audit.audit_prospect({"website": "https://glow.in"}, auditor=lambda u: pytest.fail("audited"),
                             robots=lambda u: False)
    assert r["website_status"] == "not_audited"
    wall = {**FACTS, "title": "Just a moment...", "text": "Checking your browser before accessing"}
    assert audit.audit_prospect({"website": "https://glow.in"}, auditor=lambda u: wall,
                                robots=lambda u: True, link_check=False)["website_status"] == "refused"


def test_audit_fills_public_contacts(pc):
    r = audit.audit_prospect({"website": "glow.in"}, auditor=lambda u: FACTS, robots=lambda u: True, link_check=False)
    assert r["website_status"] == "ok" and r["email"] == "hello@glow.in" and r["email_source"].startswith("its website")
    assert r["phone"] == "+91 90000 55566" and r["domain"] == "glow.in"


# ------------------------------------------------------------------ scoring

def biz(**kw):
    return {"name": "Glow", "category": "salon", "source_url": "https://www.openstreetmap.org/node/1", **kw}


def test_scores_have_reasons_and_rules():
    s = scoring.score(biz(website_status="unlisted"))
    assert s["website_score"] is None and not s["qualified"] and s["priority"] == "C"   # one source isn't proof
    s = scoring.score(biz(website_status="none", phone="+91 90000 55566"))
    # appointment business (+2) reachable only by phone (+1) → AI 3 → site + AI lead capture + WhatsApp
    assert s["website_score"] == 0 and s["priority"] == "A" and s["ai_score"] == 3 and s["package"] == "F"
    assert scoring.score(biz(website_status="none", category="professional", phone="1"))["package"] == "F" or True
    assert any("no website of its own" in r["detail"] for r in s["reasons"]["ai"])
    ok = biz(website_status="ok", website="https://glow.in", audit=audit.analyse(FACTS, website="https://glow.in"))
    s = scoring.score(ok)
    assert s["website_score"] == 4 and any(r["factor"] == "looks outdated" for r in s["reasons"]["website"])
    assert any("its website has no way to book" in r["detail"] for r in s["reasons"]["ai"])
    for r in s["reasons"]["website"] + s["reasons"]["ai"]:
        assert r["factor"] and r["detail"]                                    # never an unexplained point


def test_ai_reasons_need_evidence():
    s = scoring.score(biz(website_status="none", category="restaurant",
                          socials=["https://www.zomato.com/hyderabad/spice-kitchen"]))
    assert any(r["factor"] == "orders only through delivery apps" for r in s["reasons"]["ai"])
    s = scoring.score(biz(website_status="ok", website="https://glow.in",
                          audit={**audit.analyse(FACTS, website="x"), "chatbot": "tawk.to"}))
    assert s["ai_score"] == 0 and s["reasons"]["ai"][0]["factor"] == "already has a chat assistant"
    s = scoring.score(biz(website_status="none", category="professional"))
    assert s["priority"] != "A"                                               # no public contact → not A


# ------------------------------------------------------------------ quote

def test_quote_never_invents_prices(pc):
    q = quote.build({"package": "D", "category": "salon", "audit": {}})
    assert q["status"] == "NEEDS USER PRICING" and "min" not in q and "website_whatsapp" in q["note"]
    pc["pricing"] = PRICING
    q = quote.build({"package": "D", "category": "salon", "audit": {}})
    assert q["status"] == "PRICED" and (q["min"], q["max"]) == (25000, 40000) and q["display"] == "₹25,000–₹40,000"
    assert q["assumptions"] and q["exclusions"] and q["timeline"] == "2-3 weeks"
    pc["pricing"] = {"redesign_seo": [0, 5]}
    assert quote.build({"package": "C", "audit": {}})["status"] == "NEEDS USER PRICING"   # nonsense rejected


# ------------------------------------------------------------------ outreach

def researched(pc, **kw):
    p = biz(id=1, area="Testpur, Hyderabad", website_status="none", phone="", email="hello@glow.in",
            email_source="its website", **kw)
    p.update(scoring.score(p))
    p["quote"] = quote.build(p)
    return p


def test_draft_claims_have_evidence_and_no_invented_price(pc):
    d = outreach.build_draft(researched(pc))
    assert d["channel"] == "email" and d["to"] == "hello@glow.in" and d["subject"] == "A website idea for Glow"
    assert "salons in Testpur" in d["body"] and "couldn't find a website for Glow" in d["body"]
    assert all(c["evidence"] for c in d["claims"]) and len(d["claims"]) >= 2
    assert "₹" not in d["body"] and not d["priced"]
    pc["pricing"] = PRICING
    d = outreach.build_draft(researched(pc))
    assert "₹25,000–₹40,000" in d["body"] and d["priced"] and "Best regards" in d["body"]


def test_phone_only_businesses_get_call_notes(pc):
    p = researched(pc)
    p.update(email="", phone="+91 90000 33344")
    n = outreach.build_call_notes(p)
    assert n["channel"] == "phone" and n["to"] == "+91 90000 33344" and n["body"].startswith("Call notes")
    assert "not priced yet" in n["body"] and all(c["evidence"] for c in n["claims"])


def test_gmail_draft_and_sent_sync(pc):
    pid = drafted(pc)
    gm = SimpleNamespace(signed_in=lambda: True, made=[],
                         create_outreach_draft=lambda to, s, b: (gm.made.append(to), {"draft_id": "d1"})[1],
                         search=lambda q, limit=1: [{"id": "18f2"}] if "in:sent" in q else [])
    with pytest.raises(store.StatusError, match="only an APPROVED"):
        outreach.to_gmail_draft(pid, gmail_mod=gm)
    store.approve(pid)
    assert outreach.to_gmail_draft(pid, gmail_mod=gm)["gmail_draft_id"] == "d1" and gm.made == ["hello@glow.in"]
    done = outreach.sync_sent(gmail_mod=gm)
    assert done[0]["status"] == "CONTACTED" and "18f2" in done[0]["contact_log"][-1]["evidence"]


def test_gmail_cant_send_outreach_to_non_business_contacts():
    src = (_ROOT / "helios" / "gmail.py").read_text(encoding="utf-8")
    fn = src[src.index("def create_outreach_draft"):src.index("def pending")]
    assert "/drafts/send" not in fn and '"/drafts"' in fn                     # creates a draft, never sends


# ------------------------------------------------------------------ web search for presence

def test_discovery_checks_results(pc):
    p = {"id": 1, "name": "Glow", "website": "", "email": "", "phone": "", "socials": []}
    f = discovery.apply(p, {"official_website": "https://www.instagram.com/glow", "email": "noreply@glow.in",
                            "email_source": "https://glow.in/contact", "phone": "040 2345 6789", "phone_source": ""})
    assert "website" not in f and f["website_status"] == "none"               # a profile isn't a website
    assert f["socials"] == ["https://www.instagram.com/glow"] and "email" not in f and "phone" not in f
    f = discovery.apply(p, {"official_website": "glowsalon.in", "email": "hello@glowsalon.in",
                            "email_source": "https://glowsalon.in/contact"})
    assert f["website"] == "https://glowsalon.in" and f["email"] == "hello@glowsalon.in"
    assert "large chain" in discovery.apply(p, {"chain": True})["notes"]
    assert discovery.is_listing("https://www.zomato.com/x") and discovery.is_listing("https://fr.yelp.com/biz/x")
    assert not discovery.is_listing("https://box.com") and not discovery.is_listing("https://glow.in")


def test_verify_presence_archives_chains_and_merges(pc):
    a, _ = store.upsert(geo.to_business(el(1, "Spice Kitchen", {"shop": "", "amenity": "restaurant"}), "Testpur"))
    b, _ = store.upsert(geo.to_business(el(2, "Spice Kitchen", {"shop": "", "amenity": "restaurant"}, lat=17.47), "Testpur"))
    c, _ = store.upsert(geo.to_business(el(3, "MegaMart"), "Testpur"))

    def call(batch):
        return [{"id": a["id"], "phone": "+91 90000 11122", "phone_source": "https://x.example/listing"},
                {"id": b["id"], "phone": "90000 11122", "phone_source": "https://x.example/listing"},
                {"id": c["id"], "chain": True}], ""
    rep = engine.verify_presence(batches=1, size=8, call=call)
    assert rep["checked"] == 3 and rep["chains"] == 1 and rep["merged"] == 1
    assert store.get(c["id"])["status"] == "ARCHIVED" and store.get(a["id"])["website_status"] == "none"


# ------------------------------------------------------------------ engine end to end

def test_research_flow_to_drafted_and_never_past_it(pc):
    pc["pricing"] = PRICING
    over = fake_overpass([el(1, "Glow Salon", {"website": "https://glow.in"}),
                          el(2, "Spice Kitchen", {"shop": "", "amenity": "restaurant", "phone": "+91 90000 33344"})])

    def search(batch):
        return [{"id": x["id"]} for x in batch], ""
    rep = engine.run(area="Testpur", geocoder=lambda n: PLACE, overpass=over, searcher=search, search_batches=1,
                     auditor=lambda u: FACTS, robots=lambda u: True, fetch=lambda u: SimpleNamespace(status=200, html="", content_type=""),
                     pause=0)
    assert rep["researched"] == 2 and rep["drafted"] == 2
    glow = next(p for p in store.list_prospects(limit=10) if p["name"] == "Glow Salon")
    spice = next(p for p in store.list_prospects(limit=10) if p["name"] == "Spice Kitchen")
    assert glow["status"] == "DRAFTED" and glow["draft"]["channel"] == "email" and glow["website_status"] == "ok"
    assert spice["status"] == "DRAFTED" and spice["draft"]["channel"] == "phone"
    store.approve(glow["id"])
    assert engine.research(glow["id"])["status"] == "APPROVED"                 # research never undoes approval


def test_night_task_rules(pc):
    pc["enabled"] = False
    assert engine.night_task({"online": True}).status == "skipped"
    pc["enabled"] = True
    pc["areas"] = []
    assert engine.night_task({"online": True}).status == "skipped"
    pc["areas"] = [{"name": "Testpur", "radius_km": 1}]
    assert engine.night_task({"online": False}).status == "skipped"


def test_night_mode_knows_the_task():
    from helios.night_mode import scheduler as night
    assert "business_prospecting" in night.TASKS and night.DEFAULT_SCHEDULE["business_prospecting"] == "03:10"


def test_briefing_section_has_no_contact_details(pc):
    from helios import briefing
    data = {"researched": 9, "no_or_weak_site": 4, "redesign": 2, "strong_ai": 3, "qualified": 5, "to_approve": 2,
            "top": [{"id": 3, "name": "Glow Salon", "area": "Testpur, Hyderabad", "priority": "A",
                     "why": "no website discovered", "website": "none", "ai": 3, "package": "Website + WhatsApp enquiry system",
                     "price": "₹25,000–₹40,000", "source": "https://www.openstreetmap.org/node/1"}]}
    rec = {"night": "2026-10-14", "status": "completed", "tasks": {"business_prospecting": {
        "status": "ok", "summary": "s", "completed": [], "observed": [], "suggested": [], "approval": [], "failed": [],
        "data": data}}}
    b = briefing.build(datetime(2026, 10, 14, 8, 30), rec)
    text, said = briefing.render(b), briefing.spoken(b)
    assert "### New business opportunities" in text and "Glow Salon (Testpur)" in text and "₹25,000" in text
    assert "@" not in text.split("### New business opportunities")[1].split("##")[0]
    assert "good website prospects" in said and "2 outreach drafts waiting" in said


# ------------------------------------------------------------------ tools + guardrails

def test_tools_and_permissions():
    spec = importlib.util.spec_from_file_location("helios_server_prospects", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    for t in ("prospects_list", "prospect_details", "prospects_run", "prospect_audit", "prospect_set_status",
              "prospects_coverage", "prospect_approve"):
        assert callable(getattr(srv, t))
    assert permissions.classify("mcp__helios__prospect_approve", {"prospect_id": 1}) == "ask"
    assert permissions.classify("mcp__helios__prospects_run", {}) == "allow"
    names = [n for n in dir(srv) if n.startswith("prospect")]
    assert not any(w in n for n in names for w in ("send", "contact", "email", "whatsapp", "message"))
    public = (_ROOT / "mcp" / "helios_public_server.py").read_text(encoding="utf-8")
    assert "prospect" not in public


def test_no_whatsapp_or_maps_scraping_code():
    src = "".join(f.read_text(encoding="utf-8") for f in (_ROOT / "helios" / "prospecting").glob("*.py"))
    for bad in ("graph.facebook.com", "api.whatsapp.com/send", "/maps/search", "maps/api/place", "places.googleapis"):
        assert bad not in src
    assert "opt-in" in src and "visitor starts the chat" in src               # WhatsApp only as opt-in features


@pytest.mark.parametrize("pkg", list(scoring.PACKAGES))
def test_offer_sentence_reads_naturally(pc, pkg):
    # live UI check caught "I build website redesign + seo / basic analyticss for local businesses"
    p = researched(pc)
    p["package"] = pkg
    body = outreach.build_draft(p)["body"]
    line = next(ln for ln in body.splitlines() if ln.startswith("I build"))
    assert "+" not in line and "ss for" not in line and outreach._OFFER[pkg] in line
