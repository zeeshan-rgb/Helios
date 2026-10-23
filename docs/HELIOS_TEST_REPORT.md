# Helios — test report (final audit)

## Automated suite

Command (repo root):

```
$env:HELIOS_LIVE_BROWSER = "1"      # also run the real headless-Edge test
.venv\Scripts\python.exe -m pytest tests -q
```

| Run | Tests | Passed | Failed | Skipped | Files | Time |
|---|---|---|---|---|---|---|
| Baseline, before the audit (2026-10-14) | 1033 | 1033 | 0 | 0 | 58 | 120 s |
| After all audit fixes (2026-10-19) | **1057** | **1057** | **0** | **0** | 59 | 168 s |
| Baseline before SMS (2026-10-21) | 1136 | 1136 | 0 | 0 | 61 | — |
| After SMS integration (2026-10-21) | **1176** | **1176** | **0** | **0** | 62 | 391 s |
| After conversation reliability fixes (2026-10-23) | **1190** | **1189** | **0** | 1 | 63 | — |

**Conversation reliability (2026-10-23):** `tests/test_conversation_fixes.py` adds 14 tests, all faked
(no real agy, no network). They cover:
- background tasks: run, announce, failure reported, same request joins, unknown kind refused;
- MCP hand-off to the app, and the inline fallback;
- tool details for the chat dropdown, with secrets redacted;
- `consume_turn` ACTIVE→DONE events;
- no silent re-run after tools ran;
- stale-conversation retry kept;
- context rotation with a recap.

One expectation was updated in `test_antigravity.py`, because tool events now carry
`{id, name, detail, state}`. The chat dropdown was checked visually on a throwaway server (:8799)
with simulated events: expand, collapse, no console errors.

**SMS (2026-10-21):**
- `tests/test_sms.py` adds 40 tests, all mocked. Conftest blocks every real SMS HTTP call, so these
  tests are **not** evidence of real delivery.
- One regression appeared and was investigated:
  `test_security_acceptance::test_there_is_no_tool_that_messages_prospects_or_leads` banned any
  tool named `*sms*`. The test now checks what it was meant to guarantee, more strictly:
  - SMS tools take no phone number;
  - `send_sms` is ask, never YOLO, and blocked for background agents;
  - no SMS on the public MCP server;
  - WhatsApp, email-send and contact-lead tools are still banned.

- **No tests were removed or weakened.** Two expectations were updated to match deliberate
  changes: the public MCP tool set (13 → 15 tools, earlier phase) and a hard-coded tool count
  replaced by the real list.
- **New tests:** `tests/test_gap_closure.py` (23) and `tests/test_web_browser.py` (+1 slow-load
  re-timing, plus fake-page support).
- **Test isolation:** `tests/conftest.py` keeps tests off the live app (no real notifications,
  logs, database, flags, agy binary, web or battery). Mocked tests are labelled as such below and
  never counted as live verification.

### What the new tests cover

| Area | Tests |
|---|---|
| Proposal / quotation | no invented price; rate-card price; edited proposal never overwritten; tool + route wired and send-free |
| NEEDS_PRICING | derived view appears / disappears with the rate card |
| Antigravity lifecycle | stdout vs stderr; hung process killed at the timeout + workspace cleaned; missing agy graceful |
| Voice state | `dormant` reported, wake reason surfaced |
| Night Mode / briefing | skipped tasks never "without problems"; stale briefing refreshed; research deadline; sleep gap recorded; manual run never shown as a night; skip count |
| Prospecting reliability | Overpass fallback (transient only, https only); scan never researches; schools not "appointment"; re-research withdraws stale drafts, keeps edited ones |
| Model limits | missing usage; fresh vs stale vs forced refresh; provider failure (signed out, timeout) |
| Website audit | slow first load re-timed, "best of 2 loads" in the evidence |

## Live acceptance (prompt §24) — on the real app and machine

| | Scenario | Result | How |
|---|---|---|---|
| A | "Hey Helios, what time is it?" | **PASS (no physical mic / speaker)** | Kokoro synthesized it → faster-whisper heard "Hey Helios, what time is it?" → the running brain replied "It is 11:01 PM, sir." (clock 23:01, 21.8 s) → reply synthesized. Wake word itself: blocked (model file missing); clap wake seen in the live log. |
| B | "What application is open?" | **PASS** | `perceive` → claude.exe (foreground) |
| C | Notepad "Hello Helios" | not re-run | your Notepad holds work tabs and you were using the PC; passed live 2026-09-30 |
| D | Chrome search for Polarion docs | not run | would open a tab in your browser while you worked; open_app URL path unit-tested |
| E | JavaScript-heavy page | **PASS** | forced render via headless Edge → clean title/date/text; npm's 403 reported, not bypassed |
| F | Business discovery | **PASS** | 66 real businesses, 6/20 cells, done cells skipped on re-scan, OSM attribution + "doesn't list every business" |
| G | Website audit | **PASS** | example.com 3/5, neverssl.com 1/5 with evidence |
| H | AI opportunity | **PASS** | concrete reasons (booking, WhatsApp opt-in, enquiries); a false "appointment" claim was found and fixed |
| I | Quotation | **PASS** | editable proposal for a real business; "Price to be confirmed" (empty rate card) |
| J | "Find new leads" — no sending | **PASS** | the live pipeline drafted and never sent; no sending tool exists (tests) |
| K | Remember / recall | **PASS** | "Prefers concise reports" recalled from the real vault |
| L | Night Mode job | **PASS** | real scheduled night 2026-10-14 + a manual run, logged, reported |
| M | Morning briefing | **PASS (after fix)** | real project health; now lists the 7 skipped tasks |
| N | Dashboard | **PASS** | screenshots of the running app at 1366×768, 1536×864, 1920×1080; no overflow, no console errors; 2 bugs fixed |
| O | Disallowed operation | **PASS** | real gate denied secrets.toml, an SSH key, SSRF, and background-agent sending (5/5) |

## Security review

- **One gate for every caller.** That covers brain built-ins, MCP (internal and public),
  background agents, Night Mode, the dashboard routes and the new proposal route. The proposal
  only writes a vault note.
- **Earlier phase finding (fixed):** five destructive shell shapes were auto-allowed (`Format-Volume`, `find -delete`,
  `wmic … delete`, `git branch -D`, `git remote remove`); `helios security` now re-checks them.
- **This audit:**
  - **No new outbound path.** The proposal and the NEEDS_PRICING view send nothing.
  - **Dashboard routes stay token / Origin / Host-gated.** A test walks every route.
- **YOLO mode was ON** in your live session on 2026-10-14. It auto-approves "ask" prompts for that
  chat. The hard rails still apply (secrets, outbound from background agents, SSRF, panic,
  destructive screen actions). Turn it off when you don't need it.
- **No credentials** were read, logged or committed. The live checks used deny paths only, so no
  approval prompts popped up in your session.
