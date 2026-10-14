# Helios — architecture

Helios is a personal Windows AI assistant: voice in, screen/browser/tool context, reasoning by the
Antigravity agent engine, every action through one safety gate, memory in an Obsidian vault, voice
out. This page is the map; each area has its own document (linked below).

```
                              HELIOS (app.py — tray, dashboard, orb, scheduler tick)
                                              │
        ┌──────────────────────┬──────────────┼──────────────────┬──────────────────────┐
      VOICE                  VISION         TOOLS            SCHEDULED WORK          MEMORY
  voice/daemon.py      computer/ (UIA, OCR)  web/ (Playwright,   night_mode/, jobs.py,   memory.py
  wake word · VAD ·    screen context,       Trafilatura),       briefing.py, research,  memory_store.py
  faster-whisper ·     element lookup        gmail, polarion,    leads, prospecting      (Obsidian vault)
  Kokoro TTS                │                prospecting               │                    │
        │                   └───────── orchestrator.py ─────────┘     │                    │
        │                      perceive → locate (UIA → DOM → OCR)     │                    │
        └──────────────── server.py (HTTP + SSE, :8769, token-gated) ──┴────────────────────┘
                                              │
                                   brain (agy_brain.py → agy_cli.py)
                                   persistent `agy` session, stream-json
                                              │
                        hooks/agy_pretool.py → hooks/pretooluse.decide()   ← THE gate
                                              │
                ┌─────────────────┬───────────┴────────┬──────────────────────────┐
         agy built-ins       MCP: helios           MCP: computer            (outside clients)
   files, terminal, web     mcp/helios_server.py   cua-driver (UIA click,   mcp/helios_public_server.py
                            (~100 Helios tools)     type, window state)      curated, gated, rate-limited
```

## One request, end to end

`voice → understanding → screen/browser/tool context → Antigravity reasoning → policy → execution →
verification → TTS`

1. **Voice** — the voice daemon (its own process) hears the wake word (or a double clap / Ctrl+Alt+H),
   records until the VAD hears silence, transcribes with faster-whisper (vocabulary-biased) and posts
   the text to the app. Its state (listening / follow-up / speaking / muted) goes to `/voice/state`
   and drives the orb, the dashboard robot and the HUD.
