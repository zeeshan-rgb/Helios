# Helios

[![version](https://img.shields.io/badge/version-0.9.0-1f6feb?style=flat-square)](https://github.com/NotTimPunt/jarvis) ![python](https://img.shields.io/badge/python-3.12-3776ab?style=flat-square&logo=python&logoColor=white) ![platform](https://img.shields.io/badge/platform-Windows%2011-0078d6?style=flat-square&logo=windows&logoColor=white) ![brain](https://img.shields.io/badge/brain-Claude%20Code-d97757?style=flat-square) ![status](https://img.shields.io/badge/status-alpha-orange?style=flat-square)

A local, always-on AI assistant for Windows in the spirit of Iron Man's Helios. It
**routes its thinking to Claude** (via your Claude Code subscription — no API key needed;
API-key and local/Ollama "lite" brains also supported), **controls your PC like a human**
(reads the UI tree, clicks, types — in the background, without stealing your cursor),
**talks and listens** (fully on-device voice), and keeps a **living memory** inside your
Obsidian vault.

```
"hey Helios" ──▶ wake word → whisper STT ─┐
You ──▶ chat window / orb / tray / phone ─┴─▶ Brain (claude -p, streaming)
                                               │  • auto-routes model per task (haiku/sonnet/opus)
                                               │  • computer-use (UI-tree click/type, background)
                                               │  • shell + files + skills + Composio + own tools
                                               │  • workflows · multi-agent missions · reminders
                                               │  • risky actions ask you Approve/Deny (or on Telegram)
                                               ▼
                                     Obsidian vault = memory   ·   Kokoro TTS speaks the reply
```

## Install

One line in PowerShell — installs Python if needed, downloads Helios, sets up the venv + deps, and
adds a `helios` command to your PATH:

```powershell
irm https://raw.githubusercontent.com/NotTimPunt/jarvis/cua-driver/install.ps1 | iex
```

Then connect a brain and onboard:

```powershell
helios onboard
```

Full walkthrough (brain choices, voice engines, onboarding modes, apps) in
**[docs/SETUP.md](docs/SETUP.md)**.

## The `helios` command

```powershell
helios            # start in the background
helios stop       # stop it
helios restart    # reliable restart (kills strays, waits for health)
helios status     # running? version? health?
helios onboard    # (re)run the setup wizard
helios update     # update in place — settings preserved, stops/updates/restarts
helios uninstall  # remove everything (--purge also deletes the vault + model caches)
```

## Capabilities

- **Brains, tiered** — default is **Claude Code** (`claude -p`, your subscription, full tool
  power). Or pick a **lite brain**: any OpenAI-compatible provider (OpenRouter, OpenAI, Gemini,
  Groq, **Ollama** for fully-local) with a curated, permission-gated toolset.
- **Voice, on-device** — "hey **helios**" wake word → faster-whisper STT → **Kokoro** TTS
  (British butler by default), sentence-streamed so it starts talking immediately. Barge-in
  ("hey Helios" interrupts it mid-reply), live transcript HUD, push-to-talk dictation
  (Ctrl+Alt+D), spoken Approve/Deny for permissions, optional speaker verification. Cloud
  TTS/STT engines (OpenAI, Groq) are pluggable.
- **Controls the PC** — via the **UI Automation tree** (cua-driver): finds elements by index,
  clicks and types in the background without grabbing your mouse, verifies the result, and
  recovers (retry → replan → substitute → escalate).
- **Workflows** — Activepieces-style automations, native: a trigger + ordered steps
  (agent / brain / notify / http / delay / condition) that pass data via `{{step.output}}`.
  Build them in the dashboard or just say *"Helios, make a workflow that…"*. See
  **[docs/WORKFLOWS.md](docs/WORKFLOWS.md)**.
- **Multi-agent missions** — for big tasks a supervisor decomposes the goal and spawns a team
  (researcher / operator / coder / organizer / writer) on a shared blackboard, live in the dashboard.
- **Obsidian memory** — reads a relevant digest each turn, writes new facts back
  (Profile/People/Projects/Daily), self-edits stale facts, and remembers **how** it did
  computer-use tasks (procedural recipes). Injection-fenced.
- **Risk-based permissions** — autonomous for safe actions; asks first for system changes,
  sensitive files, sending, money, installs. **YOLO mode** (per-chat auto-approve, hard rails
  stay). Prompts from phone-started turns show up **on Telegram as buttons**. Panic hotkey
  stops everything instantly.
- **Proactivity** — reminders & timers, recurring routines, background jobs, battery/disk nudges
  (quiet hours), and a **system doctor** ("why is my laptop stuttering?" → live CPU/RAM/GPU/thermal
  diagnosis).
- **Knowledge** — local document indexing + semantic search (RAG via Ollama) and web research.
- **Composio** — Gmail, Calendar, Drive, GitHub and 500+ apps (reads autonomous, sends gated).
- **Phone** — a Telegram bridge with **persistent per-chat sessions** (your phone is its own
  continuous conversation; `/new` resets it).
- **Skills** — ships agent skills as a Claude Code plugin (`agent_skills/`); Helios can author
  new ones for itself (gated).
- **The dashboard** — a frameless quiet-luxe chat window: slide-over drawer for
  history/missions/workflows/settings, a **live tool timeline** showing what Helios is doing
  mid-turn, session token/cost stats, YOLO toggle, mid-turn **steering** (type while it works to
  fold in corrections), voice HUD, resize from any edge.
- **The orb** — a floating **neural-network orb** rendered in a per-pixel-alpha layered window
  (true transparency + click-through, GPU-independent). It breathes when idle, cascades with
  sparks while thinking, pulses to the voice level while speaking, and hides for fullscreen apps.
  Click it to summon the dashboard.

## Run

Helios auto-starts at logon (a Scheduled Task named **Helios**) — by default **hidden and
dormant**: no windows, the mic listening only for a **double clap**.

- **Wake it:** clap twice — the orb materializes (and your wake apps, e.g. Spotify, open).
- **Summon the chat window:** **Ctrl+Alt+J**, click the orb, or the tray icon.
- **Sleep / shut down:** the power menu, top-left of the dashboard.
- **Panic stop:** **Ctrl+Alt+Backspace** (or the ■ Stop button) — halts screen control, kills the
  current turn, denies pending permission prompts.
- **From your phone:** message the Telegram bot (token in `config/secrets.toml`).

## How it works

- **Brain** (`helios/brain.py`) spawns `claude -p` streaming with a resumable session;
  `helios/brain_factory.py` swaps in `lite_brain.py` (OpenAI-compatible loop) when
  `[brain].engine = "lite"`. Every spawn gets `--max-turns` + `--fallback-model` guardrails.
- **Model routing** (`helios/router.py`) — `light` → haiku (quick chat), `medium` → sonnet
  (normal work + Q&A), `heavy` → opus (debugging, multi-step engineering). Force one with a
  `/haiku` `/sonnet` `/opus` prefix. Aliases always resolve to the latest models.
- **Computer use** — the **cua-driver** MCP server drives Windows through the accessibility
  tree (`get_window_state` → numbered elements → `click_element`/`type_text_in`), with an
  elevated helper daemon for UWP apps.
- **Permissions** — every tool call passes a **PreToolUse hook** (`hooks/pretooluse.py`):
  hard rails (panic flag, SSRF, self-modification, destructive key-combos typed at the screen)
  → YOLO check → `permissions.classify()` → allow, or an Approve/Deny prompt in the UI /
  on Telegram / spoken by voice. Side agents can never send outbound (structural rule).
- **Memory** (`helios/memory.py`) — the Obsidian vault **is** the memory: digest in, extracted
  facts out, every turn. Voice, workflows, missions and the scheduler all reuse the same brain.
- **Workflows** (`helios/workflows.py`) + **missions** (`helios/missions.py`) run their agent
  steps through the side-agent pool, so everything stays hook-gated even unattended.

## Configuration — `config/settings.toml`

| Key | Meaning |
|---|---|
| `brain.engine` | `claude` (default, full power) or `lite` (OpenAI-compatible providers) |
| `paths.vault` | Obsidian vault used as memory |
| `router.auto / light / medium / heavy` | model routing |
| `voice.*` | wake word, TTS voice/speed, STT model, barge-in, live transcript |
| `startup.hidden / wake_gesture / open_on_wake` | dormant boot, double-clap wake, wake apps |
| `hotkeys.summon / panic` | global hotkeys |
| `autonomy.allow / mode` | tools allowed with no confirmation; onboarding posture |

Persona: `config/persona_helios.md` · MCP servers: `config/mcp.json` · keys:
`config/secrets.toml` (all gitignored templates generated by onboarding).

## Logs

`logs/` — `app.log`, `brain.log`, `server.log`, `permissions.log`, `voice.log`.

## Version highlights

| | |
|---|---|
| **v0.9.0** | Dashboard revamp (quiet-luxe 2.0: drawer, live tool timeline, stats popover, living canvases) + the **neural orb** rewritten as a per-pixel-alpha layered window |
| v0.8.x | **Workflow engine** + dashboard tab, procedural memory, skills plugin, Telegram permission buttons, security hardening, model guardrails, persistent phone sessions |
| v0.7.0 | Public one-line installer + onboarding wizard, tiered brains (Claude / lite), pluggable voice engines, `helios` CLI (incl. `update`, `uninstall`) |
| v0.5–0.6 | YOLO mode, mid-turn steering, all-edge resize, Spotify Web API wake-play, barge-in, computer-use recovery policy, system doctor, eval harness |
| v0.2–0.4 | On-device voice stack (wake word / STT / TTS / HUD), hidden+dormant boot, double-clap wake, power menu, orb, security + audit hardening (89 findings) |
