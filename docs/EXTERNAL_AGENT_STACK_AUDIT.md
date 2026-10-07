# External agent stack audit (Phase 0)

Date: 2026-10-07. Scope: the seven repositories proposed for Helios's multimodal / desktop
intelligence work, plus the data sources needed by the Business Prospecting engine. Nothing in
Helios's source was changed for this audit.

## 0. Baseline

| Item | Value |
|---|---|
| Last commit | `2fa4028` — Phase 14 finish + lead finder |
| Uncommitted | 22 files (+635/−20): Polarion, Gmail, folder discovery, RealtimeSTT and their tests |
| Test suite | **737 tests collected, exit 0 (no failures)** |
| Python / venv | 3.11.9, `D:\Helios\venv` (repo `.venv` is a junction) |
| Machine | Intel i7-8565U (4 cores / 8 threads), 15.9 GB RAM, Intel UHD 620 iGPU (~1 GB shared), **no CUDA GPU** |
| Disk | C: 11.4 GB free · D: 54.4 GB free · E: 57.2 GB free |
| Browsers | Chrome 154, Edge 154 (both system-installed) |
| Model cache | `D:\Helios\cache\hf` (HF_HOME) |

## 1. What Helios already has (relevant to this work)

**PC control is cua-driver 0.30.4, an MCP server (`computer`).** Its tool list (read live with
`tools/list`, no actions taken) is much broader than the Helios persona currently uses:

* **Semantic Windows control:** `get_window_state` walks the window's UI Automation tree and
  returns structured elements (`element_index`, role, label, value, enabled, selected, UIA
  actions, frame, parent). It supports a `query` filter.
  * Acting on elements: `click` / `type_text` / `set_value` / `scroll` / `press_key` /
    `hotkey` by `element_index`, using UIA Invoke and ValuePattern via PostMessage. No cursor
    move, no focus steal, works on background windows.
  * Window control: `invoke_menu` (menu paths through accessibility), `verify_state`
    (deterministic post-condition check), `launch_app`, `list_windows`, `list_apps`,
    `bring_to_front`, `set_window_frame`.
  * Pixel clicks run a UIA hit-test first and only fall back to raw input.
* **Browser control over CDP:** `get_browser_state` (a semantic snapshot that joins the
  accessibility tree, DOM, layout and viewport, with action refs), `browser_navigate`,
  `browser_click` by ref, `browser_type`, `browser_pointer`, `browser_dialog`,
  `browser_download` (to an approved directory) and `browser_set_input_files`. It drives the
  user's own Chrome/Edge after explicit `browser_prepare` authorization.
* **Visual parsing:** `parse_visual_regions` (text and icon regions) through an optional
  signed "perception" extension. It isn't usable yet: the daemon must be started with a
  reviewed catalog (`CUA_DRIVER_PERCEPTION_CATALOG`).
* **Other:** trajectory recording and replay, clipboard, `health_report` (UIA reachable,
  D3D11 capture OK).
* **Update:** cua-driver 0.34.0 is available.

**Other relevant pieces:**
* **In-process UIA:** the `uiautomation` package 2.0.29 is installed and used by
  `helios/ambient.py` (watches).
* **Screen capture:** `mss` is installed, plus cua-driver's `get_desktop_state`.
* **Web reading:** Antigravity's `search_web` + `read_url_content` (research mode),
  `research.check_source` (HTTP check + redirect resolution), and `WebFetch` (gated).
  There's no JavaScript rendering and no structured extraction.
* **Voice:** faster-whisper 1.2.1 (latest), `base.en` int8 CPU, RealtimeSTT capture,
  Silero/WebRTC VAD, models on D:.
* **Safety:** the PreToolUse gate (hard rails, then classify, then ask), protected paths, an
  outbound block for background agents, Gmail send always asks, SSRF block, `SECRET_RE` log
  redaction.

**Conclusion:** most "Windows UI intelligence" and a real browser layer **already exist
inside cua-driver**. Helios mostly needs to use them semantically (persona guidance, a fast
in-process screen-context tool, policy for the new browser tools) rather than add another
automation library.

## 2. Repository decisions

