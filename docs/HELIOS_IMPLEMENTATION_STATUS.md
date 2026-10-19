# Helios — implementation status

Status of the "Final multimodal / desktop intelligence integration" spec (phases 0–15), the
Business Prospecting engine and the dashboard, as of **2026-10-14**.

**DONE** = built **and** verified (live where it can be, tests always) · **PARTIAL** = built and
tested, but part of it isn't verified live or isn't complete · **BLOCKED** = waits on something only
you can do · **NOT STARTED**.

Regression: **1057 tests, 0 failures, 0 skipped** after the final audit (with
`HELIOS_LIVE_BROWSER=1` for the real-Edge test) — `.venv\Scripts\python.exe -m pytest tests -q`.

> **Final audit (2026-10-14/19):** the per-feature inventory with the audit's six statuses is in
> [HELIOS_FINAL_AUDIT.md](HELIOS_FINAL_AUDIT.md), the requirements matrix and fixes in
> [HELIOS_GAP_ANALYSIS.md](HELIOS_GAP_ANALYSIS.md), and live acceptance A–O in
> [HELIOS_TEST_REPORT.md](HELIOS_TEST_REPORT.md).
>
> Updates since this page was written:
> - **Research (F)** ran live in a real scheduled night: 4 new sourced findings.
> - The **RealtimeSTT** capture engine loaded live: "capture ready (2.2 s)".
> - The voice loop ran end to end (TTS → STT → brain → TTS); only the physical mic and speaker
>   weren't exercised.
> - Eleven defects were fixed; see the audit.

## Acceptance criteria (spec §29)

| | Criterion | Status | Evidence |
|---|---|---|---|
| A | Voice: "Hey Helios" → wake → listen → transcribe → reason → speak | **PARTIAL** | The loop works and was verified live (2026-09-30), started by double clap or Ctrl+Alt+H. The **spoken** wake word needs the custom model `data/wake/hey_helios.onnx`, which doesn't exist yet (**BLOCKED**, see below); until then the daemon reports the reason and the clap/hotkey wake. Not re-checked live this cycle (Helios wasn't running on 2026-10-14). |
| B | "What's on my screen?" → correct app + visible context | **DONE** | `screen_context` / `perceive` live on 2026-10-07 and 2026-10-14 (foreground app, visible browser page + URL). |
| C | "Open Notepad and type Hello Helios" → real execution | **DONE** | cua-driver live test (2026-09-30): the Notepad tab read back "Hello Helios" via UI Automation. |
| D | "Open Chrome and search for X" → browser execution | **PARTIAL** | Hidden-browser open/search/click verified live (Phase 3). For the visible browser the playbook uses `open_app` with a search URL (opens your default browser; unit-tested), but that full voice flow wasn't run live this cycle. |
| E | JavaScript-heavy page → rendered → structured → useful | **DONE** | Live 2026-10-08 (Phase 5) and 2026-10-14: forced render through headless Edge gave the same clean title / date / text; refusals (npm HTTP 403) are reported, never bypassed. |
| F | "Research MCP updates" → sources, content, summary, provenance | **DONE** | Live (2026-09-30), and in the real scheduled night of 2026-10-14 with the Phase 6 source reading: 4 new sourced findings. 3 of 5 topics returned nothing that night — worth reviewing topic wording. |
| G | Lead finder → post links, filtered contacts, rate-card prices, draft pitch, no outbound | **PARTIAL** | Live lead runs measured 2026-10-01; no-outbound, phone removal and rate-card pricing are enforced in code and tested. The Phase 6 filters (dead / old / [For Hire] posts dropped) aren't live-run yet (same reason). Reddit blocks bots → those leads are "seen via search". |
| H | "Remember X" / "What do you remember about X?" | **DONE** | Live on the real vault 2026-10-14 (save, recall, forget; a secret refused). |
| I | "Run Night Mode" → jobs → logs → report | **DONE** | Manual run live 2026-10-14 (completed, logged, report in the vault). |
| J | Antigravity handles a task → result back to Helios | **DONE** | Live 2026-10-14: JSON one-shot, streaming, persistent session, kill + resume. |
| K | Auto-send → BLOCKED · API key → BLOCKED · destructive → policy | **DONE** | Real-gate tests (`test_security_acceptance.py`); they found and fixed five destructive shell shapes that used to run unasked. |

