"""SMS notifications (docs/HELIOS_SMS_INTEGRATION.md). All providers are mocked — conftest also blocks
every real HTTP call from helios.sms — so nothing here is evidence of real SMS delivery.
"""

from __future__ import annotations

import importlib.util
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helios import executive, permissions, sms
from helios.prospecting import geo, store

_ROOT = Path(__file__).resolve().parent.parent
_real_http = sms._http        # captured at import, before conftest's no-network stub (fed only fake openers)
OWNER = "+919876543210"
AMMI = "+919812345678"


class FakeProv(sms.Provider):
    name = "fake"

    def __init__(self, results=None, statuses=None, ready=True, dlt=False):
        self.results = list(results or [])
        self.statuses = dict(statuses or {})
        self.sent = []
        self.ready, self.dlt = ready, dlt

    def validate_configuration(self):
        return (True, "fake") if self.ready else (False, "fake provider isn't set up")

    def requires_dlt(self, to):
        return self.dlt

    def send_sms(self, to, body, *, template_id=""):
        self.sent.append((to, body, template_id))
        if self.results:
            return self.results.pop(0)
        return {"ok": True, "id": f"m{len(self.sent)}", "raw_status": "queued", "detail": "", "permanent": False}

    def get_delivery_status(self, message_id):
        return self.statuses.get(message_id, {"state": "SENT_TO_PROVIDER", "raw": "sent", "detail": ""})


@pytest.fixture
def env(monkeypatch, tmp_path):
    c = {**sms._DEFAULTS, "enabled": True, "templates": {k: {} for k in sms.TEMPLATES}}
    monkeypatch.setattr(sms, "cfg", lambda: c)
    monkeypatch.setattr(sms, "provider", lambda opener=None: FakeProv())
    monkeypatch.setattr(geo.conf, "DATA_DIR", tmp_path)
    store._inited.clear()
    return c


def owner(**kw):
    return sms.add_recipient("Me", OWNER, owner=True, **kw)


def ammi(consent="granted", **kw):
    return sms.add_recipient("Ammi", AMMI, consent=consent, categories=kw.pop("categories", ["critical", "night_mode"]), **kw)


def _at(h, m=0):
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)


def status_of(mid):
    return next(m for m in sms.messages(500) if m["id"] == mid)


# ------------------------------------------------------------------ numbers + recipients

@pytest.mark.parametrize("raw,ok", [("8309419033", "+918309419033"), ("+91 98765-43210", "+919876543210"),
                                    ("0091 98765 43210", "+919876543210"), ("+1 (415) 555-0100", "+14155550100")])
def test_normalize(raw, ok):
    assert sms.normalize(raw) == ok


@pytest.mark.parametrize("bad", ["12345", "hello", "ammi@gmail.com", "+0123", "5555555555"])
def test_invalid_numbers_rejected(bad):
    with pytest.raises(sms.SMSError):
        sms.normalize(bad)


def test_recipient_without_any_email_is_fine(env):
    r = ammi()
    assert r["phone"] == AMMI and "email" not in r and r["consent"] == "granted"


def test_owner_defaults_to_critical_only(env):
    assert owner()["categories"] == ["critical"]


def test_business_leads_can_never_be_recipients(env):
    el = {"type": "node", "id": 7, "lat": 17.4, "lon": 78.4, "tags": {"name": "Glow Salon", "shop": "hairdresser"}}
    p, _ = store.upsert(geo.to_business(el, "Testpur"))
    store.update(p["id"], phone="+91 90000 11122")
    with pytest.raises(sms.SMSError, match="lead"):
        sms.add_recipient("Glow", "+919000011122", consent="granted")


# ------------------------------------------------------------------ consent, opt-out, approval

def test_no_consent_no_sms(env):
    r = ammi(consent="pending")
    res = sms.enqueue(r, "critical", "Disk failing")
    assert res["status"] == "SUPPRESSED" and "consent" in res["reason"]


def test_unknown_recipient_refused(env):
    with pytest.raises(sms.SMSError, match="only people you added"):
        sms.enqueue("Stranger", "critical", "x")


def test_category_must_be_enabled(env):
    r = ammi(categories=["night_mode"])
    assert sms.enqueue(r, "critical", "x")["status"] == "SUPPRESSED"


def test_other_people_need_approval_per_message(env):
    r = ammi()
    res = sms.enqueue(r, "critical", "Disk failing", now=_at(11))
    assert res["status"] == "APPROVAL_REQUIRED"
    assert sms.approve(res["id"])["status"] == "QUEUED"


