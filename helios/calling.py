"""Personal phone briefings — Helios calls ONLY the owner's configured number(s).

Provider: Twilio's documented REST API. A call is created with inline TwiML (<Say>), so no public
web server is needed; its status is then read back from the Call resource. This makes the call
ONE-WAY: Helios speaks a short, evidence-based summary and points to the dashboard for details.
Live two-way conversation would need Twilio <Gather> webhooks on a public URL, which a home PC
doesn't have — so it is not offered, and the call never pretends otherwise.

Rules enforced here (not in prompts):
* the number must be in [executive_reporting.calling] authorized_numbers (E.164) — never a lead,
  client or anyone else; there is no parameter that takes an arbitrary number from outside
* off until enabled AND a provider is configured; quiet hours (critical calls only if allowed);
  maximum calls per day; minimum gap between non-critical calls; one call per event (no repeats for
  the same unresolved event); retry limit; cancel; every state change logged
* states: scheduled → initiated → ringing → answered → completed | failed | no-answer | busy |
  canceled | refused — a call is never reported as made unless the provider says so
* spoken text is scrubbed: no secrets, emails or phone numbers

Credentials live in config/secrets.toml [twilio] (account_sid, auth_token, from_number), written by
`helios calling setup` (hidden prompt) — the AI can't read that file.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from . import conf, db

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_DEFAULTS = {"enabled": False, "provider": "twilio", "authorized_numbers": [], "call_for_morning_briefing": True,
             "call_for_end_of_day": False, "call_when_night_mode_finishes": False, "call_for_weekly_review": False,
             "call_for_critical_events": True, "call_for_high_priority_leads": False, "maximum_calls_per_day": 3,
             "min_minutes_between_calls": 60, "quiet_hours_start": "22:00", "quiet_hours_end": "08:00",
             "critical_in_quiet_hours": False, "max_attempts": 2, "retry_minutes": 10, "voice": "Polly.Brian"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created TEXT NOT NULL, due TEXT NOT NULL, updated TEXT NOT NULL,
  reason TEXT NOT NULL, report_kind TEXT NOT NULL DEFAULT '', event_id TEXT NOT NULL DEFAULT '',
  critical INTEGER NOT NULL DEFAULT 0, to_masked TEXT NOT NULL DEFAULT '', to_index INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, provider_sid TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
  answered INTEGER NOT NULL DEFAULT 0, duration INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '', log TEXT NOT NULL DEFAULT '[]'
);
"""
_inited: set[str] = set()
_ACTIVE = ("initiated", "queued", "ringing", "answered")
_FINAL = ("completed", "failed", "no-answer", "busy", "canceled", "refused")


def _init() -> None:
    if str(db.DB_PATH) in _inited:
        return
    db.init()
    with db._conn() as c:
        c.executescript(_SCHEMA)
    _inited.add(str(db.DB_PATH))


def cfg() -> dict:
    v = (conf._section("executive_reporting").get("calling") or {})
    return {**_DEFAULTS, **(v if isinstance(v, dict) else {})}


def numbers() -> list[str]:
    """Your authorized numbers: config/secrets.toml [calling] authorized_numbers (gitignored — a phone
    number must never land in the committed settings.toml), plus any older settings entry."""
    raw = list(conf.provider_cfg("calling").get("authorized_numbers") or []) + list(cfg().get("authorized_numbers") or [])
    out = []
    for n in (str(x).strip() for x in raw):
        if _E164.match(n) and n not in out:
            out.append(n)
    return out


def mask(n: str) -> str:
    return n[:3] + "•" * max(0, len(n) - 6) + n[-3:] if n else ""


_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
_TOKEN = re.compile(r"^[0-9a-fA-F]{32}$")


def validate_twilio(sid: str, token: str, from_number: str) -> list[str]:
    """Format checks before anything is saved or sent (a mis-paste must never reach the API)."""
    bad = []
    if not _SID.match(sid or ""):
        bad.append("Account SID should be 'AC' followed by 32 letters/digits")
    if not _TOKEN.match(token or ""):
        bad.append("Auth token should be 32 letters/digits")
    if not _E164.match(from_number or ""):
        bad.append("Twilio phone number should look like +15551234567 (no spaces)")
    return bad


