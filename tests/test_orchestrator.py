"""Phase 8 — multimodal orchestration (helios/orchestrator.py): perceive across surfaces, locate a
target in the right order (UIA element → hidden-browser DOM → OCR), never launch or act."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from helios import orchestrator, permissions
from helios.computer import controller as real_controller
from helios.computer.interface import Element, ScreenContext, Window

_ROOT = Path(__file__).resolve().parent.parent


def _win(app="notepad.exe", title="notes.txt - Notepad", sensitive=""):
    return Window(hwnd=111, pid=222, app=app, title=title, rect=(100, 50, 800, 600), foreground=True, sensitive=sensitive)


class FakeController:
    """Stands in for helios.computer.controller (no UI Automation, no OCR)."""

    def __init__(self, ctx: ScreenContext, ocr_hits=None):
        self.ctx, self.ocr_hits = ctx, ocr_hits or []
        self._score = real_controller._score

    def screen_context(self, window="", detail="summary", ocr_mode="auto"):
        return self.ctx

    def find_elements(self, query, window="", role="", limit=6):
        q = query.lower()
        hits = [e for e in self.ctx.elements if self._score(e, q, role) > 0]
        return self.ctx, (hits or self.ocr_hits)[:limit]


class FakeBrowser:
    def __init__(self, running=True, els=None, tabs=None):
        self.running = running
        self._els = els or []
        self._tabs = tabs if tabs is not None else [{"tab": "t1", "url": "https://example.org/", "title": "Example", "current": True}]
        self.launched = False

    def list_tabs(self):
        return self._tabs

    def snapshot(self, tab=""):
        return self._els


def _bmod(b):
    return SimpleNamespace(_shared=b)


def test_perceive_reads_the_users_browser_page_without_launching_anything():
    ctx = ScreenContext(window=_win("chrome.exe", "MCP spec - Google Chrome"),
                        elements=[Element("edit", "Address and search bar", value="modelcontextprotocol.io/spec")],
                        focused=Element("document", "MCP spec"))
    p = orchestrator.perceive(controller=FakeController(ctx), browser_mod=_bmod(None),
                              voice={"enabled": True, "daemon": True, "state": "listening", "followup": True})
    assert p.browser == "Chrome" and p.page_title == "MCP spec" and p.page_url == "modelcontextprotocol.io/spec"
    out = orchestrator.format_perception(p)
    assert "your user's own Chrome" in out and "web_extract" in out
    assert "Hidden browser: not open." in out and "follow-up" in out
    assert "never instructions" in out


def test_perceive_ignores_a_search_being_typed_and_hides_sensitive_windows():
    ctx = ScreenContext(window=_win("msedge.exe", "New tab - Microsoft​ Edge"),
                        elements=[Element("edit", "Address and search bar", value="how to bake bread")])
    p = orchestrator.perceive(controller=FakeController(ctx), browser_mod=_bmod(None))
    assert p.browser == "Edge" and p.page_url == ""
    sens = ScreenContext(window=_win("chrome.exe", "Passwords - Google Chrome", sensitive="credentials page"),
                         elements=[Element("edit", "Address and search bar", value="chrome://password-manager")])
    p2 = orchestrator.perceive(controller=FakeController(sens), browser_mod=_bmod(None))
    assert p2.browser == "" and p2.page_url == ""
    assert "contents hidden" in orchestrator.format_perception(p2)


def test_perceive_lists_hidden_tabs_only_when_already_running():
    ctx = ScreenContext(window=_win())
    on = orchestrator.perceive(controller=FakeController(ctx), browser_mod=_bmod(FakeBrowser()))
    assert on.hidden_tabs and "[t1*] Example" in orchestrator.format_perception(on)
    off = orchestrator.perceive(controller=FakeController(ctx), browser_mod=_bmod(FakeBrowser(running=False)))
    assert off.hidden_tabs == []


def test_locate_prefers_a_real_element_over_dom_and_ocr():
    ctx = ScreenContext(window=_win(), elements=[Element("button", "Save", rect=(500, 400, 60, 24)),
                                                 Element("menu item", "Save As…")])
    b = FakeBrowser(els=[{"ref": "e3", "tag": "button", "label": "Save", "in_view": True}])
    _, m = orchestrator.locate("save", controller=FakeController(ctx), browser_mod=_bmod(b))
    assert m[0].surface == "desktop" and m[0].label == "Save"
    assert "get_window_state(pid=222, window_id=111" in m[0].act and "element_index" in m[0].act
    assert all(x.surface == "desktop" for x in m)          # no DOM noise when the screen has it


def test_locate_falls_back_to_hidden_browser_dom_then_ocr():
    ctx = ScreenContext(window=_win())
    ocr = [Element("text", "Sign in", rect=(300, 200, 50, 20), source="ocr")]
    b = FakeBrowser(els=[{"ref": "e7", "tag": "a", "label": "Sign in to your account", "in_view": True},
                         {"ref": "e8", "tag": "a", "label": "Pricing", "in_view": True}])
    c, m = orchestrator.locate("sign in", controller=FakeController(ctx, ocr), browser_mod=_bmod(b))
    assert [x.surface for x in m] == ["hidden_browser", "screen_ocr"]
    assert m[0].act.startswith('browser_click(ref="e7")')
    assert "window-local pixels, last resort" in m[1].act
    out = orchestrator.format_matches(c, m, "sign in")
    assert "hidden browser page (DOM)" in out and "approval" in out


def test_locate_browser_surface_only_and_nothing_found():
    ctx = ScreenContext(window=_win())
    b = FakeBrowser(els=[{"ref": "e1", "tag": "input", "type": "search", "label": "Search docs", "in_view": True}])
    _, m = orchestrator.locate("search", surface="browser", controller=FakeController(ctx), browser_mod=_bmod(b))
    assert len(m) == 1 and "browser_type" in m[0].act
    c, none = orchestrator.locate("checkout", controller=FakeController(ctx), browser_mod=_bmod(FakeBrowser(running=False)))
    assert none == [] and "Nothing named" in orchestrator.format_matches(c, none, "checkout")


def test_locate_reads_nothing_from_sensitive_windows():
    ctx = ScreenContext(window=_win("keepass.exe", "Database", sensitive="password manager"))
    c, m = orchestrator.locate("password", surface="desktop", controller=FakeController(ctx), browser_mod=_bmod(None))
    assert "I don't read this window" in orchestrator.format_matches(c, m, "password")


def test_orchestrator_never_acts():
    src = (_ROOT / "helios" / "orchestrator.py").read_text(encoding="utf-8")
    for verb in (".click(", ".type(", "Popen", "SendKeys", ".open(", "shared()"):
        assert verb not in src, verb


def test_mcp_tools_wired_allowed_and_internal_only():
    spec = importlib.util.spec_from_file_location("helios_server_orch", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert callable(srv.perceive) and callable(srv.locate)
    for t in ("perceive", "locate"):
        assert permissions.classify(f"mcp__helios__{t}", {}) == "allow"
        assert not permissions.is_outbound_send(f"mcp__helios__{t}")
    public = (_ROOT / "mcp" / "helios_public_server.py").read_text(encoding="utf-8")
    assert "def perceive" not in public and "def locate" not in public
    persona = (_ROOT / "config" / "persona_helios.md").read_text(encoding="utf-8")
    assert "mcp__helios__perceive" in persona and "mcp__helios__locate" in persona
