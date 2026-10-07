"""Hidden browser (helios/web): only public http(s) is loaded (every request — redirects and DNS
included), risky clicks / Enter-submits need confirm=true which goes to the user (never YOLO,
never background agents), password/card fields are never typed into, the user's profile is never
used, and the browser runs on its own thread and closes when idle. A fake Playwright is used; set
HELIOS_LIVE_BROWSER=1 to also run one test against the real headless Edge."""

from __future__ import annotations

import importlib.util
import os
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios import permissions
from helios.web import browser as bmod
from helios.web import policy
from helios.web.browser import Browser, BrowserError, NeedsConfirm

_ROOT = Path(__file__).resolve().parent.parent


def resolver(ip):
    return lambda host, port: [(socket.AF_INET, 0, 0, "", (ip, 0))]


# ------------------------------------------------------------------ URL policy

@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a?b=1", True), ("http://news.ycombinator.com", True),
    ("about:blank", True), ("data:text/html,<p>x</p>", True),
    ("http://127.0.0.1:8769/health", False), ("http://localhost/x", False),
    ("http://192.168.1.1/", False), ("http://10.0.0.5/admin", False), ("http://[::1]/", False),
    ("http://169.254.169.254/latest/meta-data", False), ("http://printer.local/", False),
    ("file:///C:/Windows/win.ini", False), ("javascript:alert(1)", False), ("ftp://x.com/f", False),
    ("about:config", False), ("chrome://settings", False),
])
def test_check_url(url, ok):
    assert (policy.check_url(url, resolver=resolver("93.184.216.34")) == "") is ok


def test_dns_that_resolves_inside_is_blocked():
    policy._dns.clear()
    assert "resolves to an internal" in policy.check_url("https://rebind.example.net/", resolver=resolver("127.0.0.1"))
    policy._dns.clear()
    assert "resolves to an internal" in policy.check_url("https://corp.example.net/", resolver=resolver("10.1.2.3"))
    policy._dns.clear()


@pytest.mark.parametrize("info,risky", [
    ({"tag": "button", "type": "submit", "text": "Go", "in_form": True, "method": "post"}, True),
    ({"tag": "input", "type": "submit", "text": "Search", "in_form": True, "method": "get"}, False),
    ({"tag": "a", "text": "Learn more", "href": "https://iana.org/x"}, False),
    ({"tag": "a", "text": "Email us", "href": "mailto:a@b.com"}, True),
    ({"tag": "button", "type": "button", "text": "Buy now"}, True),
    ({"tag": "button", "type": "button", "text": "Accept all cookies"}, True),
    ({"tag": "div", "role": "button", "text": "Send"}, True),
    ({"tag": "button", "type": "button", "text": "Show more replies"}, False),  # expanding a thread is reading
    ({"tag": "button", "type": "button", "text": "Reply"}, True),
    ({"tag": "button", "type": "button", "text": "Next page"}, False),
    ({}, False),
])
def test_risky_action(info, risky):
    assert bool(policy.risky_action(info)) is risky


def test_enter_in_post_form_is_a_submit():
    assert policy.risky_action({"tag": "input", "type": "text", "in_form": True, "method": "post"},
                               pressing_enter=True) == "submits a form"
    assert policy.risky_action({"tag": "input", "type": "search", "in_form": True, "method": "get"},
                               pressing_enter=True) == ""


@pytest.mark.parametrize("info,cred", [
    ({"type": "password"}, True), ({"type": "text", "autocomplete": "cc-number"}, True),
    ({"type": "text", "autocomplete": "one-time-code"}, True), ({"type": "email"}, False), ({}, False),
])
def test_credential_fields(info, cred):
    assert policy.credential_field(info) is cred


# ------------------------------------------------------------------ fake Playwright

class FakeLoc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.first = self

    def click(self):
        self.page.actions.append(("click", self.sel))
        if self.page.on_click_url:
            self.page.url = self.page.on_click_url

    def fill(self, text):
        self.page.actions.append(("fill", self.sel, text))

    def press(self, key):
        self.page.actions.append(("press", self.sel, key))

    def aria_snapshot(self):
        return "- heading \"Hello\""

    def inner_html(self):
        return "<b>x</b>"


