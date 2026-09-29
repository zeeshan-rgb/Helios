# Helios Workflows — native Python automation engine

A lightweight, local, no-Docker/no-Node workflow engine that generalizes the existing
`routines` primitive (one prompt on a schedule) into **multi-step flows with data passing** —
"a workflow system like vierisid/helios", built on the machinery Helios already owns
(scheduler = triggers, side-agent pool = executor, SQLite = store, MCP tools = actions).
Borrows Activepieces' *model* (flow = trigger + step tree; step = an action from a catalog;
outputs feed later steps), not its code — so there's zero licensing entanglement and no second
runtime.

## Model

A **workflow** is a JSON document:

```json
{
  "name": "Morning brief",
  "trigger": { "type": "schedule", "schedule": "daily 08:00" },
  "steps": [
    { "id": "news",  "type": "agent",  "agent": "researcher",
      "prompt": "Summarize today's top AI headlines in 5 tight bullets." },
    { "id": "brief", "type": "brain",
      "prompt": "From these notes, write a 2-sentence spoken briefing:\n{{news.output}}" },
    { "id": "tell",  "type": "notify", "title": "Morning brief", "message": "{{brief.output}}" }
  ]
}
```

- **Trigger** — `manual` (Run-now button / `run_workflow` tool), `schedule` (a routine-style
  string: `daily HH:MM` · `weekdays HH:MM` · `hourly` · `every 30m` · `every 2h`), or `webhook`
  (phase 2).
- **Steps** run top to bottom. Each step's text output is stored under its `id`; later steps
  reference it with **`{{id.output}}`** (or `{{id}}`), plus `{{trigger.*}}` for trigger data.

### Step types (phase 1)

| type | does | key params |
|------|------|------------|
| `agent` | run a specialist side-agent (researcher/operator/coder/organizer/writer) | `agent`, `prompt` |
| `brain` | run a general reasoning turn (generalist side-agent) | `prompt` |
| `notify` | Windows toast + a chat/SSE ping to Tim | `title`, `message` |
| `http` | outbound HTTP request (SSRF-guarded: no loopback/private targets) | `method`, `url`, `body?` |
| `delay` | wait N seconds within the run (bounded 0–3600) | `seconds` |
| `condition` | guard — evaluate a check; if false, stop the run cleanly | `source`, `op`, `value` |

`agent`/`brain` steps execute through `side_agent.run_task` — so **every tool they call is still
gated by the PreToolUse hook**, and because they run as `HELIOS_AGENT_ROLE=side` they inherit the
structural "no outbound messages from a background agent" rule. Workflows are therefore safe to run
unattended by construction. (A workflow talks to Tim via `notify`, not by emailing real people.)

## Execution

- `helios/workflows.py :: WorkflowManager(pool, emit)` — constructed in `app.py` beside the
  side-agent pool and `MissionManager`, passed to the `Scheduler`, and wired into
  `httpd.app["workflows"]` (post-init, like `quit`).
- **Manual run** → server `POST /workflow/run` → `manager.run_async(id, "manual")` (instant).
- **Scheduled run** → the scheduler's 15s tick calls `manager.tick(now)`, which fires due
  enabled scheduled workflows and advances their `next_run` (exactly like routines).
- `run_async` spawns a daemon thread → `_execute`: creates a `workflow_runs` row, resolves
  `{{...}}` variables per step, runs each step, records a per-step log, and emits a `workflow`
  SSE event on every transition so the dashboard live-updates. It checks the panic `abort.flag`
  between steps, so panic stops a running flow.

## Storage (`db.py`)

```
workflows(id, name, spec /*json*/, enabled, schedule, next_run, created_at, updated_at, last_run)
workflow_runs(id, workflow_id, status, trigger, log /*json*/, result, started_at, finished_at)
```
`spec` holds the whole `{name,trigger,steps}` document; `schedule`/`next_run`/`enabled` are
denormalized columns so the scheduler can query "due" cheaply (same shape as `routines`).

## Surfaces

- **Dashboard tab** — a title-bar icon opens the **Workflows** panel (mirrors the Missions modal:
  list → detail/editor → run history), matching the existing quiet-luxe design tokens.
- **By voice / chat** — MCP tools in `mcp/helios_server.py`: `create_workflow`, `list_workflows`,
  `run_workflow`, `set_workflow_enabled`, `delete_workflow`. So "Helios, make a workflow that every
  morning researches AI news and reads me a brief" composes a flow the same way it creates routines.

## Phase 2 (not in this slice)

Webhook triggers (`POST /hook/<token>` → run a flow), a drag-and-drop visual step canvas, an
`http`-piece catalog, and an **optional** `activepieces` step type that (only if Tim opts in) calls
an Activepieces MCP sidecar for the 280-connector SaaS breadth — bolts onto this same flow model
without a rewrite.
