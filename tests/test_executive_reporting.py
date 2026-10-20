"""Executive reporting + personal phone briefings (docs/HELIOS_EXECUTIVE_REPORTING.md).

Everything here is mocked: no Telegram message, toast, phone call or vault file is real (conftest's
_no_real_outbound + explicit fakes). What's tested is the decision logic: severity routing, dedupe,
delivered-vs-reviewed, quiet hours / snooze, report assembly from records (and honesty when a
source fails), schedules that don't repeat or backfill, and the calling safety rails — authorized
numbers only, limits, retries, cancel, fallbacks, and no path for anyone but the owner to be called.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helios import briefing, calling, conf, dashboard, db, executive, health, memory_store, permissions
from helios.prospecting import geo, proposal, store

_ROOT = Path(__file__).resolve().parent.parent
_REAL_PLACE = calling.place                       # captured before conftest's no-outbound stub
from helios import channels as _channels  # noqa: E402
_REAL_SEND_VOICE = _channels.telegram_send_voice
_REAL_FIND_CHAT = _channels.telegram_find_chat
OWNER = "+919876543210"
SID, TOKEN = "AC" + "0123456789abcdef" * 2, "fedcba9876543210" * 2
TWILIO = {"account_sid": SID, "auth_token": TOKEN, "from_number": "+15550001111"}


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def ex(monkeypatch, tmp_path):
    """Executive reporting on, deterministic collectors, no real vault / memory reads."""
    c = dict(executive._DEFAULTS)
    monkeypatch.setattr(executive, "cfg", lambda: c)
    for name, val in (("_research", {"new": 0, "top": []}), ("_leads", {"new": 0, "top": []}),
                      ("_memory", {"new": 0, "items": []}), ("_jobs", {"ok": [], "failed": [], "skipped": []}),
                      ("_night", {"status": "none", "ok": [], "failed": [], "skipped": [], "partial": [], "report": ""})):
        monkeypatch.setattr(executive, name, lambda *a, v=val: json.loads(json.dumps(v)))
    monkeypatch.setattr(executive, "_projects", lambda: [])
    monkeypatch.setattr(executive, "_gmail", lambda: "not connected (helios gmail login)")
    monkeypatch.setattr(executive, "_polarion", lambda: "not configured (helios polarion token local)")
    monkeypatch.setattr(memory_store, "pending", lambda: [])
    monkeypatch.setattr(proposal, "folder", lambda: tmp_path / "Proposals")
    monkeypatch.setattr(geo.conf, "DATA_DIR", tmp_path)
    # Calling defaults to OFF in tests, never the user's real settings.toml (the `phone`/`tg`
    # fixtures override this). Without it, a test's result would depend on live on-disk config.
    monkeypatch.setattr(calling, "cfg", lambda: dict(calling._DEFAULTS))
    store._inited.clear()
    return c


@pytest.fixture
def phone(monkeypatch, ex):
    """Calling configured + enabled for the owner's number (Twilio creds mocked, never used for real)."""
    c = {**calling._DEFAULTS, "enabled": True, "authorized_numbers": [OWNER]}
    monkeypatch.setattr(calling, "cfg", lambda: c)
    real = conf.provider_cfg
    monkeypatch.setattr(conf, "provider_cfg", lambda name: dict(TWILIO) if name == "twilio" else
                        ({} if name in ("calling", "telegram") else real(name)))   # never the real secrets
    db.init()
    db.set_state("calling_verified", calling._fingerprint())         # as if `helios calling test` completed
    return c


def _biz(i, name, **kw):
    el = {"type": "node", "id": i, "lat": 17.4, "lon": 78.43, "tags": {"name": name, "shop": "hairdresser"}}
    p, _ = store.upsert(geo.to_business(el, "Testpur"))
    if kw:
        store.update(p["id"], **kw)
    return store.get(p["id"])


def _qualified(i, name, *, status="QUALIFIED", priority="A", email="hello@biz.in", quote=None, draft=None):
    now = datetime.now().isoformat(timespec="seconds")
    p = _biz(i, name, last_researched=now, website_status="none", priority=priority, ai_score=4, website_score=0,
             package="basic_website", email=email, quote=quote or {"status": "NEEDS USER PRICING"},
             reasons={"website": [{"factor": "no website found", "detail": "OSM + web search",
                                   "evidence": "https://www.openstreetmap.org/node/%d" % i}]},
             draft=draft or {"channel": "email", "to": email, "subject": "s", "body": "b", "claims": []})
    for s in ("RESEARCHED", "QUALIFIED") + (("DRAFTED",) if status == "DRAFTED" else ()):
        store.set_status(p["id"], s)
    return store.get(p["id"])


def _at(h, m=0, days=0):
    return (datetime.now() + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)


# ------------------------------------------------------------------ events: severity, dedupe, delivery