2. **Brain** — `agy_brain` sends the turn into one persistent `agy` session (Google Antigravity CLI,
   the official headless interface, signed in with the user's Google AI Pro account). The persona and
   rules come from `config/persona_helios.md`; a memory digest from the vault is added per turn.
3. **Context** — the brain calls `perceive` (foreground app, the page/URL in the user's own browser,
   dialogs, the hidden browser's tabs, voice state) and `locate` (a target across UI Automation →
   hidden-browser DOM → OCR), or `screen_context`, `web_extract`, research, memory, projects…
4. **Policy** — every tool call, built-in or MCP, passes `hooks/pretooluse.decide()` first: hard rails
   (secrets, outbound from background agents, SSRF, panic, destructive screen actions), then YOLO,
   then allow / ask. "Ask" shows Approve/Deny on the dashboard, Telegram or by voice; no answer =
   deny. Every agy turn must prove the gate loaded (a marker) and that `hooks.json` wasn't changed.
5. **Execution** — agy runs the tool; desktop actions go through cua-driver by element index, web
   work through Helios's hidden headless Edge (`web/browser.py`) or the clean reader (`web/scraper.py`).
6. **Verification** — the playbook requires checking the specific expected change (cua-driver
   `verify_state`, a fresh read, or the page the browser settled on) before claiming success.
7. **Reply** — tokens stream over SSE to the dashboard; the voice daemon speaks the reply with Kokoro.
   The exchange is logged to the vault's daily note; Night Mode learns from it later.

## Processes

| Process | Started by | What it does |
|---|---|---|
| `helios/app.py` | Scheduled Task at logon / `helios start` | tray, pywebview dashboard window, the 15-s scheduler tick (reminders, jobs, Night Mode, briefing), voice/orb supervisor |
| `helios/server.py` (thread in the app) | app | HTTP + SSE on 127.0.0.1:8769 for the dashboard, the voice daemon, the orb and the gate's Approve/Deny asks. Host + Origin + token checks |
| voice daemon (`helios/voice/daemon.py`) | app | mic, wake word, VAD, STT, TTS, barge-in, spoken permission answers |
| orb (`orb.html` / layered window) | app | the floating presence orb |
| `agy` | the brain | one persistent conversation session + one-shot runs for background work |
| `mcp/helios_server.py` | each agy session (`HELIOS_MCP_ROLE=internal`) | Helios's internal tools; refuses to start otherwise |
| cua-driver (`computer` MCP) | each agy session | Windows UI Automation actions |
| headless Edge | `web/browser.py`, on demand | Helios's hidden browser; closes when idle and after Night Mode |
| `mcp/helios_public_server.py` | an outside MCP client (e.g. the Antigravity IDE) | curated tools, each decided by the same gate |

## Code map

| Area | Files | Doc |
|---|---|---|
| Brain engines | `agy_brain.py`, `agy_cli.py`, `brain_factory.py` (+ `brain.py`, `gemini_brain.py`, `lite_brain.py` kept as alternatives) | [HELIOS_ANTIGRAVITY.md](HELIOS_ANTIGRAVITY.md) |
| Safety | `hooks/pretooluse.py`, `hooks/agy_pretool.py`, `permissions.py`, `protected.py`, `security.py` | [HELIOS_SECURITY.md](HELIOS_SECURITY.md) |
| Voice | `voice/*` (daemon, wake, vad, stt, tts, realtime_stt, speaker verify) | [HELIOS_VOICE.md](HELIOS_VOICE.md) |
| Screen + Windows | `computer/*` (uia_backend, ocr, controller, policy_bridge), cua-driver | [HELIOS_SCREEN_INTELLIGENCE.md](HELIOS_SCREEN_INTELLIGENCE.md), [HELIOS_WINDOWS_CONTROL.md](HELIOS_WINDOWS_CONTROL.md) |
| Orchestration | `orchestrator.py` (perceive, locate) | this page + [HELIOS_SCREEN_INTELLIGENCE.md](HELIOS_SCREEN_INTELLIGENCE.md) |
| Browser + web | `web/browser.py`, `web/policy.py`, `web/scraper.py`, `web/extractor.py`, `web/sources.py`, `web/cache.py` | [HELIOS_BROWSER.md](HELIOS_BROWSER.md), [HELIOS_WEB_SCRAPING.md](HELIOS_WEB_SCRAPING.md) |
| Memory + learning | `memory.py`, `memory_store.py`, `learning.py` | [HELIOS_MEMORY.md](HELIOS_MEMORY.md) |
| Night Mode + jobs | `night_mode/*`, `jobs.py`, `briefing.py` | [HELIOS_NIGHT_MODE.md](HELIOS_NIGHT_MODE.md) |
| Research, leads, prospecting | `research.py`, `leads.py`, `prospecting/*` | [HELIOS_PROSPECTING.md](HELIOS_PROSPECTING.md) |
| Projects + health | `projects.py`, `health.py` | (in-code docstrings; `helios projects --help`) |
| Gmail, Polarion | `gmail.py`, `polarion.py` | `HELIOS_SETUP.md` (local) |
| MCP | `mcp/helios_server.py`, `mcp/helios_public_server.py` | [HELIOS_MCP.md](HELIOS_MCP.md), [ANTIGRAVITY_MCP.md](ANTIGRAVITY_MCP.md) |
| Dashboard | `ui/index.html`, `ui/app.js`, `ui/dashboard.{js,css}`, `dashboard.py` | [HELIOS_DASHBOARD_UI.md](HELIOS_DASHBOARD_UI.md) |
| Hardware defaults | `hardware.py` (CPU/RAM/GPU/VRAM/disks/mic/browsers) | [HELIOS_VOICE.md](HELIOS_VOICE.md) |

## Where data lives

| What | Where |
|---|---|
| Settings | `config/settings.toml` (committed defaults), `config/secrets.toml` (git-ignored, tokens) |
| Persona / brain rules | `config/persona_helios.md` |
| Database (jobs, workflows, prospects, reminders, conversations) | `data/helios.db` (`data` → `D:\Helios\data`) |
| Memory vault | `D:\Helios\data\memory` (Obsidian) — memory items, daily logs, research library, leads, night reports, briefings |
| Usage log | `data/usage.jsonl` (every AI call's tokens) |
| Logs | `logs/` (security, permissions, mcp_public, night, jobs, antigravity, browser, voice, …) |
| Heavy files | `D:\Helios` — venv, models (HF cache), agy, cua-driver, browser data |

## Design rules (kept on purpose)

- **The model is never the security authority** — policy lives in code (the gate), not prompts.
- **Adapters, not rewrites** — external projects (Playwright, Trafilatura, faster-whisper, Windows
  OCR, RealtimeSTT) sit behind Helios's own interfaces; UFO² and pywinauto informed the design but
  aren't dependencies; Browser Use and OmniParser were rejected
  ([EXTERNAL_AGENT_STACK_AUDIT.md](EXTERNAL_AGENT_STACK_AUDIT.md)).
- **Official interfaces only** — Antigravity through the `agy` CLI; no private APIs or tokens.
- **Nothing contacts people on its own** — drafts and approval queues; sending is the user's act.
- **Night Mode never edits Helios's code** — it writes only to `data/`, `logs/` and the vault.
- **CPU-friendly** — lazy loading, models on D:, OCR only when UI Automation can't see, the browser
  closes when idle, heavy night tasks skip on a low battery.

Status of every capability: [HELIOS_IMPLEMENTATION_STATUS.md](HELIOS_IMPLEMENTATION_STATUS.md).
