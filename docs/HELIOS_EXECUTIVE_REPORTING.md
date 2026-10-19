# Helios — Executive Reporting & Proactive Communication

Helios reports to its owner the way a reliable employee would. It says what it did, what failed,
what needs a decision and which opportunities matter. It interrupts only for things that deserve
it, and it never contacts anyone except the owner.

This is built on the systems Helios already has. There is no second lead database, no second
scheduler and no second notifier.

| Part | Where | Built on |
|---|---|---|
| Event queue, routing, reports | `helios/executive.py` | `helios.db`, the existing scheduler tick |
| Morning report | `helios/briefing.py` | the existing briefing, plus a business and decisions section |
| Telegram self-message | `helios/channels.py` | the code `notify_tim` already used, now shared |
| Personal phone briefings | `helios/calling.py` | Twilio REST API, one-way |
| Lead pipeline | `helios/prospecting/store.py` | the existing prospect store, which gains PROPOSAL_ACCEPTED, WON and LOST |
| Dashboard | Reports nav item → `/dashboard/exec`, `/exec/action` | the existing drawer and token-gated API |
| Commands | MCP tools, `helios reports / events / calling` | the existing permission gate |

## Gap list (before → after)

| Requirement | Before | Now |
|---|---|---|
| Event intake from subsystems | Nothing. There were toasts only, with no history. | `executive.emit()` is called by leads (on qualify), jobs (2+ failures in a row), Night Mode (on finish), project health (FAILING), the Antigravity probe (unreachable) and calling (call not delivered). |
| Severity, dedupe, delivered vs reviewed | — | CRITICAL / HIGH / NORMAL / LOW. One event per unresolved problem, which counts repeats and keeps the highest severity it reached. Delivery is tracked per channel, separately from acknowledgement. |
| Morning report with business | The briefing covered night, projects, research, leads and approvals. | It also covers the business pipeline (last 24 h), the best opportunities and "needs your decision". The spoken version ends with a question when there is a choice to make. |
| Midday / end-of-day / Night Mode / weekly | Night Mode report only | `executive.build(kind)`, scheduled from `[executive_reporting]` |
| Lead notifications | Shown in the morning briefing only | Priority A with a public contact → HIGH (prompt). Others → NORMAL (next digest). |
| Phone calls | — | `calling.py`, off by default, authorized numbers only, with full rails (below) |
| Won / lost / accepted | — | New statuses, only after a real contact. PROPOSAL_ACCEPTED and WON need written evidence. |
| Dashboard reports section | Night report text only | Reports pane: range filters (today / yesterday / 7 days / custom), categories, pipeline, decisions, events with Reviewed buttons, report history, calls with Cancel. |
| Natural-language commands | — | 12 MCP tools, plus persona routing |

## Events

`executive.emit(source, type, severity, summary, detail=, ref=, related=, action=, approval=, dedupe_key=)`

- **Who can emit.** Only Helios's own code paths. There is no MCP tool that creates an event, so a
  web page, email or document can't cause a notification or a call.
- **Dedupe.** The same `dedupe_key`, while the event is unacknowledged and less than 7 days old,
  adds 1 to `count` instead of creating a new event. Severity only goes up. Once you mark the event
  reviewed, the next occurrence is a new event.
- **States.**
  - `new` → `delivered` (CRITICAL / HIGH were sent),
  - `new` → `held` (quiet hours or a snooze),
  - `new` → `batched` (NORMAL / LOW wait for a report),
  - `batched` → `reported` (included in a saved report).
  - `acknowledged` is separate: it records when *you* reviewed the event.
- **Delivery record.** `{channel: {status: delivered | unavailable | failed, at, detail}}`. For
  example, an unconfigured Telegram is recorded as `unavailable`, never as `delivered`.
- **Scrubbing.** Summaries, details and spoken text have secrets (`conf.SECRET_RE`), email addresses
  and phone numbers removed.

### Routing (`route()`, every scheduler tick — 15 s)

| Severity | Normal hours | Quiet hours (22:00–08:00) or snoozed |
|---|---|---|
| CRITICAL | dashboard + toast + Telegram, then a call if enabled | same; a call happens only if `critical_in_quiet_hours` is on |
| HIGH | dashboard + toast + Telegram | held, then delivered when quiet hours or the snooze end |
| NORMAL / LOW | in the next report's digest | in the next report's digest |

