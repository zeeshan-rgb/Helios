"""Gmail: replies are drafted only for verified business contacts, bulk / no-reply / forged mail is
skipped, the email is untrusted data for a tool-less AI call, nothing is ever sent by the
background check, a send needs the user's yes (always asks — even in YOLO; background agents are
blocked) and is refused for non-business recipients, and sign-in is the user's own browser OAuth
with the token written only to secrets.toml. All Gmail/Google calls are faked — no network."""

from __future__ import annotations

import base64
import importlib.util
import json
import threading
import urllib.request
from email import message_from_bytes
from pathlib import Path

import pytest

from helios import conf, gmail, jobs, leads, permissions

_ROOT = Path(__file__).resolve().parent.parent
ME = "zeeshan@maqsusi.example"
PASS = "mx.google.com; dkim=pass header.i=@acme.com; spf=pass"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _msg(mid, frm, subject="Website update", body="Hi, can you add a contact page? Thanks",
         auth=PASS, extra=None, thread=None):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": ME},
               {"name": "Subject", "value": subject}, {"name": "Date", "value": "Wed, 30 Sep 2026"},
               {"name": "Message-ID", "value": f"<{mid}@mail.example>"},
               {"name": "Authentication-Results", "value": auth}]
    headers += [{"name": k, "value": v} for k, v in (extra or {}).items()]
    return {"id": mid, "threadId": thread or f"t{mid}", "labelIds": ["UNREAD", "INBOX"], "snippet": body[:40],
            "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body)}}]}}


@pytest.fixture
def g(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path)
    c = {"enabled": True, "client_id": "1-x.apps.googleusercontent.com", "client_secret": "s",
         "refresh_token": "r", "business_contacts": ["@acme.com"], "signature": "Best, Z"}
    monkeypatch.setattr(gmail, "cfg", lambda: c)
    monkeypatch.setattr(leads, "all_leads", lambda *a, **k: [])
    msgs = {m["id"]: m for m in (
        _msg("m0000001", "Ann <ann@acme.com>"),
        _msg("m0000002", "Spam <deals@shop.example>"),
        _msg("m0000003", "Acme News <news@acme.com>", extra={"List-Unsubscribe": "<mailto:u@acme.com>"}),
        _msg("m0000004", "Fake <boss@acme.com>", auth="mx.google.com; dkim=fail; spf=softfail"),
        _msg("m0000005", "no-reply@acme.com"),
    )}
    calls, drafts = [], {}

    def api(method, path, params=None, body=None):
        calls.append((method, path, body))
        if path == "/profile":
            return {"emailAddress": ME}
        if path == "/messages" and method == "GET":
            return {"messages": [{"id": k} for k in msgs]}
        if path.startswith("/messages/"):
            return msgs[path.split("/")[2]]
        if path.startswith("/threads/"):
            return {"messages": [m for m in msgs.values() if m["threadId"] == path.split("/")[2]]}
        if path == "/drafts" and method == "POST":
            did = f"d{len(drafts) + 1}"
            raw = message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
            drafts[did] = raw
            return {"id": did}
        if path.startswith("/drafts/") and method == "GET":
            raw = drafts[path.split("/")[2]]
            return {"message": {"payload": {"headers": [{"name": k, "value": v} for k, v in raw.items()]}}}
        if path == "/drafts/send":
            return {"id": "sent1"}
        raise AssertionError(f"unexpected Gmail call {method} {path}")
    monkeypatch.setattr(gmail, "_api", api)
    return type("G", (), {"cfg": c, "calls": calls, "drafts": drafts, "msgs": msgs, "tmp": tmp_path})


def fake_llm(reply="Hi Ann, sure — I can add a contact page. [confirm: deadline]\n\nBest, Z"):
    seen = {}

    def call(thread, subject):
        seen.update(thread=thread, subject=subject)
        return {"reply": reply, "needs_you": ["deadline"], "summary": "wants a contact page"}
    call.seen = seen
    return call


# ------------------------------------------------------------------ business contacts

def test_business_contacts(g, monkeypatch):
    assert gmail.is_business("Ann <ann@acme.com>") and not gmail.is_business("x@other.example")
    assert gmail.add_business("Client@Bakery.example") == "client@bakery.example"
    assert gmail.is_business("client@bakery.example")
    for bad in ("@gmail.com", "nonsense", "a@b"):
        with pytest.raises(ValueError):
            gmail.add_business(bad)
    monkeypatch.setattr(leads, "all_leads", lambda *a, **k: [
        {"status": "won", "contact": "email bob@shop.example"},
        {"status": "new", "contact": "new@lead.example"}])
    e = gmail.business_entries()
    assert e["bob@shop.example"] == "lead" and "new@lead.example" not in e
    assert e["@acme.com"] == "settings" and e["client@bakery.example"] == "you"
    assert gmail.remove_business("client@bakery.example") and not gmail.is_business("client@bakery.example")


def test_reply_blockers(g):
    why = {mid: gmail.reply_blocker(gmail.read(mid), ME) for mid in g.msgs}
    assert why["m0000001"] == ""
    assert why["m0000002"] == "not a business contact"
    assert why["m0000003"] == "bulk / automated mail"
    assert "not verified" in why["m0000004"]
    assert why["m0000005"] == "no-reply sender"


# ------------------------------------------------------------------ drafting

def test_draft_reply_threads_correctly_and_never_sends(g):
    llm = fake_llm("Sure! key sk-abcdefghijklmnopqrstuvwx\n\nBest, Z")
    rec = gmail.draft_reply("m0000001", llm=llm)
    raw = g.drafts[rec["draft_id"]]
    assert raw["To"] == "Ann <ann@acme.com>" and raw["Subject"] == "Re: Website update"
    assert raw["In-Reply-To"] == "<m0000001@mail.example>"
    post = next(b for m, p, b in g.calls if p == "/drafts")
    assert post["message"]["threadId"] == "tm0000001"
    assert "sk-abc" not in rec["reply"] and "[removed]" in rec["reply"]
    assert "contact page" in llm.seen["thread"]                         # thread passed as data
    assert not any(p == "/drafts/send" for _, p, _ in g.calls)
    assert gmail.pending()[0]["needs_you"] == ["deadline"]
    with pytest.raises(gmail.GmailError, match="not a business contact"):
        gmail.draft_reply("m0000002", "hello")


def test_reply_rules_treat_email_as_data():
    assert "untrusted DATA" in gmail.REPLY_RULES and "NO tools" in gmail.REPLY_RULES
    src = (_ROOT / "helios" / "gmail.py").read_text(encoding="utf-8")
    assert "tools=False" in src and "allow_only" not in src


def test_check_drafts_business_mail_only(g, monkeypatch):
    toasts = []
    monkeypatch.setattr("helios.notify.toast", lambda t, m, **k: toasts.append(m))
    out = gmail.check(llm=fake_llm())
    assert [d["to"] for d in out["drafted"]] == ["ann@acme.com"] and out["skipped"] == 4
    assert not any(p in ("/drafts/send",) or m == "DELETE" for m, p, _ in g.calls)
    assert toasts and "Ann" in toasts[0]
    n = len(g.calls)
    again = gmail.check(llm=fake_llm())                                 # handled ones aren't redone
    assert not again["drafted"] and not any(p == "/drafts" for _, p, _ in g.calls[n:])


def test_no_reply_needed_is_skipped(g):
    out = gmail.check(llm=fake_llm(reply=""))
    assert not out["drafted"] and not gmail.pending()


# ------------------------------------------------------------------ sending

def test_send_requires_business_recipients(g):
    gmail.draft_reply("m0000001", llm=fake_llm())
    r = gmail.send_draft("1")
    assert r["recipients"] == ["ann@acme.com"] and gmail.pending() == []
    assert gmail.pending(include_all=True)[0]["status"] == "sent"
    gmail.draft_reply("m0000001", "second reply")
    did = gmail.pending()[0]["draft_id"]
    g.drafts[did]["Cc"] = "outsider@else.example"                       # you (or someone) edited it
    with pytest.raises(gmail.GmailError, match="outsider@else.example"):
        gmail.send_draft("1")
    with pytest.raises(gmail.GmailError, match="no pending"):
        gmail.send_draft("9")


def test_send_always_asks_and_background_agents_cant(tmp_path, monkeypatch):
    assert permissions.classify("mcp__helios__gmail_send_draft", {}) == "ask"
    assert permissions.classify("mcp__helios__gmail_add_business_contact", {}) == "ask"
    for t in ("gmail_search", "gmail_read", "gmail_pending_replies", "gmail_draft_reply", "gmail_business_contacts"):
        assert permissions.classify(f"mcp__helios__{t}", {}) == "allow"
    assert permissions.is_outbound_send("mcp__helios__gmail_send_draft")
    assert not permissions.is_outbound_send("mcp__helios__gmail_draft_reply")
    spec = importlib.util.spec_from_file_location("pretooluse_gmail", _ROOT / "hooks" / "pretooluse.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(hook.conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(hook.conf, "ABORT_FLAG", tmp_path / "abort.flag")
    asked = []

    def ask(req, timeout=None):
        asked.append(json.loads(req.data)["tool"])
        raise OSError("no app in tests")
    monkeypatch.setattr(hook.urllib.request, "urlopen", ask)
    (tmp_path / "yolo.flag").write_text("on")
    assert hook.decide("mcp__helios__gmail_send_draft", {"draft": "1"})[0] == "deny"
    assert asked == ["mcp__helios__gmail_send_draft"]                   # went to the prompt
    assert hook.decide("mcp__helios__gmail_draft_reply", {})[0] == "allow"
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    d, why = hook.decide("mcp__helios__gmail_send_draft", {"draft": "1"})
    assert d == "deny" and "background agent" in why


def test_job_type_never_sends(g, monkeypatch):
    assert "gmail_replies" in jobs.TYPES
    monkeypatch.setattr(gmail, "_call_llm", fake_llm())
    status, summary = jobs.TYPES["gmail_replies"][1]({})
    assert status == "ok" and "1 reply draft" in summary
    assert not any(p == "/drafts/send" for _, p, _ in g.calls)
    g.cfg["refresh_token"] = ""
    assert jobs.TYPES["gmail_replies"][1]({})[0] == "skipped"


def test_public_mcp_has_no_gmail():
    src = (_ROOT / "mcp" / "helios_public_server.py").read_text(encoding="utf-8")
    assert "gmail" not in src.lower()


# ------------------------------------------------------------------ setup / sign-in

def test_setup_client(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    f = tmp_path / "client.json"
    f.write_text(json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com",
                                           "client_secret": "GOCSPX-x"}}), encoding="utf-8")
    gmail.setup_client(str(f))
    import tomllib
    sec = tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))["gmail"]
    assert sec == {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-x"}
    f.write_text(json.dumps({"web": {"client_id": "1-a.apps.googleusercontent.com", "client_secret": "y"}}))
    with pytest.raises(ValueError, match="Desktop app"):
        gmail.setup_client(str(f))


def test_login_uses_loopback_pkce_and_saves_token(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    c = {"client_id": "1-x.apps.googleusercontent.com", "client_secret": "s"}
    monkeypatch.setattr(gmail, "cfg", lambda: c)
    posted = {}

    def post(url, fields):
        posted.update(fields, url=url)
        return {"access_token": "at", "refresh_token": "rt-123", "expires_in": 3600}
    monkeypatch.setattr(gmail, "_post", post)
    monkeypatch.setattr(gmail, "profile", lambda: {"emailAddress": ME})
    seen = {}

    def browser(url):                                                    # stands in for the user
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        seen.update({k: v[0] for k, v in q.items()})
        redirect = f"{q['redirect_uri'][0]}/?state=wrong&code=evil"
        good = f"{q['redirect_uri'][0]}/?state={q['state'][0]}&code=good-code"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def hit():
            opener.open(redirect, timeout=5).read()                      # forged callback ignored
            opener.open(good, timeout=5).read()
        threading.Thread(target=hit, daemon=True).start()
    assert gmail.login(open_browser=browser, timeout=20) == ME
    assert seen["scope"] == gmail.SCOPE and seen["code_challenge_method"] == "S256"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:") and seen["access_type"] == "offline"
    assert posted["code"] == "good-code" and len(posted["code_verifier"]) > 40
    import tomllib
    assert tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))["gmail"]["refresh_token"] == "rt-123"
    gmail._access.clear()


def test_set_secret_preserves_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    (tmp_path / "secrets.toml").write_text('[telegram]\ntoken = "t"\n\n[gmail]\nrefresh_token = "old"\n', encoding="utf-8")
    conf.set_secret("gmail", "refresh_token", 'new"quote')
    conf.set_secret("gmail", "client_id", "cid")
    import tomllib
    d = tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))
    assert d == {"telegram": {"token": "t"}, "gmail": {"refresh_token": 'new"quote', "client_id": "cid"}}
    conf.set_secret("gmail", "refresh_token", None)
    assert "refresh_token" not in tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))["gmail"]
    with pytest.raises(ValueError):
        conf.set_secret("gm]ail", "x", "y")
