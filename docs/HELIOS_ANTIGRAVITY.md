# Helios — Antigravity (the brain engine)

Helios thinks with **Google Antigravity** through its official headless CLI, `agy`, signed in with
the user's Google account (Google AI Pro). There is no API key. Helios keeps the voice, memory,
persona, dashboard and **safety**; agy reasons and executes: files, terminal, web search / reading,
and Helios's own MCP tools.

`[brain] engine = "antigravity"` · `[antigravity] bin = "D:/Helios/tools/agy/bin/agy.exe"`

## Official interfaces only

Helios uses `agy -p`, `--output-format json | stream-json`, `--input-format stream-json`,
`--conversation <id>`, `--model`, `--effort`, `agy models` and `agy mcp add`, all documented in
`agy --help`. It does **not**:

- use the private `agentapi`
- extract or read tokens
- scrape conversation files
- reverse-engineer the IDE
- bypass authentication

The decision record is [ANTIGRAVITY_RUNTIME_AUDIT.md](ANTIGRAVITY_RUNTIME_AUDIT.md). Using Helios
*from* Antigravity (the IDE) goes through the public MCP server: [ANTIGRAVITY_MCP.md](ANTIGRAVITY_MCP.md).

## How a turn runs (`helios/agy_cli.py`, `helios/agy_brain.py`)

| Kind | How | Used for |
|---|---|---|
| **Live conversation** | **one persistent agy process**: `--input-format stream-json --output-format stream-json`, one NDJSON user message per turn on stdin, events streamed back. The conversation id is kept so the session can be **resumed** after a restart (`--conversation`) | chat and voice turns; tokens stream to the dashboard as they arrive |
| One-shot | `agy -p <prompt>` in a throwaway workspace, below-normal priority | side agents, missions, background jobs |
| Research mode | one-shot with **no MCP servers** and the gate allowing **only** `search_web` + `read_url_content` | research, leads, prospect verification |
| Tool-less JSON | one-shot where the gate denies every tool and the reply must be a JSON object | lesson extraction, triage |

Each workspace gets `.agents/hooks.json` (the gate + a PreInvocation marker hook),
`.agents/rules/helios.md` (persona) and `.agents/mcp_config.json` (the `helios` + `computer` MCP
servers). Recovery is built in:
- A stale conversation id retries fresh.
- "New conversation" restarts the session.
- A run past its timeout is killed with its whole process tree.

## Safety: the gate is the authority

agy's own permission prompts can't work headless, so Helios runs agy with
`--dangerously-skip-permissions` and makes **its own gate the only authority**:

1. **Every tool call** → `hooks/agy_pretool.py` translates agy's tool names (`run_command`,
   `view_file`, `call_mcp_tool`, …) → `hooks/pretooluse.decide()`. It is the same gate as every
   other caller (details in [HELIOS_SECURITY.md](HELIOS_SECURITY.md)).
2. **Gate proof per turn.** agy silently drops a malformed `hooks.json`, so the PreInvocation hook
   must write a marker before the first model step. No marker means the turn is **killed**.
3. **Tamper check.** After every tool step, `hooks.json` is re-hashed. Any change means the turn is
   killed.
4. **Fail closed.** The gate prints a decision on every path, because agy treats silence as "allow".
   Crashes and garbage input mean deny.
5. Hook commands are unquoted 8.3 paths (agy runs them via `cmd.exe` and mangles quotes).

## Models

`[antigravity]`:

| Setting | Model |
|---|---|
| `model` | blank = agy's default, for live chat |
| `light` | `gemini-3.8-flash-low` |
| `medium` | `gemini-3.8-flash-medium` |
| `heavy` | `gemini-3.1-pro-high` |

These choose the model for background work by router tier. `agy models` lists what the account
actually offers. The dashboard's Model Limits panel checks each configured slug against that list.

> **Open issue (found 2026-10-14):** the configured heavy tier `gemini-3.1-pro-high` is **not** in
> this account's `agy models` list (offered: gemini-3.8-flash high/medium/low, claude-sonnet-4-6,
> claude-opus-4-6-thinking, gpt-oss-120b-medium). Heavy background runs using it will fail until
> `[antigravity].heavy` is changed to an offered model. The dashboard flags it in orange.

## Quota and usage

agy has **no quota command**, so the provider's session / weekly limits, reset time and context
window are **not exposed**. The dashboard says exactly that instead of guessing. What Helios *can*
measure, it does:
- Every agy run's input/output tokens go to `data/usage.jsonl` (`helios usage`, the dashboard,
  `ai_usage`).
- Measured on 2026-10-14: about **26k input tokens per turn** for a bare, tool-less turn (mostly
  agy's own system prompt). A **live chat turn costs far more**: a one-line "What time is it?" in
  the real persistent session used **183k input / 13.6k output tokens**. The persistent session
  carries the conversation's history plus the persona and the MCP tool definitions; output
  includes the model's thinking. Starting a new conversation ("+" in the title bar) resets the
  history part.
- agy self-updates: 1.2.13 at setup, **1.3.5** on 2026-10-14; the adapter keeps working because it
  only uses documented flags.
  Per-reply memory write-back is therefore off (`memory_writeback = false`); Night Mode learns from
  the day's log instead.

## Verified

Live, 2026-10-14 (`D:\Helios\data\agy-test\p12-final-result.json`):

| Check | Result |
|---|---|
| `agy -p "Reply exactly: HELIOS ANTIGRAVITY CONNECTION WORKING" --output-format json` | rc 0, valid JSON (`conversation_id, duration_seconds, num_turns, response, status, usage`), exact reply, 20 s |
| Streaming + persistent session | two turns in **one process**, both streamed; turn 2 recalled a code word from turn 1; gate marker present both turns |
| Process recovery | the agy process was **killed**; a **new process** resumed the same conversation id and still recalled the code word; no gate failure |
| Engine reachability | `agy models` listed 6 models (cached probe, refreshed from the dashboard) |

Also live (final audit, 2026-10-14):
- Voice text "What time is it?" (synthesized by Kokoro, transcribed by faster-whisper) went
  through the running app's brain and returned "It is 11:01 PM, sir." — correct, in 21.8 s.
- `tests/test_gap_closure.py` adds the lifecycle cases:
  - a hung agy killed at its timeout, with the workspace cleaned up
  - stdout and stderr kept apart (garbage lines ignored; a result printed on stderr is not
    trusted)
  - a missing agy fails gracefully

Unit tests: `tests/test_antigravity.py` (translation, hard rails, marker + tamper kill, persistent
session, stale resume, restart, one-shot and research modes) with a scripted stand-in agy.
`tests/conftest.py` blocks the real agy binary in tests, so the test suite never uses your quota.

## Independence

Helios starts and works without the Antigravity IDE. If agy is missing or signed out:
- The dashboard robot shows **"Antigravity unavailable"** (with the reason).
- Voice, memory, Night Mode's non-AI tasks and the dashboard keep working.
- Other engines (`claude`, `lite`) remain selectable in `[brain].engine`.