| Repository | Latest | License | Decision |
|---|---|---|---|
| Playwright Python | 1.63.0 (2026-09-15) | Apache-2.0 | **USE** — hidden scraping browser |
| Trafilatura | 2.3.1 (2026-10-06) | Apache-2.0 | **USE** — clean content + metadata |
| faster-whisper | 1.2.1 (2025-10-31) | MIT | **USE (already in)** — tune, don't replace |
| Microsoft UFO² / UFO³ | UFO² 2.0 (LTS), UFO³ Galaxy (active) | MIT | **REFERENCE ONLY** |
| pywinauto | 0.6.9 (2025-01-06) | BSD-3 | **REFERENCE ONLY** (duplicate) |
| Browser Use | 0.13.11 (2026-10-07) | MIT | **REJECT for the Helios venv** (optional isolated later) |
| Microsoft OmniParser | V2 (+ YOLOv9-E detector 2026) | Code CC-BY-4.0; weights MIT / older AGPL | **REJECT as default; OPTIONAL later** |
| *(added)* Windows OCR (`winrt-Windows.Media.Ocr`) | 3.2.1 | MIT | **USE** — OCR fallback, built into Windows |

### 2.1 Playwright Python — USE
* **Windows / Python:** native wheels, Python ≥ 3.10; 38.6 MB wheel; dependencies are only
  `pyee` and `greenlet`.
* **No browser download needed:** `channel="msedge"` / `"chrome"` uses the installed
  browsers, so the ~150 MB Chromium bundle can be skipped.
* **Role:** a *hidden, isolated-profile* browser for rendering JavaScript pages (research,
  leads, website audits). This complements cua-driver's browser tools, which act in the
  *user's visible* browser. Playwright never touches the user's profile, cookies or saved
  passwords.
* **CPU / memory:** one headless Edge instance uses about 150–300 MB. Launch it lazily, reuse
  one context, close it when idle.
* **Security work needed:**
  * Block internal and loopback targets on navigation and on every redirect (route
    interception with `permissions.is_internal_url`).
  * Downloads go only to a sandbox folder.
  * Per-domain rate limit, `robots.txt` respected for crawling.
  * No login or CAPTCHA bypass.
  * Extracted text is untrusted data (prompt injection).

### 2.2 Trafilatura — USE
* **Fit:** pure Python. Its dependencies (`lxml`, `courlan`, `htmldate` → `dateparser`,
  `justext`, `babel`, `tld`) are all small. Apache-2.0.
* **Role:** takes the rendered HTML (from Playwright or a plain fetch) and returns clean text,
  title, author, date and site name. It doesn't fetch dynamic pages itself; Playwright does.
* **Note:** extraction can drop short forum posts and comments, so Helios falls back to
  visible-text extraction for Reddit-style pages.

### 2.3 faster-whisper — USE (already integrated)
* **Version:** 1.2.1 is the latest release and is what Helios runs. The RealtimeSTT capture
  (2026-10-07) already reuses the one loaded model.
* **On this CPU:** `base.en` int8 transcribes 3 s of speech in ~1.0 s; `small.en` would be
  roughly 3× slower, which hurts the reply time. There's no GPU, so `device="cpu"`,
  `compute_type="int8"`.
* **Phase 7 candidates** (measure before switching):
  * a `distil-small.en` benchmark;
  * `word_timestamps` and language detection exposed as options;
  * a `[voice.stt]` table that keeps the current keys working;
  * a microphone-diagnostics expansion.

### 2.4 Microsoft UFO² / UFO³ — REFERENCE ONLY
* **Not a library:** installed by clone + `requirements.txt`; Python 3.10–3.11.
* **Needs model API keys:** the agents require OpenAI / Azure / Qwen / Gemini / Claude keys,
  with no offline or CLI-agent mode. That conflicts with "Antigravity is the brain, no API
  keys".
* **Its control layer isn't standalone:** the UIA / Win32 / COM layer isn't published as a
  separate package or MCP server. cua-driver already provides the same mechanics (UIA tree,
  Invoke / ValuePattern, menu invocation, verification) and is already wired through
  Helios's gate.
* **What Helios borrows (concepts):**
  1. control filtering: give the model only relevant, actionable controls, not the whole tree;
  2. the semantic-first ladder (UIA → native API → OCR → pixels);
  3. app-specific "API before GUI" actions (e.g. open a file by path rather than clicking
     through dialogs);
  4. verify after acting (`verify_state`);
  5. HostAgent/AppAgent split: pick the app, then work inside it.

### 2.5 pywinauto — REFERENCE ONLY
* **It works:** its dependencies resolve on Python 3.11 (`comtypes`, `pywin32`, both already
  installed).