def provider() -> str:
    """'telegram' (a voice message in Helios's own voice, free) or 'twilio' (a real phone call)."""
    return "telegram" if str(cfg().get("provider") or "").lower() == "telegram" else "twilio"


def targets() -> list[str]:
    """Where a briefing can go: the authorized numbers (Twilio) or the owner's own chat (Telegram)."""
    if provider() == "telegram":
        from . import channels
        return ["telegram"] if all(channels.telegram_target()) else []
    return numbers()


def label(t: str) -> str:
    return "your Telegram" if t == "telegram" else mask(t)


def provider_ready() -> tuple[bool, str]:
    if provider() == "telegram":
        from . import channels
        token, chat = channels.telegram_target()
        if not (token and chat):
            return False, "Telegram isn't set up — run `helios telegram setup`"
        return True, "telegram"
    t = conf.provider_cfg("twilio")
    missing = [k for k in ("account_sid", "auth_token", "from_number") if not str(t.get(k) or "").strip()]
    if missing:
        return False, f"Twilio isn't set up ({', '.join(missing)} missing) — run `helios calling setup`"
    bad = validate_twilio(*(str(t.get(k) or "").strip() for k in ("account_sid", "auth_token", "from_number")))
    if bad:
        return False, f"the saved Twilio details look wrong ({'; '.join(bad)}) — run `helios calling setup` again"
    return True, "twilio"


def _fingerprint() -> str:
    import hashlib
    if provider() == "telegram":
        from . import channels
        raw = "telegram|" + "|".join(channels.telegram_target())
    else:
        t = conf.provider_cfg("twilio")
        raw = "|".join([str(t.get("account_sid") or ""), str(t.get("from_number") or ""), *numbers()])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def verified() -> bool:
    """A test to these exact details completed (changing the provider, account, number or chat
    needs a new test)."""
    _init()
    return bool(targets()) and db.get_state("calling_verified") == _fingerprint()


def _clean(msg: str) -> str:
    """Provider errors must never echo credentials (urllib puts the URL — with the SID — in some)."""
    t = conf.provider_cfg("twilio")
    for k in ("account_sid", "auth_token"):
        v = str(t.get(k) or "").strip()
        if v:
            msg = msg.replace(v, "[redacted]")
    return re.sub(r"/Accounts/[^/'\s]*", "/Accounts/[redacted]", msg)


def status() -> dict:
    c, (ok, why) = cfg(), provider_ready()
    tg = provider() == "telegram"
    nums = targets()
    ver = ok and verified()
    ready = c["enabled"] and ok and bool(nums) and ver
    reasons = [r for r in ("" if c["enabled"] else "calling is off ([executive_reporting.calling] enabled = false)",
                           "" if ok else why,
                           "" if (nums or tg) else "no authorized number ([executive_reporting.calling] "
                                                   "authorized_numbers = [\"+91…\"], E.164)",
                           "" if (ver or not (ok and nums)) else "not verified yet — run `helios calling test` "
                                                                 "(the test must complete)") if r]
    note = ("voice messages in Helios's own voice to your Telegram — you play them when you like; Helios can't "
            "tell whether you listened" if tg else
            "one-way briefings only: live conversation needs a public webhook URL (Twilio <Gather>)")
    return {"ready": ready, "enabled": c["enabled"], "provider": ok, "provider_name": provider(), "verified": ver,
            "numbers": [label(n) for n in nums], "reasons": reasons, "two_way": False, "two_way_note": note}


