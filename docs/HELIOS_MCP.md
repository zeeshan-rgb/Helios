# Helios MCP server

Helios exposes a curated set of its capabilities as an **MCP server**, so other MCP clients (the
Antigravity IDE, Claude Desktop, any MCP-capable agent) can use them as tools. Helios runs
independently. Nothing in Helios depends on any client being installed.

## Two servers, on purpose

| | `mcp/helios_public_server.py` | `mcp/helios_server.py` |
|---|---|---|
| For | **other MCP clients** | Helios's **own brain** only |
| Tools | 13 curated tools (below) | ~60 tools, including writing and running new tools, spawning agents, deleting routines |
| Permission gate | **inside the server**: every call is decided by `hooks/pretooluse.decide` | the brain's PreToolUse hook, *before* the call reaches the server |
| Starts when | any client launches it | **only when Helios launches it**: it exits unless `HELIOS_MCP_ROLE=internal` (set in `config/mcp.json`) |

An outside client talks to a server directly, so a server with no gate of its own must never be
exposed. That is why the internal server refuses outside launches, and the public server enforces
the policy itself.

## Tools

| Tool | Kind | What it does | Policy name |
|---|---|---|---|
| `get_system_status` | read | Helios's runtime, voice and memory health | `mcp__helios__system_health` |
| `get_project_status(project)` | read | PROJECT HEALTH report (verdict, checks, git, dependencies, issues) | `mcp__helios__project_health` |
| `get_active_projects` | read | configured projects with their last check result | `mcp__helios__list_projects` |
| `get_project_changes(project, hours)` | read | commits / uncommitted / modified files | `mcp__helios__project_changes` |
| `search_memory(query, category)` | read | remembered items (active only; never pending lessons or secrets) | `mcp__helios__recall_memory` |
| `get_recent_memory(limit)` | read | newest remembered items | `mcp__helios__list_memories` |
| `get_morning_report(spoken)` | read | today's morning briefing (written or spoken) | `mcp__helios__morning_briefing` |
| `search_research(topic, query, days)` | read | sourced research findings (kept apart from memory) | `mcp__helios__research_findings` |
| `safe_diagnostic(check)` | read | fixed list: `system`, `disk`, `network`, `helios`, `voice`, `memory`, `errors` | `mcp__helios__system_health` |
| `save_memory(text, category, project)` | write | remember something; secrets refused; **rules/skills wait for your approval**; source recorded as `mcp client` | `mcp__helios__remember` |
| `create_reminder(text, in_minutes \| at_iso)` | write | Helios reminder (toast + chat when due) | `mcp__helios__set_reminder` |
| `open_app(name)` | action | open an app or website on the PC | `mcp__helios__open_app` |
| `run_project_tests(project, check)` | action | run one project's **configured** checks (no shell; push/publish/deploy/delete refused), then report health | `mcp__helios__run_project_checks` |

**Not exposed:**
- anything that deletes or forgets
- sending messages (Telegram / email)
- starting workflows, missions or background agents (the blueprint's `start_safe_workflow` is
  deliberately left out: workflows can run arbitrary brain steps)
- creating tools
- running arbitrary commands
- YOLO
- approving lessons
- computer control

## Authorization

Every tool call goes through the same decision as a call from Helios's own brain. The public
server maps the tool to its internal name (the table above) and calls `hooks/pretooluse.decide`:

- **panic, YOLO, the allow / ask / deny policy and `[autonomy] always_ask`** all apply.
- **"ask"** shows the normal **Approve / Deny** prompt (dashboard, Telegram or voice). If Helios
  isn't running, or nobody answers within about two minutes, the call is **denied**.
- **A gate error is a deny**, never an allow.

With the default policy, all 13 tools are allowed. To require approval for one, add it to
`always_ask` in `config/settings.toml`:

```toml
[autonomy]
always_ask = ["mcp__helios__open_app", "mcp__helios__run_project_tests"]
```

On top of the gate, the server has its own switches:

```toml
[mcp_public]
enabled = true         # false = every call is refused
read_only = false      # true = only the read tools work
disabled_tools = []    # e.g. ["open_app"]
```

Every call is written to `logs/mcp_public.log` with the tool, its kind, the decision and the
reason, plus a redacted summary of the arguments. See it with `helios mcp log`.

## Connecting a client

`helios mcp config` prints the exact snippet for this machine:

```json
{
  "mcpServers": {
    "helios": {
      "command": "C:/Users/<you>/Desktop/Claude/Helios/helios/.venv/Scripts/pythonw.exe",
      "args": ["C:/Users/<you>/Desktop/Claude/Helios/helios/mcp/helios_public_server.py"]
    }
  }
}
```

- The transport is **stdio**; the client starts the server as a child process.
- It uses `pythonw.exe` so no console window flashes.
- **Never** point a client at `helios_server.py`. It will refuse to start, by design.
- Reminders, "ask" prompts and anything needing the running app work best while Helios itself is
  running (`helios start`). Read tools work either way.
- Antigravity step-by-step setup: see blueprint phase 12 (`docs/ANTIGRAVITY_MCP.md`).

Commands:

- `helios mcp config` prints the client snippet.
- `helios mcp tools` lists the tools and whether each is enabled.
- `helios mcp log` shows the last calls.

## Adding a tool

1. Write a plain function in `mcp/helios_public_server.py` decorated with `@mcp.tool()`. Give it a
   clear docstring; clients show it to their model.
2. Add it to `TOOLS` with the **internal policy name** that matches what it does, and its kind
   (`read` / `write` / `action`).
3. Start the function with `if (r := _guard("<tool>", {<args>})): return r`.
4. Keep it non-destructive. Anything that deletes, sends, installs or runs arbitrary code does not
   belong in the public server.
5. Add it to the tests in `tests/test_mcp_public_phase11.py`. The test fails if the tool list
   changes without it.

## Verified (2026-09-30)

- A real stdio MCP client listed exactly the 13 tools and called:
  - `get_active_projects`, `get_morning_report(spoken)`, `search_research(Gemini)` and
    `safe_diagnostic(disk)`, all answered correctly.
  - `safe_diagnostic("format c:")`, which was rejected.
- Each call was logged with its policy decision.
- The same client could not start `helios_server.py`: it exited with this guide's pointer.
- Helios's own brain still uses its internal server normally.
