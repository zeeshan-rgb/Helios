# Helios security

Helios can see your screen, type, run commands and remember things, so safety is enforced **in
code**, never only in prompts. The model is never the security authority. This page lists every
protection, where it is enforced, how to configure it, and how to check it.

Quick check on your machine:

```
helios security
```

This is read-only. It verifies the permission gate, that destructive shell commands always ask,
the MCP servers, the protected paths, the Night Mode allowlist, git hygiene, secrets in logs and
settings, and the YOLO and panic state.

## The acceptance rules (spec §29 K), proven through the real gate

| Attempt | Result | Test |
|---|---|---|
| Send a message automatically (`gmail_send_draft`, a confirmed browser submit) | **BLOCKED** — YOLO can't auto-send; with nobody to approve it's denied; background agents are hard-denied | `test_security_acceptance.py` |
| Read an API key (`config/secrets.toml`, `.env`, `~/.aws`) — by file tool, shell, or agy's own tools | **BLOCKED**, even in YOLO | `test_security_acceptance.py`, `test_security_phase14.py` |
| Destructive operation (`Remove-Item -Recurse`, `git push --force`, `Format-Volume`, …) | **asks you**; unanswered = denied | `test_security_acceptance.py` |

There is no tool anywhere that sends WhatsApp, SMS, email to prospects/leads, or posts on its own.

## How a tool call is decided

Every tool call from every brain engine goes through **one** gate before it runs:
`hooks/pretooluse.py` → `decide()`. The engines are Claude (the Claude CLI hook), Antigravity
(`hooks/agy_pretool.py`, which translates agy tools and then calls `decide()`), Gemini, side agents,
missions, Night Mode's AI steps and outside MCP clients (the public server calls `decide()` too).

The order of checks, first match wins:

1. **Background agents can't contact people.** No email, message or post from an agent the user
   isn't watching.
