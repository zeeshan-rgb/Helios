# Helios — final audit (2026-10-14, completed 2026-10-19)

A traceable inventory of what Helios actually does, from source, configuration, tests and live
runs. Nothing is marked working because a note said so.

**Statuses:**
- `VERIFIED WORKING`: verified by a real run.
- `IMPLEMENTED — NOT VERIFIED`: built and unit-tested, but not run live in this audit.
- `BLOCKED BY USER SETUP`
- `MISSING`
- `BROKEN`
- `NOT APPLICABLE`

Related: [HELIOS_GAP_ANALYSIS.md](HELIOS_GAP_ANALYSIS.md) (requirements matrix + fixes),
[HELIOS_TEST_REPORT.md](HELIOS_TEST_REPORT.md) (commands + results).

## Environment as found (it differs from the audit prompt)

| Item | Prompt said | Actual |
|---|---|---|
| Repository | `D:\Helios\helios` | `%USERPROFILE%\Desktop\Claude\Helios\helios` on C: (D:\Helios\helios does not exist) |
| Package | may be `jarvis/` | `helios/` (renamed on request 2026-09-29; no `jarvis/` left) |
| Setup notes | `D:\Helios\helios\HELIOS_SETUP.md` | `…\Claude\Helios\HELIOS_SETUP.md` (outside the repo, not committed) |
| Python / venv | 3.11 at `D:\Helios\venv` | 3.11.9; `.venv` is a junction to `D:\Helios\venv` ✔ |
| Command | `D:\Helios\bin\helios.cmd` | present ✔ (`helios start` / `restart` / `status` tested) |
| Disk | C: ~10 GB free | **C: 4.8 GB free**, D: 53.5 GB free |
| agy | 1.2.12, Gemini 3.8 Flash (High) | **1.3.5** (self-updated); live chat model = agy default; the account offers 6 models |
| Test baseline | 258 (historical) | **1033 passed, 0 failed, 0 skipped**, 58 test files — before any change in this audit |

## Inventory

### Brain — Antigravity

| Feature | Status | Evidence | Verification | Result | Remaining work | Dependency |
|---|---|---|---|---|---|---|
| Single-turn JSON (`agy -p … --output-format json`) | VERIFIED WORKING | `agy_cli.py` | live command | rc 0, valid JSON, exact reply (20 s) | — | agy signed in |
| Streaming + persistent session + conversation id | VERIFIED WORKING | `agy_brain.py`, `agy_cli.AgySession` | live 2-turn session | both turns streamed; turn 2 recalled turn 1 | — | agy |
| Process recovery (kill + resume) | VERIFIED WORKING | `--conversation <id>` | live kill, new process | context kept | — | agy |
| Timeout kill, stdout/stderr separation, missing agy | VERIFIED WORKING (tests) | `run_once`, `consume_turn` | `test_gap_closure.py` | hung run killed; garbage ignored; graceful "isn't installed" | — | — |
| Gate proof per turn + tamper kill | VERIFIED WORKING (tests) | marker + `hooks.json` digest | `test_antigravity.py` | killed without marker / on tamper | — | — |
| Model info | VERIFIED WORKING | `agy models` probe | live | 6 models; configured slugs checked | **heavy tier `gemini-3.1-pro-high` not offered** | your config |
| Provider quota / reset / context window | NOT APPLICABLE | no supported interface | `agy --help` (1.3.5) | not exposed → shown as "Not exposed" | — | — |
| Helios-measured usage | VERIFIED WORKING | `usage.py`, dashboard | live | 183k-in / 13.6k-out for one live chat turn | consider fresh conversations (cost) | — |
| Gemini CLI engine | IMPLEMENTED — NOT VERIFIED | `gemini_brain.py` | — | legacy; Google stopped personal sign-in | — | — |

### Voice

