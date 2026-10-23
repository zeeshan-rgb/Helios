"""SMS notifications to AUTHORIZED recipients — the owner, and people the owner added with recorded
consent (they don't need Gmail, Telegram or any account). Docs: docs/HELIOS_SMS_INTEGRATION.md.

Built on what Helios already has: events come from the executive event queue (severity, dedupe),
reports from executive reporting / the briefing, the queue is drained by the existing scheduler
tick (executive.tick → sms.tick), and the patterns (consent-gated recipients, limits, retries,
honest states) mirror calling.py. No parallel notifier.

Safety (enforced in code, not prompts):
* a message only ever goes to a recipient in the `sms_recipients` table — there is no function that
  takes an arbitrary number from the AI, a web page or an email
* other people need consent = "granted" (recorded by the owner) and the category enabled; by default
  every automatic message to them waits in APPROVAL_REQUIRED until the owner approves it
* business leads / prospects / customers can never be recipients (their numbers are refused) and no
  prospecting code path reaches this module
* opt-outs and the suppression list always win; quiet hours, per-recipient daily caps, a minimum gap,
  a global hourly cap, bounded retries; a global emergency OFF (`helios sms off`, dashboard, or the
  brain's sms_off) cancels everything pending
* text is scrubbed (no secrets, emails, phone numbers), plain GSM characters, ≤ 300 chars
* states: QUEUED · APPROVAL_REQUIRED · SENT_TO_PROVIDER · DELIVERED · FAILED · RETRY_SCHEDULED ·
  CANCELLED · SUPPRESSED — DELIVERED only when the provider's status API reports delivery to the
  handset; "accepted by the provider" is SENT_TO_PROVIDER, never "delivered"

India: commercial SMS must follow TRAI DLT — a registered entity, sender ID (header) and each
message template, matched character-for-character. Messages here are rendered from templates
([sms.templates.*]) whose text you register on the DLT portal; an Indian number with no registered
template for that kind of message is SUPPRESSED ("DLT template not registered") rather than sent to
be silently blocked by the carrier.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from . import conf, db

STATES = ("QUEUED", "APPROVAL_REQUIRED", "SENT_TO_PROVIDER", "DELIVERED", "FAILED", "RETRY_SCHEDULED",
          "CANCELLED", "SUPPRESSED")
_PENDING = ("QUEUED", "APPROVAL_REQUIRED", "RETRY_SCHEDULED")
CATEGORIES = {
    "critical": "Critical system failures",
    "antigravity": "Antigravity connection failures",
    "task_complete": "Important task completions",
    "night_mode": "Night Mode completion summaries",
    "morning_briefing": "Morning briefings",
    "reminders": "Approved reminders",
    "project_alerts": "High-priority project alerts",
    "reports": "Reports you request / scheduled reports",
    "message": "Messages you send yourself",
    "test": "Test messages",
}
_DEFAULTS = {"enabled": False, "provider": "phone", "owner_categories": ["critical"],
             "max_per_day": 10, "max_per_hour_total": 20, "min_gap_minutes": 5, "quiet_hours_start": "22:00",
             "quiet_hours_end": "08:00", "critical_in_quiet_hours": False, "max_attempts": 3,
             "retry_minutes": [2, 10, 30], "batch_minutes": 30, "batch_categories": ["task_complete"],
             "fallback": True, "fallback_severities": ["CRITICAL", "HIGH"], "max_chars": 300,
             "country_dlt": ["+91"]}
# Message templates. In India the text must be registered on the DLT portal EXACTLY as written
# (with {#var#} placeholders) and its template id put in settings — see the docs.
TEMPLATES = {
    "alert": "HELIOS ALERT: {#var#}. Open Helios for details.",
    "morning_briefing": "Good morning. Your Helios report is ready: {#var#}. Review it in your dashboard.",
    "night_mode": "HELIOS: Night Mode finished: {#var#}. View the report for details.",
    "report": "HELIOS: Your {#var#} is ready. Open Helios to review it.",
    "task_complete": "HELIOS: {#var#}. Open Helios for details.",
    "reminder": "HELIOS reminder: {#var#}",
    "message": "HELIOS: {#var#}",
    "test": "HELIOS: test message. SMS alerts are working.",
}
_CAT_TEMPLATE = {"critical": "alert", "antigravity": "alert", "project_alerts": "alert", "night_mode": "night_mode",
                 "morning_briefing": "morning_briefing", "reports": "report", "task_complete": "task_complete",
                 "reminders": "reminder", "message": "message", "test": "test"}
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sms_recipients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, purpose TEXT NOT NULL DEFAULT '',
  is_owner INTEGER NOT NULL DEFAULT 0,
  consent TEXT NOT NULL DEFAULT 'pending',        -- owner | granted | pending | revoked
  consent_note TEXT NOT NULL DEFAULT '', consent_at TEXT,
  categories TEXT NOT NULL DEFAULT '[]',
  quiet_start TEXT NOT NULL DEFAULT '', quiet_end TEXT NOT NULL DEFAULT '',
  max_per_day INTEGER, require_approval INTEGER NOT NULL DEFAULT 1,
  prefs TEXT NOT NULL DEFAULT '{}',
  opted_out INTEGER NOT NULL DEFAULT 0, opted_out_at TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sms_suppressed (phone TEXT PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sms_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created TEXT NOT NULL, updated TEXT NOT NULL, due TEXT NOT NULL,
  recipient_id INTEGER NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'NORMAL',
  event_id TEXT NOT NULL DEFAULT '', dedupe_key TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT '',
  template TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
  status TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '', provider_id TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '', log TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS sms_messages_status ON sms_messages(status);
"""
_inited: set[str] = set()