def test_opt_out_wins_and_cancels_pending(env):
    r = ammi(require_approval=False)
    res = sms.enqueue(r, "critical", "x", now=_at(11))
    sms.opt_out(r["id"])
    assert status_of(res["id"])["status"] == "CANCELLED"
    assert sms.enqueue(r["id"], "critical", "y")["status"] == "SUPPRESSED"
    sms.remove_recipient(r["id"])
    with pytest.raises(sms.SMSError, match="suppression"):
        ammi()


# ------------------------------------------------------------------ sending, states, retries

def test_send_and_delivery_status(env):
    owner()
    res = sms.enqueue("me", "critical", "Antigravity execution failed", severity="CRITICAL", now=_at(11))
    prov = FakeProv(statuses={"m1": {"state": "DELIVERED", "raw": "delivered", "detail": ""}})
    sms.tick(_at(11, 1), prov=prov)
    m = status_of(res["id"])
    assert m["status"] == "SENT_TO_PROVIDER" and "not yet confirmed" in m["detail"]     # accepted ≠ delivered
    assert prov.sent[0][0] == OWNER and prov.sent[0][1] == "HELIOS ALERT: Antigravity execution failed. Open Helios for details."
    sms.tick(_at(11, 2), prov=prov)
    assert status_of(res["id"])["status"] == "DELIVERED"


def test_provider_failure_delivery(env):
    owner()
    res = sms.enqueue("me", "critical", "x", severity="CRITICAL", now=_at(11))
    prov = FakeProv(statuses={"m1": {"state": "FAILED", "raw": "undelivered", "detail": "carrier rejected"}})
    sms.tick(_at(11, 1), prov=prov)
    sms.tick(_at(11, 2), prov=prov)
    assert status_of(res["id"])["status"] == "FAILED"
    assert any(e["source"] == "sms" for e in executive.events())                       # notification kept


def test_timeouts_retry_then_exhaust(env):
    owner()
    res = sms.enqueue("me", "critical", "x", severity="CRITICAL", now=_at(11))
    temp = {"ok": False, "id": "", "raw_status": "", "detail": "TimeoutError: timed out", "permanent": False}
    prov = FakeProv(results=[temp, temp, temp])
    sms.tick(_at(11, 0), prov=prov)
    assert status_of(res["id"])["status"] == "RETRY_SCHEDULED"
    sms.tick(_at(11, 5), prov=prov)
    assert status_of(res["id"])["status"] == "RETRY_SCHEDULED"
    sms.tick(_at(12, 0), prov=prov)
    m = status_of(res["id"])
    assert m["status"] == "FAILED" and m["attempts"] == 3 and "gave up" in m["detail"]


def test_permanent_error_no_retry(env):
    owner()
    res = sms.enqueue("me", "critical", "x", severity="CRITICAL", now=_at(11))
    sms.tick(_at(11), prov=FakeProv(results=[{"ok": False, "id": "", "raw_status": "", "detail": "HTTP 400: bad number",
                                              "permanent": True}]))
    assert status_of(res["id"])["status"] == "FAILED" and status_of(res["id"])["attempts"] == 1


def test_missing_credentials(env):
    owner()
    res = sms.enqueue("me", "critical", "x", severity="CRITICAL", now=_at(11))
    sms.tick(_at(11), prov=FakeProv(ready=False))
    m = status_of(res["id"])
    assert m["status"] == "FAILED" and "isn't set up" in m["detail"]


def test_real_providers_report_missing_config(monkeypatch):
    from helios import conf
    monkeypatch.setattr(conf, "provider_cfg", lambda name: {})
    for cls in (sms.PhoneGatewaySMS, sms.ExotelSMS, sms.TwilioSMS):
        ok, why = cls().validate_configuration()
        assert not ok and "isn't set up" in why


# ------------------------------------------------------------------ limits, quiet hours, batching, dedupe

def test_quiet_hours_defer_non_critical(env):
    owner(categories=["critical", "reports"])
    res = sms.enqueue("me", "reports", "end-of-day report", now=_at(23, 30))
    assert res["status"] == "QUEUED" and res["due"].endswith("08:00:00")


def test_critical_in_quiet_hours_only_if_allowed(env):
    owner()
    assert sms.enqueue("me", "critical", "x", severity="CRITICAL", now=_at(23, 30))["due"].endswith("08:00:00")
    env["critical_in_quiet_hours"] = True
    assert not sms.enqueue("me", "critical", "y", severity="CRITICAL", now=_at(23, 30))["due"].endswith("08:00:00")