class FakePage:
    def __init__(self, elements):
        self.url, self._title, self.elements = "about:blank", "", elements
        self.actions, self.closed, self.on_click_url = [], False, ""

    def goto(self, url, wait_until=None):
        if "fail" in url:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED at " + url)
        self.url, self._title = url, "Page " + url.rsplit("/", 1)[-1]
        return SimpleNamespace(status=200, request=SimpleNamespace(url=url, redirected_from=SimpleNamespace(
            url="http://short.example/x", redirected_from=None)))

    def title(self):
        return self._title

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def wait_for_load_state(self, *a, **k):
        pass

    def evaluate(self, js, arg=None):
        if "data-helios-ref\"]`" in js or "el.form" in js:      # element info
            return self.elements.get(arg)
        if "querySelectorAll(sel)" in js:                      # snapshot
            return [{"ref": r, "tag": i["tag"], "type": i.get("type", ""), "role": "", "label": i.get("text", ""),
                     "href": i.get("href", ""), "in_view": True, "disabled": False} for r, i in self.elements.items()]
        return {"title": self._title, "headings": []}

    def locator(self, sel):
        return FakeLoc(self, sel)

    def inner_text(self, sel):
        return "Hello world " * 5

    def content(self):
        return "<html></html>"


class FakeCtx:
    def __init__(self, elements):
        self.elements, self.pages, self.routes, self.closed = elements, [], [], False

    def set_default_timeout(self, ms):
        pass

    def route(self, pattern, handler):
        self.routes.append(handler)

    def new_page(self):
        p = FakePage(self.elements)
        self.pages.append(p)
        return p

    def close(self):
        self.closed = True


class FakeBrowserObj:
    def __init__(self, elements):
        self.ctx, self.closed, self.elements = None, False, elements

    def new_context(self, **kw):
        self.ctx_kw = kw
        self.ctx = FakeCtx(self.elements)
        return self.ctx

    def close(self):
        self.closed = True


def fake_pw(elements=None, fail_channels=()):
    launched = []

    class Chromium:
        def launch(self, **kw):
            ch = kw.get("channel", "chromium")
            launched.append((ch, kw))
            if ch in fail_channels:
                raise RuntimeError(f"{ch} not installed")
            return FakeBrowserObj(elements or {})
    pw = SimpleNamespace(chromium=Chromium(), stop=lambda: None, launched=launched)
    return pw


FORM = {
    "e1": {"tag": "input", "type": "text", "text": "Your name", "in_form": True, "method": "post"},
    "e2": {"tag": "input", "type": "password", "text": "", "in_form": True, "method": "post"},
    "e3": {"tag": "button", "type": "submit", "text": "Send message", "in_form": True, "method": "post"},
    "e4": {"tag": "a", "text": "Next page", "href": "https://example.com/2"},
}


@pytest.fixture
def br(monkeypatch, tmp_path):
    monkeypatch.setattr(bmod.conf, "DATA_DIR", tmp_path)
    monkeypatch.setattr(policy, "_resolves_internal", lambda host, resolver=None: False)
    pw = fake_pw(FORM)
    b = Browser(launcher=lambda: pw, idle_sec=999)
    b.pw = pw
    yield b
    b._q.put(None)


def test_open_uses_installed_edge_fresh_profile_and_reports_redirects(br):
    info = br.open("https://example.com/page")
    assert info["status"] == 200 and info["redirects"] == ["http://short.example/x"]
    ch, kw = br.pw.launched[0]
    assert ch == "msedge" and kw["headless"] is True and "--disable-extensions" in kw["args"]
    ctx_kw = br._browser.ctx_kw
    assert "user_data_dir" not in ctx_kw and ctx_kw["service_workers"] == "block"
    assert br._ctx.routes, "every request must go through the policy router"


def test_falls_back_to_the_next_installed_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "_resolves_internal", lambda host, resolver=None: False)
    pw = fake_pw(fail_channels=("msedge",))
    b = Browser(launcher=lambda: pw)
    b.open("https://example.com")
    assert [c for c, _ in pw.launched] == ["msedge", "chrome"] and b.channel == "chrome"
    b._q.put(None)
    pw2 = fake_pw(fail_channels=("msedge", "chrome", "chromium"))
    b2 = Browser(launcher=lambda: pw2)
    with pytest.raises(BrowserError, match="no browser could start"):
        b2.open("https://example.com")
    b2._q.put(None)