class SMSError(ValueError):
    pass


def _init() -> None:
    if str(db.DB_PATH) in _inited:
        return
    db.init()
    with db._conn() as c:
        c.executescript(_SCHEMA)
    _inited.add(str(db.DB_PATH))


def cfg() -> dict:
    v = conf._section("sms")
    out = dict(_DEFAULTS)
    out.update({k: val for k, val in v.items() if not isinstance(val, dict)})
    out["templates"] = {k: (v.get("templates") or {}).get(k) or {} for k in TEMPLATES}
    return out


def _now() -> datetime:
    return datetime.now()


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def mask(phone: str) -> str:
    return phone[:3] + "•" * max(0, len(phone) - 6) + phone[-3:] if phone else ""


def normalize(phone: str, default_cc: str = "+91") -> str:
    """'+91 98765-43210' / '098765 43210' / '9876543210' → '+919876543210'. Raises on anything else.
    Never guesses a number from an email or a name."""
    p = re.sub(r"[\s\-().]", "", str(phone or ""))
    if p.startswith("00"):
        p = "+" + p[2:]
    if not p.startswith("+"):
        p = p.lstrip("0")
        if len(p) == 10 and default_cc == "+91" and p[0] in "6789":
            p = default_cc + p
        else:
            raise SMSError("give the number with its country code, e.g. +91 98765 43210")
    if not _E164.match(p):
        raise SMSError("that isn't a valid phone number (E.164, e.g. +919876543210)")
    return p


def scrub(text: str) -> str:
    from .executive import scrub as ex_scrub
    t = ex_scrub(text)
    t = t.replace("[contact on the dashboard]", "[see Helios]")
    t = t.replace("—", "-").replace("–", "-").replace("·", "-").replace("…", "...")
    t = re.sub(r"[‘’]", "'", t)
    t = re.sub(r"[“”]", '"', t)
    return re.sub(r"[^\x20-\x7E]", "", t).strip()        # plain GSM-safe text: no emoji / special glyphs


# ------------------------------------------------------------------ providers

class Provider:
    """Stable interface every SMS provider implements."""
    name = "none"

    def validate_configuration(self) -> tuple[bool, str]:
        return False, "no SMS provider configured"

    def send_sms(self, to: str, body: str, *, template_id: str = "") -> dict:
        """{"ok", "id", "raw_status", "detail", "permanent"} — never raises."""
        return {"ok": False, "id": "", "raw_status": "", "detail": "no provider", "permanent": True}

    def get_delivery_status(self, message_id: str) -> dict:
        """{"state": SENT_TO_PROVIDER|DELIVERED|FAILED|"", "raw": provider status, "detail"}."""
        return {"state": "", "raw": "", "detail": "no provider"}

    def cancel_or_suppress_pending_message(self, message_id: str) -> dict:
        return {"ok": False, "detail": "the provider can't recall a message once accepted"}

    def requires_dlt(self, to: str) -> bool:
        return False


def _http(req: urllib.request.Request, opener=None, timeout: int = 15) -> tuple[int, dict, str]:
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            raw = r.read() or b"{}"
            try:
                return getattr(r, "status", 200), json.loads(raw), ""
            except ValueError:
                return getattr(r, "status", 200), {}, raw[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return e.code, body, ""
    except Exception as e:                                # timeout / DNS / connection
        return 0, {}, f"{type(e).__name__}: {e}"


def _redact(msg: str, *secrets_: str) -> str:
    for s in secrets_:
        if s:
            msg = msg.replace(s, "[redacted]")
    return msg


class TwilioSMS(Provider):
    """Twilio Programmable Messaging (Messages.json). Reuses secrets [twilio] account_sid / auth_token;
    sender = [twilio] sms_from (a Twilio number) or messaging_service_sid."""
    name = "twilio"
    _MAP = {"accepted": "SENT_TO_PROVIDER", "queued": "SENT_TO_PROVIDER", "sending": "SENT_TO_PROVIDER",
            "sent": "SENT_TO_PROVIDER", "scheduled": "SENT_TO_PROVIDER", "delivered": "DELIVERED",
            "undelivered": "FAILED", "failed": "FAILED", "canceled": "FAILED"}

    def __init__(self, opener=None):
        self.opener = opener
        self.c = conf.provider_cfg("twilio")

    def _auth(self) -> dict:
        tok = base64.b64encode(f"{self.c.get('account_sid')}:{self.c.get('auth_token')}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def validate_configuration(self):
        miss = [k for k in ("account_sid", "auth_token") if not str(self.c.get(k) or "").strip()]
        if not (self.c.get("sms_from") or self.c.get("messaging_service_sid")):
            miss.append("sms_from (or messaging_service_sid)")
        if miss:
            return False, f"Twilio SMS isn't set up ({', '.join(miss)} missing) — `helios sms setup twilio`"
        return True, "twilio"

    def requires_dlt(self, to):
        return any(to.startswith(cc) for cc in cfg().get("country_dlt") or [])

    def send_sms(self, to, body, *, template_id=""):
        ok, why = self.validate_configuration()
        if not ok:
            return {"ok": False, "id": "", "raw_status": "", "detail": why, "permanent": True}
        data = {"To": to, "Body": body}
        if self.c.get("messaging_service_sid"):
            data["MessagingServiceSid"] = self.c["messaging_service_sid"]
        else:
            data["From"] = self.c["sms_from"]
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.c['account_sid']}/Messages.json"
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                     headers={**self._auth(), "Content-Type": "application/x-www-form-urlencoded"})
        code, js, err = _http(req, self.opener)
        if code in (200, 201) and js.get("sid"):
            return {"ok": True, "id": js["sid"], "raw_status": js.get("status", ""), "detail": "", "permanent": False}
        detail = err or f"HTTP {code}: {js.get('message', '')} (Twilio code {js.get('code', '?')})"
        return {"ok": False, "id": "", "raw_status": "", "permanent": 400 <= code < 500,
                "detail": _redact(detail, self.c.get("account_sid", ""), self.c.get("auth_token", ""))[:300]}

    def get_delivery_status(self, message_id):
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.c.get('account_sid')}/Messages/{message_id}.json"
        code, js, err = _http(urllib.request.Request(url, headers=self._auth()), self.opener, timeout=10)
        raw = js.get("status", "")
        detail = err or (f"error {js['error_code']}: {js.get('error_message') or ''}" if js.get("error_code") else "")
        return {"state": self._MAP.get(raw, ""), "raw": raw,
                "detail": _redact(detail, self.c.get("account_sid", ""), self.c.get("auth_token", ""))[:200]}


