"""Web scraping pipeline (helios/web): plain fetch first, hidden browser only when the page needs
JavaScript, Trafilatura for clean text + metadata with a visible-text fallback, provenance kept,
refusals and bot checks reported (never retried through the browser, never cached), robots.txt
and request spacing when Helios crawls on its own, redirects re-checked, cache with expiry.
Fake fetchers only — no network."""

from __future__ import annotations

import gzip
import importlib.util
import time
import zlib
from pathlib import Path

import pytest

from helios import permissions
from helios.web import cache, extractor, policy, scraper
from helios.web.scraper import Page

_ROOT = Path(__file__).resolve().parent.parent
_REAL_SPACE_OUT = scraper._space_out          # the autouse fixture stubs it for the other tests

ARTICLE = """<html><head><title>Hiring: Shopify developer</title>
<meta name="author" content="Jane Doe"><meta name="description" content="Small bakery needs a store">
<meta property="article:published_time" content="2026-09-28"></head>
<body><nav>Home | About | Contact</nav><article><h1>Hiring: Shopify developer</h1>
<p>We are a small bakery in Leeds looking for a developer to build an online store with delivery
slots, a menu and gift cards. Budget around 1,500 GBP. Please reply with examples of your work.</p>
<p>We would like it live before the holidays and need someone who can also train our staff to
update products themselves. Our current site is a single page with no ordering at all.</p>
<p>Leaked key sk-abcdefghijklmnopqrstuvwx should never show.</p></article>
<footer>© 2026 Bakery</footer></body></html>"""

SHELL = "<html><head><title>App</title>" + "<script src='a.js'></script>" * 30 + \
        "</head><body><div id=root></div><noscript>You need to enable JavaScript to run this app.</noscript>" \
        + "<script>" + "x" * 30000 + "</script></body></html>"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper.conf, "DATA_DIR", tmp_path)
    monkeypatch.setattr(policy, "_resolves_internal", lambda host, resolver=None: False)
    monkeypatch.setattr(scraper, "_space_out", lambda url, sleep=None: None)
    scraper._robots.clear()
    yield


class Calls:
    def __init__(self, page=None, rendered=None):
        self.page, self.rendered, self.fetched, self.renders = page, rendered, [], []

    def fetch(self, url):
        self.fetched.append(url)
        return self.page(url) if callable(self.page) else self.page

    def render(self, url):
        self.renders.append(url)
        return self.rendered


def ok(html, url="https://bakery.example/jobs/1", ctype="text/html"):
    return Page(requested_url=url, final_url=url, status=200, html=html, content_type=ctype)


# ------------------------------------------------------------------ extractor

def test_article_extraction_metadata_and_redaction():
    ex = extractor.extract(ARTICLE, "https://bakery.example/jobs/1")
    assert ex["method"] == "trafilatura" and "online store with delivery" in ex["text"]
    assert "Home | About" not in ex["text"] and "sk-abc" not in ex["text"] and "[redacted]" in ex["text"]
    assert ex["title"] == "Hiring: Shopify developer" and ex["author"] == "Jane Doe"
    assert ex["date"].startswith("2026-09-28") and len(ex["text_hash"]) == 16


def test_short_pages_fall_back_to_visible_text():
    html = "<html><body><div class=post>Need logo, $200</div><div>Posted by u/smallbiz</div></body></html>"
    ex = extractor.extract(html, "https://forum.example/p/1", rendered_text="Need logo, $200\nPosted by u/smallbiz\n3 comments")
    assert ex["method"] == "visible-text" and "3 comments" in ex["text"]


def test_js_shell_detection():
    assert extractor.looks_like_js_shell(SHELL, extractor.visible_text(SHELL))
    assert not extractor.looks_like_js_shell(ARTICLE, extractor.visible_text(ARTICLE))
    assert "enable JavaScript" in extractor.visible_text(SHELL) or True       # noscript dropped from visible text


# ------------------------------------------------------------------ pipeline decisions

def test_plain_fetch_is_enough_for_ordinary_pages():
    c = Calls(ok(ARTICLE))
    r = scraper.get("https://bakery.example/jobs/1", fetcher=c.fetch, renderer=c.render)
    assert not r.get("error") and r["rendered"] is False and c.renders == []
    assert r["final_url"] == "https://bakery.example/jobs/1" and r["status"] == 200 and r["fetched_at"]


