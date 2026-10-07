"""Hidden browser (Playwright) for pages that need JavaScript, and for multi-step browsing.

* Uses the installed Edge (or Chrome) headless — no Playwright browser download — with a fresh,
  throwaway profile: never the user's cookies, logins or saved passwords.
* Every request the page makes goes through policy.check_url (no internal addresses, even via
  redirects or DNS). Images/media/fonts are skipped by default (faster, lighter).
* Playwright's sync API can't run inside an asyncio loop (the MCP server has one), so one worker
  thread owns it; every public method runs there. The browser closes itself when idle.
* Interactive elements get refs (e1, e2, ...) from snapshot(); actions take a ref. Risky actions
  (submit / send / buy / sign up ...) raise NeedsConfirm unless confirm=True — the MCP layer maps
  confirm=True to the user's Approve/Deny prompt.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path

from .. import conf
from . import policy

_SNAPSHOT_JS = r"""
(limit) => {
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],' +
    '[role=option],[role=switch],[role=combobox],[contenteditable=""],[contenteditable=true]';
  document.querySelectorAll('[data-helios-ref]').forEach(e => e.removeAttribute('data-helios-ref'));
  const out = []; let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden' || st.display === 'none') continue;
    const ref = 'e' + (++i);
    el.setAttribute('data-helios-ref', ref);
    const tag = el.tagName.toLowerCase(), type = (el.type || '').toLowerCase();
    const isBtn = tag === 'button' || ['submit', 'button', 'reset'].includes(type);
    let label = el.getAttribute('aria-label') || (tag === 'input' || tag === 'textarea' || tag === 'select'
      ? (el.labels && el.labels[0] ? el.labels[0].innerText : '') || el.placeholder || el.name || ''
      : el.innerText) || (isBtn ? el.value : '') || el.title || '';
    out.push({ref, tag, type, role: el.getAttribute('role') || '', label: label.trim().replace(/\s+/g, ' ').slice(0, 100),
              href: tag === 'a' ? el.href : '', in_view: r.top < innerHeight && r.bottom > 0,
              disabled: !!el.disabled});
    if (out.length >= limit) break;
  }
  return out;
}
"""

_INFO_JS = r"""
(ref) => {
  const el = document.querySelector(`[data-helios-ref="${ref}"]`);
  if (!el) return null;
  const f = el.form || el.closest('form');
  return {tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
          role: el.getAttribute('role') || '', autocomplete: el.getAttribute('autocomplete') || '',
          text: (el.innerText || (['submit','button'].includes(el.type) ? el.value : '') ||
                 el.getAttribute('aria-label') || '').trim().slice(0, 100),
          name: el.getAttribute('name') || '', href: el.href || '',
          in_form: !!f, method: f ? (f.getAttribute('method') || 'get').toLowerCase() : ''};
}
"""

_META_JS = r"""
() => {
  const m = n => { const e = document.querySelector(`meta[name="${n}"],meta[property="${n}"]`); return e ? e.content : ''; };
  const ld = [];
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { ld.push(JSON.parse(s.textContent)); } catch (e) {}
  }
  const canon = document.querySelector('link[rel=canonical]');
  return {title: document.title, lang: document.documentElement.lang || '',
          description: m('description') || m('og:description'), site_name: m('og:site_name'),
          author: m('author') || m('article:author'), published: m('article:published_time') || m('date'),
          og_type: m('og:type'), image: m('og:image'), canonical: canon ? canon.href : '',
          json_ld: ld.slice(0, 5),
          headings: [...document.querySelectorAll('h1,h2,h3')].slice(0, 40)
                      .map(h => h.tagName + ': ' + h.innerText.trim().slice(0, 120)).filter(x => x.length > 4)};
}
"""

_LINKS_JS = r"""
(limit) => [...document.querySelectorAll('a[href]')].map(a => ({text: a.innerText.trim().replace(/\s+/g,' ').slice(0, 120), href: a.href}))
  .filter(l => l.href.startsWith('http')).slice(0, limit)
