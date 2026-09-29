# Helios — Claude Code sessions

**Read `handoff.md` first** (gitignored, repo root) — it is the living session-handoff for
this project: current state, architecture, hard-won gotchas, pending work. Keep it updated
as substantial work completes; Tim asks for "update handoff.md" hygiene.

Quick facts:
- **Python = the venv**, never system python: `.venv\Scripts\python.exe` (deps live there).
- **Tests**: `tools\run_tests.ps1`, or `.venv\Scripts\python.exe -m pytest tests -q` from
  the repo root. NEVER bare `pytest` from the root — it collects `.venv` and segfaults.
- **App control**: `helios start|stop|restart|status` (shim `~\.local\bin\helios.cmd`) or
  the Scheduled Task `Helios` (logon). Reliable restart: kill helios-path strays, confirm
  `http://127.0.0.1:8769/health` is DOWN, then start — a quick stop/start leaves the OLD
  instance running with old code.
- **Verify UI headlessly** against `http://127.0.0.1:8769` (token: `data\.session_token`,
  `X-Auth-Token` header; `/events` takes `?token=`). Never summon/steal Tim's screen —
  and never run tests that type or click while he's working.
- Restarting the app drops Helios's conversation session — coordinate with Tim.
- Console output stays ASCII (cp1252 consoles).

Telegram / CC-remote convention: the `telegram@claude-plugins-official` plugin is DISABLED
globally and enabled ONLY in this project's `.claude\settings.json` — so any Claude Code
session with cwd here (including coding sessions like yours) loads it and can contend for
the bot's single poller. The interactive "Claude Code Remote (Telegram)" window (launched
by `C:\Users\Tim\claude-remote.cmd`) owns the channel; tell: bot answers /status but not
messages → relaunch that window. Tim often drives sessions from his phone through it.