class ExotelSMS(Provider):
    """Exotel SMS (India): POST /v1/Accounts/<sid>/Sms/send with DLT ids; status from
    /v1/Accounts/<sid>/SMS/Messages/<SmsSid>. Secrets [exotel]: api_key, api_token, account_sid,
    sender_id (your DLT header / ExoPhone), dlt_entity_id, subdomain (api.exotel.com or api.in.exotel.com)."""
    name = "exotel"

    def __init__(self, opener=None):
        self.opener = opener
        self.c = conf.provider_cfg("exotel")

    def _base(self) -> str:
        host = str(self.c.get("subdomain") or "api.exotel.com").replace("https://", "").strip("/")
        return f"https://{host}/v1/Accounts/{self.c.get('account_sid')}"

    def _auth(self) -> dict:
        tok = base64.b64encode(f"{self.c.get('api_key')}:{self.c.get('api_token')}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def validate_configuration(self):
        miss = [k for k in ("api_key", "api_token", "account_sid", "sender_id", "dlt_entity_id")
                if not str(self.c.get(k) or "").strip()]
        if miss:
            return False, f"Exotel SMS isn't set up ({', '.join(miss)} missing) — `helios sms setup exotel`"
        return True, "exotel"

    def requires_dlt(self, to):
        return to.startswith("+91")

    def send_sms(self, to, body, *, template_id=""):
        ok, why = self.validate_configuration()
        if not ok:
            return {"ok": False, "id": "", "raw_status": "", "detail": why, "permanent": True}
        data = {"From": self.c["sender_id"], "To": to, "Body": body, "SmsType": "transactional"}
        if self.requires_dlt(to):
            data["DltEntityId"] = self.c["dlt_entity_id"]
            if template_id:
                data["DltTemplateId"] = template_id
        req = urllib.request.Request(self._base() + "/Sms/send.json", data=urllib.parse.urlencode(data).encode(),
                                     headers={**self._auth(), "Content-Type": "application/x-www-form-urlencoded"})
        code, js, err = _http(req, self.opener)
        m = js.get("SMSMessage") or {}
        if code in (200, 201) and m.get("Sid"):
            return {"ok": True, "id": m["Sid"], "raw_status": m.get("Status", ""), "detail": "", "permanent": False}
        rest = js.get("RestException") or {}
        detail = err or f"HTTP {code}: {rest.get('Message') or js}"
        return {"ok": False, "id": "", "raw_status": "", "permanent": 400 <= code < 500,
                "detail": _redact(str(detail), self.c.get("api_key", ""), self.c.get("api_token", ""))[:300]}

    def get_delivery_status(self, message_id):
        code, js, err = _http(urllib.request.Request(f"{self._base()}/SMS/Messages/{message_id}.json",
                                                     headers=self._auth()), self.opener, timeout=10)
        m = js.get("SMSMessage") or {}
        raw, det = str(m.get("Status", "")), str(m.get("DetailedStatus", ""))
        if raw in ("failed", "failed-dnd", "failed_dnd"):
            state = "FAILED"
        elif raw == "sent" and "DELIVERED" in det.upper():     # only an explicit handset delivery counts
            state = "DELIVERED"
        elif raw in ("queued", "sending", "submitted", "sent"):
            state = "SENT_TO_PROVIDER"
        else:
            state = ""
        return {"state": state, "raw": raw + (f" / {det}" if det else ""),
                "detail": _redact(err, self.c.get("api_key", ""), self.c.get("api_token", ""))[:200]}


class PhoneGatewaySMS(Provider):
    """Your OWN Android phone sends the SMS from your SIM, via the open-source "SMS Gateway for Android"
    app (github.com/capcom6/android-sms-gateway, Apache-2.0). No Twilio / Exotel, no DLT (person-to-person
    SMS from your number), uses your plan's SMS allowance. Secrets [sms_gateway]: url (local mode
    http://<phone-ip>:8080 — same Wi-Fi as this PC; or cloud https://api.sms-gate.app/3rdparty/v1),
    username, password (shown in the app). Send: POST {url}/message; status: GET {url}/messages/{id}
    (Pending → Processed → Sent → Delivered | Failed; Delivered needs withDeliveryReport)."""
    name = "phone"
    _MAP = {"pending": "SENT_TO_PROVIDER", "processed": "SENT_TO_PROVIDER", "sent": "SENT_TO_PROVIDER",
            "delivered": "DELIVERED", "failed": "FAILED", "cancelled": "FAILED", "cancelling": "SENT_TO_PROVIDER"}

    def __init__(self, opener=None):
        self.opener = opener
        self.c = conf.provider_cfg("sms_gateway")

    def _url(self) -> str:
        return str(self.c.get("url") or "").strip().rstrip("/")

    def _auth(self) -> dict:
        tok = base64.b64encode(f"{self.c.get('username')}:{self.c.get('password')}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def validate_configuration(self):
        miss = [k for k in ("url", "username", "password") if not str(self.c.get(k) or "").strip()]
        if miss:
            return False, f"the phone SMS gateway isn't set up ({', '.join(miss)} missing) — `helios sms setup phone`"
        if not re.match(r"^https?://", self._url()):
            return False, "gateway url must start with http:// or https://"
        return True, "phone"

    def send_sms(self, to, body, *, template_id=""):
        ok, why = self.validate_configuration()
        if not ok:
            return {"ok": False, "id": "", "raw_status": "", "detail": why, "permanent": True}
        payload = json.dumps({"textMessage": {"text": body}, "phoneNumbers": [to], "withDeliveryReport": True}).encode()
        req = urllib.request.Request(self._url() + "/message", data=payload,
                                     headers={**self._auth(), "Content-Type": "application/json"})
        code, js, err = _http(req, self.opener)
        if code in (200, 201, 202) and js.get("id"):
            return {"ok": True, "id": str(js["id"]), "raw_status": str(js.get("state", "Pending")), "detail": "",
                    "permanent": False}
        if code == 0:
            detail = f"can't reach your phone ({err}) — is it on the same Wi-Fi, with the gateway app running?"
        elif code == 401:
            detail = "the phone gateway rejected the username/password — check them in the app"
        else:
            detail = f"HTTP {code}: {js.get('message') or js or err}"
        return {"ok": False, "id": "", "raw_status": "", "permanent": code in (400, 401, 403),
                "detail": _redact(str(detail), self.c.get("password", ""))[:300]}

    def get_delivery_status(self, message_id):
        code, js, err = 0, {}, ""
        for path in (f"/messages/{message_id}", f"/message/{message_id}"):   # cloud / local API versions
            code, js, err = _http(urllib.request.Request(self._url() + path, headers=self._auth()), self.opener, timeout=10)
            if code != 404:
                break
        raw = str(js.get("state", ""))
        why = ""
        for rcp in js.get("recipients") or []:
            if isinstance(rcp, dict) and (rcp.get("error") or rcp.get("reason")):
                why = str(rcp.get("error") or rcp.get("reason"))
        return {"state": self._MAP.get(raw.lower(), ""), "raw": raw, "detail": (why or err)[:200]}

    def cancel_or_suppress_pending_message(self, message_id):
        return {"ok": False, "detail": "cancel it in the gateway app if it's still pending on the phone"}


PROVIDERS = {"phone": PhoneGatewaySMS, "twilio": TwilioSMS, "exotel": ExotelSMS}


def provider(opener=None) -> Provider:
    cls = PROVIDERS.get(str(cfg().get("provider") or "").lower())
    return cls(opener) if cls else Provider()


# ------------------------------------------------------------------ recipients

def _rrow(r) -> dict:
    d = dict(r)
    d["categories"] = json.loads(d.get("categories") or "[]")
    d["prefs"] = json.loads(d.get("prefs") or "{}")
    for k in ("is_owner", "opted_out", "enabled", "require_approval"):
        d[k] = bool(d.get(k))
    return d


def recipients(*, include_disabled: bool = True) -> list[dict]:
    _init()
    with db._conn() as c:
        rows = [_rrow(r) for r in c.execute("SELECT * FROM sms_recipients ORDER BY is_owner DESC, name")]
    return [r for r in rows if include_disabled or r["enabled"]]


def get_recipient(ref) -> dict | None:
    """By id, exact name (case-insensitive) or "me" (the owner)."""
    _init()
    s = str(ref or "").strip()
    with db._conn() as c:
        if s.lower() in ("me", "owner", "myself"):
            r = c.execute("SELECT * FROM sms_recipients WHERE is_owner=1").fetchone()
        elif s.isdigit():
            r = c.execute("SELECT * FROM sms_recipients WHERE id=?", (int(s),)).fetchone()
        else:
            r = c.execute("SELECT * FROM sms_recipients WHERE lower(name)=lower(?)", (s,)).fetchone()
    return _rrow(r) if r else None


def _is_business_contact(phone: str) -> bool:
    """Leads / prospects / customers can never be SMS recipients."""
    try:
        from .prospecting import store
        tail = phone[-10:]
        for p in store.list_prospects(limit=5000):
            if p.get("phone") and re.sub(r"\D", "", p["phone"]).endswith(tail):
                return True
    except Exception:
        pass
    return False


def add_recipient(name: str, phone: str, *, purpose: str = "", owner: bool = False, consent: str = "pending",
                  consent_note: str = "", categories: list[str] | None = None, quiet_start: str = "",
                  quiet_end: str = "", max_per_day: int | None = None, require_approval: bool | None = None,
                  prefs: dict | None = None) -> dict:
    """Owner-only (CLI / dashboard). Consent for other people is what YOU record after asking them."""
    _init()
    name = (name or "").strip()[:60]
    if not name:
        raise SMSError("a name is required")
    phone = normalize(phone)
    if _is_business_contact(phone):
        raise SMSError("that number belongs to a business lead / prospect — Helios never texts leads or customers")
    with db._conn() as c:
        if c.execute("SELECT 1 FROM sms_suppressed WHERE phone=?", (phone,)).fetchone():
            raise SMSError("that number is on the suppression list (it opted out)")
        if owner and c.execute("SELECT 1 FROM sms_recipients WHERE is_owner=1").fetchone():
            raise SMSError("you already have an owner number — edit it instead")
    cats = [k for k in (categories if categories is not None else
                        (cfg().get("owner_categories") if owner else [])) if k in CATEGORIES]
    if owner:
        consent, require_approval = "owner", False if require_approval is None else require_approval
    elif consent not in ("pending", "granted"):
        raise SMSError("consent is pending or granted")
    if require_approval is None:
        require_approval = True
    now = _iso(_now())
    with db._conn() as c:
        try:
            cur = c.execute("INSERT INTO sms_recipients(name, phone, purpose, is_owner, consent, consent_note, consent_at, "
                            "categories, quiet_start, quiet_end, max_per_day, require_approval, prefs, created) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (name, phone, purpose[:200], int(owner), consent, consent_note[:200],
                             now if consent in ("owner", "granted") else None, json.dumps(cats), quiet_start, quiet_end,
                             max_per_day, int(bool(require_approval)), json.dumps(prefs or {}), now))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise SMSError("that number is already a recipient") from None
            raise
    conf.log("sms", f"recipient #{cur.lastrowid} added ({name}, {mask(phone)}, consent {consent})")
    return get_recipient(cur.lastrowid)


def update_recipient(ref, **fields) -> dict:
    r = get_recipient(ref)
    if not r:
        raise SMSError("no such recipient")
    allowed = {"name", "purpose", "categories", "quiet_start", "quiet_end", "max_per_day", "require_approval",
               "prefs", "enabled"}
    sets = {}
    for k, v in fields.items():
        if k == "consent":
            if r["is_owner"]:
                continue
            if v not in ("pending", "granted", "revoked"):
                raise SMSError("consent is pending, granted or revoked")
            sets["consent"], sets["consent_at"] = v, _iso(_now())
            if fields.get("consent_note"):
                sets["consent_note"] = str(fields["consent_note"])[:200]
        elif k in allowed:
            if k == "categories":
                v = json.dumps([c for c in v if c in CATEGORIES])
            elif k == "prefs":
                v = json.dumps(v or {})
            elif k in ("require_approval", "enabled"):
                v = int(bool(v))
            sets[k] = v
    if sets:
        with db._conn() as c:
            c.execute(f"UPDATE sms_recipients SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                      (*sets.values(), r["id"]))
    return get_recipient(r["id"])


def opt_out(ref, reason: str = "asked to stop") -> dict:
    """The recipient asked to stop: nothing is ever sent to that number again (suppression list), and
    anything pending for them is cancelled."""
    r = get_recipient(ref)
    if not r:
        raise SMSError("no such recipient")
    now = _iso(_now())
    with db._conn() as c:
        c.execute("UPDATE sms_recipients SET opted_out=1, opted_out_at=? WHERE id=?", (now, r["id"]))
        c.execute("INSERT OR REPLACE INTO sms_suppressed(phone, reason, at) VALUES(?,?,?)", (r["phone"], reason[:120], now))
        ids = [x["id"] for x in c.execute(f"SELECT id FROM sms_messages WHERE recipient_id=? AND status IN "
                                          f"({','.join('?' * len(_PENDING))})", (r["id"], *_PENDING))]
    for i in ids:
        _update(i, "CANCELLED", "recipient opted out")
    conf.log("sms", f"recipient #{r['id']} opted out")
    return get_recipient(r["id"])


def opt_in(ref) -> dict:
    """Only when the person themselves asks to receive messages again."""
    r = get_recipient(ref)
    if not r:
        raise SMSError("no such recipient")
    with db._conn() as c:
        c.execute("UPDATE sms_recipients SET opted_out=0, opted_out_at=NULL WHERE id=?", (r["id"],))
        c.execute("DELETE FROM sms_suppressed WHERE phone=?", (r["phone"],))
    return get_recipient(r["id"])


def remove_recipient(ref) -> bool:
    r = get_recipient(ref)
    if not r:
        return False
    with db._conn() as c:
        c.execute("UPDATE sms_messages SET status='CANCELLED', detail='recipient removed' WHERE recipient_id=? AND "
                  f"status IN ({','.join('?' * len(_PENDING))})", (r["id"], *_PENDING))
        c.execute("DELETE FROM sms_recipients WHERE id=?", (r["id"],))
    return True


# ------------------------------------------------------------------ status / config

def set_option(key: str, value) -> None:
    """Write one [sms] setting (comments kept). Turning SMS ON is CLI / dashboard only — never the AI."""
    import os
    import tomlkit
    if key not in _DEFAULTS:
        raise SMSError(f"unknown SMS setting {key!r}")
    p = conf.CONFIG_DIR / "settings.toml"
    with conf._SETTINGS_LOCK:
        doc = tomlkit.parse(p.read_text(encoding="utf-8"))
        if "sms" not in doc:
            doc["sms"] = tomlkit.table()
        doc["sms"][key] = value
        tmp = p.with_suffix(".toml.tmp")
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(tmp, p)
        conf.reload()
    conf.log("sms", f"setting {key} -> {value}")


def emergency_off(reason: str = "you turned SMS off") -> int:
    """The kill switch: SMS off + everything pending cancelled. Safe for the AI to call."""
    set_option("enabled", False)
    _init()
    with db._conn() as c:
        ids = [r["id"] for r in c.execute(f"SELECT id FROM sms_messages WHERE status IN ({','.join('?' * len(_PENDING))})",
                                          _PENDING)]
    for i in ids:
        _update(i, "CANCELLED", reason)
    conf.log("sms", f"EMERGENCY OFF ({len(ids)} pending cancelled)")
    return len(ids)


def status() -> dict:
    c = cfg()
    ok, why = provider().validate_configuration()
    owner = get_recipient("me")
    reasons = [r for r in ("" if c["enabled"] else "SMS is off ([sms] enabled = false)", "" if ok else why,
                           "" if owner else "no owner number yet — `helios sms me +91…`") if r]
    dlt = {k: bool((c["templates"].get(k) or {}).get("dlt_template_id")) for k in TEMPLATES}
    return {"enabled": c["enabled"], "provider": c["provider"], "provider_ready": ok, "ready": c["enabled"] and ok and bool(owner),
            "reasons": reasons, "owner": mask(owner["phone"]) if owner else "", "dlt_templates": dlt,
            "credentials": _masked_creds(c["provider"])}


def _masked_creds(name: str) -> dict:
    sec = conf.provider_cfg(name)
    sec = conf.provider_cfg("sms_gateway" if name == "phone" else name)
    keys = {"twilio": ("account_sid", "auth_token", "sms_from", "messaging_service_sid"),
            "exotel": ("api_key", "api_token", "account_sid", "sender_id", "dlt_entity_id", "subdomain"),
            "phone": ("url", "username", "password")}.get(name, ())
    out = {}
    for k in keys:
        v = str(sec.get(k) or "")
        out[k] = ("set" if k in ("auth_token", "api_token", "password") else
                  (v if k == "url" else (v[:4] + "…" + v[-2:] if len(v) > 8 else v))) if v else "missing"
    return out


# ------------------------------------------------------------------ queue

def _mrow(r) -> dict:
    d = dict(r)
    d["log"] = json.loads(d.get("log") or "[]")
    return d


def messages(limit: int = 50, *, status_in: tuple | None = None) -> list[dict]:
    _init()
    q, a = "SELECT * FROM sms_messages", []
    if status_in:
        q += f" WHERE status IN ({','.join('?' * len(status_in))})"
        a += list(status_in)
    with db._conn() as c:
        rows = [_mrow(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*a, int(limit)))]
    names = {r["id"]: r for r in recipients()}
    for m in rows:
        rr = names.get(m["recipient_id"])
        m["recipient"] = rr["name"] if rr else "(removed)"
        m["to_masked"] = mask(rr["phone"]) if rr else ""
    return rows


def _update(mid: int, status: str | None = None, note: str = "", **fields) -> None:
    with db._conn() as c:
        r = c.execute("SELECT log FROM sms_messages WHERE id=?", (mid,)).fetchone()
        log = json.loads(r["log"] or "[]") if r else []
        now = _iso(_now())
        if status:
            fields["status"] = status
            log.append({"at": now, "status": status, **({"note": note[:200]} if note else {})})
            if note:
                fields.setdefault("detail", note[:300])
        fields.update(updated=now, log=json.dumps(log[-30:]))
        c.execute(f"UPDATE sms_messages SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), mid))
    if status:
        conf.log("sms", f"message #{mid}: {status}{' - ' + note if note else ''}")


def render(category: str, value: str = "") -> tuple[str, str, str]:
    """(template key, final text, dlt template id). Text = the registered template with {#var#} filled."""
    key = _CAT_TEMPLATE.get(category, "message")
    tpl = cfg()["templates"].get(key) or {}
    text = str(tpl.get("text") or TEMPLATES[key])
    v = scrub(value)[:(260 if key == "message" else 120)].rstrip(" .")
    body = text.replace("{#var#}", v, 1).replace("{#var#}", "")
    return key, scrub(body)[: int(cfg().get("max_chars") or 300)], str(tpl.get("dlt_template_id") or "")


def _in_quiet(r: dict, when: datetime) -> bool:
    from .executive import in_quiet_hours
    c = cfg()
    return in_quiet_hours(when, {"quiet_hours_start": r.get("quiet_start") or c["quiet_hours_start"],
                                 "quiet_hours_end": r.get("quiet_end") or c["quiet_hours_end"]})


def enqueue(recipient, category: str, value: str, *, severity: str = "NORMAL", event_id: str = "",
            dedupe_key: str = "", origin: str = "helios", approved: bool = False, now: datetime | None = None) -> dict:
    """Plan one SMS. Every check that can refuse happens HERE and is recorded (SUPPRESSED + why), so the
    log shows every attempt. `approved=True` only for an explicit owner action (CLI, dashboard click,
    or the brain's tool after the Approve/Deny prompt)."""
    _init()
    now = now or _now()
    c = cfg()
    r = recipient if isinstance(recipient, dict) else get_recipient(recipient)
    if not r:
        raise SMSError("no such recipient — only people you added can get SMS")
    if category not in CATEGORIES:
        raise SMSError("unknown category")
    key, body, tpl_id = render(category, value)

    def record(st: str, why: str = "", due: datetime | None = None) -> dict:
        with db._conn() as cx:
            cur = cx.execute("INSERT INTO sms_messages(created, updated, due, recipient_id, category, severity, event_id, "
                             "dedupe_key, origin, template, body, status, provider, detail, log) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (_iso(now), _iso(now), _iso(due or now), r["id"], category, severity.upper(), event_id,
                              dedupe_key, origin[:40], key, body, st, c["provider"], why[:300],
                              json.dumps([{"at": _iso(now), "status": st, **({"note": why[:200]} if why else {})}])))
        conf.log("sms", f"message #{cur.lastrowid} {st} -> {r['name']} ({category}){' - ' + why if why else ''}")
        return {"id": cur.lastrowid, "status": st, "reason": why, "due": _iso(due or now)}

    if not c["enabled"] and category != "test":
        return record("SUPPRESSED", "SMS is off")
    with db._conn() as cx:
        if cx.execute("SELECT 1 FROM sms_suppressed WHERE phone=?", (r["phone"],)).fetchone() or r["opted_out"]:
            return record("SUPPRESSED", "recipient opted out")
        if dedupe_key and cx.execute("SELECT 1 FROM sms_messages WHERE recipient_id=? AND dedupe_key=? AND status NOT IN "
                                     "('CANCELLED','SUPPRESSED','FAILED') AND created>=?",
                                     (r["id"], dedupe_key, _iso(now - timedelta(days=1)))).fetchone():
            return {"id": None, "status": "DUPLICATE", "reason": "already messaged about this", "due": ""}
    if not r["enabled"]:
        return record("SUPPRESSED", "recipient disabled")
    if not r["is_owner"] and r["consent"] != "granted":
        return record("SUPPRESSED", f"no consent recorded for {r['name']} (consent: {r['consent']})")
    if category != "test" and not approved and category not in r["categories"]:
        return record("SUPPRESSED", f"{CATEGORIES[category].lower()} isn't enabled for {r['name']}")
    if _is_business_contact(r["phone"]):
        return record("SUPPRESSED", "number belongs to a business lead / prospect")
    prov = provider()
    if prov.requires_dlt(r["phone"]) and not tpl_id:
        return record("SUPPRESSED", f"DLT template not registered for '{key}' — Indian carriers block unregistered SMS "
                                    f"(add [sms.templates.{key}] dlt_template_id)")
    due = now
    if category in (c.get("batch_categories") or []) and not approved:
        due = now + timedelta(minutes=int(c.get("batch_minutes") or 30))
    critical = severity.upper() == "CRITICAL"
    if _in_quiet(r, due) and not (critical and c.get("critical_in_quiet_hours")) and category != "test":
        from .executive import _hm
        h, m = _hm(r.get("quiet_end") or c["quiet_hours_end"], "08:00")
        nd = due.replace(hour=h, minute=m, second=0, microsecond=0)
        due = nd if nd > due else nd + timedelta(days=1)
    if not r["is_owner"] and r["require_approval"] and not approved:
        return record("APPROVAL_REQUIRED", f"waiting for your approval to text {r['name']}", due)
    return record("QUEUED", "", due)


def approve(mid: int) -> dict:
    m = next((x for x in messages(500) if x["id"] == int(mid)), None)
    if not m or m["status"] != "APPROVAL_REQUIRED":
        raise SMSError("no message waiting for approval with that id")
    _update(m["id"], "QUEUED", "approved by you")
    return next(x for x in messages(500) if x["id"] == m["id"])


def cancel(mid: int | None = None) -> int:
    """Cancel one pending message (or all). Provider-side recall isn't possible once accepted."""
    n = 0
    for m in messages(1000, status_in=_PENDING):
        if mid is None or m["id"] == int(mid):
            _update(m["id"], "CANCELLED", "cancelled by you")
            n += 1
    return n


def _sent_counts(r: dict, now: datetime) -> tuple[int, int, str]:
    day, hour = _iso(now.replace(hour=0, minute=0, second=0)), _iso(now - timedelta(hours=1))
    with db._conn() as c:
        today = c.execute("SELECT COUNT(*) n FROM sms_messages WHERE recipient_id=? AND status IN "
                          "('SENT_TO_PROVIDER','DELIVERED') AND updated>=?", (r["id"], day)).fetchone()["n"]
        last_hour = c.execute("SELECT COUNT(*) n FROM sms_messages WHERE status IN ('SENT_TO_PROVIDER','DELIVERED') "
                              "AND updated>=?", (hour,)).fetchone()["n"]
        last = c.execute("SELECT MAX(updated) d FROM sms_messages WHERE recipient_id=? AND status IN "
                         "('SENT_TO_PROVIDER','DELIVERED')", (r["id"],)).fetchone()["d"]
    return today, last_hour, last or ""


def tick(now: datetime | None = None, *, prov: Provider | None = None, only: int | None = None) -> list[str]:
    """Send what's due (respecting limits, quiet hours, batching) and follow messages in flight.
    Called from executive.tick every scheduler tick. `only` = process just that message, even while
    SMS is off — used for the test message you ask for (verify first, then turn SMS on)."""
    _init()
    now = now or _now()
    c = cfg()
    out = []
    if not c["enabled"] and only is None:
        return out
    prov = prov or provider()
    ready, why = prov.validate_configuration()
    with db._conn() as cx:
        due = [_mrow(r) for r in cx.execute("SELECT * FROM sms_messages WHERE status IN ('QUEUED','RETRY_SCHEDULED') "
                                            "AND due<=? ORDER BY id", (_iso(now),))]
        live = [_mrow(r) for r in cx.execute("SELECT * FROM sms_messages WHERE status='SENT_TO_PROVIDER'")]
    if only is not None:
        due = [m for m in due if m["id"] == only]
        live = [m for m in live if m["id"] == only]
    done_ids: set[int] = set()
    for m in due:
        if m["id"] in done_ids:
            continue
        r = get_recipient(m["recipient_id"])
        if not r or r["opted_out"] or not r["enabled"]:
            _update(m["id"], "CANCELLED", "recipient opted out / disabled / removed")
            continue
        if not ready:
            _update(m["id"], "FAILED", why)
            _fallback(m, why)
            continue
        today, hour_total, last = _sent_counts(r, now)
        critical = m["severity"] == "CRITICAL"
        cap = int(r.get("max_per_day") or c["max_per_day"])
        if not critical and today >= cap:
            _update(m["id"], "SUPPRESSED", f"daily limit for {r['name']} reached ({cap})")
            continue
        if hour_total >= int(c["max_per_hour_total"]) and not critical:
            _update(m["id"], "RETRY_SCHEDULED", "hourly limit reached - waiting",
                    due=_iso(now + timedelta(minutes=15)))
            continue
        if (last and not critical and m["category"] != "test"
                and (now - datetime.fromisoformat(last)) < timedelta(minutes=int(c["min_gap_minutes"]))):
            _update(m["id"], None, due=_iso(datetime.fromisoformat(last) + timedelta(minutes=int(c["min_gap_minutes"]))))
            continue
        body = m["body"]
        if m["category"] in (c.get("batch_categories") or []):       # combine routine updates into one SMS
            siblings = [x for x in due if x["recipient_id"] == m["recipient_id"] and x["category"] == m["category"]
                        and x["id"] != m["id"] and x["id"] not in done_ids]
            if siblings:
                body = render(m["category"], f"{len(siblings) + 1} updates")[1]   # same registered template
                for x in siblings:
                    _update(x["id"], "CANCELLED", f"combined into message #{m['id']}")
                    done_ids.add(x["id"])
        tpl_id = (c["templates"].get(m["template"]) or {}).get("dlt_template_id", "")
        res = prov.send_sms(r["phone"], body, template_id=str(tpl_id))
        attempts = m["attempts"] + 1
        if res["ok"]:
            _update(m["id"], "SENT_TO_PROVIDER", f"accepted by {prov.name} ({res['raw_status'] or 'queued'}) - "
                                                  "not yet confirmed delivered", provider_id=res["id"], attempts=attempts,
                    body=body)
            out.append(f"#{m['id']} sent")
        elif not res["permanent"] and attempts < int(c["max_attempts"]):
            waits = c.get("retry_minutes") or [2, 10, 30]
            wait = int(waits[min(attempts - 1, len(waits) - 1)])
            _update(m["id"], "RETRY_SCHEDULED", f"attempt {attempts} failed: {res['detail']}", attempts=attempts,
                    due=_iso(now + timedelta(minutes=wait)))
            out.append(f"#{m['id']} retry")
        else:
            _update(m["id"], "FAILED", res["detail"] + ("" if res["permanent"] else f" (gave up after {attempts} attempts)"),
                    attempts=attempts)
            _fallback(m, res["detail"])
            out.append(f"#{m['id']} failed")
    for m in live:
        if not m["provider_id"]:
            continue
        s = prov.get_delivery_status(m["provider_id"])
        if s["state"] == "DELIVERED":
            _update(m["id"], "DELIVERED", f"provider: {s['raw']}")
            out.append(f"#{m['id']} delivered")
        elif s["state"] == "FAILED":
            _update(m["id"], "FAILED", f"provider: {s['raw']} {s.get('detail', '')}".strip())
            _fallback(m, f"not delivered ({s['raw']})")
            out.append(f"#{m['id']} failed")
        elif now - datetime.fromisoformat(m["updated"]) > timedelta(hours=24) and "unconfirmed" not in m["detail"]:
            _update(m["id"], None, detail=f"delivery unconfirmed after 24h (provider: {s['raw'] or 'no status'})")
    return out


def _fallback(m: dict, why: str) -> None:
    """When an SMS can't go out, the notification is kept: a NORMAL event on the dashboard / digest."""
    try:
        from . import executive
        executive.emit("sms", "sms_not_delivered", "NORMAL", f"SMS not delivered ({why[:100]}) - the message is on the dashboard",
                       related=f"sms:{m['id']}", dedupe_key=f"sms:{m['id']}")
    except Exception:  # pragma: no cover
        pass


# ------------------------------------------------------------------ routing (called by executive / briefing / scheduler)

def category_for_event(ev: dict) -> str | None:
    t, src, sev = ev.get("type", ""), ev.get("source", ""), ev.get("severity", "")
    if sev == "CRITICAL":
        return "critical"
    if t == "engine_unavailable" or src == "antigravity":
        return "antigravity"
    if t == "project_failing":
        return "project_alerts"
    if t == "night_finished":
        return "night_mode"
    return None


def notify_event(ev: dict, *, other_channels_failed: bool = False) -> list[dict]:
    """Executive routing hook: an event that was just delivered (CRITICAL / HIGH) — SMS each recipient
    who has that category; for the owner, also when no other remote channel reached you (fallback)."""
    if not cfg()["enabled"]:
        return []
    cat = category_for_event(ev)
    c = cfg()
    out = []
    for r in recipients(include_disabled=False):
        wants = cat and cat in r["categories"]
        fb = (r["is_owner"] and other_channels_failed and c.get("fallback")
              and ev.get("severity") in (c.get("fallback_severities") or []))
        if not (wants or fb):
            continue
        out.append(enqueue(r, cat or "critical", ev.get("summary", ""), severity=ev.get("severity", "HIGH"),
                           event_id=ev.get("id", ""), dedupe_key=f"ev:{ev.get('dedupe_key') or ev.get('id')}",
                           origin="event", approved=bool(fb and not wants)))
    return out


def notify(category: str, value: str, *, severity: str = "NORMAL", dedupe_key: str = "", origin: str = "helios") -> list[dict]:
    """Reports / reminders / task completions: every recipient with that category enabled."""
    if not cfg()["enabled"]:
        return []
    return [enqueue(r, category, value, severity=severity, dedupe_key=dedupe_key, origin=origin)
            for r in recipients(include_disabled=False) if category in r["categories"]]


def send_now(recipient, text: str, *, origin: str = "you") -> dict:
    """An explicit owner request (CLI, dashboard, or the brain's send_sms after the Approve/Deny prompt):
    a message to a recipient YOU added. Still checks consent, opt-out, DLT, quiet hours and limits."""
    return enqueue(recipient, "message", text, origin=origin, approved=True)


def send_test(recipient="me") -> dict:
    """The fixed test message — only when you ask for it (`helios sms test`, dashboard Test button)."""
    return enqueue(recipient, "test", "", origin="you (test)", approved=True)