| Feature | Status | Evidence | Verification | Result | Remaining work | Dependency |
|---|---|---|---|---|---|---|
| Mic open, STT (faster-whisper base.en, CPU int8, cache on D:) | VERIFIED WORKING | `voice/stt.py` | live daemon log | loaded, warmed | — | mic |
| RealtimeSTT capture | VERIFIED WORKING | `voice/realtime_stt.py` | live daemon log | "capture ready (2.2 s)" | — | — |
| Kokoro TTS (voice `am_michael`, configurable) | VERIFIED WORKING | `voice/tts.py` | live synth | 1.7 s audio in 6.6 s cold | — | — |
| TTS → STT → brain → TTS loop | VERIFIED WORKING (no physical mic/speaker) | — | live script | "what time is it" → correct time | speak it yourself once | mic |
| Double-clap wake | VERIFIED WORKING | `voice/clap.py`, daemon | live log (2026-10-14 17:35) | clap → wake → listening | — | mic |
| "Hey Helios" wake word | BLOCKED BY USER SETUP | `voice/wake.py` | live daemon | model file missing — reason reported, clap fallback active | train `hey_helios.onnx` | Colab |
| Ctrl+Alt+H talk, Ctrl+Alt+D dictation, Ctrl+Alt+J summon, Ctrl+Alt+Backspace panic | IMPLEMENTED — NOT VERIFIED | `[voice]` / `[hotkeys]` in settings | config read | bindings confirmed in config | press them once | — |
| Follow-up window, barge-in, mute | VERIFIED WORKING (tests) + live log | daemon | tests; log shows mute/unmute | — | — | — |
| Voice lock (speaker verification) | BLOCKED BY USER SETUP | `voice/speaker*.py` | config | `speaker_verify = false`, no enrollment | "enroll my voice" | — |
| Dormant state reported to UI | VERIFIED WORKING (fixed in audit) | daemon `post_voice_state("dormant")` | live `/dashboard/voice` | `state: dormant` + exact wake reason | — | — |

### Screen, Windows, orchestration

| Feature | Status | Evidence | Verification | Result | Remaining work | Dependency |
|---|---|---|---|---|---|---|
| "What application is open?" | VERIFIED WORKING | `orchestrator.perceive` | live | claude.exe identified | — | — |
| Visible-browser page + URL | VERIFIED WORKING | UIA address bar | live (Edge) | title + URL read | — | — |
| Locate across UIA → DOM → OCR | VERIFIED WORKING | `orchestrator.locate` | live | UIA button, DOM ref found | Electron apps in background expose little | — |
| OCR fallback | VERIFIED WORKING | `computer/ocr.py` | live (2026-10-07) | text + coordinates | — | Windows OCR |
| Act by element via cua-driver | VERIFIED WORKING | cua-driver 0.30.4 | live (2026-09-30) | Notepad "Hello Helios" | not re-run (your Notepad holds work tabs) | cua-driver |
| Single driver lock, panic stop | VERIFIED WORKING (tests) | `pretooluse._computer_use_gate` | tests | — | — | — |
| OmniParser visual parser | NOT APPLICABLE | audit | — | no GPU; rejected | — | GPU |

### Browser, scraping, research, leads

| Feature | Status | Evidence | Verification | Result | Remaining work | Dependency |
|---|---|---|---|---|---|---|
| Hidden browser (open, DOM refs, aria, click/type, tabs, screenshot, download) | VERIFIED WORKING | `web/browser.py` | live Edge test (+ real download) | pass | — | Edge |
| Static + JS-rendered extraction with provenance | VERIFIED WORKING | `web/scraper.py` | live | title/date/text; forced render works | — | — |
| Bot walls / refusals reported, not bypassed | VERIFIED WORKING | scraper | live npm 403 | reported | — | — |
| Research (nightly, sourced) | VERIFIED WORKING | `research.py` | **real night 2026-10-14** | 4 new findings; 3 topics returned 0 | quality of 0-result topics | agy |
| Lead finder (no outbound, rate card) | IMPLEMENTED — NOT VERIFIED (this audit) | `leads.py` | tests; a run happened at 22:51 (`leads:web_dev`, 41k tokens) | — | check output in vault | agy |
| Browser Use | NOT APPLICABLE | audit | — | rejected (pins, API key, telemetry) | — | — |

### Business prospecting

| Feature | Status | Evidence | Verification | Result | Remaining work | Dependency |
|---|---|---|---|---|---|---|
| OSM discovery + coverage + dedupe | VERIFIED WORKING | `prospecting/geo.py`, `store.py` | live scans | 66 businesses, 6/20 cells, done cells skipped | — | Overpass |
| Overpass fallback | VERIFIED WORKING (added) | `geo._overpass` | live | 504 → mirror answered | — | — |
| Website audit + AI opportunity | VERIFIED WORKING | `audit.py`, `scoring.py` | live audits | evidence-based reasons | — | — |
| Rate-card quote / NEEDS_PRICING | VERIFIED WORKING | `quote.py`, `engine.needs_pricing` | live + tests | "Price to be confirmed" | add your ₹ prices | your rate card |
| Editable proposal | VERIFIED WORKING (added) | `proposal.py` | live on a real business | vault note written | — | — |
| Approval queue, never sends, CONTACTED only with evidence | VERIFIED WORKING | `store.approve`, `record_contact` | tests + dashboard test | — | — | — |
| Scheduled prospecting | BLOCKED BY USER SETUP | `[prospecting]` | config | `enabled = false`, no areas | set areas | — |
| WhatsApp | IMPLEMENTED — NOT VERIFIED | recommendations only | code | opt-in click-to-chat only; no sending code exists | — | — |

