# Helios — web scraping and content extraction

```
specific URL
   ↓  policy.check_url (http/https only, no internal addresses, DNS-checked)
plain HTTP fetch (scraper.fetch_http)              — fast, ~0.3–2 s, gzip/deflate, PDFs via pypdf
   ↓  every redirect re-checked by the policy
needs JavaScript?  (known JS-heavy site · "enable JavaScript" shell · almost no text + many scripts)
   ↓ yes
hidden browser renders it (scraper.fetch_rendered → helios/web/browser.py)
   ↓
Trafilatura → clean main text + title / author / estimated date / site / description / language
   ↓  short result (forum posts, listings) → visible-text fallback
bot check / access wall?  → reported, NOT bypassed, not cached
   ↓
result + provenance → cache (24 h, extracted text only) → research / leads / web_extract
```

## Provenance on every result

`requested_url`, `final_url`, `redirects` (chain), `status`, `rendered` + `render_reason`,
`fetched_at` (UTC), `method` (`trafilatura` | `visible-text` | `pdf-text`), `text_hash`,
`purpose` (`read` | `crawl`).

## Honesty rules

* **Refusals are reported, not worked around.** A refusal (401 / 403 / 429 / 451) is never
  retried through the browser. Verified live: old.reddit.com answers 403, and that's what Helios
  reports.
* **Bot checks are never solved.** "Prove your humanity", CAPTCHAs and Cloudflare challenges are
  detected by title and text, reported as "bot check — not bypassed", and never cached.
  Verified live: www.reddit.com challenges headless browsers.
* **Helios crawling on its own** (`purpose="crawl"`: research, leads, prospecting) obeys
  `robots.txt` (agent "Helios"; a 403 on `robots.txt` means disallow all) and keeps
  `crawl_delay_sec` (2 s) between requests to one host.
* **Your own requests** (`purpose="read"`, "read this page") behave like a browser would.
* **Dates are labelled "estimated".** Trafilatura guesses them; live example: python.org's 3.11.9
  page got 2017-11-03 while the text says April 2, 2024.

## Pitfalls found while building

| Issue | Fix |
|---|---|
| python.org sends gzip unasked; urllib doesn't decompress, so binary garbage was "extracted" and cached | `Accept-Encoding: gzip, deflate` + `_decompress`; the cache refuses text that isn't text |
| Trafilatura `deduplicate=True` remembers text *across calls*, so re-reading a page discarded it | Option removed; regression test added |
| `open(new_tab=True)` as the first browser call crashed | `_new_tab` starts the browser itself |
| `lxml` is a native DLL (MCP server stdin hang) | Preloaded in `helios_server.py` before `mcp.run()` |

## Tools

* `web_extract(url, render=auto|never|always, fresh=false)` reads one page cleanly, with
  provenance.
* `browser_read(what="article")` gives the same clean extraction for the page already open in
  the hidden browser.

Settings: `[web] cache_hours`, `crawl_delay_sec`. Cache: `data/web_cache/` (JSON, at most
2,000 entries).

## Source verification for research and leads (Phase 6, `helios/web/sources.py`)

Antigravity still *finds* candidates (`search_web` + `read_url_content`; that's also how Reddit
posts are seen). Helios then *reads* every cited source itself before saving:

```
source_url (often a search redirect) → resolve → scraper.get(purpose="crawl")
  → ok:      provenance (page title, site, estimated date, fetch time, rendered?) + support score
  → blocked: site refuses automated reading (403/429, bot check, robots.txt) — exists, not penalised
  → failed:  dead / 404 / 5xx / unreachable
```

* **Support score:** the share of the claim's significant words found on the page (stemmed).
  ≥ 0.5 is *strong*, ≥ 0.25 is *partial*, below that is *weak*. When the main extraction is short,
  the page's whole visible text is checked too (`alt_text`).
* **Research:** a *weak* finding keeps its source but loses 40% confidence and says "the source
  page barely mentions this". A *blocked* finding isn't penalised. Each note stores
  `source_title`, `source_date` (estimated), `fetched_at`, `rendered`, `grounding` and an
  `evidence` quote.
* **Leads, dropped when:**
  * someone is *offering* services ("[For Hire]", "available for work") — checked on the agent's
    title before fetching, then on the page;
  * the post doesn't mention the need (*weak*);
  * the post is older than `max_post_age_days` (45);
  * the link is dead.
* **Leads, other rules:**
  * No hiring signal on the page and no budget: confidence × 0.7.
  * The evidence quote has phone numbers and emails removed.
  * A *blocked* site (Reddit: its `robots.txt` asks bots not to read posts) keeps the
    search-seen lead, marked "not checked (seen via search)".
* **Fallback:** if the page can't be read for a *network* reason, a plain "does the link open"
  check (`research.check_source`) decides ok / blocked / failed. Never for refusals or
  `robots.txt`.
* **Resources:** each batch closes the hidden browser when it finishes.

Live check against the existing vault: 4 Reddit leads → *blocked by robots.txt* (kept as
search-seen); 4 research findings → read, with support of 0.43 / 0.52 / 0.54 / 0.82 (the 0.54 one
was 0.0 until GitHub's session banner stopped being mistaken for the release notes).
