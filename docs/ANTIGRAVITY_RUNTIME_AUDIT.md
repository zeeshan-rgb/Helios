# Antigravity Runtime Audit — can Helios run on the user's Antigravity account?

- **Date:** 2026-09-29.
- **Scope:** audit only. No Helios code was changed.
- **Test baseline:** re-run and preserved at **258 passed, 2 skipped** (`trimesh`-dependent modules, by design).
- **Requirement:** "Hey Helios" → an autonomous desktop assistant that uses the user's **Google Antigravity
  account / Google AI Pro entitlement**, with **no separate Gemini API key**.

Evidence labels used below:
- **[DOC]** an official Google Antigravity documentation page.
- **[TERMS]** the official Terms / FAQ.
- **[GH]** the official `google-antigravity` or `google-gemini` GitHub repo.
- **[FORUM]** a Google AI Developers Forum reply; the responder is **not** marked as Google staff, so it isn't authoritative.
- **[LOCAL]** verified on this machine.
- **[UNVERIFIED]** not documented; must be tested after install.

## Correction to earlier advice

Earlier in this project I said I knew of no supported way for Helios to use the Antigravity account. That was
wrong. I had only inspected the IDE-internal `agentapi` and never searched for the separate, official
**Antigravity CLI (`agy`)**, which has a documented headless mode. The decision **not** to touch
`agentapi`, tokens or private files still stands and is unaffected by this audit.

## Local state [LOCAL]

- `agy` is **not installed**: it isn't on PATH and `%LOCALAPPDATA%\agy\bin` is absent.
- The Antigravity IDE 1.107.0 and the Antigravity app are installed.
- The installer is `irm https://antigravity.google/cli/install.ps1 | iex` and puts the binary in
  `C:\Users\<user>\AppData\Local\agy\bin` [DOC install]. That's a small binary, acceptable on C:.

---

## The 13 questions

### 1. Can `agy` authenticate with my Google account, with no Gemini API key? — **YES**

- [DOC install] Sign-in uses the OS keyring (Windows Credential Manager) and falls back to Google Sign-In in the
  browser. A Gemini API key is **optional**: it's only needed when `modelProvider` is set to `gemini` for
  browserless CI.
