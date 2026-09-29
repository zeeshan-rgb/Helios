# Helios — Architecture Audit (Phase 0)

- **Date:** 2026-09-29
- **Scope:** read-only inspection plus a full test run. No source code was changed in this phase;
  this document is the only file this phase created.
- **Base:** NotTimPunt/jarvis `main` @ `a425ffe` (v0.10.0) with local, **uncommitted** Helios changes.

---

## 0. Ground truth that differs from the master prompt

| Prompt says | Actual (verified) |
|---|---|
| Repo at `D:\Helios\helios` | Repo is at `C:\Users\SyedZeeshanMehdi\Desktop\Claude\Helios\helios`. `D:\Helios\helios` does not exist. |
| `HELIOS_SETUP.md` inside the repo | It is one level up: `Desktop\Claude\Helios\HELIOS_SETUP.md`. |
| `D:\Helios\venv` + repo `.venv` | Correct: `helios\.venv` is a **junction** to `D:\Helios\venv` (Python 3.11.9). |
| `D:\Helios\data` | Correct: `helios\data` is a **junction** to `D:\Helios\data`. |
| "258 tests pass" | **258 passed, 2 skipped.** The 2 skipped are whole modules (`test_model_routes.py`, `test_model_tool.py`) that need `trimesh` (the 3D stack is intentionally not installed). |
| `voice/` folder | Voice lives in `helios/voice/`. |
| Gemini CLI brain "blocked" | Confirmed for **personal Google-account OAuth**. The same code path **works with a Gemini API key** (see §12). |

Git: 81 modified files plus 4 new ones (`helios/gemini_cli.py`, `helios/gemini_brain.py`,
`hooks/gemini_pretool.py`, `tests/test_gemini.py`), plus one rename (`config/persona_jarvis.md` →
`persona_helios.md`). Nothing is committed on top of `a425ffe`, so there is no checkpoint yet.

---

## 1. Current architecture

One Windows process (`run_helios.pyw` → `helios/app.py:main`) plus helper processes:

```
app.py (pythonw, single instance, port 8769)
 ├─ server.py        ThreadingHTTPServer: dashboard UI, /message, /events (SSE), /permission/*,
 │                   /settings, /workflow/*; token-authenticated (data/.session_token)
 ├─ brain            built by brain_factory.build_brain() per [brain].engine
 │    ├─ brain.Brain              engine="claude"  → spawns `claude -p` (stream-json)
 │    ├─ gemini_brain.GeminiBrain engine="gemini"  → spawns Gemini CLI (stream-json)   [new]
 │    └─ lite_brain.LiteBrain     engine="lite"    → in-process OpenAI-compatible loop
 ├─ side_agent.SideAgentPool   background agents (max_parallel=3), one CLI process each
 ├─ missions.MissionManager    multi-agent supervisor/worker teams (mission_agent.py)
 ├─ workflows.WorkflowManager  trigger + step flows (docs/WORKFLOWS.md)
 ├─ scheduler.Scheduler        15-second tick: reminders, routines, jobs, missions, workflows,
 │                             proactive observers, quiet hours, opt-in screen checks
 ├─ telegram_bridge            optional phone channel (per-chat sessions)
 ├─ hotkeys / tray             Ctrl+Alt+J summon, Ctrl+Alt+Backspace panic
 ├─ orb process                orb_overlay.py → webview | layered | tk
 └─ voice daemon process       helios/voice/daemon.py (talks to the app over HTTP/SSE)

Brain child processes load MCP servers from config/mcp.json:
   "computer" → cua-driver.exe mcp   (UI-Automation computer use; NOT installed)
   "helios"   → mcp/helios_server.py (≈40 Helios tools, FastMCP stdio, mcp<2)
   composio_mcp.json (optional)
Storage: data/helios.db (SQLite), Obsidian vault (Markdown memory), logs/*.log
```

## 2. Runtime flow (a dashboard or Telegram message)

1. The UI POSTs `/message` (or Telegram polls) → `server.Handler` → `brain.run_turn(message, …)`.
2. Single-flight lock; a mid-turn message *steers* (preempt + merge) instead of being rejected.
3. The brain builds the system prompt: persona → tone → `memory.build_digest()` →
   `agents.orchestrator_brief()` (+ an engine note for Gemini).
4. `router.choose_model()` picks a tier (light/medium/heavy) by heuristics, with optional triage.
5. The CLI child streams events → `emit()` → `Hub.publish` → SSE to the dashboard, orb and voice.
6. Tool calls from the CLI run the permission hook (§6).
7. After the turn: history goes to SQLite; `memory.extract_and_write()` runs on a background
   thread; procedural "recipes" are saved if computer-use was involved.