def test_internal_urls_refused_before_launch(br):
    with pytest.raises(BrowserError, match="SSRF"):
        br.open("http://127.0.0.1:8769/token")
    assert br.pw.launched == []                                          # nothing started


def test_router_blocks_internal_and_media(br, monkeypatch):
    br.open("https://example.com")
    handler = br._ctx.routes[0]
    done = []

    def route(url, rtype="document"):
        r = SimpleNamespace(request=SimpleNamespace(url=url, resource_type=rtype),
                            abort=lambda *a: done.append(("abort", url)),
                            continue_=lambda: done.append(("go", url)))
        handler(r)
    monkeypatch.setattr(policy, "_resolves_internal", lambda host, resolver=None: host == "evil-rebind.example")
    route("http://169.254.169.254/latest/meta-data")
    route("https://evil-rebind.example/x")
    route("https://cdn.example.com/a.png", "image")
    route("https://example.com/app.js", "script")
    assert done == [("abort", "http://169.254.169.254/latest/meta-data"), ("abort", "https://evil-rebind.example/x"),
                    ("abort", "https://cdn.example.com/a.png"), ("go", "https://example.com/app.js")]
    assert len(br.blocked) == 2


def test_risky_click_needs_confirm_and_is_logged(br):
    br.open("https://example.com/contact")
    with pytest.raises(NeedsConfirm, match="submits a form"):
        br.click("e3")
    page = br.tabs[br.current]
    assert page.actions == []                                            # nothing was clicked
    br.click("e3", confirm=True)
    assert page.actions == [("click", '[data-helios-ref="e3"]')]
    br.click("e4")                                                       # ordinary link: no confirm
    with pytest.raises(BrowserError, match="fresh snapshot"):
        br.click("e99")
    with pytest.raises(BrowserError, match="ref must look like"):
        br.click("body > form")


def test_typing_rules(br):
    br.open("https://example.com/contact")
    page = br.tabs[br.current]
    with pytest.raises(BrowserError, match="never types into password"):
        br.type("e2", "hunter2", confirm=True)
    br.type("e1", "Helios")
    with pytest.raises(NeedsConfirm, match="submits a form"):
        br.type("e1", "Helios", submit=True)
    assert ("press", '[data-helios-ref="e1"]', "Enter") not in page.actions
    br.type("e1", "Helios", submit=True, confirm=True)
    assert page.actions[-1] == ("press", '[data-helios-ref="e1"]', "Enter")


def test_tabs_and_cap(br):
    br.max_tabs = 2
    br.open("https://example.com/1")
    br.open("https://example.com/2", new_tab=True)
    br.open("https://example.com/3", new_tab=True)
    tabs = br.list_tabs()
    assert len(tabs) == 2 and tabs[-1]["current"] and tabs[-1]["url"].endswith("/3")
    br.switch(tabs[0]["tab"])
    assert br.current == tabs[0]["tab"]
    br.close_tab()
    assert len(br.list_tabs()) == 1
    with pytest.raises(BrowserError):
        br.switch("t99")


def test_load_failure_is_a_clean_error(br):
    with pytest.raises(BrowserError, match="couldn't load"):
        br.open("https://fail.example.com/")


def test_runs_on_its_own_thread_and_closes_when_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "_resolves_internal", lambda host, resolver=None: False)
    seen = []
    pw = fake_pw()
    b = Browser(launcher=lambda: pw, idle_sec=0.01)
    orig = b._ensure
    b._ensure = lambda: (seen.append(threading.current_thread().name), orig())[1]
    b.open("https://example.com")
    assert seen and seen[0] == "helios-browser" and threading.current_thread().name != "helios-browser"
    deadline = time.time() + 8
    while b.running and time.time() < deadline:
        time.sleep(0.2)
    assert not b.running                                                 # idle shutdown
    b._q.put(None)