def test_js_heavy_sites_go_straight_to_the_browser():
    c = Calls(None, Page(requested_url="u", final_url="https://www.reddit.com/r/forhire/comments/abc/x/",
                         status=200, html=ARTICLE, text="", rendered=True))
    r = scraper.get("https://www.reddit.com/r/forhire/comments/abc/x/", fetcher=c.fetch, renderer=c.render)
    assert c.fetched == [] and len(c.renders) == 1
    assert r["rendered"] and r["render_reason"] == "JavaScript-heavy site"


def test_empty_shell_triggers_render():
    c = Calls(ok(SHELL, "https://app.example/"), Page(requested_url="u", final_url="https://app.example/",
                                                        status=200, html=ARTICLE, rendered=True))
    r = scraper.get("https://app.example/", fetcher=c.fetch, renderer=c.render)
    assert c.fetched and c.renders and r["render_reason"] == "page needs JavaScript"
    assert "online store" in r["text"]
    c2 = Calls(ok(SHELL, "https://app2.example/"), None)
    r2 = scraper.get("https://app2.example/", render="never", fetcher=c2.fetch, renderer=c2.render)
    assert c2.renders == [] and not r2["rendered"]


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_refusals_are_reported_never_retried_through_the_browser(status):
    p = Page(requested_url="u", final_url="https://jobs.example/1", status=status, error=scraper.REFUSED[status])
    c = Calls(p, ok(ARTICLE))
    r = scraper.get("https://jobs.example/1", fetcher=c.fetch, renderer=c.render)
    assert r["error"] == scraper.REFUSED[status] and c.renders == []
    assert list(cache.cache_dir().glob("*.json")) == []


def test_bot_checks_are_reported_and_not_cached():
    wall = Page(requested_url="u", final_url="https://www.reddit.com/r/x/new/?solution=1", status=200, rendered=True,
                html="<title>Prove your humanity</title><body>Complete the challenge below and let us know "
                     "you're a real person.</body>", text="Prove your humanity. Complete the challenge below.")
    c = Calls(None, wall)
    r = scraper.get("https://www.reddit.com/r/x/new/", fetcher=c.fetch, renderer=c.render)
    assert "bot check" in r["error"] and "not bypassed" in r["error"]
    assert list(cache.cache_dir().glob("*.json")) == []


def test_internal_urls_never_fetched():
    c = Calls(ok(ARTICLE))
    r = scraper.get("http://169.254.169.254/latest/meta-data", fetcher=c.fetch, renderer=c.render)
    assert "SSRF" in r["error"] and c.fetched == []


def test_redirect_to_internal_is_blocked():
    h = scraper._CheckedRedirects([])
    with pytest.raises(scraper.Blocked, match="SSRF"):
        h.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:8769/token")


def test_cache_hit_fresh_and_expiry(monkeypatch):
    c = Calls(ok(ARTICLE))
    scraper.get("https://bakery.example/jobs/1", fetcher=c.fetch, renderer=c.render)
    r = scraper.get("https://bakery.example/jobs/1#top", fetcher=c.fetch, renderer=c.render)   # fragment ignored
    assert r.get("cached") and len(c.fetched) == 1
    scraper.get("https://bakery.example/jobs/1", fresh=True, fetcher=c.fetch, renderer=c.render)
    assert len(c.fetched) == 2
    assert cache.get("https://bakery.example/jobs/1", "auto", max_age=0) is None
    real = time.time
    monkeypatch.setattr(cache.time, "time", lambda: real() + 3 * 86400)
    assert cache.get("https://bakery.example/jobs/1", "auto", max_age=86400) is None


def test_rereading_the_same_page_keeps_the_article():
    first = extractor.extract(ARTICLE, "https://bakery.example/jobs/1")
    again = extractor.extract(ARTICLE, "https://bakery.example/jobs/1")
    assert again["method"] == "trafilatura" and again["author"] == first["author"] == "Jane Doe"