- [GH gemini-cli #27274, maintainer announcement] On 2026-06-18 the Gemini CLI stopped serving Google AI Pro/Ultra
  and free users; those tiers are now served through the Antigravity CLI.
- [DOC plans] Google AI Pro gives a "High, generous quota" and "Access to all product features, such as … the CLI".

### 2. Can an external application launch `agy` programmatically after I've signed in? — **YES (documented mechanism)**

- [DOC headless] Headless mode exists "to script agent tasks, integrate with CI pipelines, and capture
  machine-readable output" and for when "you need the agent's output in a program instead of a terminal UI".
- It requires prior authentication; with no cached credentials, it fails with "authentication required".
- [FORUM 183051] "Launching the official agy binary as a local child process in headless mode … while relying on
  agy's cached Google credentials is a supported workflow. It consumes the exact same account entitlements."
  (Not staff-verified. See Q13.)

### 3. Can stdin stream-json keep a persistent multi-turn conversation? — **YES**

[DOC headless]
- The command is `agy --input-format stream-json --output-format stream-json`, **without `-p`** (combining `-p`
  with stdin input drops the prompts).
- Input lines have the form `{"event":"user","message":{"content":"…"}}`; only `text` blocks are supported.
- "The process exits after the input pipe is closed and the current turn completes."
- Alternative: one process per turn, `agy -p "…" --conversation <id>`, or `--continue`.
- Limits:
  - Slash commands in a streaming session end in `ERROR` with exit code 2.
  - Malformed JSON ends the session with exit code 1 or 2.

### 4. Can Helios read the agent's replies programmatically? — **YES**

[DOC headless] The output is NDJSON with three event types:
- `init`: `conversation_id`, cwd, tools, `permission_mode`, model, agent.
- `step_update`: `step_type` (`user_input` / `agent_response` / `tool` / `checkpoint`…), a streaming
  `text_delta`, `tool_info{name,parameters,output}`, `subagent_info`, and `usage`.
- `result`: one per turn, with `status` (SUCCESS / ERROR / CANCELED / INTERRUPTED / INVALID / WAITING /
  RUNNING), `response`, `error`, `num_turns`, token `usage`, and optional `structured_output` (`--json-schema`).

### 5. Can tools run inside that session? — **YES**

- [DOC headless] Tool calls appear as `step_update` events with `step_type: tool` and `tool_info`.
- Headless permission behavior: reading and writing files **inside the workspace is auto-allowed**. Shell
  commands default to **Ask**, and "a tool that requires approval it cannot obtain is **soft-denied**: the run
  continues, exits 0, and prints a notice to stderr."
- Tools are enabled with `settings.json` `permissions.allow` rules such as `command(git)`,
  `command(regex:npm run (build|lint|test))` or `write_file(src/)`, or with `--dangerously-skip-permissions`
  (auto-approves everything).
- [DOC hooks] A **PreToolUse** hook can return `allow | deny | ask | force_ask | deny_unless_prior_grant`
  (global file `~/.gemini/config/hooks.json`, or workspace `.agents/hooks.json`; 30 s default timeout).
  [UNVERIFIED] Whether hooks run in headless mode, and whether a failed or timed-out hook fails open or closed.

### 6. How do permissions behave in Turbo mode? — **Turbo is an IDE preset, not a CLI mode**

- [DOC agent-settings] Turbo (macOS/Linux): "All commands run without prompting with no isolation or
  restrictions, and the agent has full read and write access to your filesystem."
- On Windows the closest IDE setting is "Always Proceed": everything runs except what's on the Deny list.
- [DOC cli/modes] The CLI's modes are `default` (review), `accept-edits` and `plan`. There is **no Turbo** mode.
  The CLI equivalent of Turbo is `--dangerously-skip-permissions`.
- [DOC settings] Preferences and permissions sync between the IDE and the CLI, so IDE settings can affect CLI runs.
- **Helios position (revised after testing, see "Verified on this machine"):** never Turbo and never widen
  global agy/IDE settings. `--dangerously-skip-permissions` is used **only** on Helios's own agy child
  process, because it's the only way headless tools can run. Helios's PreToolUse hook is then the sole
  approval authority, with verified fail-closed behavior plus a per-turn marker kill-switch.

### 7. Can Antigravity access files across the machine? — **Only with explicit rules**

[DOC permissions]
- Workspace files are allowed without prompting. Files outside the workspace **require approval**, or an allow rule
  such as `read_file(/path)`, which grants recursive access.
- Precedence is **Deny > Ask > Allow**.
- In headless mode, "Ask" means soft-deny unless a rule or hook allows the action. So in headless mode
  machine-wide access is closed by default, and Helios would open specific folders (project allowlist)
  and deny credential stores.

### 8. Can Antigravity use browser or computer tools? — **Browser YES; desktop mouse/keyboard NO (natively)**

- [DOC permissions/mcp/features] There's browser automation (`execute_url`) and page fetching (`read_url`),
  and a browser subagent integrated with **Chrome DevTools MCP**.
- There's no native desktop computer-use (mouse/keyboard on arbitrary Windows apps) in the docs.
- **MCP is supported**: `~/.gemini/config/mcp_config.json`, or workspace `.agents/mcp_config.json`, with stdio or
  remote servers, and `mcp(server/tool)` permission rules.
- ⇒ Helios can give Antigravity **desktop control by exposing its own `computer` (cua-driver) and `helios`
  MCP servers** to agy, still gated by Helios's policy.

### 9. Can Helios send voice-derived prompts to the agent? — **YES**

A transcribed utterance is plain text, which becomes one `{"event":"user",…}` line on stdin (or a `-p` argument).
Nothing in the docs restricts where prompt text comes from.

### 10. Can Helios receive the reply and speak it through TTS? — **YES**

`step_update.text_delta` (agent_response) streams text, and `result.response` is the final text. Helios's existing
sentence-streaming Kokoro pipeline consumes text tokens today (the SSE `token` events), so this maps directly.

### 11. Limits for long-running and background sessions

- [DOC headless] `--print-timeout` defaults to **5 minutes** (configurable). A persistent stdin session exits when
  stdin closes.
- [GH #1044, open, v1.2.6, **Windows 11**] In `-p` mode a turn can end `SUCCESS` while a `run_command` is still
  running, and **the CLI kills it on exit**. The workaround is to instruct the agent to poll until done, and
  to prefer the persistent session.
- [GH #1077] A delegated subagent's output may not appear on the parent stream: `SUCCESS` with thin text.
- The agent is single-flight per conversation. Parallel work needs parallel conversations, and each costs quota.

### 12. Limits for Google AI Pro quotas

[DOC plans; blog.google]
- Pro quota refreshes **every 5 hours until a weekly limit is reached**. Usage "correlated with the amount of
  work done by the agent".
- When quota runs out, the run waits for the refresh, or uses purchased AI credits if overages are set to "Always".
- [FORUM] Headless runs use the **same** entitlement as interactive use. [DOC] doesn't state it explicitly.
- ⇒ Night Mode, research and background agents all draw from the **same** 5-hour and weekly pool as live chat.
  Helios needs a usage budget and should keep cheap helper calls (memory extraction, triage) local or rare.

### 13. Is this architecture supported by Google? — **The mechanism is documented; the account-policy fit is not explicitly confirmed**

- **For:**
  - Headless mode and stream-json are official, documented interfaces for use "in a program" [DOC].
  - The Gemini-CLI shutdown moved AI Pro users to exactly this CLI [GH].
  - A forum reply draws the line as "Supported: invoking the official agy binary via standard process pipes.
    Unsupported: extracting OAuth tokens, reusing credentials in custom HTTP clients, or calling backend
    endpoints directly" [FORUM, not staff-verified].
- **Against / risk:**
  - [TERMS] "Using third party software, tools, or services to access the Service (e.g. using OpenClaw with
    Antigravity OAuth) is a breach of this Agreement… may be grounds for suspension or termination."
  - The FAQ's examples are Claude Code, OpenClaw and OpenCode *with your Antigravity login*.
  - The terms also forbid "using the Service in connection with products not provided by us" under
    "abuse, harm, interfere with, or disrupt".
- **Assessment:** the prohibited pattern is a third-party client using the Antigravity **login/OAuth**
  itself. Helios would launch the **official agy binary**, which does its own authentication, through a
  documented interface, and would never touch credentials. That's the pattern the forum reply calls supported.
  But **no official page explicitly authorizes a personal wrapper**, and the "in connection with products not
  provided by us" wording is broad. **Residual risk to the account is low but non-zero.** Mitigation: ask Google
  for written confirmation (forum thread 178472 asks exactly this and has no answer yet), keep usage
  personal and single-user, and stay strictly within the documented interface.

---

## Comparison

| | **A. Gemini API provider** | **B. Antigravity CLI (`agy`), account-authenticated** | **C. Antigravity SDK (`google-antigravity`)** |
|---|---|---|---|
| Uses your AI Pro entitlement | ❌ Separate API billing / free tier | ✅ Same entitlement as interactive use | ❌ Signs in **only** with `GEMINI_API_KEY` or Vertex/GCP credentials [DOC sdk] |
| API key needed | Yes | **No** | Yes (or a GCP project) |
| Agent capability | Whatever Helios builds (loop, tools) | Full Antigravity harness: files, terminal, web, browser subagent, subagents, skills, MCP | Full harness in-process (tools, hooks, policies, sessions) |
| Desktop mouse/keyboard | Via Helios tools | Via Helios `computer` MCP server (cua-driver) | Via MCP / custom tools |
| Helios policy enforcement | Native (in-process) | PreToolUse hook → Helios `decide()` + agy permission rules (hook behavior in headless mode needs a test) | SDK lifecycle hooks + policy engine (in-process) |
| Integration effort | High (new loop + MCP client + tools) | **Medium**: mirrors the existing `gemini_brain`/`gemini_cli`/hook pattern | Medium–High (async Python harness) |
| Dependencies | `openai`/`google-genai` | the `agy` binary (Go, from Google), browser sign-in once | `pip install google-antigravity` (preview) |
| Terms risk | None | Low but non-zero (see Q13) | None (API/Vertex billing) |
| Quota | API rate limits / billing | 5-hour + weekly AI Pro pool, shared with your IDE use | API/Vertex quotas |
| Meets "use my Antigravity account" | ❌ | **✅** | ❌ |

## Recommendation

**Option B, the official Antigravity CLI with account sign-in, is the only option that meets the requirement.**
A and C both need an API key or GCP billing. Build it the same way the Gemini CLI brain was built:
Helios stays the orchestrator, and agy is the execution backend.

```
"Hey Helios" → wake → STT → Helios orchestration (persona, memory digest, routing, policy)
   → AntigravityBrain: persistent `agy --input-format stream-json --output-format stream-json`
        (cwd = Helios workspace; per-turn fallback `agy -p … --conversation <id>` for recovery/panic)
   → agy tools + Helios MCP servers (helios tools, computer/cua-driver, Chrome DevTools MCP)
        every tool call → agy PreToolUse hook → hooks/pretooluse.decide()  (panic, SSRF, screen lock,
        approvals via UI/Telegram/voice, protected paths) — no --dangerously-skip-permissions, no Turbo
   → step_update.text_delta → Helios SSE tokens → Kokoro TTS;  result → history + memory write-back
```

Helios keeps voice, wake word, TTS, memory, persona, orchestration, safety, reminders, Telegram and project
intelligence. Antigravity only executes.

**Preconditions and first verification steps (Phase 1, after your go-ahead):**
1. **You** install agy (the official installer; I'll show you the exact command, and you approve the download),
   then run `agy` once and sign in with Google. I never see or handle credentials.
2. Empirically verify the [UNVERIFIED] items before relying on them:
   - PreToolUse hooks fire in headless mode.
   - What happens on hook error or timeout (if it fails open, add a start-of-turn check like the Gemini marker).
   - Soft-deny behavior.
   - Loading MCP servers from workspace `.agents/mcp_config.json`.
   - Stream-json field names as emitted by the installed version.
3. Ship with deny rules for credential stores (`.ssh`, browser profiles, Credential Manager exports, `.aws`,
   `.azure`, `gcloud`…) and a per-project read/write allowlist. Keep IDE-synced settings from widening CLI
   permissions.
4. Add a quota guard: count turns and tokens from `result.usage`, and set a Night Mode budget.
5. Optional but advised: post a short question on the Google AI Developers Forum to get written confirmation
   that a personal, single-user voice wrapper around official headless agy is acceptable.

What becomes of existing work:
- The Gemini CLI brain (`gemini_cli.py`, `gemini_brain.py`, `hooks/gemini_pretool.py`, 31 tests) stays
  as-is and is **reused as the template**: same event-loop and hook-translation shape.
- The "native Gemini API provider" decision from the Phase 0 audit is **superseded** and won't be built.

## Verified on this machine (2026-09-29, agy 1.2.12 → 1.2.13, account: Google AI Pro)

These were run in an isolated test workspace (`D:\Helios\data\agy-test\ws`), headless, with a logging
PreToolUse hook in `ws/.agents/hooks.json`.

**Install note:** the official installer run from inside the Claude desktop app landed in the app's
MSIX-virtualized `AppData\Local` (invisible to other programs). agy is therefore installed at
**`D:\Helios\tools\agy\bin\agy.exe`** (`install.ps1 --dir`), on the user PATH. agy self-updates in place.

| # | Test | Result |
|---|---|---|
| 1 | Account sign-in, then `agy -p … --output-format json` from the user's terminal | ✅ SUCCESS (Gemini 3.8 Flash, Google AI Pro). No API key. |
| 2 | Workspace `.agents/hooks.json` PreToolUse fires in headless mode | ✅ fires for every tool call. stdin has `toolCall{name,args}`, `conversationId`, `stepIdx`, `workspacePaths`, `transcriptPath`, `modelName`. Matcher `"*"` matches all tools. |
| 3 | Hook `allow` in default headless mode | ❌ does **not** grant. `run_command` is still soft-denied ("headless mode cannot prompt… auto-denied"). `permissionOverrides` doesn't grant either. |
| 4 | Hook `deny` with `--dangerously-skip-permissions` | ✅ blocked: "tool call denied by pre-tool hook". |
| 5 | Hook `allow` with `--dangerously-skip-permissions` | ✅ tool ran. |
| 6 | Hook **crashes** (exit 1) | ✅ **fails closed**: tool blocked ("JSON hook … failed"). |
| 7 | Hook **times out** (> `timeout`) | ✅ **fails closed**: tool blocked. |
| 8 | Hook exits 0 and **prints nothing** | ⚠️ **fails open**: the tool ran. |
| 9 | `hooks.json` **invalid**: bad JSON, or one malformed entry anywhere in the file | ⚠️ **fails open**: agy logs "Failed to parse hooks file" and drops the **entire** file; no hook runs and the tool ran. |
| 10 | `PreInvocation` marker hook (handlers go directly under the event, no matcher) | ✅ the marker is **absent at `init`** and **present at the first model step**, i.e. before any tool can run. PreInvocation can't block, only inject steps. |
| 11 | Workspace `.agents/settings.json` `permissions.deny` | ❌ **ignored** (the command ran). Permission rules only live in global `~/.gemini/antigravity-cli/settings.json`, which is shared with the user's interactive agy and synced with the IDE. |
| 12 | Persistent `--input-format stream-json` session, 2 turns | ✅ same `conversation_id`, context kept between turns, `agent_response` `text_delta` streaming, `result` per turn, clean exit on stdin close. |
| 13 | Tool names (57 in `init.tools`) | Includes `run_command{CommandLine,Cwd}`, `write_to_file{TargetFile,CodeContent}`, `replace_file_content`, `multi_replace_file_content`, `sed_file`, `view_file`, `list_dir`, `find_by_name`, `grep_search`, `read_url_content`, `search_web`, `open_browser_url`, `browser_*` (click/input/scroll/javascript/screenshot…), `browser_subagent`, `call_mcp_tool`, `invoke_subagent`, `define_subagent`, `send_command_input`, `command_status`, `schedule`, `manage_task`, `generate_image`, `notebook_edit/execution`, `delete_knowledge`, `ask_permission`. |

### Consequences for the design (this revises the recommendation above)

- **`--dangerously-skip-permissions` is required** for Helios to let agy use any tool headlessly. A hook
  can't grant permission, and workspace settings aren't honored. Widening the user's **global** agy
  settings would also widen their own interactive agy and the IDE, which is worse. The flag applies only
  to Helios's own agy child process.
- With the flag set, **Helios's PreToolUse hook is the sole permission gate.** That's the same model as the Gemini
  engine (`--approval-mode yolo` plus the BeforeTool hook). It maps agy tool names to Helios policy
  (`hooks/pretooluse.decide`): panic, screen lock, SSRF, protected paths, the Approve/Deny flow.
- **Fail-open paths and their mitigations (all mandatory):**
  1. Silent hook: the Helios hook **always prints a decision** and catches every exception → deny (as `gemini_pretool.py` does).
  2. Unparsable `hooks.json`: Helios **generates** it from code, validates it, and adds a
     `PreInvocation` marker hook in the same file. On the first non-user `step_update`, Helios requires the
     marker, and **kills the turn** if it's missing (a missing marker means the file, and with it the gate, didn't load).
  3. Global hooks and settings synced from the IDE could add behavior; Helios runs agy with its own workspace
     `.agents/hooks.json` and never edits the user's global agy/IDE config.
- The persistent stream-json session works, and the per-turn `-p --conversation <id>` path remains the recovery
  and panic fallback.

## Sources

- Headless mode: https://antigravity.google/docs/cli/headless/
- Installation & auth: https://antigravity.google/docs/cli/install/
- Using the CLI: https://antigravity.google/docs/cli/using/
- Execution modes: https://antigravity.google/docs/cli/modes/
- Permissions: https://antigravity.google/docs/permissions/
- Agent settings (Turbo): https://antigravity.google/docs/agent-settings/
- Hooks: https://antigravity.google/docs/hooks/
- CLI features: https://antigravity.google/docs/cli/features/
- MCP: https://antigravity.google/docs/mcp/
- Plans & quotas: https://antigravity.google/docs/plans/
- SDK overview: https://antigravity.google/docs/sdk/overview/
- Terms: https://antigravity.google/terms/
- FAQ: https://antigravity.google/docs/faq/
- Gemini CLI → Antigravity CLI transition: https://github.com/google-gemini/gemini-cli/discussions/27274
- Headless bug #1044: https://github.com/google-antigravity/antigravity-cli/issues/1044
- Headless bug #1077: https://github.com/google-antigravity/antigravity-cli/issues/1077
- Forum (external orchestration): https://discuss.ai.google.dev/t/is-external-orchestration-of-antigravity-cli-headless-mode-supported-with-account-based-usage/183051
- Forum (personal wrapper, unanswered): https://discuss.ai.google.dev/t/question-about-personal-wrapper-around-official-antigravity-cli-headless-mode/178472
- AI Pro rate limits: https://blog.google/feed/new-antigravity-rate-limits-pro-ultra-subsribers/
