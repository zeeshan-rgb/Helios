# Helios — gap analysis (final audit, 2026-10-14 / 2026-10-19)

The audit prompt's requirements, checked against the real implementation. "Verification" is what
was actually run. Inventory detail: [HELIOS_FINAL_AUDIT.md](HELIOS_FINAL_AUDIT.md).

Priorities:
- **P0:** safety / startup / backend / secrets / data loss
- **P1:** core voice, agent, PC control, browser, memory
- **P2:** extraction, prospecting reliability, screen, dashboard accuracy
- **P3:** Night Mode improvements, polish

## Gaps found and closed

| # | Requirement (prompt §) | Was | Verification | Pri | Fix | Test |
|---|---|---|---|---|---|---|
| 1 | Never overwrite user work (§2, §11 "quote editable") | re-research **overwrote an edited outreach draft** | code read + test | **P0** (data loss) | edited drafts are kept and flagged `outdated` | `test_reresearch_never_overwrites_your_edited_draft` |
| 2 | Never invent facts about a business (§11) | schools told they "work by appointment"; a stale draft with that claim stayed in the queue | live scan + re-research | **P0** (false claims in outreach) | `ENROLMENT` journey for education; re-research that no longer qualifies withdraws the draft | `test_schools_are_not_called_appointment_businesses`, `test_rereseach_withdraws…` |
| 3 | Robot reflects actual assistant state (§18) | "Online & Ready" while asleep; wake reason never reached the UI | live `/dashboard/voice` | P1 | daemon posts `dormant` + wake fields; UI / orbs handle it | `test_dormant_state_is_reported…` + live |
| 4 | Morning briefing shows failures **and skipped jobs** (§15) | "finished without problems" with 7/9 skipped; stale "still running" | live briefing | P1 | skipped-task sentence + section; stale refresh | `test_briefing_never_calls…`, `test_stale_running_briefing…` |
| 5 | Handle missed schedules after sleep (§15) | sleep mid-task invisible; research ran topics 6 h past the window | real night record + heartbeat | P2 | sleep gaps recorded per task; scheduled-run deadline | `test_sleep_during_a_task…`, `test_research_stops…` |
| 6 | Quotation: proposal, editable (§11 "Quotation") | data only, no proposal document | code read | P2 | `prospecting/proposal.py`, `prospect_proposal` tool, CLI, dashboard button | 4 proposal tests + live |
| 7 | `NEEDS_PRICING` status (§13) | not listable | code read | P2 | derived view (CLI + dashboard filter) — not a new status, so research isn't blocked on pricing | `test_needs_pricing_view` |
| 8 | "Do not infer … from a request that failed once" (§11) | one slow load → "your website is slow" | live audit (7.6 s, not reproducible) | P2 | slow loads re-timed in a fresh context; best counts | `test_slow_audit_load_is_retimed…` |
| 9 | Retry limits / error handling for sources (§10) | one Overpass server; 504 = zero results | live scan | P2 | fallback mirror on transient errors only | `test_overpass_falls_back…` + live |
| 10 | Scan vs research distinction (§11) | `scan` researched and drafted | live scan | P2 | `max_research=0` honoured | `test_scan_only_never_researches` |
| 11 | Dashboard: no blank panels (§18) | right panel blank with unqualified businesses | live screenshot | P3 | empty state "Nothing qualified yet" | live screenshot |
| 12 | Night status accuracy (§15/§18) | a manual run could pass as "last night"; skips not counted | live dashboard | P3 | last **scheduled** night + skip count | `test_night_indicator…`, `test_dashboard_counts_skipped_tasks` |
| 13 | Antigravity lifecycle tests (§5 checks 7–10) | not covered | test review | P3 | timeout kill, stdout/stderr separation, missing CLI | 3 tests |
| 14 | Model-limit state tests (§17) | missing / stale / refresh / failure untested | test review | P3 | 3 tests | `test_gap_closure.py` |

## Requirements already met (verified, not re-implemented)

| Requirement | Where | Verification |
|---|---|---|
| Antigravity as the runtime; documented CLI only; no private API | `agy_cli.py`, `agy_brain.py` | live JSON, streaming, session, kill + resume (§5 checks 1–6, 11–15) |
| Fail gracefully without agy | `agy_brain._turn` | test |
| Voice stack modular (wake, VAD, faster-whisper on D:, RealtimeSTT, Kokoro, barge-in, mute) | `voice/*` | live daemon + TTS→STT→brain loop |
| Layered screen intelligence (UIA → OCR → screenshot → pixels last) | `computer/*`, `orchestrator.py` | live |
| Playwright browser subsystem, DOM / aria first | `web/browser.py` | live Edge incl. download |
| Trafilatura extraction with provenance, caching, robots, no bypassing | `web/scraper.py` | live static + rendered + a refusal |
| External repos audited with USE / OPTIONAL / REFERENCE / REJECT | `EXTERNAL_AGENT_STACK_AUDIT.md` | — |
| Prospecting: lawful sources (OSM), honest coverage, no Google Maps scraping | `prospecting/geo.py` | live scan; Maps links refused (tests) |
| Rate card only; never invents a price | `quote.py` | tests + live "Price to be confirmed" |
| Outreach never automatic; CONTACTED only with evidence | `store.py`, `outreach.py` | tests + dashboard test |
| WhatsApp only opt-in / official platform | scoring, quote, proposal | code |
| Memory distinctions, secrets refused, learning with approval | `memory_store.py`, `learning.py` | live + tests |
| Night Mode: allowlist, isolation, no core edits, power-aware | `night_mode/*` | real night 2026-10-14 |
| Project intelligence from manifests (10 configured) | `projects.py`, `health.py` | live briefing |
| Honest model limits (no fabricated quota) | `dashboard.limits` | live + tests |
| Dashboard layout / responsiveness | `ui/*` | running-app screenshots 1366 / 1536 / 1920 |
| MCP public server gated, curated | `mcp/helios_public_server.py` | stdio test, 15 tools |
| One safety gate for every new tool | `hooks/pretooluse.py` | live denies + acceptance tests |

## Not closed — and why

| Requirement | Status | Reason / next action |
|---|---|---|
| "Hey Helios" wake word | BLOCKED BY USER SETUP | train `hey_helios.onnx` (HELIOS_SETUP.md); clap / Ctrl+Alt+H work |
| Gmail end-to-end | BLOCKED BY USER SETUP | `helios gmail setup <client.json>` → `helios gmail login` |
| Polarion end-to-end | BLOCKED BY USER SETUP | `helios polarion token local`; `server_url` + token for the server |
| Scheduled prospecting | BLOCKED BY USER SETUP | `[prospecting] enabled`, areas, ₹ pricing |
| Voice lock | BLOCKED BY USER SETUP | "enroll my voice" |
| Heavy-tier model | BLOCKED BY USER SETUP | `[antigravity].heavy` isn't offered by the account |
| Acceptance C (Notepad typing) and D (visible-browser search) | IMPLEMENTED — NOT VERIFIED (this audit) | not run while you were using the PC (your Notepad holds work tabs); C passed live on 2026-09-30 |
| Physical mic → speaker voice turn | IMPLEMENTED — NOT VERIFIED | needs a person to speak; every stage verified separately |
| Provider quota display | NOT APPLICABLE | agy exposes no quota; the UI says so |
| Browser Use, OmniParser | NOT APPLICABLE | rejected in the stack audit |
| Research topics returning 0 results (3 of 5 last night) | open observation | worth a look at topic wording / agy search quality |
| Live chat cost (183k input tokens for one short turn) | open observation | persistent-session history + tools; start fresh conversations; consider a slimmer tool set for chat |