2. **Credential stores and secrets are hard-denied.** See [Protected paths](#protected-paths).
3. **Computer use:**
   - Destructive key combos and typed commands are denied (Win+L, Ctrl+Alt+Del, format, root
     wipe, …).
   - Panic stop denies every screen action.
   - The single-driver **screen lock** stops two agents fighting over the mouse.
   - Risky actions (clipboard, killing apps, recording, …) ask.
4. **SSRF:** fetching a private, loopback or link-local address is denied.
5. **Helios's own control files:** writing Claude Code's `~/.claude`, or the project manifests,
   is denied, by file tools or shell. The Antigravity hook additionally protects `.agents`,
   `.gemini`, `hooks.json` and `mcp_config.json`.
6. **YOLO mode**, if you turned it on for this chat, auto-approves the asks below. Everything
   above still applies.
7. **Policy** (`helios/permissions.classify`): allow, or **ask**. Asks show the Approve/Deny prompt
   (dashboard, Telegram or voice). **No answer, the app unreachable, or any error means deny.**

### Shell commands: the read-only allowlist

Shell commands run on their own only when they just read or report (`Get-*`, `git status/log/diff`,
`dir`, `ping`, …) **and** aren't chained or redirected (`;`, `&`, `|`, `>`, `` ` ``, `$(` → ask).
Read-looking commands that can destroy through an argument always ask (found and fixed 2026-10-14 —
they used to run without asking):

| Command shape | Why it asks |
|---|---|
| `Format-Volume …` | only `Format-Table/List/Wide/Custom/Hex` count as reading |
| `find … -delete / -exec / -ok` | deletes or runs commands |
| `wmic … delete / call / set / create / terminate / uninstall` | changes the system |
| `git branch -d/-D/-m/-M/-c/-f`, `--delete/--move/--force…` | deletes or rewrites branches |
| `git remote add / remove / rename / set-url / prune / update` | changes where code is pushed |

`helios security` re-checks these shapes on every run ("destructive commands").

### New tools since the first security pass

| Tool / surface | Gate |
|---|---|
| `perceive`, `locate`, `screen_context`, `find_ui_element`, `web_extract` | read-only → allowed; never on the public server; password managers / credential pages never read |
| `browser_click` / `browser_type` that submit, send, buy, sign up or delete | refused unless `confirm=true`, which asks you — **never** auto-approved by YOLO, always denied to background agents |
| `browser_download` | asks; files land only in `data/browser/downloads` with a sanitised name (a hostile `../../…` name can't escape) |
| `prospect_approve` | asks; approving sends nothing |
| Dashboard routes (`/dashboard/*`, `/prospects*`, …) | token on every route; POSTs also need our own Origin; Host must be loopback — a test walks **every** route in `server.py` |

**Fail closed everywhere:**
- A gate that crashes, can't load its policy, or gets unreadable input returns **deny**.
- The Antigravity gate prints a decision on every path, because agy treats silence as "allow".

## Protected paths

`helios/protected.py` holds the one list of credential stores and secrets:

| Category | Examples |
|---|---|
| SSH keys | `~/.ssh`, `id_rsa`, `id_ed25519`, `authorized_keys` |
| Password manager data | `*.kdbx`, KeePass, 1Password, Bitwarden, LastPass, Dashlane, Keeper |
| Browser credential stores | Chrome / Edge / Brave / Opera `Login Data`, `Cookies`, `Local State`; Firefox `logins.json`, `key4.db` |
| Windows credential store | `AppData\…\Microsoft\Credentials`, `Protect`, `Vault`; `cmdkey`, `vaultcmd`; SAM/SECURITY hives |
| API credential files | `.env*`, `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`, `credentials.json`, `token.json`, `client_secret*.json`, `*.pem`, `*.key`, `*.pfx` |
| Cloud credentials | `~/.aws`, `~/.azure`, gcloud credentials, `~/.kube/config`, `~/.docker/config.json`, GitHub CLI `hosts.yml` |
| GPG keys | `.gnupg`, `private-keys-v1.d` |
| Helios / assistant secrets | `config/secrets.toml`, `composio_mcp.json`, `data/.session_token`, `~/.claude/.credentials.json`, Gemini `oauth_creds.json` |

- Any file tool (read, write, edit, list, glob, grep path) or shell command that touches one of
  these is **denied**:
  - for the brain, background agents, Night Mode and MCP clients
  - **even in YOLO mode**, and even to list a folder
- Each denial is logged to `logs/security.log` (tool and category only).
- It deliberately does **not** block:
  - searching code for words like "password" (a grep *pattern*)
  - personal documents, which stay on the normal "ask" path
  - code *about* passwords (e.g. `password_reset.py`)

**Explicit configuration** is the only way through:

```toml
[security]
allow_protected = ["C:/Users/you/.ssh/config"]
```

A listed path (or everything under a listed folder) then goes through the normal policy, which
still asks you. `helios security` warns whenever this list isn't empty.

## Autonomous work only goes where you allow it

- **Night Mode and project health** inspect and run checks **only** in folders listed in the
  project manifests (`D:\Helios\data\projects\*.yaml`). That is the allowlist.
  - Manifests can't point at a protected or system folder, a whole drive or your home folder.
  - Checks run only the commands you wrote: no shell, a timeout, and anything that pushes,
    publishes, deploys or deletes is refused.
  - The brain can't edit manifests.
- **Research** only searches the web and reads public pages. Its gate mode allows nothing else.
- **Learning** reads the day's conversation log and writes memory notes. New rules and skills wait
  for your approval, and a rejected lesson is never proposed again.
- **Scheduled jobs** can only run fixed Helios actions, never a command or a prompt. A job pauses
  after 5 failures in a row.
- **MCP:**
  - The internal tool server refuses to start unless Helios launched it.
  - The public server exposes 15 curated, non-destructive tools, each decided by the gate and
    limited to 30 calls per tool per minute (`docs/HELIOS_MCP.md`). Business data, screen tools
    and browsing/screen memory notes are not exposed.

## Secret isolation

- **Memory refuses** anything that looks like a password, key, code or account number — and also
  credentials inside URLs / connection strings, cookies and session ids, cloud account keys and
  card numbers (Luhn-checked). Full list: [HELIOS_MEMORY.md](HELIOS_MEMORY.md#never-stored).
- **Every log line is redacted:** `conf.log` strips key and token shapes (`sk-…`, `ghp_…`,
  `AKIA…`, JWTs, private-key headers, bearer tokens). Vault notes use the same pattern.
- **Approval prompts log only the tool name and an id,** never the command or path.
- **Secrets stay out of git:** `config/secrets.toml`, `config/composio_mcp.json`, `data/` and
  `logs/` are git-ignored.

## Audit logs (`logs\`)

| Log | What |
|---|---|
| `security.log` | every protected-path denial |
| `permissions.log` | every Approve/Deny ask (tool and id) |
| `mcp_public.log` | every public MCP call with its decision (redacted arguments) |
| `night.log`, `jobs.log` | every Night Mode task and scheduled job run |
| `notify.log` | every notification shown or suppressed |
| `antigravity.log` | gate failures (missing marker or tampered hooks) — the run is killed |

## Per-turn validation (Antigravity engine)

- **Gate marker check.** Each agy turn must prove the permission gate loaded: a PreInvocation
  marker must exist by the first model step, otherwise the turn is killed. agy silently drops a
  malformed `hooks.json`.
- **Tamper check.** After every tool step, `hooks.json` is re-checked against its digest. Any
  change kills the turn.

## Panic

- **Panic hotkey or tray panic:** kills the brain, side agents, missions and workflow steps, and
  stops Night Mode.
- **The dashboard's Stop button:** stops the current reply only.

## Tests

- `tests/test_security_acceptance.py`: the three acceptance rules through the real gate, the
  destructive-shell shapes (and that real read-only commands stay autonomous), the new read tools,
  every server route needing the token / our Origin, and a foreign Host refused.
- `tests/test_security_phase14.py`:
  - protected categories and false-positive guards
  - YOLO and background-agent denials
  - the explicit allowlist
  - Antigravity translation
  - log redaction
  - preserved protections (panic, SSRF, `~/.claude`, lock combo, unanswered ask)
  - a gate crash denies
  - the self-check
- Plus the earlier phase tests: the gate, SSRF, the screen content filter, the side-agent outbound
  block, the MCP servers and research mode.
- `tests/conftest.py` guarantees tests never touch the live app: no real notifications, logs,
  database, flags or agy.