def test_daily_cap_and_min_gap(env):
    env.update(max_per_day=1, min_gap_minutes=5)
    owner(categories=["critical", "reports"])
    a = sms.enqueue("me", "reports", "r1", now=_at(11))
    b = sms.enqueue("me", "reports", "r2", now=_at(11))
    prov = FakeProv()
    sms.tick(_at(11, 1), prov=prov)
    assert status_of(a["id"])["status"] == "SENT_TO_PROVIDER"
    assert status_of(b["id"])["status"] == "SUPPRESSED" and "daily limit" in status_of(b["id"])["detail"]
    c_ = sms.enqueue("me", "critical", "c", severity="CRITICAL", now=_at(11))      # critical ignores the cap
    sms.tick(_at(11, 2), prov=prov)
    assert status_of(c_["id"])["status"] == "SENT_TO_PROVIDER"


def test_routine_updates_are_batched(env):
    owner(categories=["task_complete"])
    ids = [sms.enqueue("me", "task_complete", f"Job {i} finished", now=_at(11))["id"] for i in range(3)]
    prov = FakeProv()
    sms.tick(_at(11, 5), prov=prov)
    assert prov.sent == []                                                          # waits for the batch window
    sms.tick(_at(11, 40), prov=prov)
    assert len(prov.sent) == 1 and prov.sent[0][1] == "HELIOS: 3 updates. Open Helios for details."
    assert [status_of(i)["status"] for i in ids].count("CANCELLED") == 2


def test_duplicate_events_send_once(env):
    owner()
    ev = {"id": "ev-1", "type": "disk", "source": "system", "severity": "CRITICAL", "summary": "Disk almost full",
          "dedupe_key": "disk:C"}
    first = sms.notify_event(ev)
    again = sms.notify_event(ev)
    assert first[0]["status"] == "QUEUED" and again[0]["status"] == "DUPLICATE"


def test_fallback_when_telegram_did_not_reach_you(env):
    owner()                                                                         # owner has critical only
    ev = {"id": "ev-2", "type": "job_failing", "source": "jobs", "severity": "HIGH", "summary": "Job x failed twice",
          "dedupe_key": "job:x"}
    assert sms.notify_event(ev, other_channels_failed=False) == []
    out = sms.notify_event(ev, other_channels_failed=True)
    assert out and out[0]["status"] == "QUEUED"


def test_executive_routes_critical_events_to_sms(env, monkeypatch):
    monkeypatch.setattr(executive, "cfg", lambda: dict(executive._DEFAULTS))
    owner()
    executive.emit("system", "disk", "CRITICAL", "Disk almost full")
    executive.route(_at(11))
    assert any(m["category"] == "critical" and m["status"] == "QUEUED" for m in sms.messages())


# ------------------------------------------------------------------ text, DLT, safety

def test_sensitive_data_is_scrubbed():
    k, body, _ = sms.render("critical", "Key sk-ant-abcdefghijklmnopqrstuvwxyz123456 leaked; mail a@b.com or +91 98765 43210 🚨")
    assert "sk-ant" not in body and "a@b.com" not in body and "98765" not in body and "🚨" not in body
    assert body.isascii() and len(body) <= 300


def test_indian_numbers_need_a_registered_dlt_template(env, monkeypatch):
    monkeypatch.setattr(sms, "provider", lambda opener=None: FakeProv(dlt=True))
    owner()
    res = sms.enqueue("me", "critical", "x", severity="CRITICAL")
    assert res["status"] == "SUPPRESSED" and "DLT template not registered" in res["reason"]
    env["templates"]["alert"] = {"dlt_template_id": "1107123456789"}
    assert sms.enqueue("me", "critical", "y", severity="CRITICAL")["status"] == "QUEUED"


def test_emergency_off_cancels_everything_and_stops_sending(env, monkeypatch):
    monkeypatch.setattr(sms, "set_option", lambda k, v: env.__setitem__(k, v))
    owner()
    res = sms.enqueue("me", "critical", "x", severity="CRITICAL", now=_at(11))
    assert sms.emergency_off() == 1
    assert status_of(res["id"])["status"] == "CANCELLED" and env["enabled"] is False
    prov = FakeProv()
    assert sms.tick(_at(11, 5), prov=prov) == [] and prov.sent == []
    assert sms.enqueue("me", "critical", "y")["status"] == "SUPPRESSED"


def test_test_message_works_while_off(env):
    env["enabled"] = False
    owner()
    res = sms.send_test("me")
    assert res["status"] == "QUEUED"
    prov = FakeProv()
    sms.tick(only=res["id"], prov=prov)
    assert prov.sent[0][1] == "HELIOS: test message. SMS alerts are working."