- **Channels.** `[executive_reporting] channels = ["dashboard", "toast", "telegram"]`.
- **Toasts.** These keep `notify.toast`'s own protections: 120 s dedupe and at most 4 a minute.

## Reports

| Kind | When | Period |
|---|---|---|
| morning | the existing briefing at `[briefing] time` (08:00) | last 24 h |
| midday | `midday_time` (13:00); skipped if nothing meaningful changed | since midnight |
| eod | `eod_time` (18:30) | since midnight |
| night | when a **scheduled** Night Mode run ends | the night's window |
| weekly | `weekly_day` / `weekly_time` (Mon 09:00) | last 7 days |

**Catch-up rules**

- **Once only.** Each report runs once per day; the weekly review once per ISO week.
- **Catch-up window.** If the PC slept through the time, the report is delivered on waking if
  that's within 4 hours. Otherwise it is skipped: a midday report is never delivered at night, and
  old days are never backfilled.

**Sections are kept apart.** Every report separates these, and every number comes from a record:

- completed
- attempted
- partly done (for example, the PC slept during the task)
- failed
- skipped (missing configuration or conditions)
- needs verification (for example, "no website" seen in OpenStreetMap only)
- suggested
- waiting for your decision

**When a source fails.** If one source can't be read (for example, the research library), the
report shows *couldn't read the research library: …* under Failed and is still produced.

**Weekly metrics.** The weekly review adds:

- discovered, researched, qualified;
- no website (verified), redesign, strong AI;
- proposals written, approved, contacted (verified), replies, won, rejected;
- research findings;
- OSM coverage per area.

Revenue shows as **"not tracked — no verified sales records are configured"**, because leads and
proposals are not revenue.

**Where reports go**

- **Files.** Reports are saved to `<vault>\Executive Reports\<date>-<kind>.md` using `safe_write`,
  which never overwrites your edits.
- **History.** They are recorded in `exec_reports`. The morning briefing is recorded there too.

**Spoken version.** This is an executive summary:

- the headline numbers;
- the strongest opportunity and why;
- drafts waiting ("Nothing has been sent");
- what failed or was skipped;
- the number of decisions;
- then, if there is a choice, *"Would you like the business report or the problems first?"*

It never includes ids, links, emails or phone numbers.

## Lead notifications

`prospecting.engine.research` calls `executive.lead_event(p)` when a business newly qualifies.

**Severity**

| Condition | Severity |
|---|---|
| Priority A **and** a public business email or phone | HIGH (prompt notification) |
| Everything else | NORMAL (next digest) |

**What the event contains**

- name, category, area;
- why it qualified;
- evidence;
- package;
- price from the rate card, or *needs pricing* (never invented);
- an approval flag when a draft is waiting.

A lead event never authorises contact. Outreach still needs your approval and your own send.

## Pipeline statuses

- **Full status order.** NEW → RESEARCHED → QUALIFIED → DRAFTED → APPROVED → CONTACTED → REPLIED /
  INTERESTED / NOT_INTERESTED / **PROPOSAL_ACCEPTED** / **WON** / **LOST** / CLOSED (also REJECTED,
  ARCHIVED).
- **CONTACTED.** Only through `record_contact` with evidence of the send. Nothing in this feature
  can set it.
- **After-contact statuses.** These are refused unless the business was actually contacted.
- **Evidence.** PROPOSAL_ACCEPTED and WON need evidence (a signed quote, an invoice or a payment
  reference). It is written into the prospect's history.
- **NEEDS_PRICING.** This stays a derived view (`engine.needs_pricing()`), not a status.

## Spoken briefings to the owner: Telegram voice (free) or Twilio call

`[executive_reporting.calling] provider` chooses how a spoken briefing reaches you.

| | `telegram` (voice message) | `twilio` (phone call) |
|---|---|---|
| Arrives as | A Telegram voice message from your own Helios bot | A ringing call on your number |
| Voice | Helios's own voice (Kokoro, rendered on this PC to OGG/Opus by `voice_note.py`) | Twilio's Polly voice |
| If missed | Stays in the chat, can be replayed | Lost; you get a dashboard fallback |
| Cost and setup | Free; a bot from @BotFather, then `helios telegram setup` | Paid account, compliance profile, a Twilio number |
| Delivery truth | "completed" = Telegram accepted the message. Helios can't know whether you played it, so `answered` stays 0. | "completed" or "answered" only when Twilio says so |

