# Helios — Dashboard UI

The dashboard is the Helios window: a three-column command center built on the existing vanilla-JS
UI. It doesn't use a framework or new dependencies, and the existing architecture is unchanged.
**Every number on it is real.** A value comes from a Helios data source or a live event, or it's
shown as an empty state or "Not exposed".

```
┌ title bar (drag · power · YOLO · mic · history · missions · workflows · 3D · Stop · window) ┐
│ SIDEBAR            │ COMMAND BAR  [Dashboard|Conversation] [ Ask Helios anything… 🎤 ↑ ] ● │ BUSINESS PANEL     │
│ logo               │ ───────────────────────────────────────────────────────────── │ opportunity        │
│ nav (13, with      │ hero: date · greeting · real summary · shortcuts               │ website audit      │
│  status dots)      │ metric cards (6)                                               │ recommended package│
│ robot (live state) │ top leads table (search · filter · sort · pages)               │ outreach: View/Edit│
│ MODEL LIMITS       │ discovery map │ area coverage                                  │  Approve/Reject/   │
│ SYSTEM STATUS      │ recent research │ activity                                     │  Archive           │
│                    │ quick actions                                                  │ upcoming tasks     │
└────────────────────┴────────────────────────────────────────────────────────────────┴────────────────────┘
```

## Files

| File | What |
|---|---|
| `helios/ui/index.html` | Shell markup. All existing ids are kept (`#chat`, `#input`, `#send`, `#voicehud`, `#modal`, drawer views), so the chat logic is unchanged |
| `helios/ui/dashboard.css` | Design tokens (midnight blue, neon cyan, purple / teal / green / orange), layout, robot states, responsive drawers, reduced motion. Loaded after `style.css` |
| `helios/ui/dashboard.js` | Renders every dashboard section from `/dashboard/*`. Reuses `app.js` helpers (`jfetch`, `esc`, `sendMessage`, `openDrawer`) |
| `helios/ui/app.js` | Small `window.Dash?.on…` hooks (bubble, busy, mic, voice, model, error, connection, `dashboard` SSE events) |
| `helios/dashboard.py` | Read-only data layer. Never raises; each source is wrapped |
| `helios/server.py` | `GET /dashboard/<name>` and `POST /dashboard/run`; `POST /prospects/action` gains `edit` |
| `helios/prospecting/store.py` | `edit_draft()`: the user's own edit, **DRAFTED only** (an approved message can't change) |
| `helios/voice/daemon.py` | State pushes now carry `followup` (hot-mic listening) and wake-word readiness |

## Endpoints

All of them sit behind the dashboard token. POSTs also check origin and host, like every other route.

| Route | Source |
|---|---|
| `GET /dashboard/summary` | Metrics, nav module states, system (psutil), voice status, engine health, featured prospect |
| `GET /dashboard/limits[?refresh=1]` | Model Limits (see below). `refresh=1` re-runs the engine check |
| `GET /dashboard/business` | Prospect rows (up to 500) + coverage |
| `GET /dashboard/map` | Plotted prospects (lat/lon) + the OSM cells actually searched + attribution |
| `GET /dashboard/activity` | Merged timeline: Night Mode runs, job runs, prospect events, research, leads (+ recent research) |
| `GET /dashboard/upcoming` | Next Night Mode tasks, enabled jobs with a next run, scheduled workflows, drafts awaiting approval |
| `GET /dashboard/prospect?id=` | One prospect: card + audit rows + package + draft + history |
| `GET /dashboard/module?id=` | One nav module's status, details and setup help |
| `GET /dashboard/projects` | Health verdicts (`health.reports`) |
| `GET /dashboard/report` | Latest Night Mode report |
| `POST /dashboard/run {action}` | `night_mode` / `project_health` in the background. The result arrives as a `dashboard` SSE event. Anything else gives 409 |

## Model Limits: what's real

| Field | Source | Today |
|---|---|---|
| Backend | `[brain].engine` | e.g. "Antigravity CLI (agy)" |
| Engine | A cached `agy models` run (background thread, 10 min TTL, refresh button) | reachable / unavailable + reason |
| Model | `[antigravity]` model + router tiers | Each one is checked against your account's `agy models` list. A slug the account doesn't offer is flagged in orange |
| Last used | The `model` SSE event of the latest turn | — |
| Session / weekly quota, reset, context window | **No supported interface exposes them** | Shown as **"Not exposed"**, with a tooltip explaining why. Helios never reads private APIs or tokens |
| Measured by Helios | `data/usage.jsonl` (every AI call) | Today / 7 days, calls, top purposes. Labelled as Helios's own measurement, **not** the provider quota |

If a future official interface exposes quota, set the `provider` fields in `dashboard.limits()` and the
UI renders them. Nothing else needs to change.

## The robot

It reflects real state only, in this priority order:

| Shown | When |
|---|---|
| Helios server unreachable | The SSE stream dropped |
| Listening... / Waiting for follow-up... | Voice daemon `listening` (`followup` = hot mic, no wake word needed) |
| Speaking... | Daemon `speaking`; the mouth follows the real TTS level |
| Processing... | Daemon `thinking`, or a brain turn is running |
| Antigravity unavailable | Engine check failed (hover for the reason) |
| Microphone muted / No microphone found | Daemon `off` / `disabled` |
| Asleep — double-clap to wake | Daemon `dormant` (Helios's normal boot state; added by the final audit — it used to show "Online & Ready" while asleep) |
| Online & Ready | Otherwise |

Below the status: mic state (on / live / muted / daemon not running / voice off) and wake-word
readiness, reported by the daemon itself. With `prefers-reduced-motion`, all animation stops.

## Data honesty rules (enforced in code)

* **Metric cards, table, map, coverage, research, activity, upcoming:** live data. When there's
  none, the section shows an empty state saying where the data would come from.
* **Map:** a schematic plot of OpenStreetMap coordinates plus the cells searched.
  * No basemap tiles are fetched (no tile-policy issues, no network).
  * Attribution "© OpenStreetMap contributors (ODbL)" is always shown when anything is plotted.
  * The note "OSM doesn't list every business" is always shown.
  * Coverage reads "N/M cells", never "complete".
* **Right panel:**
  * The audit rows come from the stored audit.
  * The price comes from your rate card, or shows **"Needs your pricing"**.
  * **Approve sends nothing** (confirm dialog, then `store.approve`).
  * Edit works only while DRAFTED.
  * CONTACTED still requires evidence (`record_contact`).
* **Quick actions:**
  * Scan area, Audit website and Find leads send a normal message through `/message`, so the
    existing permission gates apply.
  * Generate quote opens the selected prospect's rate-card package.
  * View reports shows the latest night report.
  * Run Night Mode and Check project health each need a confirm, then start the real job in the
    background.
* **Nav indicators:**
  * Green = ready, amber = needs setup / not running, grey = off, red = problem.
  * They come from settings and cheap local checks (no network calls). Hover for the detail.

## Responsive behaviour

| Window width | Layout |
|---|---|
| ≥ 1280px | Three columns (sidebar 244px, business panel 316px; 264 / 360px from 1700px) |
| 1024–1279px | Business panel becomes a slide-over (panel button in the command bar) |
| < 1024px | Sidebar becomes a slide-over too (menu button) |
| < 640px | Command bar wraps |

Columns and cards scroll internally, so the page never scrolls horizontally. The leads table drops
its Source, Area and Website columns as the workspace narrows (container queries).

## Verification on the running app (2026-10-14, final audit)

Screenshots of the **real** app (`helios start` → `http://127.0.0.1:8769`), real data:
- **1366×768, 1536×864, 1920×1080:** no horizontal overflow, no console errors.
- **Robot and Model Limits:** the robot showed the daemon's real "Asleep — double-clap to wake" and
  the exact wake-model reason. Model Limits showed agy reachable, the quota "Not exposed", and
  Helios-measured usage.
- **Real data:** metrics showed 65–66 businesses from the live scan and today's real token count.

Fixed during the check:
- **Blank right panel:** it was blank when businesses existed but none were qualified. It now
  shows "Nothing qualified yet — pick a row".
- **Night Mode indicator:**
  - It now shows the last **scheduled** night: a manual run is never passed off as a night.
  - It counts skipped tasks ("last night: completed (7 of 9 tasks skipped)").

New:
- **"Prepare proposal"** button in the right panel.
- **"Needs pricing"** filter on Top leads.

## Verification (2026-10-13)

* **Test server:** `:8799` with a temp DB, data dir and vault, a fake brain, and clearly fake seeded
  businesses. Never the live app.
* **Screen sizes, all with no horizontal overflow:** 1366×768, 1536×864, 1920×1080, 2560×1440;
  1065×614 (the default window on a 1366 screen: right panel drawer); 900×640 (sidebar drawer).
* **Robot:** real `/voice/state` pushes drove it to "Waiting for follow-up...", "Speaking..." and
  "Microphone muted"; a typed message switched to the conversation with "Processing...".
* **Right panel:**
  * Selecting a row loaded its audit.
  * Edit, then Approve, worked; afterwards edit was refused (400) and Approve/Edit were disabled.
  * `/dashboard/run` with an unknown action gave 409; a foreign origin gave 403; no token gave 401.
* **Engine check:** the live `agy models` probe reported 6 models. It flagged that the configured heavy
  tier `gemini-3.1-pro-high` isn't offered by the account.
* **Empty install:** zeros and empty states everywhere, no placeholder data.
* **Tests:** `tests/test_dashboard.py` (13). Full suite: 949 tests, 0 failures, 1 skipped.
