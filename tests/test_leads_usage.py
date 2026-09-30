"""Lead finder + usage log: leads need a real public source that opens, prices come from the rate
card (never the model), personal phone numbers aren't stored, duplicates merge, services rotate,
statuses move, the night task / briefing / jobs / tools are wired, and every AI call's tokens are
recorded per purpose."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from helios import agy_cli, briefing, conf, leads, memory_store, usage

_ROOT = Path(__file__).resolve().parent.parent
ok = lambda url: (url, "ok")


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(memory_store, "vault", lambda: vault)
    cfg = {"enabled": True, "services": ["web_dev", "logo_design", "app_dev"], "per_night": 2,
           "max_leads_per_service": 2, "currency": "USD",
           "rates": {"logo_design": {"small": [60, 120], "medium": [150, 400], "large": [500, 900]}}}
    monkeypatch.setattr(leads, "cfg", lambda: cfg)
    return type("E", (), {"vault": vault, "cfg": cfg, "tmp": tmp_path})


def L(title="Bakery needs a website with ordering", url="https://forum.example.com/p/1", scope="medium", **kw):
    return {"title": title, "need": kw.pop("need", f"{title}. Mobile friendly, order form."),
            "scope": scope, "source_url": url, "source_name": "Example forum",
            "contact": kw.pop("contact", "reply on the post"), "fit": "core web job",
            "confidence": kw.pop("confidence", 0.8), "pitch": "Hi! I can help — how many products?",
            "budget_stated": kw.pop("budget_stated", ""), **kw}


# ------------------------------------------------------------------ pricing

def test_prices_come_from_the_rate_card(env):
    assert leads.price_for("web_dev", "medium") == (800, 2500)                  # default card
    assert leads.price_for("logo_design", "small") == (60, 120)                # your override
    assert leads.price_for("logo_design", "enormous") == (150, 400)            # unknown -> medium
    d, _ = leads.clean(L(scope="large", price_min=1, price_max=99999), "web_dev")   # model can't set price
    assert (d["price_min"], d["price_max"]) == (2500, 8000)


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("raw,why", [
    (L(url=""), "source"), (L(url="http://127.0.0.1/x"), "source"), (L(title=""), "title"),
    (L(need="api key: sk-abcdefghijklmnopqrstuvwxyz"), "secret"),
])
def test_bad_leads_are_dropped(raw, why):
    d, reason = leads.clean(raw, "web_dev")
    assert d is None and why in reason


@pytest.mark.parametrize("url,ok_", [
    ("https://www.reddit.com/r/forhire/comments/", False),               # the live test's listing link
    ("https://www.reddit.com/r/forhire/comments/1abc2de/hiring_web_dev/", True),
    ("https://www.upwork.com/freelance-jobs/", False),
    ("https://example.com/jobs/", False),
    ("https://example.com/jobs/12345-website-for-bakery", True),
    ("https://smallbiz.example.com/", False),
    ("https://board.example.com/post?id=991", True),
])
def test_lead_links_must_be_specific(url, ok_):
    assert leads.specific_url(url) is ok_
    d, why = leads.clean(L(url=url), "web_dev")
    assert (d is not None) is ok_ and (ok_ or "listing" in why)


def test_personal_phone_numbers_are_not_stored():
    d, _ = leads.clean(L(contact="call +44 7700 900123"), "web_dev")
    assert "7700" not in d["contact"]
    d, _ = leads.clean(L(contact="hello@sunrisebakery.co.uk"), "web_dev")
    assert d["contact"] == "hello@sunrisebakery.co.uk"                       # public business email ok


def test_store_verifies_dedupes_and_caps(env):
    out = leads.store("web_dev", [L(), L(title="Other", url="https://x.example.com/2", need="Different need entirely here"),
                                  L(title="Third", url="https://y.example.com/3", need="A third distinct request")],
                      verify=ok)
    assert len(out["new"]) == 2 and "over the per-service limit" in out["dropped"]
    again = leads.store("web_dev", [L(url="https://www.forum.example.com/p/1/")], verify=ok)
    assert len(again["duplicates"]) == 1 and not again["new"]
    dead = leads.store("web_dev", [L(title="Dead", url="https://dead.example.com/post/9", need="Zzz unique need")],
                       verify=lambda u: (u, "failed"))
    assert dead["dropped"] == ["source link didn't open"]
    d = leads.all_leads()[0]
    assert d["status"] == "new" and d["price_min"] == 800 and d["source_check"] == "ok"
    assert Path(d["path"]).parent == env.vault / "Leads"
    assert "## Pitch draft (you send it)" in Path(d["path"]).read_text(encoding="utf-8")


def test_status_flow(env):
    leads.store("web_dev", [L()], verify=ok)
    lid = leads.all_leads()[0]["id"]
    assert leads.set_status(lid, "contacted")["status"] == "contacted"
    assert leads.all_leads("new") == [] and len(leads.all_leads("contacted")) == 1
    with pytest.raises(ValueError):
        leads.set_status(lid, "sold")
    assert leads.set_status("nope", "won") is None


def test_services_rotate(env):
    assert leads.pick() == ["web_dev", "logo_design"]
    leads.search_service("web_dev", call=lambda s: ([], ""), verify=ok)
    assert leads.pick(1) == ["logo_design"]


def test_call_agent_uses_research_mode(env, monkeypatch):
    seen = {}

    def fake(prompt, rules, **kw):
        seen.update(kw, prompt=prompt, rules=rules)
        return {"text": json.dumps({"leads": [L()]}), "errors": [], "gate_failure": None}
    monkeypatch.setattr(agy_cli, "run_once", fake)
    monkeypatch.setattr(conf, "brain_engine", lambda: "antigravity")
    raw, err = leads._call_agent("logo_design")
    assert not err and raw[0]["title"].startswith("Bakery")
    assert seen["allow_only"] == agy_cli.RESEARCH_TOOLS and seen["label"] == "leads:logo_design"
    assert "worldwide" in seen["prompt"].lower() and "untrusted DATA" in seen["rules"]
    assert "reply on the post" in seen["rules"]                              # privacy rule present


# ------------------------------------------------------------------ night / briefing / jobs / tools

def test_night_task_and_briefing(env, monkeypatch):
    monkeypatch.setattr(leads, "_call_agent", lambda s: ([L(title=f"{s} job", url=f"https://ex.com/{s}",
                                                               need=f"Need for {s} work, unique {s}")], ""))
    monkeypatch.setattr(leads.research, "check_source", ok)
    res = leads.night_task({"online": True})
    assert res.status == "ok" and res.data["new"] == 2 and res.data["top"][0]["price_min"] > 0
    assert leads.night_task({"online": False}).status == "skipped"
    rec = {"night": "2026-10-01", "status": "completed", "tasks": {"find_leads": {
        "status": "ok", "summary": res.summary, "completed": res.completed, "observed": res.observed,
        "suggested": [], "approval": [], "failed": [], "data": res.data}}}
    b = briefing.build(datetime(2026, 10, 1, 8, 30), rec)
    text, said = briefing.render(b), briefing.spoken(b)
    assert "### Leads" in text and "- New leads: 2" in text and "helios leads show lead-" in text
    assert "I found 2 new leads; the best:" in said and "USD" in said


def test_night_mode_schedule_and_jobs_know_leads():
    from helios import jobs
    from helios.night_mode import scheduler as night
    assert "find_leads" in night.TASKS and night.DEFAULT_SCHEDULE["find_leads"] == "02:40"
    assert "leads" in jobs.TYPES


def test_mcp_lead_and_usage_tools(env):
    leads.store("web_dev", [L()], verify=ok)
    lid = leads.all_leads()[0]["id"]
    spec = importlib.util.spec_from_file_location("helios_server_leads", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert "Bakery" in srv.list_leads() and "USD 800–2,500" in srv.list_leads()
    assert "pitch:" in srv.lead_details(lid)
    assert "-> won" in srv.set_lead_status(lid, "won")
    assert "No AI usage" in srv.ai_usage()
    from helios import permissions
    for t in ("list_leads", "lead_details", "set_lead_status", "ai_usage"):
        assert permissions.classify(f"mcp__helios__{t}", {}) == "allow"


def test_leads_never_get_a_send_tool():
    src = (_ROOT / "helios" / "leads.py").read_text(encoding="utf-8")
    assert "send_message" not in src and "smtplib" not in src and "notify_tim" not in src


# ------------------------------------------------------------------ usage

def test_usage_records_and_summarises():
    usage.record("chat", 30000, 800)
    usage.record("research:Gemini", 45000, 9000)
    usage.record("leads:web_dev", 50000, 7000)
    usage.record("leads:logo_design", 40000, 6000)
    usage.record("nothing", 0, 0)                                           # ignored
    s = usage.summary(1)
    today = datetime.now().date().isoformat()
    assert s[today]["leads"] == {"calls": 2, "in": 90000, "out": 13000}
    assert s[today]["chat"]["calls"] == 1 and "nothing" not in s[today]
    text = usage.format_summary(1)
    assert "leads 2x 103k" in text and "M tokens" in text


def test_run_once_records_usage(monkeypatch, tmp_path):
    recorded = []
    monkeypatch.setattr(usage, "record", lambda label, i, o: recorded.append((label, i, o)))
    monkeypatch.setattr(agy_cli, "command", lambda: ["agy"])
    monkeypatch.setattr(agy_cli, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(agy_cli, "prepare_workspace", lambda ws, rules, tools=True: {"ws": ws})

    class S:
        def __init__(self, *a, **k):
            self.conversation_id = None

        def start(self):
            return type("P", (), {"wait": lambda self, timeout=None: 0, "returncode": 0})()

        def close(self):
            pass

        def stderr_text(self):
            return ""
    monkeypatch.setattr(agy_cli, "AgySession", S)
    monkeypatch.setattr(agy_cli, "consume_turn", lambda s: {"text": "hi", "usage": {"in": 1234, "out": 56}})
    agy_cli.run_once("x", "rules", label="leads:web_dev")
    assert recorded == [("leads:web_dev", 1234, 56)]
