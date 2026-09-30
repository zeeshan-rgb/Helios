# Using Helios from Antigravity (via MCP)

Antigravity can use selected Helios capabilities as tools: project health, memory, reminders, the
morning briefing, research, safe diagnostics and more. The integration goes in **one supported
direction only**:

```
Antigravity (IDE or agy CLI)
      │  MCP (stdio)
      ▼
Helios public MCP server   mcp/helios_public_server.py
      │  every call → Helios's permission gate (hooks/pretooluse.decide)
      ▼
Helios tools
```

Helios never touches Antigravity's internals. It does **not** use private APIs, extract tokens,
reverse-engineer authentication, call hidden `agentapi` endpoints, decode conversation files, or
imitate IDE protocols. Antigravity just starts Helios's MCP server like any other MCP server.

Helios does **not** depend on Antigravity (see [Independence](#independence)).

## 1. Register the server

Helios prints the exact command for your machine:

```
helios mcp antigravity
```

It looks like this (paths are this machine's):

```
agy mcp add helios-public "C:/Users/<you>/Desktop/Claude/Helios/helios/.venv/Scripts/pythonw.exe" "C:/Users/<you>/Desktop/Claude/Helios/helios/mcp/helios_public_server.py"
```

`agy mcp add` is Antigravity's own, documented command. It writes Antigravity's **global** MCP
configuration, which the Antigravity IDE and the `agy` CLI share.

- The name **must be `helios-public`**, not `helios`. Inside Helios's own brain sessions, `helios`
  is Helios's internal tool server, and it must not be shadowed.
- **Do not** point Antigravity at `mcp/helios_server.py`. That is the internal server. It has no
  permission gate of its own and refuses to start when anything other than Helios launches it.

**One workspace only (alternative):** put this in `<workspace>\.agents\mcp_config.json` instead:

```json
{
  "mcpServers": {
    "helios-public": {
      "command": "C:/Users/<you>/Desktop/Claude/Helios/helios/.venv/Scripts/pythonw.exe",
      "args": ["C:/Users/<you>/Desktop/Claude/Helios/helios/mcp/helios_public_server.py"]
    }
  }
}
```

## 2. Check it

- `agy mcp list` should show `helios-public`.
- In the Antigravity IDE, open the agent panel's MCP servers view and refresh; `helios-public` and
  its 13 tools should appear. The labels differ between versions; see
  https://antigravity.google/docs/mcp/.
- Ask the agent: *"Use the helios-public tools to show my projects and today's morning briefing."*
- `helios mcp log` shows each call Helios received, with the permission decision.

## 3. What Antigravity can do

The 13 tools are listed in [HELIOS_MCP.md](HELIOS_MCP.md):
- **read:** system status, project status/list/changes, memory search/recent, morning report,
  research, and safe diagnostics from a fixed list
- **write:** save a memory, create a reminder
- **action:** open an app, run one project's configured tests

Nothing destructive is exposed.

## 4. Permissions — three layers

1. **Antigravity's own MCP permission settings** may ask you before a tool runs.
2. **Helios's permission gate** decides every call exactly as for Helios's own brain: panic, YOLO,
   allow/ask/deny, and `[autonomy] always_ask`. "Ask" shows Helios's Approve/Deny prompt
   (dashboard, Telegram or voice). No answer, Helios not running, or a gate error all mean
   **denied**.
3. **`[mcp_public]` in `config\settings.toml`:** `enabled`, `read_only`, `disabled_tools`.

For example, to make Antigravity ask you in Helios before opening apps or running tests:

```toml
[autonomy]
always_ask = ["mcp__helios__open_app", "mcp__helios__run_project_tests"]
```

Memories saved from Antigravity are recorded as `source: mcp client`. Rules and skills saved that
way wait for your approval (`helios learn review`), and secrets are refused.

## 5. Remove

```
agy mcp remove helios-public
```

To keep the registration but stop answering, set `[mcp_public] enabled = false`.

## Helios's own brain is unaffected

Helios's brain also runs on the `agy` CLI, and the global MCP configuration is shared. Once
`helios-public` is registered, Helios's own brain sessions start it too. Helios marks its sessions
(`HELIOS_AGY_MARKER`), and the public server then serves **no tools**, because the brain already
has the internal server. So nothing is duplicated and nothing is shadowed.

## Independence

Helios runs without Antigravity:

- **The brain engine is a setting** (`[brain].engine` in `config\settings.toml`, or
  `helios onboard`): `antigravity`, `claude`, `gemini` or `lite`. If `agy` is missing, the
  Antigravity brain says so politely instead of failing.
- **Everything else is Helios's own:** voice, clap and hotkey wake, memory, projects, Night Mode,
  project health, the morning briefing and the MCP server.
  - Without a working brain, the AI steps degrade instead of crashing: lesson extraction falls
    back to the no-AI heuristic, and research records a clear failure.
- **The public MCP server works with any MCP client.** Antigravity is one option, not a
  requirement.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `helios-public` fails to start | Run `helios mcp tools`. If that errors, the venv path in the registration is wrong; re-run `helios mcp antigravity`. |
| "helios_server.py is Helios's INTERNAL tool server…" | The registration points at the internal server. Use `helios_public_server.py`. |
| Every call says "Not done: denied by Helios's permission policy" | A tool needing approval got no answer. Start Helios (`helios start`) and approve the prompt, or relax `always_ask`. |
| "the server is read-only" / "disabled" | `[mcp_public]` settings. |
| Reminders created but never shown | Reminders fire from the running Helios app; start it. |

## Verified (2026-09-30)

- The official Antigravity agent (`agy` 1.2.13, headless) ran in a test workspace:
  - It had only Helios's public server registered. A test hook denied every other tool, so no
    files, terminal or web were available.
  - It called `get_active_projects` and `get_morning_report(spoken=true)` over MCP and answered
    with the real project names and the briefing.
  - Helios's audit log recorded both calls as allowed by its permission gate.
- Inside a Helios brain session the public server lists 0 tools, by the marker guard.
- The test suite confirms Helios keeps working with no `agy` available.