**What both share.** Scheduling, the report triggers (`call_for_*`), quiet hours, the daily limit,
the minimum gap, one per event, critical retries, cancel, the log, dashboard fallbacks, the
verified-test requirement, and the same MCP tools and CLI.

**Why Telegram rendering runs in the background.** A voice message takes about 20 s to render on
this CPU (including model load). So it is marked `initiated` first, so it can never be sent twice,
then rendered and sent on a background thread.

**Telegram setup.**

```
helios telegram setup     # NEW bot via @BotFather (not Claude Code Remote's); token at a hidden prompt;
                          # press Start in your bot -> Helios finds your chat and asks you to confirm
helios calling test       # one voice message to you; must complete
helios calling on
```

**The bridge switches on too.** Setting a bot token also enables Helios's existing Telegram bridge
on the next restart, so you can text Helios from your phone. It accepts only the Telegram account
setup confirmed (`allowed_ids`).

**Where personal details are stored.** Your phone number(s), the bot token and your chat id live
only in `config/secrets.toml`, which is gitignored and unreadable by the AI. They never go in the
committed `settings.toml`; `helios calling number` writes to `[calling] authorized_numbers` in
secrets.

## Personal phone briefings via Twilio (`calling.py`)

**Off until you turn it on.** Calling stays off until all three of these are true:

1. `[executive_reporting.calling] enabled = true`
2. Twilio is configured (`helios calling setup`)
3. at least one E.164 number is in `authorized_numbers`

| Rule | How it's enforced (in code, not prompts) |
|---|---|
| Owner only | No function or tool takes a phone number from outside. Calls go to `authorized_numbers[i]`, and `place()` refuses any other number as a last check. Leads, clients and anyone else can't be called. |
| No untrusted triggers | Calls come only from: the scheduler (report kinds enabled by `call_for_*`); CRITICAL events (when `call_for_critical_events` is on); HIGH leads (when `call_for_high_priority_leads` is on); or your explicit request (`schedule_call` / `call_me_now`, which are Approve/Deny, never YOLO, and blocked for background agents). |
| Turning calls on | CLI or dashboard only. The AI can only turn calls **off** (`calling_off`). |
| Quiet hours | A non-critical call is moved to the end of quiet hours. A critical call is refused unless `critical_in_quiet_hours` is on. |
| Limits | `maximum_calls_per_day` (3) and `min_minutes_between_calls` (60) for non-critical calls. One call per event. |
| Retries | Critical calls only, up to `max_attempts` (2), `retry_minutes` (10) apart |
| States | scheduled → initiated → ringing → answered → completed, or failed / no-answer / busy / canceled / refused. Each change is logged with a time. A state never steps backwards. A call with no provider status for an hour is marked failed. |
| Honest outcome | "completed" only when Twilio says so; "answered" only on in-progress, or completed with a duration |
| Fallback | Failed, no-answer, busy, or calling not ready when due → a "Phone briefing not delivered" event. The report is still on the dashboard. |
| Cancel | `helios calling cancel [id]`, the dashboard Cancel button, or "cancel my call" |
| Content | Up to 3,500 characters of the scrubbed spoken summary, XML-escaped, ending "The details are on your Helios dashboard. Goodbye." |

**One-way only.** The call is created with inline TwiML (`<Say>`), so no public web server is
needed. A live two-way conversation would need Twilio `<Gather>` webhooks on a public URL, which a
home PC doesn't have, so it isn't offered. Helios says so when asked.

### Setup (you do this — Helios never asks for credentials in chat)

```
helios calling setup                 # hidden prompt: Twilio Account SID, Auth Token, Twilio number
helios calling number +91XXXXXXXXXX  # your own phone (E.164); Helios will call no other number
helios calling test                  # asks "Type yes", then places ONE real call to you
helios calling on
helios calling status | log | cancel [id] | off
```

On a Twilio trial account, the number you call must first be verified in the Twilio console.