## Phases

| Phase | Status | Notes |
|---|---|---|
| 0 External stack + Helios audit | **DONE** | `docs/EXTERNAL_AGENT_STACK_AUDIT.md` |
| 1 Windows UI intelligence (UIA, UFO² concepts) | **DONE** | `computer/uia_backend.py`, `controller.py`; pywinauto / UFO² reference-only |
| 2 Screen context / OCR (OmniParser optional) | **DONE** | Windows OCR, adaptive; OmniParser **not used** (no GPU) — by decision |
| 3 Browser abstraction (Playwright) | **DONE** | hidden headless Edge, SSRF-checked, risky actions confirm; live-tested |
| 4 High-level browser agent (Browser Use) | **NOT STARTED — by decision** | rejected in the audit (pins `mcp` 2.x, needs an API key, telemetry); Antigravity drives the `browser_*` tools instead |
| 5 Web scraping (Trafilatura) | **DONE** | `web/scraper.py`, `extractor.py`, cache, robots / bot-wall handling |
| 6 Lead finder / research on the browser pipeline | **PARTIAL** | built + tested; awaiting a live night (see F/G) |
| 7 Whisper / STT optimisation | **DONE** | benchmarked on real recordings: `base.en` kept, vocabulary prompt (WER 5.2 → 2.8 %), model load 16 s → 1.2 s; RealtimeSTT capture engine loaded live on 2026-10-14 ("capture ready (2.2 s)") |
| 8 Multimodal orchestration | **DONE** | `orchestrator.py`: `perceive`, `locate`; live-verified |
| 9 Memory improvements | **DONE** | new kinds, episodic expiry, wider secret refusal, unified recall; live on the vault |
| 10 Night Mode improvements | **DONE** | power-aware, browser closed after the night, memory hygiene; live manual run |
| 11 MCP integration improvements | **DONE** | discovery, rate limit, new read tools; stdio-verified |
| 12 Antigravity verification | **DONE** | live JSON / streaming / session / recovery |
| 13 Security hardening | **DONE** | acceptance tests + destructive-shell fix + self-check entry |
| 14 Full regression | **DONE** | 1033 / 0 / 0; browser download + accessibility tests added |
| 15 Documentation | **DONE** | this file + the docs below |

## Capabilities