def test_severity_routing_critical_now_high_prompt_normal_batched(ex):
    pub = []
    executive.emit("system", "disk", "CRITICAL", "Disk almost full")
    executive.emit("projects", "project_failing", "HIGH", "Project X is failing")
    executive.emit("research", "finding", "NORMAL", "New finding")
    executive.emit("research", "note", "LOW", "Minor note")
    done = executive.route(_at(11), publish=lambda k, d: pub.append((k, d)))
    assert done["delivered"] == 2 and done["batched"] == 2
    st = {e["summary"]: e for e in executive.events()}
    assert st["Disk almost full"]["state"] == "delivered" and st["Project X is failing"]["state"] == "delivered"
    assert st["New finding"]["state"] == "batched" and st["Minor note"]["state"] == "batched"
    assert any(k == "dashboard" for k, _ in pub)


def test_quiet_hours_hold_high_but_never_critical(ex):
    executive.emit("projects", "project_failing", "HIGH", "Project X is failing")
    executive.emit("system", "disk", "CRITICAL", "Disk almost full")
    executive.route(_at(23, 30))
    st = {e["summary"]: e["state"] for e in executive.events()}
    assert st == {"Project X is failing": "held", "Disk almost full": "delivered"}
    executive.route(_at(9))                                            # quiet hours over → delivered
    assert {e["summary"]: e["state"] for e in executive.events()}["Project X is failing"] == "delivered"


def test_snooze_holds_non_critical(ex):
    executive.snooze(2)
    executive.emit("jobs", "job_failing", "HIGH", "Job y failed twice")
    executive.route(_at(11))
    assert executive.events()[0]["state"] == "held"
    executive.snooze(0)
    assert executive.snoozed_until() is None
    executive.route(_at(11))
    assert executive.events()[0]["state"] == "delivered"


def test_dedupe_counts_instead_of_repeating_and_escalation_sticks(ex):
    a = executive.emit("projects", "project_failing", "NORMAL", "X failing", dedupe_key="project:X")
    b = executive.emit("projects", "project_failing", "HIGH", "X failing again", dedupe_key="project:X")
    c = executive.emit("projects", "project_failing", "LOW", "X failing still", dedupe_key="project:X")
    assert (a["status"], b["status"], c["status"]) == ("new", "repeat", "repeat")
    evs = executive.events()
    assert len(evs) == 1 and evs[0]["count"] == 3 and evs[0]["severity"] == "HIGH"
    executive.acknowledge([evs[0]["id"]])                              # reviewed → a new occurrence is new
    assert executive.emit("projects", "project_failing", "HIGH", "X failing", dedupe_key="project:X")["status"] == "new"


def test_delivered_is_not_acknowledged(ex):
    executive.emit("projects", "project_failing", "HIGH", "Project X is failing")
    executive.route(_at(11), publish=lambda *a: None)
    ev = executive.events()[0]
    assert ev["state"] == "delivered" and ev["acknowledged"] is None
    assert ev["delivery"]["dashboard"]["status"] == "delivered"
    assert executive.acknowledge(None) == 1
    assert executive.events(unacked=True) == []


