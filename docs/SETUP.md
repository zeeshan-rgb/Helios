# Helios — Setup Guide

Helios is a local, always-on Windows AI assistant. This guide gets it running on a fresh machine.

## Quick start

One line in PowerShell. It installs Python if it's missing, downloads Helios, builds the venv +
dependencies, and adds a `helios` command to your PATH:

```powershell
irm https://raw.githubusercontent.com/NotTimPunt/jarvis/cua-driver/install.ps1 | iex
```

Then connect a brain and set everything up:

```powershell
helios onboard
```

`helios onboard` runs the wizard (below) — you never hand-edit config files; pick options and it
writes `config/settings.toml` + `config/secrets.toml` for you. When it finishes, start Helios with
`helios` (or it auto-starts at login if you enabled that). Re-run `helios onboard` any time to change
providers, add voice, or reconnect apps — it's idempotent and offers your current values as defaults.

<details>
<summary>Prefer to do it by hand? (clone + setup.py)</summary>

```powershell
git clone https://github.com/NotTimPunt/jarvis.git
cd helios
python setup.py     # bootstraps the venv + deps, then runs the same wizard
```
</details>

## What the wizard asks

### 1 · Brain
Pick how Helios thinks:

| Choice | Power | Needs |
|---|---|---|
| **Headless Claude** (sign in) | Full — computer-use, MCP tools, multi-agent missions, memory | Claude Code CLI + a Claude account login (free on your plan) |
| **Claude API key** | Full (same as above) | An Anthropic API key (`sk-ant-…`) + Claude Code CLI |
| **OpenRouter / OpenAI / Gemini / Ollama** | *Lite* — a curated, permission-gated toolset | That provider's API key (Ollama is local/keyless) |

**Full vs lite.** Only the Claude engine gets computer-use (cua-driver), the `mcp__helios__*` tools,
and missions. The **lite** brain is a native loop for any OpenAI-compatible provider with a curated,
permission-gated toolset: read/write files, run a shell command, open an app, fetch a URL (and
search the web if you add a search key), check system health, recall/save memory, and set reminders.
Lite is the foundation of a future provider-agnostic agent loop.

The Claude brain works with **either** your account login **or** an `ANTHROPIC_API_KEY` — both give
full power. The login path is free on your Claude plan.

### 2 · Voice (optional)
After the brain, the wizard asks "set up voice now?". Voice is fully pluggable:

- **TTS:** `kokoro` (local, no key — downloads ~350 MB of models), `openai`, or `groq`.
- **STT:** `faster-whisper` (local, no key — model auto-downloads), `openai`, or `groq`.

Cloud engines reuse the provider keys in `secrets.toml`. You can skip voice and add it later.

### 3 · About you (onboarding mode)
Choose how much Helios knows and how freely it acts:

- **Blank** — knows nothing; asks permission for *everything* not on the allow-list.
- **Default** — a few quick questions; low-risk actions run automatically.
- **Full** — a deeper interview (written to your Obsidian `Profile.md`); you pick which actions are
  auto-approved.
- **Manual** — choose exactly which questions to answer, which permissions to grant, and which apps
  to connect.

### 4 · Apps & memory
- **Obsidian vault** — *required for every setup*; it's how Helios remembers. The wizard creates the
  folder and seeds `Profile.md` from your interview.
- **Telegram / Spotify / Composio** — each offered but skippable ("set up later").

## Configuration files

| File | Purpose | Committed? |
|---|---|---|
| `config/settings.toml` | Non-secret settings (brain engine, voice, autonomy, …) | yes |
| `config/secrets.toml` | API keys + tokens (per provider) | no (gitignored) |
| `config/secrets.example.toml` | Template for the above | yes |
| `config/mcp.json` · `config/claude_settings.json` | Per-install (machine paths) | no — generated from the committed `*.template` files |

### Manual config (optional)
You normally never touch these, but for reference:

```toml
# config/settings.toml
[brain]
engine = "claude"      # or "lite"
provider = "openai"    # lite only: openai | openrouter | gemini | ollama | groq
model = ""             # lite only: blank = a sensible default per provider

[voice]
tts_engine = "kokoro"          # kokoro | openai | groq
stt_engine = "faster-whisper"  # faster-whisper | openai | groq
```

```toml
# config/secrets.toml  (per-provider keys; reused by brain AND cloud voice)
[anthropic]
api_key = "sk-ant-..."   # only for the Claude API-key path
[openai]
api_key = "sk-..."
[search]                 # optional web search for the lite brain
backend = "tavily"       # tavily | brave | serper
api_key = "..."
```

## Running

- **`helios`** or **`helios start`** — launch Helios in the background.
- **`helios stop`** · **`helios restart`** · **`helios status`** — control / check it.
- **`helios uninstall`** — remove the app, scheduled task, and PATH entry. Add `--purge` to also
  delete the Obsidian vault + downloaded model caches.
- **Auto-start:** if you opted in, the `Helios` scheduled task launches it at login.
- **Summon:** `Ctrl+Alt+J` · **Panic:** `Ctrl+Alt+Backspace`.
- **Health:** `http://127.0.0.1:8769/health` → `{"ok":true}`.

## Troubleshooting

- **"Claude Code CLI not found"** — install Node.js, then `npm install -g @anthropic-ai/claude-code`,
  and run `claude` once to sign in. Re-run setup.
- **Lite tool-calling is poor** — pick a tool-capable model (GPT-4o-class, Gemini 2.x, a strong
  OpenRouter model, or an Ollama model with tool support).
- **Kokoro voice silent** — ensure `data/voices/kokoro-v1.0.onnx` + `voices-v1.0.bin` downloaded
  (re-run setup → voice).
- **Change anything** — just re-run `python setup.py`.