| Area | Capability | Status | Doc |
|---|---|---|---|
| Voice | wake gesture (double clap), talk hotkey, VAD, faster-whisper STT, Kokoro TTS, barge-in, live transcript, spoken approvals | **DONE** | HELIOS_VOICE.md |
| | spoken wake word "hey Helios" | **BLOCKED** | needs the trained model file |
| | voice lock (speaker verification) | **BLOCKED** | built; off until you enroll your voice ("enroll my voice") |
| Screen | text screen context, dialogs/errors, selection, OCR fallback, privacy filter | **DONE** | HELIOS_SCREEN_INTELLIGENCE.md |
| Windows | semantic element actions via cua-driver, verify, recovery ladder | **DONE** | HELIOS_WINDOWS_CONTROL.md |
| | Electron apps in the background | limitation | expose almost nothing to UIA → OCR / foreground |
| Orchestration | perceive (incl. visible-browser URL), locate across UIA → DOM → OCR | **DONE** | HELIOS_SCREEN_INTELLIGENCE.md |
| Browser | hidden browser: open, read (text/links/meta/aria/html), snapshot refs, click/type/select/scroll, tabs, screenshot, download | **DONE** | HELIOS_BROWSER.md |
| Scraping | clean extraction, render-when-needed, provenance, cache, robots, bot walls reported | **DONE** | HELIOS_WEB_SCRAPING.md |
| Research | nightly topics, sourced findings, separate library | **DONE** | see F |
| Leads | worldwide paid-work leads, rate-card prices, pitch drafts, never contacts | **PARTIAL** | see G |
| Prospecting | OSM discovery, website audit, scores, packages, quotes, drafts, approval queue | **DONE** (engine) / **BLOCKED** (running) | HELIOS_BUSINESS_PROSPECTING.md — off until you set areas + ₹ pricing |
| Memory | categories incl. leads/browsing/screen, secrets refused, duplicates merged, learning with approval | **DONE** | HELIOS_MEMORY.md |
| Night Mode | schedule, isolation, reports, missed/interrupted nights, power-aware | **DONE** | HELIOS_NIGHT_MODE.md |
| | nights actually running | **BLOCKED** | Helios must be running (PC awake) 01:00–05:00; the last three nights were missed for that reason |
| Morning briefing | written + spoken | **DONE** | HELIOS_NIGHT_MODE.md |
| Antigravity | persistent session, gate proof, tamper kill, research mode, usage measured | **DONE** | HELIOS_ANTIGRAVITY.md |
| | provider quota / context window | **not exposed** by agy — shown as such, never guessed | HELIOS_ANTIGRAVITY.md |
| | heavy tier model | **BLOCKED** | `gemini-3.1-pro-high` isn't offered by your account — change `[antigravity].heavy` |
| MCP | internal server (99 tools), public server (15 curated, gated, rate-limited) | **DONE** | HELIOS_MCP.md |
| | Helios tools inside the Antigravity IDE | **BLOCKED** | your call: run the command from `helios mcp antigravity` |
| Gmail | read + draft replies to business contacts; send only on your approval | **BLOCKED** | built + tested; needs `helios gmail setup <client.json>` + `helios gmail login` |
| Polarion | read-only search / items | **BLOCKED** | built + tested; needs `helios polarion token local` (and `server_url` + token for the server) |
| Dashboard | three-column UI, live data only, model limits, real-state robot, approvals | **DONE** | HELIOS_DASHBOARD_UI.md — verified on a test server; shows in the app after `helios restart` |
| Performance | hardware detection, lazy loading, models on D:, OCR on demand, browser idle-close | **DONE** | HELIOS_ARCHITECTURE.md |
| Security | one gate for every caller, hard rails, fail-closed, audit logs, self-check | **DONE** | HELIOS_SECURITY.md |

## What only you can do (in order of impact)

1. **Keep Helios running overnight** (PC awake, `helios start`) so Night Mode runs — research, leads
   and prospecting get their live runs; also plug in the laptop (heavy tasks skip below 30 %
   battery).
2. **Fix the heavy model:** set `[antigravity].heavy` to an offered model (e.g.
   `"gemini-3.8-flash-high"` or `"claude-opus-4-6-thinking"`; list: `agy models`).
3. `helios restart` (when you're not mid-conversation) — loads the final audit's server-side fixes
   (briefing, Night Mode, prospecting reliability, Night indicator).
4. **Wake word:** train "hey Helios" and drop it at `D:\Helios\data\wake\hey_helios.onnx`
   (steps in `HELIOS_SETUP.md`). Optional: enroll your voice for the voice lock.
5. **Prospecting:** `[prospecting] enabled = true`, your areas, and ₹ prices in
   `[prospecting.pricing]`.
6. **Gmail:** Google Cloud desktop client → `helios gmail setup <client.json>` → `helios gmail login`.
7. **Polarion:** `helios polarion token local` (and `server_url` + `helios polarion token server`).
8. Optional: register Helios in Antigravity (`helios mcp antigravity`).

## Known limitations

- agy turns cost about 26k input tokens each (mostly agy's own system prompt); provider quota isn't
  visible to Helios.
- Some sites refuse automated readers (npm 403, Reddit's bot wall); Helios reports this and never works
  around it.
- OpenStreetMap lists few websites/contacts for small Hyderabad businesses — hence the web-search
  verification step; coverage is always "cells searched", never "everything".
- Electron apps in the background expose almost nothing to UI Automation.
- Windows OCR can misread words; results say so.