def test_send_sms_tool_cannot_reach_strangers_and_needs_the_owner():
    name = "mcp__helios__send_sms"
    assert permissions.classify(name, {}) == "ask"
    assert permissions.is_outbound_send(name) and permissions.never_yolo(name, {})
    for t in ("sms_status", "sms_log", "sms_off"):
        assert permissions.classify(f"mcp__helios__{t}", {}) == "allow"


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("helios_mcp_server_sms", _ROOT / "mcp" / "helios_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mcp_send_sms_has_no_number_parameter(srv, env):
    import inspect
    assert set(inspect.signature(srv.send_sms).parameters) == {"recipient", "message"}
    assert "only people you added" in srv.send_sms("+919999999999", "hi")
    owner()
    assert srv.send_sms("me", "Running late").startswith("Queued SMS")


# ------------------------------------------------------------------ real provider adapters (mocked HTTP)

class _R(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_phone_gateway_adapter(monkeypatch):
    from helios import conf
    monkeypatch.setattr(conf, "provider_cfg", lambda n: {"url": "http://192.168.1.23:8080", "username": "sms",
                                                         "password": "pw-secret"} if n == "sms_gateway" else {})
    seen = []

    def opener(req, timeout=None):
        seen.append(req)
        if req.get_method() == "POST":
            return _R(json.dumps({"id": "abc", "state": "Pending"}).encode())
        return _R(json.dumps({"id": "abc", "state": "Delivered", "recipients": [{"phoneNumber": OWNER, "state": "Delivered"}]}).encode())
    monkeypatch.setattr(sms, "_http", _real_http)
    p = sms.PhoneGatewaySMS(opener)
    r = p.send_sms(OWNER, "hello")
    body = json.loads(seen[0].data)
    assert r["ok"] and r["id"] == "abc" and seen[0].full_url == "http://192.168.1.23:8080/message"
    assert body == {"textMessage": {"text": "hello"}, "phoneNumbers": [OWNER], "withDeliveryReport": True}
    assert p.get_delivery_status("abc")["state"] == "DELIVERED"
    assert not p.requires_dlt(OWNER)                                                # your own SIM: no DLT


def test_exotel_adapter_sends_dlt_ids_and_maps_status_honestly(monkeypatch):
    from helios import conf
    monkeypatch.setattr(conf, "provider_cfg", lambda n: {"api_key": "k", "api_token": "tok-secret", "account_sid": "acct",
                                                         "sender_id": "HELIOS", "dlt_entity_id": "1101"} if n == "exotel" else {})
    monkeypatch.setattr(sms, "_http", _real_http)
    seen, replies = [], [{"SMSMessage": {"Sid": "S1", "Status": "queued"}},
                         {"SMSMessage": {"Sid": "S1", "Status": "sent", "DetailedStatus": "SUBMITTED_TO_OPERATOR"}},
                         {"SMSMessage": {"Sid": "S1", "Status": "sent", "DetailedStatus": "DELIVERED_TO_HANDSET"}}]

    def opener(req, timeout=None):
        seen.append(req)
        return _R(json.dumps(replies.pop(0)).encode())
    p = sms.ExotelSMS(opener)
    r = p.send_sms(OWNER, "HELIOS ALERT: x. Open Helios for details.", template_id="1107")
    from urllib.parse import parse_qs
    form = parse_qs(seen[0].data.decode())
    assert r["ok"] and form["DltEntityId"] == ["1101"] and form["DltTemplateId"] == ["1107"] and form["From"] == ["HELIOS"]
    assert seen[0].full_url == "https://api.exotel.com/v1/Accounts/acct/Sms/send.json"
    assert p.get_delivery_status("S1")["state"] == "SENT_TO_PROVIDER"              # "sent" alone isn't delivery
    assert p.get_delivery_status("S1")["state"] == "DELIVERED"


def test_provider_errors_never_leak_credentials(monkeypatch):
    from helios import conf
    monkeypatch.setattr(conf, "provider_cfg", lambda n: {"url": "http://10.0.0.5:8080", "username": "u",
                                                         "password": "pw-secret"} if n == "sms_gateway" else {})
    monkeypatch.setattr(sms, "_http", lambda *a, **k: (0, {}, "ConnectionRefused pw-secret"))
    r = sms.PhoneGatewaySMS().send_sms(OWNER, "x")
    assert not r["ok"] and "pw-secret" not in r["detail"] and "same Wi-Fi" in r["detail"]


def test_dashboard_masks_numbers(env):
    from helios import dashboard
    owner()
    ammi()
    v = dashboard.sms_view()
    assert all("•" in r["phone"] and OWNER not in r["phone"] for r in v["recipients"])
    assert OWNER not in json.dumps(v) and AMMI not in json.dumps(v)