def set_option(key: str, value) -> None:
    """Write one [executive_reporting.calling] setting (comments preserved), then reload.
    Turning calls ON is only exposed to the CLI / dashboard — never to the AI."""
    import os
    import tomlkit
    if key not in _DEFAULTS:
        raise ValueError(f"unknown calling setting {key!r}")
    if key == "authorized_numbers":
        bad = [n for n in value if not _E164.match(str(n).strip())]
        if bad:
            raise ValueError("numbers must be E.164, e.g. +91XXXXXXXXXX")
        conf.set_secret("calling", "authorized_numbers", [str(n).strip() for n in value])   # private, gitignored
        value = []                                     # …and never in the committed settings.toml
    p = conf.CONFIG_DIR / "settings.toml"
    with conf._SETTINGS_LOCK:
        doc = tomlkit.parse(p.read_text(encoding="utf-8"))
        er = doc.setdefault("executive_reporting", tomlkit.table())
        if "calling" not in er:
            er["calling"] = tomlkit.table()
        er["calling"][key] = value
        tmp = p.with_suffix(".toml.tmp")
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(tmp, p)
        conf.reload()
    conf.log("calling", f"setting {key} changed" + (f" to {value}" if key != "authorized_numbers" else ""))


def _quiet(now: datetime) -> bool:
    from .executive import in_quiet_hours
    c = cfg()
    return in_quiet_hours(now, {"quiet_hours_start": c["quiet_hours_start"], "quiet_hours_end": c["quiet_hours_end"]})


def _row(r) -> dict:
    d = dict(r)
    d["log"] = json.loads(d.get("log") or "[]")
    return d