def test_screenshots_are_capped(br, monkeypatch):
    br.open("https://example.com")
    page = br.tabs[br.current]
    page.screenshot = lambda path, full_page=False: Path(path).write_bytes(b"png")
    names = []
    for i in range(8):
        monkeypatch.setattr(bmod.time, "strftime", lambda fmt, i=i: f"2026-{i:02d}")
        names.append(br.screenshot().name)
    left = sorted(p.name for p in (bmod.data_dir() / "screenshots").glob("shot-*.png"))
    assert len(left) == 5 and left[-1] == names[-1]


# ------------------------------------------------------------------ gate wiring

def _hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("pretooluse_browser", _ROOT / "hooks" / "pretooluse.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(hook.conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(hook.conf, "ABORT_FLAG", tmp_path / "abort.flag")
    asked = []

    def ask(req, timeout=None):
        import json
        asked.append(json.loads(req.data)["tool"])
        raise OSError("no app in tests")
    monkeypatch.setattr(hook.urllib.request, "urlopen", ask)
    return hook, asked


def test_gate_for_browser_tools(tmp_path, monkeypatch):
    c = permissions.classify
    for t in ("browser_open", "browser_read", "browser_snapshot", "browser_scroll", "browser_tabs",
              "browser_select", "browser_screenshot", "browser_close"):
        assert c(f"mcp__helios__{t}", {}) == "allow"
    assert c("mcp__helios__browser_click", {"ref": "e1"}) == "allow"
    assert c("mcp__helios__browser_click", {"ref": "e1", "confirm": True}) == "ask"
    assert c("mcp__helios__browser_type", {"ref": "e1", "confirm": "true"}) == "ask"
    assert c("mcp__helios__browser_download", {"ref": "e1"}) == "ask"
    hook, asked = _hook(tmp_path, monkeypatch)
    (tmp_path / "yolo.flag").write_text("on")
    assert hook.decide("mcp__helios__browser_click", {"ref": "e3"})[0] == "allow"        # plain click
    assert hook.decide("mcp__helios__browser_click", {"ref": "e3", "confirm": True})[0] == "deny"
    assert asked == ["mcp__helios__browser_click"]                       # YOLO still asked the user
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    d, why = hook.decide("mcp__helios__browser_click", {"ref": "e3", "confirm": True})
    assert d == "deny" and "background agent" in why


def test_mcp_tools_registered_and_not_public():
    spec = importlib.util.spec_from_file_location("helios_server_browser", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    for t in ("browser_open", "browser_read", "browser_snapshot", "browser_click", "browser_type",
              "browser_select", "browser_scroll", "browser_tabs", "browser_screenshot", "browser_download",
              "browser_close"):
        assert callable(getattr(srv, t))
    public = (_ROOT / "mcp" / "helios_public_server.py").read_text(encoding="utf-8")
    assert "browser_" not in public
    main = (_ROOT / "mcp" / "helios_server.py").read_text(encoding="utf-8")
    main = main[main.index('if __name__ == "__main__":'):]
    assert main.index("playwright.sync_api") < main.index("mcp.run()")


def test_needs_confirm_message_tells_the_brain_not_to_self_confirm():
    spec = importlib.util.spec_from_file_location("helios_server_browser2", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)

    def boom():
        raise NeedsConfirm("e3 submits a form — this could act on someone's behalf")
    out = srv._browser_call(boom)
    assert out.startswith("NOT DONE") and "Never on your own" in out


@pytest.mark.skipif(not os.environ.get("HELIOS_LIVE_BROWSER"), reason="set HELIOS_LIVE_BROWSER=1 for real Edge")
def test_live_headless_edge():
    b = Browser()
    try:
        form = ("<form method=post action='https://example.com/x'><input name=n placeholder=Name>"
                "<button type=submit>Send</button></form>")
        b.open("data:text/html," + urllib.parse.quote(form))
        refs = {e["label"]: e["ref"] for e in b.snapshot()}
        with pytest.raises(NeedsConfirm):
            b.click(refs["Send"])
        with pytest.raises(BrowserError, match="SSRF"):
            b.open("http://127.0.0.1:1/")
    finally:
        b.close()
