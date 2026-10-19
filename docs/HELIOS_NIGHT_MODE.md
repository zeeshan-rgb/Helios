# Helios — Night Mode

Overnight maintenance that runs while the PC is awake and Helios is running: sync projects, run
their health checks, research, find paid-work leads, prospect local businesses, learn from the day,
summarise projects, and write a report that becomes the morning briefing.

**Night Mode never modifies Helios's own code** and **never contacts anyone.** It writes only to
`data/`, `logs/` and the vault (`night_mode/common.safe_write` refuses anything else).

## Schedule (`[night_mode]` in `config/settings.toml`)

```toml
[night_mode]
enabled = true
start = "01:00"       # window start
end = "05:00"         # hard end: no task starts after this
min_battery = 30      # on battery below this %, heavy tasks are skipped (0 = never skip)

[night_mode.schedule] # task = earliest start; delete a line to turn a task off
sync_projects = "01:00"
run_checks = "01:15"
research = "02:00"
find_leads = "02:40"
analyze_conversations = "03:00"
business_prospecting = "03:10"
extract_memories = "03:30"
update_rules_skills = "03:45"
project_summaries = "04:00"
```

The morning briefing follows at `[briefing].time` (08:00), spoken the first time you wake Helios.

| Task | What it does | Uses |
|---|---|---|
| `sync_projects` | git state of every project in the manifests | local git |
| `run_checks` | each project's configured tests / lint / build + dependency check + probes | the manifest's own commands (no shell) — **heavy** |
| `research` | configured topics → sourced findings in the research library | agy search-only mode + the clean reader / hidden browser — **heavy** |
| `find_leads` | worldwide paid-work posts → leads with rate-card prices and pitch drafts | agy search + source checks — **heavy** |
| `business_prospecting` | OSM discovery → website audits → scores → quotes → drafts for approval | OSM, hidden browser, agy search — **heavy**; skips until `[prospecting]` has areas |
| `analyze_conversations` | what happened in recent conversations | daily logs |
| `extract_memories` | lessons from each day's log; prunes expired browsing/screen notes | one tool-less AI call per day |
| `update_rules_skills` | reviews learned rules/skills (new ones stay pending) | memory |
| `project_summaries` | per-project summary for the report | project state |

## Guarantees

- **Clear boundaries** — each task waits for its time; nothing starts after `end`.
- **Task isolation** — an exception fails that task only; the rest still run.
- **One run per night** — a per-night run record plus a cross-process lock; a manual run is separate.
- **Never silent** — every task's start / result / duration is logged (`logs/night.log` and the run
  record); a crashed run, a skipped task and a **missed night** (with the reason: PC off, asleep,
  or Helios not running — from boot time, app start and heartbeat gaps) are all reported.
- **Interrupted runs** — a run left "running" by a crash or shutdown is marked interrupted at the
  next start, reported, and recorded in job history.
- **Stop** — `helios night stop` (its own stop flag; the dashboard's Stop button doesn't end the night).
- **Power-aware** — heavy tasks (checks, research, leads, prospecting) are skipped on battery below
  `min_battery`, checked per task (the laptop can be unplugged mid-night); a power skip is reported,
  not counted as a failure.
- **Sleep is reported, not hidden** — if the app's heartbeat shows the PC was asleep while a task
  ran, the report says so ("the PC was asleep from 02:03 to 11:16 during research"). Scheduled
  runs pass the window end as a deadline: research starts no new topic after it (the rest are
  reported as "not started — window ended"). Found by the real night of 2026-10-14, when the
  laptop slept at 14 % battery and research "took" 9 h 20 min.
- **The briefing counts skips** — "Night Mode finished, but 7 of 9 task(s) were skipped (low
  battery: run checks; the night window ended: …)" with a "Skipped tonight" section; a briefing
  written while the night was still running is refreshed once the night finishes.
- **Resource hygiene** — the hidden browser that research / leads / prospecting share is closed when
  the night ends (also after a crash).
- **Offline-aware** — tasks know whether the PC is online; research skips, learning uses the no-AI
  fallback.
- **Learning stays reviewable** — new rules and skills wait for approval; rejected lessons never return.

## The report

`<vault>/Night Reports/<night>.md` — sections kept apart: **completed** (verified work), **observed**,
**suggested**, **needs your approval**, **failed**. A task is only listed as completed if it really
completed. The morning briefing (`helios briefing`, `morning_briefing` tool) builds on it.

## Commands

| Command | |
|---|---|
| `helios night status` | on/off, next window and tasks, power state, the last nights |
| `helios night run [--only task1,task2]` | run now (manual run) and print the report |
| `helios night report [night]` | show a report |
| `helios night stop` | stop a running night before its next task |
| `helios night on` / `off` | switch it |
| Dashboard → Quick actions → **Run Night Mode** | same as `run`, with a confirmation; the result arrives as a notification |

Ask Helios: "What happened overnight?" → `night_report`; "Is Night Mode on?" → `night_mode_status`;
"Good morning" → `morning_briefing`.

## Tests

`tests/test_night_mode_phase7.py` (window maths, plan, isolation, logging, report sections,
duplicate runs, cross-process lock, missed-night reasons, interrupted recovery, the app tick, the
code-write guard), `tests/test_night_mode_phase10.py` (power rule, low-battery skips, browser closed
— including after a crash — and nightly memory pruning), plus the research / leads / prospecting
task tests. `tests/conftest.py` stubs the battery so results never depend on whether the laptop is
plugged in.

Verified live (2026-10-14): `helios night status` showed the real power state (on battery, 22 % →
heavy tasks would be skipped); a manual `sync_projects` run completed (10 projects, 4.9 s), logged
each step and wrote its report to the vault. The last three scheduled nights were recorded as
**missed — Helios wasn't running during the night**: Night Mode only works while Helios is running.