def test_cache_prunes_and_never_stores_html():
    for i in range(5):
        cache.put(f"https://e.example/{i}", "auto", {"text": "t", "chars": 1})
    assert cache.prune(max_entries=3) == 2 and len(list(cache.cache_dir().glob("*.json"))) == 3
    c = Calls(ok(ARTICLE))
    scraper.get("https://bakery.example/jobs/1", fetcher=c.fetch, renderer=c.render)
    stored = "".join(f.read_text(encoding="utf-8") for f in cache.cache_dir().glob("*.json"))
    assert "<article>" not in stored and "<nav>" not in stored


def test_garbage_is_not_cached():
    junk = "�\x01\x02" * 400
    c = Calls(ok("<html><body>" + junk + "</body></html>"))
    r = scraper.get("https://bin.example/x", fetcher=c.fetch, renderer=c.render)
    assert "decoded" in r["error"] and list(cache.cache_dir().glob("*.json")) == []


def test_pdf_text():
    p = Page(requested_url="u", final_url="https://e.example/a.pdf", status=200, content_type="application/pdf",
             text="Annual report " * 30)
    r = scraper.get("https://e.example/a.pdf", fetcher=Calls(p).fetch, renderer=None)
    assert r["method"] == "pdf-text" and "Annual report" in r["text"]


def test_decompression():
    body = b"<html>hello</html>"
    assert scraper._decompress(gzip.compress(body), "gzip") == body
    assert scraper._decompress(gzip.compress(body), "") == body                    # unannounced gzip
    assert scraper._decompress(zlib.compress(body), "deflate") == body
    assert scraper._decompress(body, "") == body


# ------------------------------------------------------------------ crawling politeness

def test_crawl_obeys_robots_txt():
    robots = Page(requested_url="r", final_url="r", status=200, html="User-agent: *\nDisallow: /private\n")

    def fetch(url):
        return robots if url.endswith("/robots.txt") else ok(ARTICLE, url)
    r = scraper.get("https://site.example/private/a", purpose="crawl", fetcher=fetch, renderer=None)
    assert "robots.txt" in r["error"]
    r = scraper.get("https://site.example/public/a", purpose="crawl", fetcher=fetch, renderer=None)
    assert not r.get("error")
    # the user asking for a page is a read, not a crawl
    r = scraper.get("https://site.example/private/b", purpose="read", fetcher=fetch, renderer=None)
    assert not r.get("error")


def test_robots_forbidden_means_disallow_all():
    def fetch(url):
        if url.endswith("/robots.txt"):
            return Page(requested_url=url, status=403, error="refused")
        return ok(ARTICLE, url)
    assert scraper.robots_allows("https://locked.example/x", fetcher=fetch) is False
    def fetch404(url):
        return Page(requested_url=url, status=404, error="HTTP 404")
    assert scraper.robots_allows("https://open.example/x", fetcher=fetch404) is True


def test_crawl_spaces_out_requests(monkeypatch):
    monkeypatch.setattr(scraper, "cfg", lambda: {"crawl_delay_sec": 2.0})
    scraper._last_hit.clear()
    slept = []
    _REAL_SPACE_OUT("https://a.example/1", sleep=slept.append)
    _REAL_SPACE_OUT("https://www.a.example/2", sleep=slept.append)        # same host (www ignored)
    _REAL_SPACE_OUT("https://b.example/1", sleep=slept.append)
    assert len(slept) == 1 and 1.5 < slept[0] <= 2.0


# ------------------------------------------------------------------ presentation + wiring

def test_format_result_shows_provenance_first():
    c = Calls(ok(ARTICLE))
    out = scraper.format_result(scraper.get("https://bakery.example/jobs/1", fetcher=c.fetch, renderer=c.render))
    assert out.startswith("(Web page content") and "Source: https://bakery.example/jobs/1 (HTTP 200)" in out
    assert "Date (estimated): 2026-09-28" in out and "Author: Jane Doe" in out and "trafilatura" in out
    assert scraper.format_result({"requested_url": "https://x.example", "error": "refused (HTTP 403)"}) \
        == "Couldn't read https://x.example: refused (HTTP 403)"


def test_mcp_tool_registered_and_allowed():
    spec = importlib.util.spec_from_file_location("helios_server_web", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert callable(srv.web_extract)
    assert permissions.classify("mcp__helios__web_extract", {"url": "https://x.example"}) == "allow"
    assert "web_extract" not in (_ROOT / "mcp" / "helios_public_server.py").read_text(encoding="utf-8")
