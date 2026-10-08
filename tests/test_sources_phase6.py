"""Phase 6: research findings and leads are checked by READING their source (resolve the search
redirect → render if needed → extract → does the page support the claim?), with provenance kept.
Leads must come from clients (not "[For Hire]" freelancers), be recent, and be supported by the
post; Reddit-style sites that block automated reading keep the search-seen lead, marked unchecked.
Fake getters only — no network."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helios import leads, memory_store, research
from helios.web import sources

POST = ("Looking for a web developer to build an online store for our bakery in Leeds. We need "
        "delivery slots, a menu and gift cards. Budget around 1,500 GBP. Call me on +44 7700 900123 "
        "or email jane.doe@gmail.com. Please reply with examples of your work.")


def page(text=POST, title="Need a bakery website with online ordering", date="2026-10-01", **kw):
    return {"text": text, "title": title, "sitename": "Example forum", "date": date, "rendered": False,
            "method": "trafilatura", "fetched_at": "2026-10-08T09:00:00+00:00", "text_hash": "abc",
            "final_url": kw.pop("final_url", "https://forum.example.com/p/1"), **kw}


def getter_for(result):
    calls = []

    def g(url, purpose="read"):
        calls.append((url, purpose))
        return result(url) if callable(result) else result
    g.calls = calls
    return g


# ------------------------------------------------------------------ sources.check

def test_check_reads_page_with_provenance_and_strong_grounding():
    g = getter_for(page())
    chk = sources.check("https://forum.example.com/p/1", "bakery online store delivery slots gift cards",
                        getter=g, resolver=lambda u: u)
    assert chk["status"] == "ok" and chk["read"] and chk["grounding"] == "strong"
    assert chk["page_title"].startswith("Need a bakery") and chk["page_date"] == "2026-10-01"
    assert g.calls == [("https://forum.example.com/p/1", "crawl")]          # polite crawl mode
    assert "7700" not in chk["excerpt"] and "jane.doe" not in chk["excerpt"]
    assert "[number removed]" in chk["excerpt"] and "[email removed]" in chk["excerpt"]


def test_check_weak_grounding():
    chk = sources.check("https://x.example/p", "quantum computing chip benchmark results",
                        getter=getter_for(page()), resolver=lambda u: u)
    assert chk["grounding"] == "weak" and chk["coverage"] < sources.WEAK


@pytest.mark.parametrize("err,status", [
    ("refused (HTTP 403)", "blocked"), ("rate-limited (HTTP 429)", "blocked"),
    ("the site shows a bot check / access wall — not bypassed", "blocked"),
    ("robots.txt asks automated visitors not to read this page", "blocked"),
    ("HTTP 404", "failed"), ("HTTP 410", "failed"), ("HTTP 503", "failed"),
    ("blocked: internal address (blocked: SSRF protection)", "failed"),
])
def test_check_error_classes(err, status):
    opener_calls = []
    chk = sources.check("https://site.example/p", "x", getter=getter_for({"error": err, "final_url": "https://site.example/p"}),
                        resolver=lambda u: u, opener=lambda u: (opener_calls.append(u), (u, "ok"))[1])
    assert chk["status"] == status and not chk["read"]
    assert opener_calls == []              # refusals / robots / dead links never get a second fetch


def test_network_trouble_falls_back_to_a_plain_link_check():
    chk = sources.check("https://site.example/p", "x",
                        getter=getter_for({"error": "couldn't reach the site (timed out)"}),
                        resolver=lambda u: u, opener=lambda u: (u, "ok"))
    assert chk["status"] == "ok" and not chk["read"] and "plain link check" in chk["reason"]


def test_search_redirect_links_are_resolved_first(monkeypatch):
    real = "https://blog.example.com/mcp-update"
    monkeypatch.setattr(research, "check_source", lambda u, timeout=8.0: (real, "ok"))
    g = getter_for(page(final_url=real))
    chk = sources.check("https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "bakery", getter=g)
    assert g.calls[0][0] == real and chk["final_url"] == real


def test_normalize_old_verifier_shapes():
    assert sources.normalize("ok", "u") == {"status": "ok", "final_url": "u", "read": False}
    assert sources.normalize(("f", "blocked"), "u")["final_url"] == "f"
    assert sources.normalize({"status": "ok", "read": True}, "u")["read"]


# ------------------------------------------------------------------ research

@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(memory_store, "vault", lambda: v)
    monkeypatch.setattr(research, "cfg", lambda: {"enabled": True, "max_findings_per_topic": 4})
    monkeypatch.setattr(leads, "cfg", lambda: {"enabled": True, "services": ["web_dev"], "max_leads_per_service": 3,
                                               "max_post_age_days": 45, "currency": "USD"})
    return v


def F(title="Bakery store builders compared", summary="A guide to building a bakery online store with delivery slots and gift cards.",
      url="https://forum.example.com/p/1", confidence=0.8):
    return {"title": title, "summary": summary, "source_url": url, "source_name": "Example", "confidence": confidence}


def as_verifier(chk):
    return lambda url: chk


def test_research_keeps_provenance_and_evidence(vault):
    chk = sources.check("https://forum.example.com/p/1", "Bakery store builders compared bakery online store delivery slots gift cards",
                        getter=getter_for(page()), resolver=lambda u: u)
    out = research.store("Shops", "", [F()], verify=as_verifier(chk))
    f = out["new"][0]
    assert f["source_check"] == "ok" and f["grounding"] in ("strong", "partial")
    saved = research.get(f["id"])
    assert saved["source_title"].startswith("Need a bakery") and saved["fetched_at"].startswith("2026-10-08")
    assert saved["evidence"] and "7700" not in saved["evidence"]
    text = research.format_findings([saved], verbose=True)
    assert "source: page \"Need a bakery" in text and "dated 2026-10-01 (estimated)" in text and "evidence:" in text


def test_research_weak_support_lowers_confidence(vault):
    chk = {"status": "ok", "read": True, "final_url": "https://forum.example.com/p/1", "grounding": "weak",
           "coverage": 0.1, "page_title": "Something else", "excerpt": ""}
    f = research.store("Shops", "", [F(confidence=0.8)], verify=as_verifier(chk))["new"][0]
    assert f["confidence"] == 0.48 and f["uncertainty"].startswith("The source page barely mentions this")


def test_research_blocked_site_is_not_penalised(vault):
    chk = {"status": "blocked", "read": False, "final_url": "https://www.reddit.com/r/x/comments/1/a/"}
    f = research.store("Shops", "", [F(url="https://www.reddit.com/r/x/comments/1/a/")], verify=as_verifier(chk))["new"][0]
    assert f["confidence"] == 0.8 and "blocks automated reading" in f["grounding"]


# ------------------------------------------------------------------ leads

def L(title="Bakery needs a website with ordering", need="Bakery in Leeds wants an online store with delivery slots and gift cards.",
      url="https://forum.example.com/p/1", posted="", **kw):
    return {"title": title, "need": need, "scope": "medium", "source_url": url, "source_name": "Example forum",
            "contact": "reply on the post", "fit": "web job", "confidence": 0.8, "pitch": "Hi!",
            "posted": posted, "budget_stated": kw.pop("budget_stated", ""), **kw}


def read_ok(**kw):
    def verify(url):
        c = sources.check(url, "bakery online store delivery slots gift cards leeds",
                          getter=getter_for(page(**kw)), resolver=lambda u: u)
        return c
    return verify


def test_lead_with_provenance_and_scrubbed_evidence(vault):
    out = leads.store("web_dev", [L()], verify=read_ok())
    assert len(out["new"]) == 1
    saved = leads.get(out["new"][0]["id"])
    assert saved["source_title"].startswith("Need a bakery") and saved["grounding"] in ("strong", "partial")
    assert saved["evidence"] and "7700" not in saved["evidence"] and "@gmail" not in saved["evidence"]
    note = Path(saved["path"]).read_text(encoding="utf-8")
    assert "## Evidence from the source" in note and "## Pitch draft (you send it)" in note
    shown = leads.format_leads([saved], verbose=True)
    assert "checked: page" in shown and "evidence:" in shown


def test_for_hire_posts_are_not_leads(vault):
    calls = []
    out = leads.store("web_dev", [L(title="[For Hire] Web developer, 5 years experience")],
                      verify=lambda u: (calls.append(u), "ok")[1])
    assert out["dropped"] == ["someone offering services, not a client"] and calls == []   # dropped before any fetch
    out = leads.store("web_dev", [L()], verify=read_ok(title="[For Hire] I build bakery sites"))
    assert out["dropped"] == ["someone offering services, not a client"]


@pytest.mark.parametrize("title,offering", [
    ("[For Hire] Logo designer available", True), ("Available for freelance work — React dev", True),
    ("[Hiring] Need a logo, budget $200", False), ("[Hiring] Web dev (not for hire posts please)", False),
    ("Looking for a Shopify developer", False),
])
def test_offering_detection(title, offering):
    assert leads.offering_not_hiring(title) is offering


def test_old_posts_are_dropped(vault):
    old = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    out = leads.store("web_dev", [L(posted=old)], verify=read_ok())
    assert out["dropped"][0].startswith("posted 90 days ago")
    out = leads.store("web_dev", [L(url="https://forum.example.com/p/2")], verify=read_ok(date=old))
    assert out["dropped"][0].startswith("page dated 90 days ago")


def test_unsupported_leads_are_dropped(vault):
    out = leads.store("web_dev", [L()], verify=read_ok(text="Our quarterly report on steel prices and shipping. " * 5))
    assert out["dropped"] == ["the source page doesn't mention this need"]


def test_no_hiring_signal_lowers_confidence(vault):
    text = "Sunrise bakery in Leeds. We make bread. Online store with delivery slots and gift cards coming soon."
    out = leads.store("web_dev", [L()], verify=read_ok(text=text))
    assert out["new"] and out["new"][0]["confidence"] == 0.56


def test_reddit_blocked_keeps_the_search_seen_lead(vault):
    chk = {"status": "blocked", "read": False, "final_url": "https://www.reddit.com/r/forhire/comments/1abc/need_a_site/"}
    out = leads.store("web_dev", [L(url="https://www.reddit.com/r/forhire/comments/1abc/need_a_site/")],
                      verify=lambda u: chk)
    assert len(out["new"]) == 1 and out["new"][0]["source_check"] == "blocked"
    assert "seen via search" in out["new"][0]["grounding"]


def test_dead_links_are_dropped(vault):
    out = leads.store("web_dev", [L()], verify=lambda u: {"status": "failed", "final_url": u, "read": False})
    assert out["dropped"] == ["source link didn't open"]


def test_rules_tell_the_agent_to_skip_for_hire():
    assert "[For Hire]" in leads.LEAD_RULES and "competitors" in leads.LEAD_RULES


def test_short_extraction_is_checked_against_the_whole_page():
    r = page(text="Release v16.3.8", alt_text="Security fixes: image optimization SSRF, SSG ISR cache poisoning")
    chk = sources.check("https://github.com/x/releases/tag/v1", "SSRF image optimization cache poisoning ISR",
                        getter=getter_for(r), resolver=lambda u: u)
    assert chk["grounding"] == "strong"