* **But it duplicates what's there:** the `uiautomation` package (already a dependency) for
  in-process reading, and cua-driver for acting. Release cadence is slow (0.6.9, Jan 2025).
* **Decision:** don't add a third UIA stack. Revisit only if a specific app needs pywinauto's
  Win32 backend (old MFC/VB6 controls) and cua-driver fails on it.

### 2.6 Browser Use — REJECT for the Helios venv
* **Hard version conflicts** (from its 37 exact pins):
  * `mcp==2.1.1` vs Helios `mcp` 1.30 (Helios's FastMCP servers use the 1.x API);
  * `openai==2.26.0` vs 3.20.0 (downgrade);
  * `anyio==4.12.1` vs 4.15.1 (downgrade);
  * `requests==2.33.0` vs 2.34.2 (downgrade).
* **Needs its own model:** the agent requires an LLM API key (OpenAI / Anthropic / Gemini /
  its BU cloud) or a local Ollama model. It can't use Antigravity.
* **Bundles telemetry:** `posthog==7.7.0`.
* **Duplicates capability:** Playwright plus cua-driver's CDP tools give deterministic browser
  control, and Antigravity is already the high-level agent that can sequence them.
* **If wanted later:** run it in a separate venv as its "browser-harness" CLI (no own LLM),
  behind Helios's gate, opt-in. Not planned now.

### 2.7 Microsoft OmniParser — REJECT as default (OPTIONAL later)
* **Too heavy for this machine:** needs torch + transformers + a YOLO detector and a Florence-2
  captioner. It's designed for a CUDA GPU; on this i7-8565U without a GPU, parsing a single
  screenshot would take several seconds and hold GBs of RAM.
* **Licensing is mixed:** code CC-BY-4.0; the new `icon_detect_v3` is MIT, but older
  detectors are AGPL.
* **What Helios uses instead:** UIA (cua-driver + `uiautomation`) for anything with controls,
  Windows' built-in OCR for text in non-UIA surfaces, and the model's own vision on a
  screenshot only when asked.
* **If wanted later:** cua-driver's `parse_visual_regions` extension, or OmniParser on a GPU
  machine.

### 2.8 Windows OCR (added) — USE
* **What:** `winrt-Windows.Media.Ocr` 3.2.1 (MIT) uses the OCR engine built into Windows. It's
  CPU-fast, offline and needs no models.
* **Fit:** the dry-run shows only small `winrt-*` packages. It's the "OCR" rung of the ladder
  for canvases, images and remote desktops.
* **Alternative considered:** RapidOCR, which would pull in OpenCV (~40 MB).

## 3. Proposed dependency additions (dry-run verified, nothing installed yet)

```
playwright==1.63.0          (+ greenlet, pyee)                 — Phase 3
trafilatura==2.3.1          (+ lxml, courlan, babel, tld, htmldate, dateparser, regex,
                               pytz, tzlocal, tzdata, python-dateutil, jusText, lxml_html_clean) — Phase 5
winrt-Windows.Media.Ocr==3.2.1 + winrt-runtime, Windows.Graphics.Imaging,
  Windows.Storage.Streams, Windows.Globalization, Windows.Foundation      — Phase 2
```
* `pip install --dry-run`: **24 new packages, zero downgrades.** The only change to an
  existing package is `charset-normalizer` 3.5.1 → 3.5.2 (patch).
* No Playwright browser download (it uses the installed Edge or Chrome).
* All three are optional at runtime: features degrade with a clear message if a package is
  missing.

**Not added:** browser-use, UFO, pywinauto, OmniParser, torch-based OCR (EasyOCR), and
`ultralytics` (AGPL).

## 4. Conflicts

| Conflict | Resolution |
|---|---|
| browser-use pins `mcp` 2.x and downgrades openai / anyio / requests | Not installed |
| UFO / browser-use / OmniParser need model API keys | Not used; Antigravity stays the only brain |
| Two browser stacks (Playwright vs cua-driver CDP) | Different jobs: Playwright = hidden scraping; cua-driver = acting in the user's browser |
| Two UIA stacks (`uiautomation` vs cua-driver) | `uiautomation` = fast in-process **read-only** context; cua-driver = all **actions** (one input driver, existing lock and panic) |
| `early_transcription_on_silence` unit bug (RealtimeSTT) | Already handled (passes seconds) |

## 5. Performance risks (this laptop) and mitigations

* **Headless browser:** 150–300 MB RAM per instance. Launch lazily, use one shared context,
  close after idle, cap at 2 concurrent pages, block images/fonts/media during scraping.
* **UIA tree walks** can be slow on huge windows (browsers, IDEs). Bound depth and element
  count, use a time budget, and include only controls from the foreground window.
* **OCR on full 4K screenshots:** OCR the active window only, downscale, and run it only when
  UIA returns no text.
* **No continuous screen capture:** screen context is captured on demand (adaptive), and
  screenshots aren't stored by default.
* **Night Mode CPU:** the browser pipeline runs BELOW_NORMAL priority like the other
  background work.

## 6. Security risks and controls

| Risk | Control |
|---|---|
| Scraper used for SSRF (internal URLs, redirects to localhost) | Every navigation and redirect checked with `is_internal_url`; hard deny |
| Prompt injection from pages, emails or screens | Extracted content labelled as untrusted data; scraping is tool-less (same as research mode) |
| Browser credentials and cookies | Playwright uses a throwaway profile; the user's profile is only reachable through cua-driver `browser_prepare` (explicit grant); never logged or stored |
| Outbound actions via the browser (submit, post, send, buy) | New browser tools classified: navigation and reading = allow; click/type = allowed only in the hidden scraping browser; form submit / post / purchase / message = **ask**, and background agents are hard-blocked |
| Reading secrets off the screen (password fields, password managers) | UIA reader skips `IsPassword` controls and never reads password-manager windows; text passes through `SECRET_RE` redaction |
| Supply chain | Pinned versions; only Apache / MIT / BSD; cua-driver update reviewed before applying |
| Business prospecting PII and anti-spam | Business-owned public contacts only; no personal data; drafts only; send needs approval (Gmail send already always asks) |

## 7. Business-prospecting data sources (for the extension spec)

| Source | Use | Terms |
|---|---|---|
| **OpenStreetMap via Overpass API** | Primary bulk discovery by area / radius / category (`shop`, `amenity`, `craft`, `office`, `healthcare`, with `website` / `phone` / `email` tags when present) | ODbL: attribution "© OpenStreetMap contributors"; share-alike applies only if a derived database is published (internal use is fine). Fair use: a few queries per area, cached, well under the public instance guidance (~10k queries/day). |
| **Nominatim** | Turn "Mehdipatnam, Hyderabad" into a boundary or point | Max 1 request/second, identifying User-Agent, results cached |
| **Business's own website** | Verification, audit, contact details | Public; respect `robots.txt`; Playwright + Trafilatura |
| **Google Places API** | Not used by default (needs an API key and billing; strict storage limits) | If ever enabled: official API only, field masks, attribution, store only `place_id` long-term |
| **Google Maps website** | **Never scraped** | — |

Coverage is reported honestly: OSM coverage of small Indian businesses is partial. The report
says "searched OpenStreetMap across the selected area", never "found every business".

## 8. Recommended integration order (adjusted to these findings)

| Phase | Work | Size |
|---|---|---|
| 1 | `helios/computer/` — in-process read-only UIA context (`uiautomation`), semantic element lookup, password-safe, routing over cua-driver; persona: semantic-first ladder + verify | M |
| 2 | Screen context: UIA + Windows OCR fallback + optional screenshot-to-model; adaptive, nothing stored | M |
| 3 | `helios/web/browser.py` — Playwright hidden browser (Edge channel), SSRF-guarded, cached | M |
| 4 | High-level browser agent = Antigravity + Playwright / cua-driver tools (no Browser Use) | S |
| 5 | `helios/web/extractor.py` — Trafilatura + visible-text fallback, metadata, provenance cache | M |
| 6 | Research + lead finder use the render → extract pipeline | M |
| 6b | Business Prospecting engine (OSM discovery → site audit → scores → rate-card quote → drafts → approval queue) | L |
| 7 | STT tuning: measure `distil-small.en`, expose timestamps / language options, `[voice.stt]` config | S |
| 8 | Multimodal orchestration: voice → screen / browser context → Antigravity → verify → TTS | M |
| 9–13 | Memory categories, Night Mode schedule, MCP tools, Antigravity verification, security tests | M |
| 14–15 | Full regression + documentation set + `HELIOS_IMPLEMENTATION_STATUS.md` | M |

S ≈ a few hours, M ≈ half a day to a day, L ≈ several days, including tests.