def test_channel_failures_are_recorded_honestly(ex, monkeypatch):
    from helios import notify
    monkeypatch.setattr(notify, "toast", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no toast")))
    executive.emit("system", "disk", "CRITICAL", "Disk almost full")
    executive.route(_at(11), publish=None)
    d = executive.events()[0]["delivery"]
    assert d["dashboard"]["status"] == "unavailable"          # no dashboard connected — not claimed
    assert d["toast"]["status"] == "failed" and "no toast" in d["toast"]["detail"]
    assert d["telegram"]["status"] == "unavailable"           # not configured


def test_payloads_are_scrubbed(ex):
    executive.emit("prospecting", "x", "HIGH", "Call +91 98765 43210 or mail a@b.com, key sk-ant-abcdefghijklmnopqrstuvwxyz123456")
    s = executive.events()[0]["summary"]
    assert "98765" not in s and "a@b.com" not in s and "sk-ant-abc" not in s


def test_disabled_reporting_records_nothing(ex):
    ex["enabled"] = False
    assert executive.emit("x", "y", "HIGH", "z")["status"] == "off"
    assert executive.route() == {"off": True}


# ------------------------------------------------------------------ leads

def test_lead_alert_high_only_for_priority_a_with_public_contact(ex):
    a = _qualified(1, "Glow Salon", status="DRAFTED")
    b = _qualified(2, "Quiet Bakery", priority="B")
    c = _qualified(3, "No Contact Cafe", email="")
    for p in (a, b, c):
        executive.lead_event(p)
    sev = {e["related"]: e for e in executive.events()}
    assert sev[f"prospect:{a['id']}"]["severity"] == "HIGH" and sev[f"prospect:{a['id']}"]["approval"]
    assert "add a price" in sev[f"prospect:{a['id']}"]["action"]          # rate card missing → asked, not invented
    assert sev[f"prospect:{b['id']}"]["severity"] == "NORMAL"
    assert sev[f"prospect:{c['id']}"]["severity"] == "NORMAL"
    executive.lead_event(a)                                              # same lead again → one event
    assert len([e for e in executive.events() if e["related"] == f"prospect:{a['id']}"]) == 1
    assert "hello@biz.in" not in json.dumps(executive.events())


# ------------------------------------------------------------------ reports

def test_eod_report_is_built_from_records(ex):
    _qualified(1, "Glow Salon", status="DRAFTED")
    _qualified(2, "Spice Kitchen")
    _biz(3, "Unchecked Shop")                                            # discovered, not verified
    rep = executive.build("eod")
    b = rep["business"]
    assert b["discovered"] == 3 and b["researched"] == 2 and b["qualified"] == 2
    assert b["no_website_verified"] == 2 and b["no_website_unverified"] == 1
    assert b["drafted"] == 1 and b["needs_pricing"] == 2 and b["contacted_in_period"] == 0
    assert any("not yet confirmed" in v for v in rep["sections"]["verify"])
    assert any("nothing has been sent" in a for a in rep["sections"]["approval"])
    assert "Glow Salon" in rep["text"] and "Every number below comes from Helios's records" in rep["text"]
    assert rep["spoken"].startswith("Here's your end-of-day report, sir.")
    assert "hello@biz.in" not in rep["spoken"] and "helios prospects" not in rep["spoken"]


def test_a_failing_source_is_reported_and_the_report_continues(ex, monkeypatch):
    monkeypatch.setattr(executive, "_research", lambda *a: (_ for _ in ()).throw(OSError("library locked")))
    _qualified(1, "Glow Salon")
    rep = executive.build("midday")
    assert any("research library" in f and "library locked" in f for f in rep["sections"]["failed"])
    assert rep["business"]["qualified"] == 1 and rep["text"]


def test_weekly_review_never_counts_leads_as_revenue(ex):
    _qualified(1, "Glow Salon", status="DRAFTED")
    rep = executive.build("weekly")
    assert rep["metrics"]["revenue"].startswith("not tracked")
    assert rep["metrics"]["won"] == 0
    assert any("prices" in s for s in rep["sections"]["suggested"])


def test_pipeline_outcomes_need_contact_and_evidence(ex):
    p = _qualified(1, "Glow Salon", status="DRAFTED")
    with pytest.raises(store.StatusError, match="actually contacted"):
        store.set_status(p["id"], "LOST")
    store.approve(p["id"])
    store.record_contact(p["id"], channel="email", evidence="gmail sent id 123")
    with pytest.raises(store.StatusError, match="needs evidence"):
        store.set_status(p["id"], "WON")
    store.set_status(p["id"], "PROPOSAL_ACCEPTED", evidence="signed quote Q-7")
    won = store.set_status(p["id"], "WON", evidence="invoice INV-12 paid")
    assert won["status"] == "WON" and "invoice INV-12" in won["contact_log"][-1]["event"]
    b = executive.business(datetime.now() - timedelta(hours=1))
    assert b["won"] == 1 and b["contacted_in_period"] == 1


def test_quiet_midday_is_skipped_and_nothing_saved(ex):
    res = executive.deliver_report("midday")
    assert res["status"] == "skipped" and executive.reports() == []


def test_report_saved_delivered_and_digest_marked(ex):
    _qualified(1, "Glow Salon")
    executive.emit("research", "finding", "NORMAL", "New finding")
    executive.route(_at(11))
    res = executive.deliver_report("eod", publish=lambda *a: None)
    assert res["status"] == "delivered" and Path(res["saved"]["path"]).exists()
    assert res["delivery"]["dashboard"]["status"] == "delivered" and res["call"] is None
    assert executive.events()[0]["state"] == "reported"
    assert executive.reports()[0]["kind"] == "eod"


def test_schedule_runs_once_per_day_and_never_backfills(ex, monkeypatch):
    sent = []
    monkeypatch.setattr(executive, "deliver_report", lambda kind, now=None, publish=None, **k: sent.append(kind))
    ex["weekly_review"] = False
    executive.tick(_at(12, 59))
    assert sent == []
    executive.tick(_at(13, 1))
    executive.tick(_at(13, 5))
    assert sent == ["midday"]                                             # once, not every tick
    executive.tick(_at(23, 30))                                           # eod 18:30 missed by 5 h → not sent at night
    assert sent == ["midday"]
    db.set_state("exec_done_midday", "")
    executive.tick(_at(16, 30))                                           # PC slept through 13:00, woke within 4 h
    assert sent == ["midday", "midday"]


def test_weekly_review_once_per_week(ex, monkeypatch):
    sent = []
    monkeypatch.setattr(executive, "deliver_report", lambda kind, now=None, publish=None, **k: sent.append(kind))
    ex.update(midday_report=False, end_of_day_report=False)
    day = datetime.now()
    ex["weekly_day"] = day.strftime("%a").lower()
    executive.tick(day.replace(hour=9, minute=30))
    executive.tick(day.replace(hour=10, minute=0))
    assert sent == ["weekly"]


def test_night_finished_event_and_report(ex, monkeypatch):
    sent = []
    monkeypatch.setattr(executive, "deliver_report", lambda kind, now=None, publish=None, **k: sent.append(kind))
    executive.on_night_finished({"night": "2026-10-19", "status": "completed", "kind": "scheduled",
                                 "tasks": {"research": {"status": "ok"}, "run_checks": {"status": "failed"}}})
    ev = executive.events()[0]
    assert ev["severity"] == "HIGH" and "1 failed" in ev["summary"] and sent == ["night"]
    executive.on_night_finished({"night": "2026-10-20", "status": "completed", "kind": "manual", "tasks": {}})
    assert sent == ["night"]                                              # manual runs: event only


def test_morning_briefing_has_business_and_decisions(ex, monkeypatch):
    monkeypatch.setattr(health, "reports", lambda *a: [])
    _qualified(1, "Glow Salon", status="DRAFTED")
    b = briefing.build(datetime.now(), rec={})
    text, said = briefing.render(b), briefing.spoken(b)
    assert "## Business pipeline (last 24 hours)" in text and "Glow Salon" in text
    assert "## Needs your decision" in text and "outreach draft" in text
    assert "nothing has been sent" in text
    assert "qualified 1 business lead" in said and "Nothing has been sent" in said and "decision on" in said


def test_jobs_failing_twice_raises_one_high_event(ex):
    from helios import jobs
    jobs._init()
    job = {"id": 999, "name": "nightly-x", "fail_count": 1, "system": 0}
    jobs._finish_run(job, 0, "failed", "boom")
    jobs._finish_run({**job, "fail_count": 2}, 0, "failed", "boom")       # third failure → same event, counted
    evs = [e for e in executive.events() if e["type"] == "job_failing"]
    assert len(evs) == 1 and evs[0]["severity"] == "HIGH" and evs[0]["count"] == 2
    assert "3 times" in evs[0]["summary"]


# ------------------------------------------------------------------ calling: safety rails

def test_calling_refused_until_configured(ex, monkeypatch):
    monkeypatch.setattr(conf, "provider_cfg", lambda name: {})
    st = calling.status()
    assert not st["ready"] and not st["enabled"] and st["two_way"] is False
    res = calling.schedule("Morning briefing", report_kind="morning")
    assert res["status"] == "refused" and "calling is off" in res["reason"] and "Twilio isn't set up" in res["reason"]
    assert calling.calls()[0]["status"] == "refused"                      # the attempt is logged too


def test_schedule_and_quiet_hours(phone):
    r = calling.schedule("Morning briefing", report_kind="morning", due=_at(7), now=_at(6, 50))
    assert r["status"] == "scheduled" and r["call"]["due"].endswith("08:00")   # moved to the end of quiet hours
    crit = calling.schedule("Server down", critical=True, event_id="ev-1", due=_at(23), now=_at(23))
    assert crit["status"] == "refused" and "quiet hours" in crit["reason"]
    phone["critical_in_quiet_hours"] = True
    assert calling.schedule("Server down", critical=True, event_id="ev-2", due=_at(23), now=_at(23))["status"] == "scheduled"


def test_daily_limit_min_gap_and_one_call_per_event(phone):
    phone.update(maximum_calls_per_day=2, min_minutes_between_calls=60)
    assert calling.schedule("a", due=_at(10), now=_at(9))["status"] == "scheduled"
    gap = calling.schedule("b", due=_at(10, 30), now=_at(9))
    assert gap["status"] == "refused" and "since the last call" in gap["reason"]
    assert calling.schedule("c", due=_at(12), now=_at(9))["status"] == "scheduled"
    lim = calling.schedule("d", due=_at(15), now=_at(9))
    assert lim["status"] == "refused" and "daily limit" in lim["reason"]
    assert calling.schedule("e", event_id="ev-9", critical=True, due=_at(16), now=_at(9))["status"] == "scheduled"
    again = calling.schedule("e", event_id="ev-9", critical=True, due=_at(17), now=_at(9))
    assert again["status"] == "refused" and "already called" in again["reason"]


def test_unknown_number_index_refused(phone):
    assert calling.schedule("x", number_index=3)["status"] == "refused"


def test_cancel(phone):
    calling.schedule("a", due=_at(10), now=_at(9))
    calling.schedule("b", due=_at(12), now=_at(9))
    assert calling.cancel(calling.calls(upcoming=True)[0]["id"]) == 1
    assert calling.cancel() == 1
    assert calling.calls(upcoming=True) == []
    assert all(c["status"] == "canceled" for c in calling.calls())


def test_call_lifecycle_initiated_answered_completed(phone):
    calling.schedule("Morning briefing", due=_at(9), now=_at(9))
    placed = []
    states = iter([{"status": "ringing"}, {"status": "in-progress"}, {"status": "completed", "duration": 42}])

    def placer(text, to):
        placed.append((text, to))
        return {"ok": True, "sid": "CA1", "status": "queued", "detail": ""}
    calling.tick(_at(9, 1), placer=placer, poller=lambda sid: next(states))
    assert placed[0][1] == OWNER
    for _ in range(3):
        calling.tick(_at(9, 2), placer=placer, poller=lambda sid: next(states))
    c = calling.calls()[0]
    assert c["status"] == "completed" and c["answered"] == 1 and c["duration"] == 42
    assert [x["status"] for x in c["log"]] == ["scheduled", "initiated", "ringing", "answered", "completed"]
    assert len(placed) == 1                                               # never placed twice


def test_provider_failure_falls_back_to_dashboard(phone):
    calling.schedule("End-of-day report", due=_at(19), now=_at(19))
    calling.tick(_at(19, 1), placer=lambda t, to: {"ok": False, "sid": "", "status": "failed", "detail": "provider error: 401"})
    assert calling.calls()[0]["status"] == "failed"
    ev = executive.events()[0]
    assert ev["source"] == "calling" and "not delivered" in ev["summary"] and "401" in ev["summary"]


def test_no_answer_falls_back(phone):
    calling.schedule("x", due=_at(10), now=_at(10))
    calling.tick(_at(10, 1), placer=lambda t, to: {"ok": True, "sid": "CA2", "status": "queued", "detail": ""},
                 poller=lambda sid: {"status": "no-answer"})
    calling.tick(_at(10, 2), poller=lambda sid: {"status": "no-answer"})
    assert calling.calls()[0]["status"] == "no-answer" and calling.calls()[0]["answered"] == 0
    assert "no-answer" in executive.events()[0]["summary"]


def test_critical_retries_up_to_the_limit(phone):
    phone.update(max_attempts=2, retry_minutes=10, critical_in_quiet_hours=True)
    calling.schedule("Server down", critical=True, event_id="ev-3", due=_at(10), now=_at(10))
    fail = lambda t, to: {"ok": False, "sid": "", "status": "failed", "detail": "provider error: 500"}  # noqa: E731
    calling.tick(_at(10, 1), placer=fail)
    c = calling.calls()[0]
    assert c["status"] == "scheduled" and c["attempts"] == 1
    calling.tick(_at(10, 5), placer=fail)                                 # not due yet
    assert calling.calls()[0]["attempts"] == 1
    calling.tick(_at(10, 12), placer=fail)
    assert calling.calls()[0]["status"] == "failed" and calling.calls()[0]["attempts"] == 2


def test_live_call_without_status_times_out(phone):
    calling.schedule("x", due=_at(10), now=_at(10))
    calling.tick(_at(10, 1), placer=lambda t, to: {"ok": True, "sid": "CA3", "status": "queued", "detail": ""})
    calling.tick(datetime.now() + timedelta(hours=2), poller=lambda sid: {"status": "", "error": "timeout"})
    assert calling.calls()[0]["status"] == "failed"


def test_disabled_before_due_fails_safely(phone):
    calling.schedule("x", due=_at(10), now=_at(10))
    phone["enabled"] = False
    called = []
    calling.tick(_at(10, 1), placer=lambda *a: called.append(a))
    assert called == [] and calling.calls()[0]["status"] == "failed"


def test_place_only_dials_authorized_numbers_and_scrubs(phone, monkeypatch):
    sent = []

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        sent.append(req)
        return R(json.dumps({"sid": "CA9", "status": "queued"}).encode())
    bad = _REAL_PLACE("hello", "+15559999999", opener=opener)
    assert bad["status"] == "refused" and sent == []                      # a lead's/client's number: refused
    ok = _REAL_PLACE("Report for sir. Key sk-ant-abcdefghijklmnopqrstuvwxyz123456, mail x@y.com <b>", OWNER, opener=opener)
    assert ok["ok"] and ok["sid"] == "CA9"
    body = urllib.parse.parse_qs(sent[0].data.decode())
    assert body["To"] == [OWNER] and body["From"] == [TWILIO["from_number"]]
    tw = body["Twiml"][0]
    assert "sk-ant-abc" not in tw and "x@y.com" not in tw and "<b>" not in tw and "&lt;b&gt;" in tw
    assert tw.endswith("Goodbye.</Say></Response>")
    assert TOKEN not in tw and sent[0].full_url.endswith(f"/Accounts/{SID}/Calls.json")


def test_mispasted_credentials_never_reach_the_provider(phone, monkeypatch):
    monkeypatch.setattr(conf, "provider_cfg", lambda name: {"account_sid": "helios calling number +91XXXX",
                                                            "auth_token": "x", "from_number": "+15550001111"})
    ok, why = calling.provider_ready()
    assert not ok and "look wrong" in why and "helios calling number" not in why
    assert _REAL_PLACE("hi", OWNER, opener=lambda *a, **k: pytest.fail("must not call Twilio"))["status"] == "failed"
    assert calling.validate_twilio(SID, TOKEN, "+15550001111") == []
    assert len(calling.validate_twilio("ACshort", "tok", "+1 555")) == 3


def test_provider_errors_never_echo_credentials(phone):
    def boom(req, timeout=None):
        raise ValueError(f"URL can't contain control characters. '/2010-04-01/Accounts/{SID}/Calls.json' {TOKEN}")
    res = _REAL_PLACE("hi", OWNER, opener=boom)
    assert res["status"] == "failed" and SID not in res["detail"] and TOKEN not in res["detail"]
    assert "[redacted]" in res["detail"]


def test_twilio_error_message_is_explained(phone):
    import urllib.error
    body = io.BytesIO(json.dumps({"code": 21219, "message": "The number is unverified."}).encode())

    def reject(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, body)
    res = _REAL_PLACE("hi", OWNER, opener=reject)
    assert "unverified" in res["detail"] and "21219" in res["detail"] and SID not in res["detail"]


def test_calls_need_a_completed_test_for_these_exact_details(phone):
    assert calling.status()["ready"]
    phone["authorized_numbers"] = ["+919999999999"]                       # changed number → re-verify
    st = calling.status()
    assert not st["ready"] and any("not verified" in r for r in st["reasons"])
    assert calling.schedule("x")["status"] == "refused"
    calling.test_call(placer=lambda t, to: {"ok": True, "sid": "CA7", "status": "queued", "detail": ""},
                      poller=lambda sid: {"status": "failed"}, sleep=lambda s: None)
    assert not calling.verified()                                         # a failed test doesn't verify
    calling.test_call(placer=lambda t, to: {"ok": True, "sid": "CA8", "status": "queued", "detail": ""},
                      poller=lambda sid: {"status": "completed", "duration": 5}, sleep=lambda s: None)
    assert calling.verified() and calling.status()["ready"]


def test_test_call_works_while_disabled_but_only_to_owner(phone):
    phone["enabled"] = False
    st = iter([{"status": "in-progress"}, {"status": "completed", "duration": 9}])
    res = calling.test_call(placer=lambda t, to: {"ok": to == OWNER, "sid": "CA5", "status": "queued", "detail": ""},
                            poller=lambda sid: next(st), sleep=lambda s: None)
    assert res["status"] == "completed" and res["answered"]
    phone["authorized_numbers"] = []
    assert calling.test_call(placer=lambda *a: pytest.fail("must not dial"))["status"] == "refused"


def test_reports_trigger_calls_only_when_enabled_for_them(phone):
    assert calling.wants_call_for_report("morning") and not calling.wants_call_for_report("eod")
    assert calling.wants_call_for({"severity": "CRITICAL"})
    assert not calling.wants_call_for({"severity": "HIGH", "type": "lead_qualified"})
    phone["call_for_high_priority_leads"] = True
    assert calling.wants_call_for({"severity": "HIGH", "type": "lead_qualified"})
    phone["enabled"] = False
    assert not calling.wants_call_for({"severity": "CRITICAL"}) and not calling.wants_call_for_report("morning")


def test_critical_event_schedules_one_call(phone):
    phone["critical_in_quiet_hours"] = True
    executive.emit("system", "disk", "CRITICAL", "Disk almost full")
    executive.route(datetime.now())
    executive.route(datetime.now())
    assert len([c for c in calling.calls() if c["status"] == "scheduled"]) == 1


def test_parse_when():
    now = datetime(2026, 10, 20, 10, 0)
    assert calling.parse_when("now", now) == now
    assert calling.parse_when("in 20 minutes", now) == now + timedelta(minutes=20)
    assert calling.parse_when("in 2 hours", now) == now + timedelta(hours=2)
    assert calling.parse_when("08:00", now) == datetime(2026, 10, 21, 8, 0)
    assert calling.parse_when("6:30 pm", now) == datetime(2026, 10, 20, 18, 30)
    with pytest.raises(ValueError):
        calling.parse_when("whenever", now)


def test_set_option_writes_nested_table_and_validates(tmp_path, monkeypatch):
    (tmp_path / "settings.toml").write_text("# top\n[executive_reporting]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(conf, "reload", lambda: None)
    calling.set_option("enabled", True)
    calling.set_option("authorized_numbers", [OWNER])
    import tomllib
    doc = tomllib.loads((tmp_path / "settings.toml").read_text(encoding="utf-8"))
    assert doc["executive_reporting"]["calling"] == {"enabled": True, "authorized_numbers": []}   # number → secrets
    assert tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))["calling"]["authorized_numbers"] == [OWNER]
    with pytest.raises(ValueError):
        calling.set_option("authorized_numbers", ["98765"])
    with pytest.raises(ValueError):
        calling.set_option("to_anyone", True)