## Commands

| Say | Tool |
|---|---|
| "give me my end-of-day report" / "weekly review" / "morning briefing" | `exec_report(kind)` |
| "what needs my approval?" | `pending_decisions` |
| "which leads should I contact today?" | `lead_digest(days)` |
| "anything urgent?" / "what happened while I was away?" | `exec_events` |
| "mark those reviewed" | `ack_events` |
| "don't disturb me for two hours" | `snooze_reports(hours)` |
| "call me at 8 with my briefing" / "call me in 20 minutes" | `schedule_call(when, report_kind)` (asks you) |
| "call me now with the update" | `call_me_now` (asks you) |
| "cancel my call" / "stop calling me" | `cancel_calls` / `calling_off` |
| "are calls set up?" / "did the call go through?" | `calling_status` / `call_log` |
| "we won Acme — invoice INV-12" | `prospect_set_status(id, "WON", evidence)` |

**CLI equivalents**

```
helios reports [list | midday | eod | night | weekly [--deliver] [--spoken] | snooze <h>]
helios events [all | ack <id…> | ack all]
helios calling …
```

## Dashboard

The **Reports** item in the side nav opens the Executive Overview drawer. It has:

- **Filters.** Range (today / yesterday / last 7 days / custom dates) and category (business,
  research, projects, Night Mode, jobs, system, approvals, failures, calls).
- **Business pipeline.** Pipeline tiles and the best opportunities.
- **Waiting for your decision.**
- **Events.** Each shows severity, count, source, delivery per channel and reviewed state, with a
  **Reviewed** button and **Mark all reviewed**.
- **Snooze.** A 2-hour snooze, and a button to cancel it.
- **Report history.** Click a report to read it. You can also preview a midday, end-of-day or
  weekly report now.
- **Phone briefings.** Readiness (with the masked number), the call log, **Cancel** for scheduled
  calls, and the schedule and quiet hours.

**API.** `GET /dashboard/exec?range=&from=&to=&cat=`, `GET /dashboard/exec_report?id=`, and
`POST /exec/action` (ack, ack_all, snooze, cancel_call, build). All are token- and origin-checked,
and none of them contacts anyone.

## Configuration

These live in `config/settings.toml` under `[executive_reporting]` and
`[executive_reporting.calling]`; see the comments there.

- **Secrets.** These are only in `config/secrets.toml`, which the AI can't read:
  - `[twilio]` account_sid, auth_token, from_number;
  - `[telegram]` token, allowed_ids.

## Tests

`tests/test_executive_reporting.py` contains 42 tests, all mocked. `conftest._no_real_outbound`
makes sure no test sends a Telegram message, places a call or writes to the real vault. The tests
cover:

- severity routing;
- quiet hours and snooze;
- dedupe and escalation;
- delivered vs reviewed;
- channel failures recorded honestly;
- scrubbing;
- lead alert rules and the rate card;
- report assembly from records;
- a failing source;
- revenue never inferred;
- evidence-gated WON / PROPOSAL_ACCEPTED;
- skipping a quiet midday report;
- schedules that run once and never backfill;
- the weekly review running once a week;
- the Night Mode hook;
- the morning briefing's business section;
- the jobs failure event;
- calling refused until configured;
- quiet hours, the daily limit, the minimum gap and one call per event;
- cancel;
- the full call lifecycle;
- provider failure and no-answer fallbacks;
- critical retries;
- the stale-call timeout;
- calling disabled before a call is due;
- authorized numbers only;
- TwiML escaping and scrubbing;
- the test call working while calling is disabled;
- `set_option` validation;
- permissions (ask, outbound-blocked, never YOLO);
- no tool taking a phone number;
- the MCP tools;
- dashboard filters and actions.

## Not done / blocked

- **Live two-way calls.** These need a public webhook URL (Twilio `<Gather>`), so they aren't built.
- **Real end-to-end call.** This needs your Twilio account and number, then `helios calling test`.
  It is not done until you run it.
- **Revenue.** Not tracked. There are no verified sales records; WON with evidence is the closest
  signal.
- **Email / WhatsApp / SMS to the owner.** Not added. Telegram and the dashboard cover the owner's
  channels, and Gmail stays limited to drafting replies to business contacts.