"""

_LAUNCH_ARGS = ["--disable-extensions", "--disable-background-networking", "--disable-component-update",
                "--disable-default-apps", "--disable-sync", "--no-first-run", "--disable-gpu",
                "--renderer-process-limit=2", "--mute-audio",
                "--disable-features=Translate,OptimizationHints,MediaRouter,EdgeCollections,"
                "msEdgeShopping,msEdgeDiscoverHub,AutofillServerCommunication"]


class BrowserError(Exception):
    pass


class NeedsConfirm(BrowserError):
    """A risky action (submit/send/buy...) that the user must approve: retry with confirm=True."""


def cfg() -> dict:
    v = conf.SETTINGS.get("browser")
    return v if isinstance(v, dict) else {}


def data_dir() -> Path:
    d = conf.DATA_DIR / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Browser:
    def __init__(self, *, channels=None, block_media=None, idle_sec=None, launcher=None):
        c = cfg()
        self.channels = list(channels or c.get("channels") or ["msedge", "chrome", "chromium"])
        self.block_media = c.get("block_media", True) if block_media is None else block_media
        self.idle_sec = float(idle_sec or c.get("idle_close_sec", 180))
        self.max_tabs = int(c.get("max_tabs", 6))
        self.timeout_ms = int(float(c.get("page_timeout_sec", 25)) * 1000)
        self._launcher = launcher            # tests inject a fake Playwright
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pw = self._pwm = self._browser = self._ctx = None
        self.tabs: dict[str, object] = {}
        self.current = ""
        self._n = 0
        self._last = time.monotonic()
        self.channel = ""
        self.blocked: list[str] = []         # recent blocked requests (for the reply)

    # ------------------------------------------------------------------ worker thread
    def _loop(self):
        while True:
            try:
                job = self._q.get(timeout=5)
            except queue.Empty:
                if self._browser is not None and time.monotonic() - self._last > self.idle_sec:
                    self._shutdown()
                continue
            if job is None:
                self._shutdown()
                return
            fn, box, done = job
            try:
                box["ok"] = fn()
            except BaseException as e:      # noqa: BLE001 — re-raised in the caller
                box["err"] = e
            self._last = time.monotonic()
            done.set()

    def _run(self, fn, timeout: float = 60):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="helios-browser", daemon=True)
            self._thread.start()
        box, done = {}, threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(timeout):
            raise BrowserError("the browser didn't respond in time")
        if "err" in box:
            raise box["err"]
        return box.get("ok")

    # ------------------------------------------------------------------ lifecycle (worker thread)
    def _ensure(self):
        if self._ctx is not None:
            return
        if self._launcher is not None:
            self._pw = self._launcher()
        else:
            from playwright.sync_api import sync_playwright
            self._pwm = sync_playwright()
            self._pw = self._pwm.start()
        last = None
        for ch in self.channels:
            try:
                kw = {"headless": True, "args": _LAUNCH_ARGS}
                if ch != "chromium":
                    kw["channel"] = ch
                self._browser = self._pw.chromium.launch(**kw)
                self.channel = ch
                break
            except Exception as e:
                last = e
        if self._browser is None:
            raise BrowserError(f"no browser could start (tried {', '.join(self.channels)}): {last}")
        self._ctx = self._browser.new_context(
            viewport={"width": 1366, "height": 900}, locale="en-US", accept_downloads=True,
            service_workers="block")
        self._ctx.set_default_timeout(self.timeout_ms)
        self._ctx.route("**/*", self._route)
        self._lower_priority()
        conf.log("browser", f"hidden browser started ({self.channel})")

    def _route(self, route):
        req = route.request
        why = policy.check_url(req.url)
        if why:
            self.blocked = (self.blocked + [f"{req.url[:120]} — {why}"])[-10:]
            conf.log("browser", f"blocked {req.url[:200]}: {why}")
            return route.abort("blockedbyclient")
        if self.block_media and req.resource_type in ("image", "media", "font"):
            return route.abort()
        return route.continue_()

    def _lower_priority(self):
        try:
            import psutil
            for p in psutil.Process().children(recursive=True):
                if p.name().lower() in ("msedge.exe", "chrome.exe", "node.exe", "chromium.exe"):
                    p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass

    def _shutdown(self):
        for obj in (self._ctx, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pwm is not None:
                self._pwm.__exit__(None, None, None)
            elif self._pw is not None and hasattr(self._pw, "stop"):
                self._pw.stop()
        except Exception:
            pass
        if self._browser is not None:
            conf.log("browser", "hidden browser closed")
        self._pw = self._pwm = self._browser = self._ctx = None
        self.tabs, self.current = {}, ""

    def close(self):
        if self._thread is not None and self._thread.is_alive():
            self._run(self._shutdown, timeout=30)

    # ------------------------------------------------------------------ tabs (worker thread)
    def _page(self, tab: str = ""):
        self._ensure()
        tab = tab or self.current
        if tab and tab in self.tabs:
            p = self.tabs[tab]
            if not p.is_closed():
                return tab, p
            del self.tabs[tab]
        return self._new_tab()

    def _new_tab(self):
        self._ensure()                       # open(new_tab=True) can be the very first call
        if len(self.tabs) >= self.max_tabs:
            oldest = next(iter(self.tabs))
            try:
                self.tabs.pop(oldest).close()
            except Exception:
                pass
        self._n += 1
        tab = f"t{self._n}"
        self.tabs[tab] = self._ctx.new_page()
        self.current = tab
        return tab, self.tabs[tab]

    def _info(self, tab, page, resp=None) -> dict:
        chain = []
        try:
            req = resp.request if resp else None
            while req is not None:
                chain.insert(0, req.url)
                req = req.redirected_from
        except Exception:
            pass
        return {"tab": tab, "url": page.url, "title": page.title(),
                "status": getattr(resp, "status", None), "redirects": chain[:-1] if len(chain) > 1 else []}

    # ------------------------------------------------------------------ public API
    def open(self, url: str, *, tab: str = "", new_tab: bool = False, wait: str = "domcontentloaded") -> dict:
        why = policy.check_url(url)
        if why:
            raise BrowserError(f"won't open {url}: {why}")
        if wait not in ("load", "domcontentloaded", "networkidle", "commit"):
            wait = "domcontentloaded"

        def go():
            t, p = self._new_tab() if new_tab else self._page(tab)
            try:
                resp = p.goto(url, wait_until=wait)
            except Exception as e:
                raise BrowserError(f"couldn't load {url}: {str(e).splitlines()[0][:200]}") from None
            if wait == "domcontentloaded":
                try:                       # give scripts a moment to render the content
                    p.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
            self.current = t
            return self._info(t, p, resp)
        return self._run(go, timeout=self.timeout_ms / 1000 + 20)

    def navigate(self, action: str, tab: str = "") -> dict:
        def go():
            t, p = self._page(tab)
            if action == "back":
                resp = p.go_back()
            elif action == "forward":
                resp = p.go_forward()
            elif action == "reload":
                resp = p.reload()
            else:
                raise BrowserError("action must be back, forward or reload")
            return self._info(t, p, resp)
        return self._run(go)

    def list_tabs(self) -> list[dict]:
        def go():
            return [{"tab": t, "url": p.url, "title": p.title(), "current": t == self.current}
                    for t, p in self.tabs.items() if not p.is_closed()]
        return self._run(go) if self._ctx is not None or self._launcher else []

    def switch(self, tab: str) -> dict:
        def go():
            if tab not in self.tabs:
                raise BrowserError(f"no tab {tab}")
            self.current = tab
            return self._info(tab, self.tabs[tab])
        return self._run(go)

    def close_tab(self, tab: str = "") -> None:
        def go():
            t = tab or self.current
            p = self.tabs.pop(t, None)
            if p is not None:
                p.close()
            self.current = next(reversed(self.tabs), "") if self.tabs else ""
        self._run(go)

    def text(self, tab: str = "", limit: int = 20000) -> str:
        def go():
            _, p = self._page(tab)
            return p.inner_text("body")[:limit]
        return self._run(go)

    def html(self, tab: str = "", selector: str = "", limit: int = 400000) -> str:
        def go():
            _, p = self._page(tab)
            return (p.locator(selector).first.inner_html() if selector else p.content())[:limit]
        return self._run(go)

    def meta(self, tab: str = "") -> dict:
        def go():
            t, p = self._page(tab)
            m = p.evaluate(_META_JS)
            m["url"], m["tab"] = p.url, t
            return m
        return self._run(go)

    def links(self, tab: str = "", limit: int = 150) -> list[dict]:
        return self._run(lambda: self._page(tab)[1].evaluate(_LINKS_JS, limit))

    def aria(self, tab: str = "", limit: int = 12000) -> str:
        return self._run(lambda: self._page(tab)[1].locator("body").aria_snapshot()[:limit])

    def snapshot(self, tab: str = "", limit: int = 150) -> list[dict]:
        return self._run(lambda: self._page(tab)[1].evaluate(_SNAPSHOT_JS, limit))

    def _element(self, p, ref: str):
        if not re.fullmatch(r"e\d{1,4}", ref or ""):
            raise BrowserError("ref must look like e12 (from a snapshot)")
        info = p.evaluate(_INFO_JS, ref)
        if not info:
            raise BrowserError(f"no element {ref} — take a fresh snapshot (the page changed)")
        return info, p.locator(f'[data-helios-ref="{ref}"]').first

    def _settle(self, t, p) -> dict:
        try:
            p.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return self._info(t, p)

    def click(self, ref: str, *, tab: str = "", confirm: bool = False) -> dict:
        def go():
            t, p = self._page(tab)
            info, loc = self._element(p, ref)
            why = policy.risky_action(info)
            if why and not confirm:
                raise NeedsConfirm(f"{ref} {why} — this could act on someone's behalf")
            if why:
                conf.log("browser", f"CONFIRMED risky click {ref} ({why}) on {p.url[:120]}")
            loc.click()
            return self._settle(t, p)
        return self._run(go)

    def type(self, ref: str, text: str, *, tab: str = "", submit: bool = False, confirm: bool = False) -> dict:
        def go():
            t, p = self._page(tab)
            info, loc = self._element(p, ref)
            if policy.credential_field(info):
                raise BrowserError("Helios never types into password, card or one-time-code fields "
                                   "— you enter those yourself")
            why = policy.risky_action(info, pressing_enter=True) if submit else ""
            if why and not confirm:
                raise NeedsConfirm(f"pressing Enter in {ref} {why}")
            loc.fill(text)
            if submit:
                loc.press("Enter")
            return self._settle(t, p)
        return self._run(go)

    def select(self, ref: str, value: str, *, tab: str = "") -> dict:
        def go():
            t, p = self._page(tab)
            _info, loc = self._element(p, ref)
            try:
                loc.select_option(label=value)
            except Exception:
                loc.select_option(value=value)
            return self._info(t, p)
        return self._run(go)

    def scroll(self, *, tab: str = "", direction: str = "down", ref: str = "") -> dict:
        def go():
            t, p = self._page(tab)
            if ref:
                self._element(p, ref)[1].scroll_into_view_if_needed()
            else:
                p.mouse.wheel(0, -800 if direction == "up" else 800)
                p.wait_for_timeout(400)
            return self._info(t, p)
        return self._run(go)

    def wait_for(self, *, tab: str = "", selector: str = "", text: str = "", timeout: float = 10) -> bool:
        def go():
            _, p = self._page(tab)
            try:
                if selector:
                    p.wait_for_selector(selector, timeout=timeout * 1000)
                elif text:
                    p.get_by_text(text).first.wait_for(timeout=timeout * 1000)
                else:
                    p.wait_for_load_state("networkidle", timeout=timeout * 1000)
                return True
            except Exception:
                return False
        return self._run(go, timeout=timeout + 15)

    def screenshot(self, *, tab: str = "", full_page: bool = False) -> Path:
        d = data_dir() / "screenshots"
        d.mkdir(exist_ok=True)

        def go():
            _, p = self._page(tab)
            out = d / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
            p.screenshot(path=str(out), full_page=full_page)
            for old in sorted(d.glob("shot-*.png"))[:-5]:     # keep only the last few
                old.unlink(missing_ok=True)
            return out
        return self._run(go)

    def download(self, ref: str, *, tab: str = "") -> Path:
        d = data_dir() / "downloads"
        d.mkdir(exist_ok=True)

        def go():
            _, p = self._page(tab)
            _info, loc = self._element(p, ref)
            with p.expect_download(timeout=self.timeout_ms) as dl:
                loc.click()
            name = re.sub(r"[^\w.\- ]", "_", dl.value.suggested_filename or "download")[:120]
            out = d / name
            dl.value.save_as(str(out))
            conf.log("browser", f"downloaded {name} ({out.stat().st_size} bytes) from {p.url[:120]}")
            return out
        return self._run(go, timeout=self.timeout_ms / 1000 + 60)

    @property
    def running(self) -> bool:
        return self._ctx is not None


_shared: Browser | None = None
_shared_lock = threading.Lock()


def shared() -> Browser:
    """The one hidden browser for this process (MCP server, research, Night Mode)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Browser()
            import atexit                    # don't leave headless Edge processes behind
            atexit.register(lambda: _shared is not None and _shared.running and _shared.close())
        return _shared


def as_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)[:20000]
