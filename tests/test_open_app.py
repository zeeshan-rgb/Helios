"""helios.open_app — the website-vs-app split (ported from Helios-main with the fix for
'open YouTube typed into the Start search bar'). Websites -> default browser; apps ->
PATH / protocol / Start-menu tiers. All launchers mocked; no real windows open."""
import pytest

from helios import open_app as oa


@pytest.fixture
def no_launch(monkeypatch):
    """Any real launch attempt fails the test."""
    def boom(*a, **k):
        raise AssertionError("app launcher must not run for a website")
    monkeypatch.setattr(oa, "_launch_windows", boom)


def test_named_site_opens_browser(no_launch, monkeypatch):
    opened = []
    monkeypatch.setattr(oa.webbrowser, "open", lambda u: opened.append(u) or True)
    out = oa.open_app("YouTube")
    assert opened == ["https://youtube.com"]
    assert "browser" in out


def test_bare_domain_opens_browser(no_launch, monkeypatch):
    opened = []
    monkeypatch.setattr(oa.webbrowser, "open", lambda u: opened.append(u) or True)
    oa.open_app("wikipedia.org")
    assert opened == ["https://wikipedia.org"]


def test_full_url_passes_through(no_launch, monkeypatch):
    opened = []
    monkeypatch.setattr(oa.webbrowser, "open", lambda u: opened.append(u) or True)
    oa.open_app("https://news.ycombinator.com/item?id=1")
    assert opened == ["https://news.ycombinator.com/item?id=1"]


def test_app_name_skips_website_branch(monkeypatch):
    launched = []
    monkeypatch.setattr(oa, "_launch_windows", lambda n: launched.append(n) or True)
    monkeypatch.setattr(oa.webbrowser, "open",
                        lambda u: (_ for _ in ()).throw(AssertionError("browser must not open")))
    out = oa.open_app("Discord")
    assert launched == ["Discord"]                # alias map hit
    assert "Opened Discord" in out


def test_desktop_app_names_stay_apps(monkeypatch):
    # spotify/whatsapp/discord mean the INSTALLED apps, never web fallbacks
    launched = []
    monkeypatch.setattr(oa, "_launch_windows", lambda n: launched.append(n) or True)
    for name in ("Spotify", "WhatsApp", "notepad"):
        oa.open_app(name)
    assert launched == ["Spotify", "WhatsApp", "notepad.exe"]


def test_empty_name():
    assert "No application name" in oa.open_app("  ")


def test_start_menu_typing_bails_on_panic(tmp_path, monkeypatch):
    # tier 3 must never emit synthetic keystrokes after a Stop
    flag = tmp_path / "abort.flag"
    flag.write_text("x")
    monkeypatch.setattr(oa.conf, "ABORT_FLAG", flag)
    monkeypatch.setattr(oa.shutil, "which", lambda *a: None)     # tier 1 misses
    out = oa.open_app("SomeStoreApp")
    assert "Could not confirm" in out                            # bailed, no typing
