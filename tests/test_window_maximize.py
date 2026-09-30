"""Dashboard maximize/restore: the /window/maximize route (auth-gated, returns the new state),
the app-side toggle (falls back to pywebview's own maximize/restore), and the title-bar button."""

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from helios import conf, permissions, server

_UI = Path(__file__).resolve().parent.parent / "helios" / "ui"


class _StubBrain:
    def busy(self):
        return False


@pytest.fixture
def live(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(conf, "PORT", port)
    monkeypatch.setattr(conf, "BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(conf, "AUTH_TOKEN_FILE", tmp_path / ".session_token")
    monkeypatch.setattr(conf, "_auth_token_cache", None)
    state = {"max": False, "calls": 0}

    def toggle():
        state["calls"] += 1
        state["max"] = not state["max"]
        return state["max"]
    hub = server.Hub()
    httpd = server.start(_StubBrain(), permissions.PendingRegistry(hub.publish), hub, maximize=toggle)
    yield {"port": port, "token": conf.auth_token(), "state": state}
    httpd.shutdown()
    httpd.server_close()


def _post(port, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=b"")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def test_route_toggles_and_reports_state(live):
    assert _post(live["port"], "/window/maximize", live["token"]) == (200, {"ok": True, "maximized": True})
    assert _post(live["port"], "/window/maximize", live["token"])[1]["maximized"] is False
    assert live["state"]["calls"] == 2


def test_route_needs_the_session_token(live):
    code, _ = _post(live["port"], "/window/maximize")
    assert code in (401, 403) and live["state"]["calls"] == 0


def test_app_toggle_falls_back_to_pywebview(monkeypatch):
    from helios import app
    calls = []

    class FakeWin:
        uid = "not-a-real-window"

        def maximize(self):
            calls.append("max")

        def restore(self):
            calls.append("restore")
    monkeypatch.setitem(app._state, "window", FakeWin())
    monkeypatch.setitem(app._state, "maximized", False)
    monkeypatch.setattr(app, "_dispatch", lambda op: op())
    assert app._toggle_maximize_dashboard() is True
    assert app._toggle_maximize_dashboard() is False
    assert calls == ["max", "restore"]


def test_title_bar_has_the_button():
    html = (_UI / "index.html").read_text(encoding="utf-8")
    js = (_UI / "app.js").read_text(encoding="utf-8")
    assert 'id="maxbtn"' in html and html.index('id="minbtn"') < html.index('id="maxbtn"') < html.index('id="closebtn"')
    assert '"/window/maximize"' in js and 'addEventListener("dblclick"' in js