## 3. Voice flow (`helios/voice/`, separate process)

```
Microphone (audio.py, sounddevice 16 kHz)
 → [dormant: clap.py double-clap only]
 → wake.py  openWakeWord (model = [voice].wake_word; custom .onnx path supported)
 → vad.py   Silero endpointer (silence_ms)
 → stt.py   faster-whisper (engines.py: faster-whisper | openai | groq)
 → intent.py  follow-up window gate (directed vs ambient speech)
 → bridge.py  POST /message to the app; SSE tokens back
 → tts.py   Kokoro ONNX sentence-streamed playback (engines.py: kokoro | openai | groq)
 → earcons, live-transcript HUD, VU level to the orb
```

Already present: configurable `tts_voice`, `tts_speed`, `tts_lang`, `tts_engine`, `stt_engine`,
`stt_model`, barge-in (`barge_in`, `barge_threshold`), follow-up without a wake word,
push-to-talk dictation (Ctrl+Alt+D), spoken Approve/Deny, optional speaker verification, and
panic silences playback.

## 4. Brain flow

- `brain_factory.build_brain` supports three engines: `gemini` → `GeminiBrain`, `lite` → `LiteBrain`, and anything else → `Brain` (Claude).
- `llm.complete_json` routes one-shot helper calls (memory extraction, router triage) by engine:
  - `claude_cli`
  - `gemini_cli`
  - the OpenAI-compatible client for lite providers
- **Lite already supports Gemini over the API** (`provider="gemini"`, key in secrets, OpenAI-compatible
  endpoint `generativelanguage.googleapis.com/v1beta/openai`), but only with the curated
  `lite_tools` set: no MCP, no computer use, no missions.
- Model tiers: `[router]` light/medium/heavy (Claude aliases); `[gemini]` light/medium/heavy
  (`flash-lite`/`flash`/`pro` aliases resolved by the Gemini CLI).

## 5. Tool flow

| Engine | Tools available | Where tools execute |
|---|---|---|
| claude / gemini | CLI built-ins (shell, files, web) + MCP (`computer`, `helios`, composio) | inside the CLI child process |
| lite | `lite_tools.LiteTools` (files, PowerShell, open_app, web fetch/search, system health, memory, reminders) | in-process |
| side agents / missions | same as their engine | separate CLI processes (`HELIOS_AGENT_ROLE=side`) |
| workflows | `agent`/`brain` steps → side agents; `notify`/`http`/`delay`/`condition` native | app process |

## 6. Safety flow

```
Claude:  CLI → PreToolUse hook (hooks/pretooluse.py:decide)
Gemini:  CLI → BeforeTool hook (hooks/gemini_pretool.py) → translate names → pretooluse.decide
Lite:    LiteTools.execute → _classify_key → same hard rails → permissions.classify → perms.create

decide():  side-agent outbound-send ban → computer-use content check (win+L / format c: denied;
           alt+F4 / pipe-to-shell asked) → panic flag → single-driver screen lock → SSRF deny →
           ~/.claude write deny → YOLO bypass (hard rails still apply) → classify() allow/ask →
           /permission/ask to the app (UI / Telegram buttons / spoken yes-no; 125 s) →
           app unreachable ⇒ deny
```

Protected patterns (`permissions.py`):
- `_SENSITIVE`: .env, .ssh, keys, .kdbx, .aws, .azure, gcloud, .kube, credentials/secret/password, the Windows and Program Files folders, browser profiles, cookies…
- `_PROTECTED_WRITE`: shell profiles, the Startup folder, .bat/.cmd/.ps1/.vbs/.scr, custom_tools.
- Self-modification: writes inside the Helios repo always ask.

Gemini-specific additions:
- The hook fails closed.
- A separate `GEMINI_CLI_HOME`.
- A SessionStart marker must exist at `init`, or the turn is killed.
- `@path` escaping.

**Gap:** there is no dedicated audit log beyond `logs/permissions.log` plus per-module logs, and no
single structured record of "tool X, input Y, decision Z, by whom".

## 7. Memory flow (`helios/memory.py`)

- Store = Obsidian vault (`[paths].vault`, default `~/Documents/Obsidian Vaults/LocalAI`):
  `Profile.md` (facts + preferences), `People/`, `Projects/`, `Daily/`, `_index.md`.