### Memory, Night Mode, briefing, projects

| Feature | Status | Evidence | Verification | Result | Remaining work | Dependency |
|---|---|---|---|---|---|---|
| Remember / recall / forget, duplicates, secret refusal | VERIFIED WORKING | `memory_store.py` | live vault | "Prefers concise reports" recalled (stored 2026-09-29) | — | — |
| Learning, rules pending approval | VERIFIED WORKING (tests) | `learning.py` | tests | — | runs in Night Mode | agy |
| Night Mode scheduled run | VERIFIED WORKING | `night_mode/scheduler.py` | **real night 2026-10-14** | ran; battery rule skipped checks at 14 % | keep the PC awake + plugged in | — |
| Sleep during a task reported; research deadline | VERIFIED WORKING (fixed in audit, tests) | scheduler, research | tests + root cause from heartbeat | PC slept 02:03–11:16 | — | — |
| Morning briefing (written + spoken) | VERIFIED WORKING (fixed in audit) | `briefing.py` | live | now lists the 7 skipped tasks; stale text refreshed | — | — |
| Project health (10 manifests) | VERIFIED WORKING | `projects.py`, `health.py` | live briefing | real verdicts per project | — | — |

### Integrations, tools, safety, UI

| Feature | Status | Evidence | Verification | Result | Remaining work | Dependency |
|---|---|---|---|---|---|---|
| Gmail (read, client detection, thread drafts, send only on approval) | BLOCKED BY USER SETUP | `gmail.py` | `gmail.signed_in()` | False | `helios gmail setup <client.json>` → `helios gmail login` | Google Cloud client |
| Polarion (read-only REST `/polarion/rest/v1`) | BLOCKED BY USER SETUP | `polarion.py` | `polarion.configured()` | local: no token; server: no URL | `helios polarion token local`; set `server_url` | tokens |
| Reminders, jobs, routines, workflows, missions, RAG, weather, notifications, 3D | VERIFIED WORKING (tests) | respective modules | test suite | — | — | — |
| Public MCP server (15 tools) | VERIFIED WORKING | `mcp/helios_public_server.py` | stdio client test | 15 tools listed, gated, rate-limited | register in Antigravity (optional) | — |
| Safety gate (secrets, SSRF, outbound, destructive, panic) | VERIFIED WORKING | `hooks/pretooluse.py` | live deny checks + tests | 5/5 denied live | **YOLO mode was ON in your session** | — |
| `helios security` | VERIFIED WORKING | `security.py` | live | all checks OK | — | — |
| Dashboard (3 columns, real data, robot, model limits) | VERIFIED WORKING | `ui/*`, `dashboard.py` | screenshots of the running app at 3 sizes | no overflow; 2 bugs fixed | `helios restart` loads the server-side fixes | — |

## Defects found and fixed in this audit

1. **Robot showed "Online & Ready" while Helios was asleep.** The daemon never reported `dormant`.
2. **Briefing said "finished without problems" with 7 of 9 tasks skipped**, and kept a stale "still
   running" spoken text.
3. **Sleep during a Night Mode task went unreported**, and research kept starting topics after the
   window.
4. **Dashboard right panel was blank** when businesses existed but none qualified.
5. **Night indicator could present a manual run as last night** and didn't count skips.
6. **`prospects scan` researched and drafted** (`max_research=0` became the default 12).
7. **Schools were called "appointment businesses"**: an unsupported claim reaching drafts.
8. **Re-research left a stale draft with a false claim** in the approval queue, and would have
   overwritten a draft you edited.
9. **One slow page load became "your website is slow".** It is now re-timed before it counts.
10. **One overloaded Overpass server stopped discovery.** A fallback mirror was added.
11. **No editable proposal, and no NEEDS_PRICING view.** Both added.

Each fix has a test in `tests/test_gap_closure.py` or `tests/test_web_browser.py`.
