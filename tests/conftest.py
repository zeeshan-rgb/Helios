"""Make the repo root importable so `from helios import ...` resolves under pytest.

The tests target Helios's pure decision functions (permissions / router / voice.intent), which
import only `helios.conf` + stdlib — no audio, no app, no `claude`, so the suite runs fast and
offline. Putting the repo root on sys.path here means pytest can be invoked from anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent  # the repo root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _isolate_runtime_flags(tmp_path_factory, monkeypatch):
    """No test may touch the live app's cross-process flags in logs/ (a brain.panic() in a test
    used to leave a real panic stop behind). Tests that want a flag patch it again themselves."""
    from helios import conf
    d = tmp_path_factory.mktemp("flags")
    # Test log lines stay out of the real logs/ too: they used to show up as fake "voice errors"
    # in Helios's own health report.
    monkeypatch.setattr(conf, "LOGS_DIR", d)
    monkeypatch.setattr(conf, "ABORT_FLAG", d / "abort.flag")
    monkeypatch.setattr(conf, "YOLO_FLAG", d / "yolo.flag")
    monkeypatch.setattr(conf, "SCREEN_LOCK", d / "screen.lock")
    # ...and never the real helios.db (Night Mode / the briefing now record runs in job history).
    from helios import db, jobs, usage
    monkeypatch.setattr(db, "DB_PATH", d / "helios.db")
    monkeypatch.setattr(usage, "_file", lambda: d / "usage.jsonl")
    monkeypatch.setattr(jobs, "_first_tick", True)


@pytest.fixture(autouse=True)
def _no_real_toasts(monkeypatch):
    """Tests never pop real Windows notifications. (They used to: Night Mode / jobs / briefing
    tests fired dozens of real toasts per run onto the user's desktop, each one spawning a
    PowerShell process.) Tests that check notifying patch notify.toast themselves."""
    from helios import notify
    monkeypatch.setattr(notify, "toast", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_launch", lambda *a, **k: None, raising=False)


@pytest.fixture(autouse=True)
def _no_real_web(monkeypatch):
    """Tests never fetch real web pages or start the hidden browser through the scraper's default
    fetchers (research/leads now read their sources). Tests that exercise fetching pass their own
    fetcher/renderer, or patch research.check_source for the plain link check."""
    try:
        from helios.web import scraper
    except Exception:  # pragma: no cover
        return
    offline = lambda url, *a, **k: scraper.Page(requested_url=url, error="network disabled in tests")  # noqa: E731
    monkeypatch.setattr(scraper, "fetch_http", offline)
    monkeypatch.setattr(scraper, "fetch_rendered", offline)
    from helios.prospecting import geo
    monkeypatch.setattr(geo, "_down", {})                # Overpass "recently failed" memory is per test


@pytest.fixture(autouse=True)
def _no_real_outbound(tmp_path_factory, monkeypatch):
    """Executive reporting reaches the owner on Telegram and by phone (Twilio). No test may send a
    real Telegram message or place a real call, and reports never land in the real vault. Tests of
    those paths pass their own opener / placer / poller or patch these again."""
    from helios import calling, channels, executive, voice_note
    d = tmp_path_factory.mktemp("exec")
    monkeypatch.setattr(channels, "telegram_send", lambda *a, **k: (False, "Telegram isn't configured (tests)"))
    monkeypatch.setattr(channels, "telegram_send_voice", lambda *a, **k: (False, "Telegram isn't configured (tests)"))
    monkeypatch.setattr(channels, "telegram_find_chat", lambda *a, **k: (_ for _ in ()).throw(ValueError("no network in tests")))
    monkeypatch.setattr(voice_note, "_speaker", lambda: (_ for _ in ()).throw(RuntimeError("no real TTS in tests")))
    # Gmail: this PC has a real signed-in account, and outreach emails can now be SENT on approval —
    # no test may ever reach Google. Tests that exercise Gmail install their own fake _api / _post.
    from helios import gmail

    def _no_gmail(*a, **k):
        raise gmail.GmailError("network disabled in tests")
    monkeypatch.setattr(gmail, "_api", _no_gmail)
    monkeypatch.setattr(gmail, "_post", _no_gmail)
    # SMS: no test may reach a real gateway / provider (your phone could actually send), and the real
    # [sms] settings never decide a test's outcome. Tests that exercise SMS set cfg / a fake provider.
    from helios import sms
    monkeypatch.setattr(sms, "_http", lambda *a, **k: (0, {}, "network disabled in tests"))
    monkeypatch.setattr(sms, "cfg", lambda: {**sms._DEFAULTS, "templates": {k: {} for k in sms.TEMPLATES}})
    # The real email signature (your phone / LinkedIn) is never read by tests; they set their own.
    from helios import email_signature
    monkeypatch.setattr(email_signature, "card", lambda: {k: "" for k in email_signature.FIELDS})
    monkeypatch.setattr(calling, "place", lambda *a, **k: {"ok": False, "sid": "", "status": "failed",
                                                           "detail": "network disabled in tests"})
    monkeypatch.setattr(calling, "fetch_status", lambda *a, **k: {"status": "", "error": "network disabled in tests"})
    monkeypatch.setattr(executive, "folder", lambda: d / "Executive Reports")


@pytest.fixture(autouse=True)
def _no_real_battery(monkeypatch):
    """Night Mode skips heavy tasks on a low battery; tests must not depend on whether this laptop
    happens to be unplugged. Tests of the power rule pass their own reader."""
    from helios.night_mode import scheduler
    monkeypatch.setattr(scheduler, "battery", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_agy(monkeypatch):
    """Tests never launch the real Antigravity CLI (slow, networked, uses the user's quota).
    A scripted stand-in (a .py 'bin', see test_antigravity's fake_agy) still works."""
    from helios import agy_cli
    real = agy_cli.command

    def guarded():
        cmd = real()
        return cmd if cmd and cmd[-1].lower().endswith(".py") else []
    monkeypatch.setattr(agy_cli, "command", guarded)