- Pre-turn `build_digest(message)` injects up to 16k characters: profile, relevant people and
  projects, recent days, and recalled recipes. The block is fenced against prompt injection.
- Post-turn `extract_and_write()` makes a light-model JSON extraction and applies add/update/remove
  facts and upserts notes, deduplicated and redacted (`_redact` strips secret-looking strings).
  Everything is serialized by `_VAULT_LOCK`.
- Procedural memory: the `recipes` table (tool sequences for computer-use tasks).
- Knowledge: `rag.py` + the `chunks` table (embeddings **via Ollama**, which isn't installed) and the
  `index_folder` / `rag_search` MCP tools.
- Skills: `agent_skills/skills/*/SKILL.md`. The `create_skill` MCP tool (asks first) writes into the
  **repo tree**.

## 8. Mission flow

`start_mission` (MCP) queues a supervisor → the scheduler dispatches it → `MissionManager.run_supervisor` →
`mission_agent.execute_run`:
- The supervisor spawns workers (`spawn_agent`), with depth and budget caps.
- Workers share a blackboard (`mission_log`: `post_finding` / `read_mission`), and each has a role persona.
- Every worker is a CLI process with `HELIOS_AGENT_ROLE=side`, which means no outbound sends.
- Panic kills worker PIDs recorded in `agent_runs`.

## 9. CLI flow

- `D:\Helios\bin\helios.cmd` runs `.venv\Scripts\python.exe helios_cli.py`. The commands are onboard/setup, start,
  stop, restart, update, status, where, gemini-login and uninstall.
- `start` spawns `pythonw run_helios.pyw`. `status` checks `/health`.
- `update` refuses while the tree is dirty.
- `uninstall` refuses on a git checkout.

---

## 10. Already implemented (verified by code reading, and by tests where noted)

| Capability | Files | Verified by |
|---|---|---|
| Rebrand (user-facing) | UI, CLI, setup.py, persona_helios.md | tests + manual CLI run |
| D: layout (venv/data/cache/bin/tools) | junctions + env vars | filesystem |
| Claude brain | brain.py, claude_cli.py | existing tests (not run live: no `claude` CLI) |
| Gemini CLI brain + gate | gemini_cli.py, gemini_brain.py, hooks/gemini_pretool.py | tests/test_gemini.py (31); real-CLI smoke: gate loaded ⇒ proceeds, missing ⇒ killed |
| Lite brain (incl. Gemini over the API) | lite_brain.py, lite_tools.py, llm.py | test_lite_brain.py, test_lite_tools.py |
| Model routing | router.py | test_router.py |
| Permission policy + hooks | permissions.py, hooks/*.py | test_permissions, test_yolo_hook, test_screen_content, test_side_agent_outbound, test_telegram_perms, test_gemini |
| Scheduler: reminders/routines/jobs/quiet hours | scheduler.py, sched_util.py, db.py | test_sched_weekly (partial) |
| Workflows engine | workflows.py | test_workflows.py |
| Missions | missions.py, mission_agent.py | no dedicated test |
| Memory (vault) | memory.py | test_memory_fence.py, test_recipes.py |
| Voice pipeline code | helios/voice/* | test_intent.py, test_tts_split.py |
| Custom wake-word .onnx loading | voice/wake.py | code only (no test) |
| MCP tool server | mcp/helios_server.py | test_forge_verify.py (custom tools only) |
| Deep research (background job) | helios_server.deep_research | no test |
| Skills authoring | helios_server.create_skill | no test |
| Weather, open_app, sysdoctor, notify | respective modules | test_weather, test_open_app, test_notify_tim |

## 11. Partially implemented

| Item | State |
|---|---|
| Voice at runtime | Code complete, but **no Kokoro models** (`data/voices/*.onnx|bin` missing), **no Whisper model** cached, **no openWakeWord models** downloaded, `wake_word = "hey_jarvis"`. Mic and speakers are present (Realtek). |
| Wake-word missing-model behavior | `wake.py` logs the error on every frame and returns False: voice stays silent with **no user-visible diagnostic**. |
| Memory | Vault folder exists (created by my own smoke test on 2026-09-28) with an **upstream seed "Profile — Tim"**. The Obsidian app isn't installed (not required; plain Markdown). No explicit categories for rules, decisions, research or skills; no forget/inspect UI. |
| Personalization | About 90 "Tim" references remain in **runtime prompts and tool docstrings** (memory.py 12, helios_server.py 14, permissions.py 11, pretooluse.py 10, daemon.py 8, agents.py 6, …). The model is still told the user is "Tim". |
| Research | `deep_research` is an ad-hoc background job; no configured topics, dedup, confidence or separate store. |
| Skills/rules learning | `create_skill` exists (asks first); nothing extracts rules or skills from corrections automatically. |
| Onboarding | Never run: `config/mcp.json`, `config/secrets.toml` and `config/claude_settings.json` don't exist; the `Helios` scheduled task isn't registered; the app has never been launched. |
| MCP for external clients | `helios_server.py` exists but was designed for **Helios's own brain**, behind the brain's hook (see risk R1). |
| Audit logging | Only scattered per-module logs. |

## 12. Blocked externally

- **Gemini CLI with a personal Google AI Pro account:** Google rejects it ("client no longer supported
  for Gemini Code Assist for individuals… migrate to Antigravity"). Not bypassed, by decision.
- **Antigravity as a Helios backend:** its `agentapi` needs the IDE-internal `ANTIGRAVITY_LS_ADDRESS`.
  Not used, by decision.
- **Not blocked:** the Gemini CLI with a **Gemini API key**. It was verified end-to-end against the real CLI with an
  invalid test key: authentication passed validation, the gate marker was written, and the stream reached the API.

## 13. Missing components (files and modules that don't exist yet)

| Blueprint item | Missing |
|---|---|
| Phase 1 | an engine named `gemini_api`; API-key reading from env/secrets for the Gemini engine; clear configuration errors |
| Phase 2 | a voice diagnostics command; a user-visible missing-model fallback; `[wake]`-style config keys (today they're under `[voice]`); `data/wake/hey_helios.onnx` (external training) |
| Phase 3 | **cua-driver install** (not in `%LOCALAPPDATA%\Programs\Cua`); no install instructions in this repo (installer targets the `cua-driver` branch; `docs/SETUP.md` doesn't cover it) |
| Phase 4 | categorized memory store, remember/recall/forget API, conversation summarization, inspect UI |
| Phase 5 | lesson extraction → rule/skill classification with provenance and confidence |
| Phase 6 | project manifests (`projects/*.yaml`), project scanner, change detection |
| Phase 7 | `helios/night_mode/` package |
| Phase 8 | research topics config, research store, dedup |
| Phase 9 | per-project health-check runner |
| Phase 10 | morning report generator (completed / observed / suggested / needs approval / failed) |
| Phase 11 | an **external-facing, restricted** Helios MCP server + `docs/HELIOS_MCP.md` |
| Phase 12 | Antigravity MCP setup instructions (and verifying where Antigravity reads its MCP config) |
| Phase 13 | job registry with status, next-run, failure state and history for built-in jobs (routines and workflows exist and can host these) |
| Phase 14 | structured audit log; explicit Night Mode allowlist; protected-path config |
| Docs | HELIOS_ARCHITECTURE, MEMORY, NIGHT_MODE, MCP, VOICE, SECURITY, PROJECTS, IMPLEMENTATION_STATUS |

### Dependency gaps

- **Runtime assets:**
  - Kokoro models (~350 MB, to D:)
  - the faster-whisper `base.en` model (auto-downloads to `HF_HOME` on D:)
  - openWakeWord feature models
  - cua-driver
- **Optional:** Ollama (for RAG embeddings; currently unusable), `resemblyzer` (speaker verification, pulls PyTorch).
- **Brain:** a Gemini API key (user-supplied; free tier available; Pro-tier models may need billing).
- **Not needed:** the Obsidian app (the vault is plain Markdown).

## 14. Risks and conflicts

| # | Risk / conflict | Impact | Mitigation |
|---|---|---|---|
| R1 | **Exposing `helios_server.py` to Antigravity bypasses the Helios hook.** The hook lives in the *brain's* CLI; an external MCP client calls tools directly (including `create_tool`, which writes and runs code, `start_mission`, and deletes). | High | Build a **separate, allowlisted** external server; enforce policy **inside** each tool (`permissions.classify` + app approval), never rely on the client. |
| R2 | Night Mode must never modify core code, but `create_tool` / `create_skill` write into the repo (`custom_tools/`, `agent_skills/`). | High | Night Mode gets its own tool allowlist; store learned skills and rules under `D:\Helios\data\…`, not the repo. |
| R3 | 85 uncommitted changes, no checkpoint. | Medium | Create a local branch and baseline commit (needs your OK). |
| R4 | Gemini API free-tier limits (and possible training-data use under free-tier terms); Pro models may be paid. | Medium | Default tiers flash-lite/flash; make heavy configurable; check Google's terms. |
| R5 | The Gemini CLI is an extra moving part (Node + a Google CLI that changes often; `tools.exclude` already deprecated). | Medium | Pin the version; keep the tests against a fake CLI; move exclusions to the policy engine later. |
| R6 | "Tim" persona leakage in runtime prompts. | Low–Med | Replace with a configurable user name from the profile. |
| R7 | C: has ~10 GB free; the proactive disk alert fires at <10 GB; the Whisper and HF caches must stay on D:. | Low | `HF_HOME` is already on D:; verify all downloads land on D:. |
| R8 | CUA install source isn't documented in this checkout. | Medium | Research the official cua-driver release in Phase 3 before installing anything. |
| R9 | Tests write to the real `logs/` folder (e.g. brain.stderr.log). | Low | Optional: redirect in conftest. |
| R10 | Repo location vs the prompt's `D:\Helios\helios`. | Low | Decide: keep it in `Desktop\Claude\Helios` (your earlier instruction) or move to D:. |
| R11 | RAG depends on Ollama (not installed). | Low | Keep RAG optional, or switch embeddings to the Gemini API in the Research phase. |
| R12 | Proactive nags (Downloads/battery/disk) may be noisy on first run. | Low | Tune `[proactive]` during onboarding. |

## 15. Recommended implementation sequence (smallest safe steps)

0. **Checkpoint** (with your approval): local branch `helios` plus a baseline commit.
1. **Gemini API brain:** add `engine = "gemini_api"` = the existing `GeminiBrain` / `gemini_cli`
   with **API-key authentication** (key from the `GEMINI_API_KEY` env var or `config/secrets.toml`
   `[gemini].api_key`, injected only into the child's environment and never logged). This reuses all
   orchestration, MCP, missions and the tested gate. Lite+Gemini stays as a no-Node fallback. Tests:
   provider selection, secret handling (never in argv or logs), missing-key error.
2. **Run onboarding pieces non-interactively where safe:** generate `config/mcp.json`, fix the vault
   seed and location, then **voice**: download Kokoro and Whisper to D:, add `helios voice-check`
   diagnostics and a missing-wake-model fallback (clap/hotkey plus a clear status).
3. **CUA:** research and install cua-driver from its official source, then run acceptance tests A–D.
4. **Memory:** extend the vault with category folders (`rules/`, `decisions/`, `research/`,
   `skills/`), plus remember/recall/forget and provenance metadata. Keep Obsidian compatibility and point
   `[paths].vault` at `D:\Helios\data\memory`.
5–10. Learning → projects → **Night Mode as jobs on the existing Scheduler/Workflows** → research
   → health → morning report.
11–12. External MCP server (restricted) → Antigravity MCP setup docs.
13–16. Scheduling registry, security hardening (audit log, allowlists), final acceptance tests, docs.

## 16. Test baseline

```
.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
258 passed, 2 skipped in 5.37s     (2026-09-29)
skipped: tests/test_model_routes.py, tests/test_model_tool.py  — no module 'trimesh' (3D stack not installed, by design)
```

## Decisions (user, 2026-09-29)

> **Superseded (2026-09-29):** the user's actual requirement is Helios on their **Antigravity account**,
> not a Gemini API subscription. The Gemini API provider will **not** be built. See
> `docs/ANTIGRAVITY_RUNTIME_AUDIT.md` (recommendation: official `agy` CLI headless backend).

1. ~~**Gemini API implementation → native Python provider** (`engine = "gemini_api"`, no Node/CLI).~~
   Implication: Phase 1 must add, to the native loop, the pieces the CLI engines get for free.
   These are an MCP client for Helios's servers (`helios`, `computer`, composio), tool calling,
   sessions/history, streaming, side-agent and mission support, and every tool call through
   `hooks/pretooluse.decide` / `permissions.classify` + the approval flow (the same chain
   `lite_tools` already uses). Build on `lite_brain`'s loop rather than duplicating it.
2. **Repo location → stays in `Desktop\Claude\Helios\helios`** (heavy data remains on D:).
3. **No git commits during the project.** Commit/push to the user's GitHub only after the whole
   project is complete. Consequence (R3): no rollback checkpoints, so keep each phase's diff
   small and re-run the full suite after every phase.
4. **Memory → `D:\Helios\data\memory`** (Obsidian-compatible Markdown with the blueprint's categories).