def _update(cid: int, status: str | None = None, note: str = "", **fields) -> None:
    with db._conn() as c:
        r = c.execute("SELECT log FROM calls WHERE id=?", (cid,)).fetchone()
        log = json.loads(r["log"] or "[]") if r else []
        now = datetime.now().isoformat(timespec="seconds")
        if status:
            fields["status"] = status
            log.append({"at": now, "status": status, **({"note": note[:200]} if note else {})})
            if note:
                fields.setdefault("detail", note[:300])       # the latest reason, shown by `calling log`
        fields.update(updated=now, log=json.dumps(log[-30:]))
        c.execute(f"UPDATE calls SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), cid))
    if status:
        conf.log("calling", f"call #{cid}: {status}{' — ' + note if note else ''}")


def calls(limit: int = 30, *, upcoming: bool = False) -> list[dict]:
    _init()
    q = "SELECT * FROM calls" + (" WHERE status='scheduled'" if upcoming else "") + " ORDER BY id DESC LIMIT ?"
    with db._conn() as c:
        return [_row(r) for r in c.execute(q, (int(limit),)).fetchall()]


def wants_call_for(ev: dict) -> bool:
    c = cfg()
    if not c["enabled"]:
        return False
    if ev.get("severity") == "CRITICAL":
        return bool(c["call_for_critical_events"])
    return ev.get("type") == "lead_qualified" and ev.get("severity") == "HIGH" and bool(c["call_for_high_priority_leads"])


def wants_call_for_report(kind: str) -> bool:
    c = cfg()
    key = {"morning": "call_for_morning_briefing", "eod": "call_for_end_of_day",
           "night": "call_when_night_mode_finishes", "weekly": "call_for_weekly_review"}.get(kind)
    return bool(c["enabled"] and key and c.get(key))


def schedule(reason: str, *, due: datetime | None = None, report_kind: str = "", event_id: str = "",
             critical: bool = False, number_index: int = 0, now: datetime | None = None) -> dict:
    """Plan a call to an AUTHORIZED number. Returns {"status": scheduled|refused, "reason", "call"}.
    Refusals are recorded too (so the log shows every attempt and why it didn't happen)."""
    _init()
    now = now or datetime.now()
    due = due or now
    c, st = cfg(), status()

    def refuse(why: str) -> dict:
        with db._conn() as cx:
            cur = cx.execute("INSERT INTO calls(created, due, updated, reason, report_kind, event_id, critical, status, "
                             "detail, log) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (now.isoformat(timespec="seconds"), due.isoformat(timespec="seconds"),
                              now.isoformat(timespec="seconds"), reason[:200], report_kind, event_id, int(critical),
                              "refused", why[:300], json.dumps([{"at": now.isoformat(timespec='seconds'),
                                                                  "status": "refused", "note": why[:200]}])))
        conf.log("calling", f"call refused ({reason[:60]}): {why}")
        return {"status": "refused", "reason": why, "call": {"id": cur.lastrowid}}
    if not st["ready"]:
        return refuse("; ".join(st["reasons"]))
    nums = targets()
    if not (0 <= int(number_index) < len(nums)):
        return refuse("that number isn't in your authorized list")
    with db._conn() as cx:
        if event_id and cx.execute("SELECT 1 FROM calls WHERE event_id=? AND status NOT IN ('refused','canceled')",
                                   (event_id,)).fetchone():
            return {"status": "refused", "reason": "already called (or scheduled) about this event", "call": None}
        day = now.date().isoformat()
        made = cx.execute("SELECT COUNT(*) n FROM calls WHERE substr(due,1,10)=? AND status NOT IN "
                          "('refused','canceled') AND critical=0", (day,)).fetchone()["n"]
        last = cx.execute("SELECT MAX(due) d FROM calls WHERE status NOT IN ('refused','canceled') AND critical=0"
                          ).fetchone()["d"]
    if not critical:
        if made >= int(c["maximum_calls_per_day"]):
            return refuse(f"daily limit reached ({c['maximum_calls_per_day']} calls)")
        if last:
            gap = (due - datetime.fromisoformat(last)).total_seconds() / 60
            if abs(gap) < int(c["min_minutes_between_calls"]):
                return refuse(f"less than {c['min_minutes_between_calls']} min since the last call")
    if _quiet(due) and not (critical and c["critical_in_quiet_hours"]):
        if critical:
            return refuse("quiet hours (critical calls in quiet hours are off)")
        # non-critical: move to the end of quiet hours rather than drop it
        from .executive import _hm
        h, m = _hm(c["quiet_hours_end"], "08:00")
        nd = due.replace(hour=h, minute=m, second=0, microsecond=0)
        due = nd if nd > due else nd + timedelta(days=1)
    with db._conn() as cx:
        cur = cx.execute("INSERT INTO calls(created, due, updated, reason, report_kind, event_id, critical, to_masked, "
                         "to_index, status, log) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (now.isoformat(timespec="seconds"), due.isoformat(timespec="seconds"),
                          now.isoformat(timespec="seconds"), reason[:200], report_kind, event_id, int(critical),
                          label(nums[number_index]), int(number_index), "scheduled",
                          json.dumps([{"at": now.isoformat(timespec="seconds"), "status": "scheduled"}])))
    conf.log("calling", f"call #{cur.lastrowid} scheduled for {due:%Y-%m-%d %H:%M} ({reason[:60]})")
    return {"status": "scheduled", "reason": "", "call": {"id": cur.lastrowid, "due": due.isoformat(timespec="minutes")}}


def parse_when(s: str, now: datetime | None = None) -> datetime:
    """'' / 'now' → now; 'HH:MM' → next occurrence; 'in 20 minutes' / 'in 2 hours'; 'YYYY-MM-DD HH:MM'."""
    now = now or datetime.now()
    t = (s or "").strip().lower()
    if t in ("", "now"):
        return now
    m = re.fullmatch(r"in\s+(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?)", t)
    if m:
        n = int(m.group(1))
        return now + (timedelta(hours=n) if m.group(2).startswith("h") else timedelta(minutes=n))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(am|pm)?", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(3) == "pm" and h < 12:
            h += 12
        if m.group(3) == "am" and h == 12:
            h = 0
        d = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return d if d > now else d + timedelta(days=1)
    try:
        return datetime.fromisoformat(s.strip())
    except ValueError:
        raise ValueError("time must be 'now', 'HH:MM', 'in N minutes/hours' or 'YYYY-MM-DD HH:MM'") from None