# ------------------------------------------------------------------ permissions + tool surface

def test_call_tools_need_the_owner_and_never_run_unattended():
    for t in ("schedule_call", "call_me_now"):
        name = f"mcp__helios__{t}"
        assert permissions.classify(name, {}) == "ask"
        assert permissions.is_outbound_send(name)                         # background agents: blocked
        assert permissions.never_yolo(name, {})
    for t in ("exec_report", "pending_decisions", "lead_digest", "calling_status", "call_log", "cancel_calls",
              "calling_off", "snooze_reports", "exec_events", "ack_events"):
        assert permissions.classify(f"mcp__helios__{t}", {}) == "allow"


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("helios_mcp_server_exec", _ROOT / "mcp" / "helios_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_tool_takes_a_phone_number_or_can_enable_calls(srv):
    import inspect
    for name in ("schedule_call", "call_me_now", "cancel_calls", "calling_off"):
        params = set(inspect.signature(getattr(srv, name)).parameters)
        assert not params & {"to", "number", "phone", "phone_number"}
    assert not hasattr(srv, "set_calling_enabled") and not hasattr(srv, "emit_event")


def test_mcp_schedule_call_goes_through_the_rails(srv, phone):
    out = srv.schedule_call("in 30 minutes", "eod")
    assert "scheduled" in out and "one-way" in out
    phone["enabled"] = False
    assert srv.schedule_call("now").startswith("Not scheduled")


def test_mcp_reports_and_digest(srv, ex):
    _qualified(1, "Glow Salon", status="DRAFTED")
    assert "End-of-day report" in srv.exec_report("eod")
    assert "Glow Salon" in srv.lead_digest(1) and "contacted (verified) 0" in srv.lead_digest(1)
    assert "outreach draft" in srv.pending_decisions()
    assert "must be one of" in srv.exec_report("hourly")


# ------------------------------------------------------------------ dashboard

def test_dashboard_view_filters_and_actions(ex, phone):
    executive.emit("projects", "project_failing", "HIGH", "Project X is failing")
    executive.emit("research", "finding", "NORMAL", "New finding", approval=True)
    calling.schedule("a", due=_at(23, 59), now=datetime.now())
    v = dashboard.executive_view("today")
    assert v["unacked"] == 2 and len(v["events"]) == 2 and v["calling"]["ready"]
    assert {"pipeline", "decisions", "reports", "calls", "schedule"} <= set(v)
    assert [e["summary"] for e in dashboard.executive_view("today", cat="projects")["events"]] == ["Project X is failing"]
    assert [e["summary"] for e in dashboard.executive_view("today", cat="approvals")["events"]] == ["New finding"]
    assert dashboard.executive_view("yesterday")["events"] == []
    assert dashboard.exec_action({"action": "ack_all"})["count"] == 2
    assert dashboard.exec_action({"action": "snooze", "hours": 1})["until"]
    assert dashboard.exec_action({"action": "cancel_call"})["count"] == 1
    assert not dashboard.exec_action({"action": "call_number", "to": "+15550000000"})["ok"]
    assert not dashboard.exec_action({"action": "build", "kind": "hourly"})["ok"]
    assert "End-of-day" in dashboard.exec_action({"action": "build", "kind": "eod"})["text"]


# ------------------------------------------------------------------ Telegram voice briefings (no Twilio)

BOT = "123456789:" + "A" * 35
CHAT = "555000111"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def tg(monkeypatch, ex):
    """Briefings by Telegram voice message: bot configured + verified, voice rendering + sending faked."""
    c = {**calling._DEFAULTS, "enabled": True, "provider": "telegram"}
    monkeypatch.setattr(calling, "cfg", lambda: c)
    real = conf.provider_cfg
    monkeypatch.setattr(conf, "provider_cfg",
                        lambda name: {"token": BOT, "allowed_ids": [CHAT]} if name == "telegram" else
                        ({} if name in ("twilio", "calling") else real(name)))
    sent = []

    def send_voice(audio, caption="", **k):
        sent.append((audio, caption))
        return True, "42"
    from helios import channels, voice_note
    monkeypatch.setattr(channels, "telegram_send_voice", send_voice)
    monkeypatch.setattr(voice_note, "render", lambda text, **k: b"OggS" + text.encode()[:200])
    monkeypatch.setattr(calling, "_run_bg", lambda fn: fn())             # run the send inline
    db.init()
    db.set_state("calling_verified", calling._fingerprint())
    c["sent"] = sent
    return c


def test_telegram_briefing_needs_no_number_and_reports_honestly(tg):
    st = calling.status()
    assert st["ready"] and st["provider_name"] == "telegram" and st["numbers"] == ["your Telegram"]
    assert calling.schedule("End-of-day report", due=_at(19), now=_at(19))["status"] == "scheduled"
    calling.tick(_at(19, 1), placer=lambda *a: pytest.fail("Twilio must not be used"))
    c = calling.calls()[0]
    assert c["status"] == "completed" and c["answered"] == 0              # delivered ≠ listened
    assert "can't tell whether you played it" in c["detail"] and c["provider_sid"] == "tg:42"
    assert [x["status"] for x in c["log"]] == ["scheduled", "initiated", "completed"]
    assert tg["sent"][0][1].startswith("Helios · End-of-day report")
    calling.tick(_at(19, 5))
    assert len(tg["sent"]) == 1                                           # never sent twice


def test_telegram_failure_falls_back_and_critical_retries(tg, monkeypatch):
    from helios import channels
    monkeypatch.setattr(channels, "telegram_send_voice",
                        lambda *a, **k: (False, "Telegram rejected the voice message: blocked"))
    tg.update(critical_in_quiet_hours=True, max_attempts=2)
    calling.schedule("Disk almost full", critical=True, event_id="ev-t", due=_at(10), now=_at(10))
    calling.tick(_at(10, 1))
    assert calling.calls()[0]["status"] == "scheduled" and calling.calls()[0]["attempts"] == 1
    calling.tick(datetime.now() + timedelta(minutes=30))
    c = calling.calls()[0]
    assert c["status"] == "failed" and c["attempts"] == 2 and "blocked" in c["detail"]
    assert any(e["source"] == "calling" for e in executive.events())


def test_telegram_render_failure_is_reported(tg, monkeypatch):
    from helios import voice_note
    monkeypatch.setattr(voice_note, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model missing")))
    calling.schedule("x", due=_at(10), now=_at(10))
    calling.tick(_at(10, 1))
    assert calling.calls()[0]["status"] == "failed" and "model missing" in calling.calls()[0]["detail"]
    assert tg["sent"] == []


def test_telegram_test_verifies_and_changing_the_chat_needs_a_new_test(tg, monkeypatch):
    db.set_state("calling_verified", "")
    assert not calling.status()["ready"]
    res = calling.test_call()
    assert res["status"] == "completed" and calling.verified() and len(tg["sent"]) == 1
    monkeypatch.setattr(conf, "provider_cfg", lambda name: {"token": BOT, "allowed_ids": ["999"]} if name == "telegram" else {})
    assert not calling.verified()


def test_telegram_not_set_up(ex, monkeypatch):
    monkeypatch.setattr(calling, "cfg", lambda: {**calling._DEFAULTS, "enabled": True, "provider": "telegram"})
    monkeypatch.setattr(conf, "provider_cfg", lambda name: {})
    st = calling.status()
    assert not st["ready"] and any("helios telegram setup" in r for r in st["reasons"])
    assert calling.test_call()["status"] == "refused"


def test_send_voice_is_multipart_to_owner_only_and_hides_the_token(monkeypatch):
    monkeypatch.setattr(conf, "provider_cfg", lambda name: {"token": BOT, "allowed_ids": [CHAT]} if name == "telegram" else {})
    seen = []

    def opener(req, timeout=None):
        seen.append(req)
        return _Resp(json.dumps({"ok": True, "result": {"message_id": 7}}).encode())
    ok, mid = _REAL_SEND_VOICE(b"OggS-audio", "Helios · test", opener=opener)
    assert ok and mid == "7"
    req = seen[0]
    assert req.full_url.endswith("/sendVoice") and b"OggS-audio" in req.data
    assert f'name="chat_id"\r\n\r\n{CHAT}'.encode() in req.data and "multipart/form-data" in req.headers["Content-type"]

    def boom(req, timeout=None):
        raise OSError(f"failed {req.full_url}")
    ok, why = _REAL_SEND_VOICE(b"x", opener=boom)
    assert not ok and BOT not in why and "[redacted]" in why


def test_find_chat_takes_the_newest_private_chat():
    upd = {"ok": True, "result": [{"message": {"chat": {"id": 1, "type": "private", "first_name": "Old"}}},
                                  {"message": {"chat": {"id": -5, "type": "group"}}},
                                  {"message": {"chat": {"id": 77, "type": "private", "first_name": "Syed"}}}]}
    assert _REAL_FIND_CHAT(BOT, opener=lambda r, timeout=None: _Resp(json.dumps(upd).encode())) == ("77", "Syed")
    with pytest.raises(ValueError, match="press Start"):
        _REAL_FIND_CHAT(BOT, opener=lambda r, timeout=None: _Resp(b'{"ok": true, "result": []}'))
    with pytest.raises(ValueError, match="refused the token"):
        _REAL_FIND_CHAT(BOT, opener=lambda r, timeout=None: _Resp(b'{"ok": false, "description": "Unauthorized"}'))


def test_voice_note_renders_ogg_opus_from_scrubbed_text():
    import numpy as np
    import soundfile as sf
    from helios import voice_note
    said = []

    def synth(s):
        said.append(s)
        return np.zeros(2400, dtype=np.float32)
    b = voice_note.render("Good morning, sir. Call +91 98765 43210 or mail a@b.com now!", synth=synth)
    info = sf.info(io.BytesIO(b))
    assert info.format == "OGG" and info.subtype == "OPUS" and info.samplerate == 24000
    assert len(said) == 2 and "98765" not in " ".join(said) and "a@b.com" not in " ".join(said)
    with pytest.raises(ValueError):
        voice_note.render("   ", synth=synth)


def test_phone_numbers_live_in_secrets_not_committed_settings(tmp_path, monkeypatch):
    (tmp_path / "settings.toml").write_text("[executive_reporting.calling]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(conf, "reload", lambda: None)
    calling.set_option("authorized_numbers", [OWNER])
    assert OWNER not in (tmp_path / "settings.toml").read_text(encoding="utf-8")
    import tomllib
    sec = tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))
    assert sec["calling"]["authorized_numbers"] == [OWNER]
    monkeypatch.setattr(conf, "provider_cfg", lambda name: sec.get(name, {}))
    monkeypatch.setattr(calling, "cfg", lambda: dict(calling._DEFAULTS))
    assert calling.numbers() == [OWNER]


def test_set_secret_writes_lists_as_string_arrays(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    conf.set_secret("telegram", "allowed_ids", ["123"])
    conf.set_secret("telegram", "token", "t")
    import tomllib
    assert tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))["telegram"] == {"token": "t", "allowed_ids": ["123"]}