def cancel(call_id: int | None = None) -> int:
    """Cancel one scheduled call, or all upcoming ones."""
    _init()
    n = 0
    for c in calls(200, upcoming=True):
        if call_id is None or c["id"] == int(call_id):
            _update(c["id"], "canceled", "canceled by you")
            n += 1
    return n


# ------------------------------------------------------------------ provider (Twilio REST)

def _auth(t: dict) -> dict:
    tok = base64.b64encode(f"{t['account_sid']}:{t['auth_token']}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


def twiml(text: str, voice: str) -> str:
    from .executive import scrub
    body = escape(scrub(text))[:3500]
    return (f'<Response><Say voice="{escape(voice)}">{body}</Say><Pause length="1"/>'
            f'<Say voice="{escape(voice)}">The details are on your Helios dashboard. Goodbye.</Say></Response>')


def place(text: str, to: str, *, opener=None) -> dict:
    """Create the call. {"ok", "sid", "status", "detail"} — never raises."""
    ok, why = provider_ready()
    if not ok:
        return {"ok": False, "sid": "", "status": "failed", "detail": why}
    if to not in numbers():                      # last line of defence
        return {"ok": False, "sid": "", "status": "refused", "detail": "number not authorized"}
    t = conf.provider_cfg("twilio")
    data = urllib.parse.urlencode({"To": to, "From": t["from_number"], "Twiml": twiml(text, cfg()["voice"])}).encode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{t['account_sid']}/Calls.json"
    try:
        req = urllib.request.Request(url, data=data, headers={**_auth(t), "Content-Type": "application/x-www-form-urlencoded"})
        with (opener or urllib.request.urlopen)(req, timeout=15) as r:
            out = json.loads(r.read() or b"{}")
        return {"ok": bool(out.get("sid")), "sid": out.get("sid", ""), "status": out.get("status", "queued"), "detail": ""}
    except Exception as e:
        return {"ok": False, "sid": "", "status": "failed", "detail": _clean(f"provider error: {_explain(e)}")[:300]}


def _explain(e: Exception) -> str:
    """Twilio's own error message (e.g. 'unverified number' on trial accounts) instead of a bare HTTP code."""
    try:
        body = json.loads(e.read() or b"{}")          # urllib.error.HTTPError carries Twilio's JSON
        return f"{getattr(e, 'code', '')} {body.get('message', '')} (Twilio code {body.get('code', '?')})".strip()
    except Exception:
        return str(e)


def fetch_status(sid: str, *, opener=None) -> dict:
    t = conf.provider_cfg("twilio")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{t['account_sid']}/Calls/{sid}.json"
    try:
        with (opener or urllib.request.urlopen)(urllib.request.Request(url, headers=_auth(t)), timeout=10) as r:
            out = json.loads(r.read() or b"{}")
        return {"status": out.get("status", ""), "duration": int(out.get("duration") or 0)}
    except Exception as e:
        return {"status": "", "error": _clean(str(e))[:160]}


def test_call(*, number_index: int = 0, wait_s: int = 90, placer=None, poller=None, sleep=None) -> dict:
    """The verification call `helios calling test` makes when the owner asks for it. Works while
    calling is still disabled (that's the point: verify first, then enable) but still only to an
    authorized number. Follows the call until the provider reports a final state."""
    import time
    _init()
    ok, why = provider_ready()
    nums = targets()
    if not ok or not nums or not (0 <= number_index < len(nums)):
        return {"status": "refused", "detail": why if not ok else "no authorized number configured"}
    now = datetime.now().isoformat(timespec="seconds")
    with db._conn() as cx:
        cid = cx.execute("INSERT INTO calls(created, due, updated, reason, to_masked, to_index, status, log) "
                         "VALUES(?,?,?,?,?,?,?,?)", (now, now, now, "test (you ran helios calling test)",
                                                     label(nums[number_index]), number_index, "scheduled",
                                                     json.dumps([{"at": now, "status": "scheduled"}]))).lastrowid
    if provider() == "telegram":
        res = send_voice_message("Hello sir, this is Helios. This is the test voice message you asked for. "
                                 "Voice briefings are working.", "Helios · test voice briefing")
        _update(cid, "completed" if res["ok"] else "failed", res["detail"], attempts=1, provider_sid=res["sid"])
        if res["ok"]:
            db.set_state("calling_verified", _fingerprint())
        return {"status": "completed" if res["ok"] else "failed", "answered": False, "duration": 0, "id": cid,
                "detail": res["detail"]}
    res = (placer or place)("Hello, this is Helios. This is the test call you asked for. Phone briefings "
                            "are working.", nums[number_index])
    if not res["ok"]:
        _update(cid, "failed", res["detail"], attempts=1)
        return {"status": "failed", "detail": res["detail"], "id": cid}
    _update(cid, "initiated", f"provider status {res['status']}", provider_sid=res["sid"], attempts=1)
    state, answered, dur = "initiated", 0, 0
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        (sleep or time.sleep)(3)
        s = (poller or fetch_status)(res["sid"])
        mapped = _MAP.get(s.get("status", ""), "")
        if mapped and mapped != state:
            state, dur = mapped, s.get("duration", 0)
            answered = answered or int(mapped == "answered" or (mapped == "completed" and dur > 0))
            _update(cid, mapped, f"provider: {s.get('status')}", answered=answered, duration=dur)
        if state in _FINAL:
            break
    if state == "completed":
        db.set_state("calling_verified", _fingerprint())
    return {"status": state, "answered": bool(answered), "duration": dur, "id": cid,
            "detail": "" if state in _FINAL else f"still {state} after {wait_s}s — see `helios calling log`"}


_MAP = {"queued": "initiated", "initiated": "initiated", "ringing": "ringing", "in-progress": "answered",
        "completed": "completed", "busy": "busy", "no-answer": "no-answer", "failed": "failed", "canceled": "canceled"}


def _text_for(call: dict) -> str:
    from . import executive
    if call.get("report_kind") == "morning":
        from . import briefing
        return briefing.latest_text(spoken_version=True)
    if call.get("report_kind"):
        rows = [r for r in executive.reports(limit=20) if r["kind"] == call["report_kind"]]
        if rows:
            return "Hello, this is Helios. " + rows[0]["spoken"]
    return f"Hello, this is Helios with an urgent update. {call.get('reason', '')}"


def tick(now: datetime | None = None, publish=None, *, placer=None, poller=None) -> list[str]:
    """Place due calls and follow in-flight ones (called from executive.tick)."""
    _init()
    now = now or datetime.now()
    out = []
    c = cfg()
    with db._conn() as cx:
        due = [_row(r) for r in cx.execute("SELECT * FROM calls WHERE status='scheduled' AND due<=?",
                                           (now.isoformat(timespec="seconds"),)).fetchall()]
        live = [_row(r) for r in cx.execute(f"SELECT * FROM calls WHERE status IN ({','.join('?' * len(_ACTIVE))})",
                                            _ACTIVE).fetchall()]
    st = status()
    for call in due:
        if not st["ready"]:
            _update(call["id"], "failed", "; ".join(st["reasons"]))
            _fallback(call, "; ".join(st["reasons"]), publish)
            continue
        if provider() == "telegram":
            # Rendering the voice takes a while (Kokoro on CPU): mark it in flight first so the next
            # tick can't send it twice, then render + send off the scheduler thread.
            _update(call["id"], "initiated", "rendering the voice message", provider_sid="tg:pending",
                    attempts=call["attempts"] + 1)
            _run_bg(lambda call=call: _finish_telegram(call, publish))
            out.append(f"#{call['id']} initiated")
            continue
        nums = numbers()
        to = nums[call["to_index"]] if call["to_index"] < len(nums) else ""
        res = (placer or place)(_text_for(call), to)
        if res["ok"]:
            _update(call["id"], "initiated", f"provider status {res['status']}", provider_sid=res["sid"],
                    attempts=call["attempts"] + 1)
            out.append(f"#{call['id']} initiated")
        else:
            _failed(call, call["attempts"] + 1, res["detail"], now, publish)
            out.append(f"#{call['id']} failed")
    for call in live:
        if str(call["provider_sid"]).startswith("tg:"):          # Telegram sends finish on their own thread
            if now - datetime.fromisoformat(call["updated"]) > timedelta(hours=1):
                _update(call["id"], "failed", "the voice message never finished sending")
                _fallback(call, "the voice message never finished sending", publish)
            continue
        s = (poller or fetch_status)(call["provider_sid"])
        mapped = _MAP.get(s.get("status", ""), "")
        if not mapped:
            if now - datetime.fromisoformat(call["updated"]) > timedelta(hours=1):   # never left "live" forever
                _update(call["id"], "failed", f"status unknown after an hour ({s.get('error', 'no provider status')})")
                _fallback(call, "the call's outcome couldn't be confirmed", publish)
                out.append(f"#{call['id']} failed")
            continue
        if mapped == call["status"] or (mapped in _ACTIVE and _ACTIVE.index(mapped) < _ACTIVE.index(call["status"])):
            continue                                                                  # never step backwards
        extra = {"duration": s.get("duration", 0)}
        if mapped in ("answered", "completed") and (mapped == "answered" or s.get("duration", 0) > 0):
            extra["answered"] = 1
        _update(call["id"], mapped, f"provider: {s.get('status')}", **extra)
        if mapped in ("failed", "no-answer", "busy"):
            _fallback(call, f"the call ended as {mapped}", publish)
        out.append(f"#{call['id']} {mapped}")
    return out


def _failed(call: dict, attempts: int, detail: str, now: datetime, publish=None) -> None:
    """A failed attempt: critical ones retry up to max_attempts, everything else falls back."""
    c = cfg()
    if call["critical"] and attempts < int(c["max_attempts"]):
        _update(call["id"], "scheduled", f"attempt {attempts} failed: {detail}; retrying", attempts=attempts,
                due=(now + timedelta(minutes=int(c["retry_minutes"]))).isoformat(timespec="seconds"))
    else:
        _update(call["id"], "failed", detail, attempts=attempts)
        _fallback(call, detail, publish)


def _run_bg(fn) -> None:
    import threading
    threading.Thread(target=fn, daemon=True, name="voice-briefing").start()


def send_voice_message(text: str, caption: str) -> dict:
    """Render `text` in Helios's own voice and send it to the owner's Telegram. Never raises."""
    from . import channels, voice_note
    try:
        audio = voice_note.render(text)
    except Exception as e:
        return {"ok": False, "sid": "", "detail": f"couldn't render the voice message: {type(e).__name__}: {e}"[:300]}
    ok, detail = channels.telegram_send_voice(audio, caption)
    if ok:
        return {"ok": True, "sid": f"tg:{detail}",
                "detail": "voice message delivered to your Telegram (Helios can't tell whether you played it)"}
    return {"ok": False, "sid": "", "detail": detail}


def _finish_telegram(call: dict, publish=None) -> None:
    try:
        text = _text_for(call) + " The full report is on your Helios dashboard."
        res = send_voice_message(text, f"Helios · {call.get('reason', '')}"[:200])
    except Exception as e:  # pragma: no cover
        res = {"ok": False, "sid": "", "detail": f"voice briefing failed: {e}"[:300]}
    if res["ok"]:
        _update(call["id"], "completed", res["detail"], provider_sid=res["sid"])
    else:
        _failed({**call, "attempts": call["attempts"] + 1}, call["attempts"] + 1, res["detail"], datetime.now(), publish)


def _fallback(call: dict, why: str, publish=None) -> None:
    """When a call can't happen, the report still reaches the owner on the dashboard / toast."""
    from . import executive
    executive.emit("calling", "call_not_completed", "NORMAL",
                   f"Phone briefing not delivered ({why[:120]}) — the report is on the dashboard",
                   related=f"call:{call['id']}", dedupe_key=f"call:{call['id']}")
