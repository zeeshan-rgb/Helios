#!/usr/bin/env python3
"""Helios one-command setup — `python setup.py`.

A friendly, provider-driven CLI installer. You never hand-edit config files: pick a provider and
the wizard writes the right settings/secrets for you. Two stages so the pretty UI can exist on a
fresh clone:

  Stage 0 (stdlib only): verify Python, create .venv, pip install -r requirements.txt, then re-exec
                         into the venv.
  Stage 1 (in the venv): a rich/questionary wizard —
      1. Brain   — Headless Claude (sign in) / Claude API key / OpenRouter / OpenAI / Gemini / Ollama
      2. Voice   — optional ("set up voice now?"): pluggable TTS/STT (Kokoro+faster-whisper / cloud)
      3. Onboard — blank / default / full / manual (persona interview -> vault Profile.md + autonomy)
      4. Apps    — Obsidian vault (REQUIRED, all setups) + optional Telegram / Spotify / Composio
      5. Wires up the per-install config files and (optionally) the auto-start scheduled task.

Idempotent and re-runnable: existing values are offered as defaults; you can re-run any time to
change providers, add voice, etc. This script NEVER starts Helios — it only configures it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
VENV = REPO / ".venv"
_SCRIPTS = "Scripts" if os.name == "nt" else "bin"
VENV_PY = VENV / _SCRIPTS / ("python.exe" if os.name == "nt" else "python")
VENV_PYW = VENV / _SCRIPTS / ("pythonw.exe" if os.name == "nt" else "python")


# ============================================================ STAGE 0 — bootstrap (stdlib only)
def _in_repo_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == VENV_PY.resolve()
    except Exception:
        return False


def bootstrap() -> int:
    """Create the venv + install deps under whatever Python launched us, then re-exec the wizard."""
    print("\n=== Helios setup ===\n")
    if sys.version_info < (3, 10):
        print(f"! Python 3.10+ required (found {sys.version.split()[0]}).")
        return 1
    if not VENV.exists():
        print("• Creating virtual environment (.venv) …")
        rc = subprocess.run([sys.executable, "-m", "venv", str(VENV)]).returncode
        if rc != 0:
            print("! Failed to create the virtual environment.")
            return rc
    print("• Installing dependencies (this can take a few minutes) …")
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    rc = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-r",
                         str(REPO / "requirements.txt")]).returncode
    if rc != 0:
        print("! Dependency installation failed. Fix the error above and re-run `python setup.py`.")
        return rc
    print("• Launching the setup wizard …\n")
    return subprocess.run([str(VENV_PY), str(REPO / "setup.py"), "--run-wizard"]).returncode


# ============================================================ STAGE 1 — the wizard
def run_wizard() -> int:
    sys.path.insert(0, str(REPO))
    import questionary
    from rich.console import Console
    from rich.panel import Panel

    from helios import conf

    console = Console()

    # ---------- small helpers ----------
    def section(title: str, subtitle: str = "") -> None:
        body = f"[bold cyan]{title}[/bold cyan]"
        if subtitle:
            body += f"\n[dim]{subtitle}[/dim]"
        console.print(Panel(body, border_style="cyan"))

    def info(msg: str) -> None:
        console.print(f"  [dim]›[/dim] {msg}")

    def ok(msg: str) -> None:
        console.print(f"  [green]✓[/green] {msg}")

    def warn(msg: str) -> None:
        console.print(f"  [yellow]![/yellow] {msg}")

    def ask_select(msg, choices, default=None):
        return questionary.select(msg, choices=choices, default=default).ask()

    def ask_text(msg, default=""):
        return questionary.text(msg, default=default).ask()

    def ask_secret(msg):
        return questionary.password(msg).ask()

    def ask_yes(msg, default=True):
        # auto_enter=False so a y/n keypress only TOGGLES the shown answer — the user presses
        # Enter to confirm (and can flip their choice first), instead of submitting instantly.
        return bool(questionary.confirm(msg, default=default, auto_enter=False).ask())

    def set_settings(**dotted) -> None:
        conf.update_settings(dotted)

    def write_secret(section_name: str, key: str, value) -> None:
        """Set [section_name].key = value in config/secrets.toml (created/merged, comments kept)."""
        import tomlkit
        p = conf.CONFIG_DIR / "secrets.toml"
        doc = tomlkit.parse(p.read_text(encoding="utf-8")) if p.exists() else tomlkit.document()
        if section_name not in doc:
            doc[section_name] = tomlkit.table()
        doc[section_name][key] = value
        tmp = p.with_suffix(".toml.tmp")
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(tmp, p)
        conf.reload()

    # ---------- welcome ----------
    console.print(Panel(
        "[bold]Welcome to Helios setup[/bold]\n\n"
        "I'll connect a [bold]brain[/bold], optionally set up [bold]voice[/bold], learn a bit "
        "[bold]about you[/bold], and wire up your apps.\nNothing is started — when we're done you "
        "launch Helios yourself.",
        title="Helios", border_style="cyan"))

    # ============================== 1. BRAIN ==============================
    section("1 · Brain", "How should Helios think? Claude gives the full experience; the others "
                         "run a lighter, tool-capable assistant.")
    brain_choice = ask_select(
        "Pick a brain provider:",
        choices=[
            "Antigravity CLI — sign in with your Google account (full power, uses your Google AI Pro plan)",
            "Headless Claude — sign in with your Claude account (full power, free on your plan)",
            "Gemini CLI — sign in with your Google account (full power, uses your Gemini plan)",
            "Claude API key — full power via an Anthropic API key",
            "OpenRouter — any model via one key (lite)",
            "OpenAI — GPT models (lite)",
            "Gemini — Google models (lite)",
            "Ollama — fully local models, no key (lite)",
        ])
    if brain_choice is None:
        warn("Setup cancelled.")
        return 1

    if brain_choice.startswith("Headless Claude"):
        _setup_headless_claude(console, info, ok, warn, ask_yes)
        set_settings(**{"brain.engine": "claude"})
        ok("Brain: Claude (account login).")
    elif brain_choice.startswith("Antigravity CLI"):
        _setup_antigravity(info, ok, warn)
        set_settings(**{"brain.engine": "antigravity"})
        ok("Brain: Antigravity CLI (Google account).")
    elif brain_choice.startswith("Gemini CLI"):
        _setup_gemini_cli(info, ok, warn, ask_yes, set_settings)
        set_settings(**{"brain.engine": "gemini"})
        ok("Brain: Gemini CLI (Google account).")
    elif brain_choice.startswith("Claude API key"):
        key = ask_secret("Paste your Anthropic API key (sk-ant-…):")
        if key:
            write_secret("anthropic", "api_key", key.strip())
        set_settings(**{"brain.engine": "claude"})
        _ensure_claude_installed(console, info, ok, warn, ask_yes)
        ok("Brain: Claude (API key).")
    else:
        prov = {"OpenRouter": "openrouter", "OpenAI": "openai",
                "Gemini": "gemini", "Ollama": "ollama"}[brain_choice.split(" ")[0]]
        _setup_lite_provider(prov, console, info, ok, warn, ask_text, ask_secret, write_secret,
                             set_settings)

    # ============================== 2. VOICE ==============================
    if ask_yes("\nSet up voice (wake word, speech-to-text, text-to-speech) now?", default=False):
        section("2 · Voice", "Local engines need no keys; cloud engines (OpenAI/Groq) trade "
                             "privacy for quality.")
        _setup_voice(console, info, ok, warn, ask_select, ask_text, ask_secret, ask_yes,
                     write_secret, set_settings, conf)
        set_settings(**{"voice.enabled": True})
    else:
        set_settings(**{"voice.enabled": False})
        info("Skipping voice — you can enable it later by re-running setup or in Settings.")

    # ============================== 3. ONBOARDING ==============================
    section("3 · About you", "How much should Helios know and how freely should it act?")
    mode = ask_select(
        "Choose an onboarding mode:",
        choices=[
            "Default — a few quick questions; low-risk actions allowed automatically",
            "Blank — Helios knows nothing and asks permission for everything",
            "Full — a deeper interview; you choose permissions and apps",
            "Manual — pick exactly which questions, permissions, and apps you want",
        ])
    profile_answers, allow_list, always_ask_list, autonomy_mode = _run_onboarding(
        mode or "Default", console, info, ok, ask_select, ask_text, ask_yes)
    set_settings(**{"autonomy.allow": allow_list, "autonomy.always_ask": always_ask_list,
                    "autonomy.mode": autonomy_mode,
                    "onboarding.mode": (mode or "Default").split(" ")[0].lower()})

    # ============================== 4. APPS ==============================
    section("4 · Apps & memory", "Obsidian is required (it's how Helios remembers). The rest are "
                                 "optional and can be added later.")
    vault = _setup_obsidian(console, info, ok, warn, ask_text, set_settings, conf, profile_answers)
    if ask_yes("Connect Telegram (text Helios from your phone)?", default=False):
        _setup_telegram(info, ok, warn, ask_secret, ask_text, write_secret)
    else:
        info("Skipped Telegram (set up later).")
    if ask_yes("Set up Spotify (play-on-wake / playback control)?", default=False):
        _setup_spotify(info, ok, warn, ask_yes, ask_text, set_settings, conf)
    else:
        info("Skipped Spotify (set up later).")
    if ask_yes("Connect Composio (Gmail, GitHub, 1000+ apps)?", default=False):
        _setup_composio(info, ok, warn)
    else:
        info("Skipped Composio (set up later).")

    # ============================== 5. WIRE-UP ==============================
    section("5 · Finishing up", "Writing your machine-specific config files.")
    # De-hardcode the brain's working dir to THIS user's home (the committed default is the author's).
    set_settings(**{"paths.workspace": str(Path.home())})
    _write_generated_configs(conf, info, ok, warn)
    if ask_yes("Start Helios automatically when you log in (recommended)?", default=True):
        _register_scheduled_task(info, ok, warn)
    else:
        info("Skipped auto-start. Launch manually with run_helios.pyw.")

    console.print(Panel(
        "[bold green]All set, sir.[/bold green]\n\n"
        f"• Brain + memory configured (vault: {vault})\n"
        "• Launch: [bold]helios[/bold]  (or double-click [bold]run_helios.pyw[/bold]; or it "
        "auto-starts at next login if you enabled that)\n"
        "• Summon: [bold]Ctrl+Alt+J[/bold]  ·  Panic: [bold]Ctrl+Alt+Backspace[/bold]\n"
        "• Health check: http://127.0.0.1:8769/health\n\n"
        "Re-run [bold]helios onboard[/bold] (or [bold]python setup.py[/bold]) any time to change "
        "providers or add voice/apps.",
        title="Helios ready", border_style="green"))
    return 0


# ---------------------------------------------------------------- brain helpers
def _ensure_claude_installed(console, info, ok, warn, ask_yes) -> bool:
    """Make sure the `claude` CLI exists; offer to npm-install it. Returns True if available."""
    import shutil
    if shutil.which("claude") or shutil.which("claude.exe"):
        ok("Claude Code CLI found.")
        return True
    warn("The Claude Code CLI isn't installed (required for the Claude brain).")
    if shutil.which("npm"):
        if ask_yes("Install it now with npm (npm install -g @anthropic-ai/claude-code)?", True):
            rc = subprocess.run(["npm", "install", "-g", "@anthropic-ai/claude-code"]).returncode
            if rc == 0 and (shutil.which("claude") or shutil.which("claude.exe")):
                ok("Claude Code installed.")
                return True
            warn("Install didn't complete — see https://docs.claude.com/claude-code")
    else:
        warn("Node/npm not found. Install Node.js, then: npm install -g @anthropic-ai/claude-code")
        warn("Guide: https://docs.claude.com/claude-code")
    return False


def _setup_antigravity(info, ok, warn) -> None:
    """Antigravity brain: the official agy CLI, signed in with the user's Google account."""
    from helios import agy_cli
    if not agy_cli.command():
        warn("The Antigravity CLI (agy) isn't installed or [antigravity].bin points nowhere.")
        info("Install it with Google's official installer (PowerShell), e.g. to D: :")
        info("  irm https://antigravity.google/cli/install.ps1 -OutFile install.ps1; "
             ".\\install.ps1 --dir D:\\Helios\\tools\\agy\\bin")
        info("Then set [antigravity].bin in config/settings.toml and re-run setup.")
        return
    ok("Antigravity CLI found.")
    info("Helios uses your Google account login (your Google AI Pro quota — no API key).")
    info("If you haven't yet: open a terminal, run `agy`, choose Google OAuth and sign in, "
         "then type /quit.")


def _setup_gemini_cli(info, ok, warn, ask_yes, set_settings) -> None:
    """Gemini CLI brain: make sure `gemini` exists (offer npm install), then sign in with Google."""
    import shutil
    from helios import gemini_cli
    if not gemini_cli.command():
        warn("The Gemini CLI isn't installed (required for the Gemini brain).")
        if shutil.which("npm") and ask_yes(
                "Install it now with npm (npm install -g @google/gemini-cli)?", True):
            subprocess.run(["npm", "install", "-g", "@google/gemini-cli"])
        found = shutil.which("gemini.cmd") or shutil.which("gemini")
        if found:
            set_settings(**{"gemini.bin": found.replace("\\", "/")})
        if not gemini_cli.command():
            warn("Install didn't complete — see https://github.com/google-gemini/gemini-cli")
            return
    ok("Gemini CLI found.")
    info("Helios uses your Google account login (your Gemini plan's quota — no API key).")
    if gemini_cli.signed_in():
        ok("Helios's Gemini home is already signed in.")
    elif ask_yes("Sign in to Gemini now (opens the Gemini CLI here)?", True):
        import helios_cli
        helios_cli.cmd_gemini_login(None)
    else:
        info("When you're ready, run `helios gemini-login`.")


def _setup_headless_claude(console, info, ok, warn, ask_yes) -> None:
    if not _ensure_claude_installed(console, info, ok, warn, ask_yes):
        return
    info("Helios uses your Claude account login (free on your plan — no API key needed).")
    if ask_yes("Sign in to Claude now (opens the Claude CLI in a new window)?", True):
        try:
            if os.name == "nt":
                subprocess.Popen('start "Claude login" cmd /k claude', shell=True)
            else:
                subprocess.Popen(["x-terminal-emulator", "-e", "claude"])
            info("Complete the sign-in in that window, then return here.")
            input("  Press Enter once you're signed in… ")
            ok("Claude sign-in acknowledged.")
        except Exception as e:
            warn(f"Couldn't launch Claude automatically ({e}). Run `claude` once to sign in.")
    else:
        info("When you're ready, run `claude` once in a terminal and sign in.")


def _setup_lite_provider(prov, console, info, ok, warn, ask_text, ask_secret, write_secret,
                         set_settings) -> None:
    defaults = {"openrouter": "openai/gpt-4o", "openai": "gpt-4o",
                "gemini": "gemini-3.8-flash", "ollama": "llama3.1"}
    if prov == "ollama":
        base = ask_text("Ollama base URL:", "http://127.0.0.1:11434/v1")
        if base:
            write_secret("ollama", "base_url", base.strip())
        models = _ollama_models(base or "http://127.0.0.1:11434/v1")
        if models:
            info("Installed Ollama models: " + ", ".join(models[:8]))
        model = ask_text("Model to use:", models[0] if models else defaults["ollama"])
    else:
        key = ask_secret(f"Paste your {prov} API key:")
        if key:
            write_secret(prov, "api_key", key.strip())
        model = ask_text("Model to use:", defaults[prov])
    set_settings(**{"brain.engine": "lite", "brain.provider": prov,
                    "brain.model": (model or "").strip()})
    ok(f"Brain: {prov} (lite) · model {model}")
    warn("Lite mode has a curated, permission-gated toolset (files, shell, open app, web, system "
         "health, memory, reminders) — no computer-use or multi-agent missions.")


def _ollama_models(base_url: str) -> list[str]:
    import json
    import urllib.request
    try:
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        with urllib.request.urlopen(root + "/api/tags", timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


# ---------------------------------------------------------------- voice helpers
def _setup_voice(console, info, ok, warn, ask_select, ask_text, ask_secret, ask_yes,
                 write_secret, set_settings, conf) -> None:
    tts = ask_select("Text-to-speech engine:",
                     choices=["Kokoro — local, no key (recommended)",
                              "OpenAI — cloud", "Groq — cloud"])
    tts_engine = {"Kokoro": "kokoro", "OpenAI": "openai", "Groq": "groq"}[(tts or "Kokoro").split(" ")[0]]
    set_settings(**{"voice.tts_engine": tts_engine})
    if tts_engine == "kokoro":
        _ensure_kokoro_models(conf, info, ok, warn, ask_yes)
    else:
        _ensure_provider_key(tts_engine, info, ok, ask_secret, write_secret)

    stt = ask_select("Speech-to-text engine:",
                     choices=["faster-whisper — local, no key (recommended)",
                              "OpenAI — cloud", "Groq — cloud"])
    stt_engine = {"faster-whisper": "faster-whisper", "OpenAI": "openai",
                  "Groq": "groq"}[(stt or "faster-whisper").split(" ")[0]]
    set_settings(**{"voice.stt_engine": stt_engine})
    if stt_engine == "faster-whisper":
        size = ask_select("Whisper model size:",
                          choices=["base.en — balanced", "tiny.en — fastest", "small.en — best"])
        set_settings(**{"voice.stt_model": (size or "base.en").split(" ")[0]})
        info("The Whisper model downloads automatically on first use.")
    else:
        _ensure_provider_key(stt_engine, info, ok, ask_secret, write_secret)
    ok(f"Voice: TTS={tts_engine}, STT={stt_engine}")


def _ensure_provider_key(prov, info, ok, ask_secret, write_secret) -> None:
    if conf_has_key(prov):
        ok(f"Using the existing {prov} API key.")
        return
    key = ask_secret(f"Paste your {prov} API key (for cloud voice):")
    if key:
        write_secret(prov, "api_key", key.strip())
        ok(f"{prov} key saved.")


def conf_has_key(prov: str) -> bool:
    sys.path.insert(0, str(REPO))
    from helios import conf
    return bool(conf.provider_cfg(prov).get("api_key"))


def _ensure_kokoro_models(conf, info, ok, warn, ask_yes) -> None:
    if conf.KOKORO_MODEL.exists() and conf.KOKORO_VOICES.exists():
        ok("Kokoro voice models already present.")
        return
    urls = {
        conf.KOKORO_MODEL: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                           "model-files-v1.0/kokoro-v1.0.onnx",
        conf.KOKORO_VOICES: "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                            "model-files-v1.0/voices-v1.0.bin",
    }
    if not ask_yes("Download Kokoro voice models now (~350 MB)?", True):
        warn("Kokoro TTS won't work until the models are in data/voices/.")
        return
    conf.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request
    for dest, url in urls.items():
        if dest.exists():
            continue
        info(f"Downloading {dest.name} …")
        try:
            urllib.request.urlretrieve(url, str(dest))
            ok(f"{dest.name} downloaded.")
        except Exception as e:
            warn(f"Download failed ({e}). Get it manually from {url} -> {dest}")


# ---------------------------------------------------------------- onboarding helpers
# Granular permission capabilities for onboarding. Each maps to the EXACT tool names the
# permission engine (helios/permissions.py classify()) gates, in one of two groups:
#   default_auto=True  -> the engine ALREADY runs it automatically; UNCHECKING adds the tools to
#                         `always_ask` (tighten). Never added to `allow`, so the sensitive-file /
#                         self-modify guards inside classify() are never bypassed.
#   default_auto=False -> the engine asks every time; CHECKING adds the tools to `allow` (pre-approve).
# (label, [tool names], default_auto)
_CAPABILITIES = [
    # ---- Safe: automatic by default — uncheck to make Helios ask ----
    ("See the screen (screenshots, list windows)",
     ["mcp__computer__screenshot", "mcp__computer__screen_size", "mcp__computer__cursor_pos",
      "mcp__computer__list_windows", "mcp__computer__focus_window"], True),
    ("Control mouse & keyboard (click, type, drag)",
     ["mcp__computer__move", "mcp__computer__click", "mcp__computer__double_click",
      "mcp__computer__scroll", "mcp__computer__drag", "mcp__computer__type_text",
      "mcp__computer__press"], True),
    ("Open / launch apps", ["mcp__computer__launch_app"], True),
    ("Read your files  (secrets, financial & medical always ask)", ["Read", "Grep"], True),
    ("Create & edit files  (scripts & Helios's own code always ask)",
     ["Write", "Edit", "MultiEdit", "NotebookEdit"], True),
    ("Search the web", ["WebSearch"], True),
    # ---- Powerful: Helios asks every time — check to pre-approve ----
    ("Open & read web pages (WebFetch — internal addresses always blocked)", ["WebFetch"], False),
    ("Run shell commands that change things (installs, moves, settings)",
     ["Bash", "PowerShell", "BashOutput"], False),
    ("Run background agents & multi-agent missions",
     ["mcp__helios__run_in_background", "mcp__helios__start_mission", "mcp__helios__spawn_agent"],
     False),
    ("Write & run its own new tools", ["mcp__helios__create_tool"], False),
    ("Turn on screen-watching by itself", ["mcp__helios__set_screen_awareness"], False),
]


def _perms_from_checked(checked_labels) -> tuple[list, list]:
    """Map the CHECKED capability labels to (allow, always_ask) deltas vs the default policy:
    checked & default-ask -> allow (pre-approve); unchecked & default-auto -> always_ask (tighten)."""
    allow, always_ask = [], []
    for label, tools, default_auto in _CAPABILITIES:
        checked = label in checked_labels
        if checked and not default_auto:
            allow += tools
        elif not checked and default_auto:
            always_ask += tools
    return allow, always_ask

# (key, prompt, kind) — kind 'fact' or 'pref'. Full uses all; default uses a short head; manual lets
# the user pick.
_INTERVIEW = [
    ("name", "What should Helios call you?", "fact"),
    ("role", "What do you do (work / study)?", "fact"),
    ("location", "Where are you based (city / timezone)?", "fact"),
    ("respond_style", "How should Helios talk to you (tone, length, formality)?", "pref"),
    ("people", "Key people in your life Helios should know (name — relationship)?", "fact"),
    ("projects", "What are you currently working on?", "fact"),
    ("tools", "Which apps/tools do you use most?", "fact"),
    ("routine", "Anything about your daily routine Helios should know?", "fact"),
    ("interests", "Hobbies / interests?", "fact"),
    ("avoid", "Anything Helios should always AVOID doing?", "pref"),
]


def _run_onboarding(mode, console, info, ok, ask_select, ask_text, ask_yes):
    """Returns (answers dict, allow_list, always_ask_list, autonomy_mode)."""
    import questionary
    m = mode.split(" ")[0].lower()
    answers: dict[str, str] = {}

    if m == "blank":
        info("Blank slate: Helios will ask before doing anything until you grant permissions.")
        return answers, [], [], "blank"

    if m == "default":
        for key, prompt, _kind in _INTERVIEW[:4]:
            a = ask_text(prompt, "")
            if a:
                answers[key] = a
        ok("Thanks — the safe defaults run automatically; powerful actions will ask.")
        return answers, [], [], ""   # the engine's standard policy, no overrides

    # full / manual share the deeper interview; manual lets you skip questions.
    questions = _INTERVIEW
    if m == "manual":
        picked = questionary.checkbox(
            "Pick the questions you want to answer:",
            choices=[q[1] for q in _INTERVIEW]).ask() or []
        questions = [q for q in _INTERVIEW if q[1] in picked]
    for key, prompt, _kind in questions:
        a = ask_text(prompt, "")
        if a:
            answers[key] = a

    # granular permission picker: Safe items pre-checked, Powerful items unchecked. Checking /
    # unchecking computes the allow / always_ask deltas (see _perms_from_checked).
    choices = [questionary.Choice(title=label, checked=default_auto)
               for (label, _tools, default_auto) in _CAPABILITIES]
    checked = questionary.checkbox(
        "Which actions may Helios take automatically?  (space toggles, enter confirms)\n"
        "  Checked = automatic · unchecked = ask first. Top group is safe & on by default;\n"
        "  the rest are powerful & off by default.",
        choices=choices).ask()
    if checked is None:   # user escaped -> keep the safe defaults
        checked = [label for (label, _t, da) in _CAPABILITIES if da]
    allow, always_ask = _perms_from_checked(set(checked))
    ok(f"Permissions set: {len(allow)} extra auto-approved, {len(always_ask)} moved to ask-first.")
    return answers, allow, always_ask, ""


def _profile_markdown(answers: dict) -> str:
    name = answers.get("name") or "you"
    facts, prefs = [], []
    label = {"role": "Role", "location": "Location", "people": "People", "projects": "Projects",
             "tools": "Tools", "routine": "Routine", "interests": "Interests"}
    for key, lab in label.items():
        if answers.get(key):
            facts.append(f"- {lab}: {answers[key]}")
    if answers.get("respond_style"):
        prefs.append(f"- Respond: {answers['respond_style']}")
    if answers.get("avoid"):
        prefs.append(f"- Avoid: {answers['avoid']}")
    return ("---\ntype: profile\n---\n\n"
            f"# Profile — {name}\n\n"
            "## Facts\n" + ("\n".join(facts) + "\n" if facts else "") +
            "\n## Preferences\n" + ("\n".join(prefs) + "\n" if prefs else ""))


# ---------------------------------------------------------------- app helpers
def _setup_obsidian(console, info, ok, warn, ask_text, set_settings, conf, answers):
    default = str(Path.home() / "Documents" / "Obsidian Vaults" / "LocalAI")
    existing = conf.vault_path()
    vault = ask_text("Obsidian vault folder (created if missing — REQUIRED):",
                     str(existing) if existing else default) or default
    vpath = Path(vault).expanduser()
    for sub in ("", "People", "Projects", "Daily"):
        (vpath / sub).mkdir(parents=True, exist_ok=True)
    profile = vpath / "Profile.md"
    if not profile.exists() or answers:
        profile.write_text(_profile_markdown(answers), encoding="utf-8")
    index = vpath / "_index.md"
    if not index.exists():
        index.write_text("---\ntype: index\n---\n\n# LocalAI — Helios memory hub\n\n[[Profile]]\n",
                         encoding="utf-8")
    set_settings(**{"paths.vault": str(vpath)})
    ok(f"Obsidian vault ready at {vpath}")
    return vpath


def _setup_telegram(info, ok, warn, ask_secret, ask_text, write_secret) -> None:
    info("In Telegram: @BotFather -> /newbot (token); @userinfobot -> your numeric id.")
    token = ask_secret("Telegram bot token:")
    ids = ask_text("Allowed Telegram user id(s), comma-separated:", "")
    if token:
        write_secret("telegram", "token", token.strip())
    id_list = []
    for part in (ids or "").split(","):
        part = part.strip()
        if part.isdigit():
            id_list.append(int(part))
    write_secret("telegram", "allowed_ids", id_list)
    if token and not id_list:
        warn("No allowed ids set — the bridge denies everyone until you add your id.")
    else:
        ok("Telegram configured.")


def _setup_spotify(info, ok, warn, ask_yes, ask_text, set_settings, conf) -> None:
    info("Spotify needs a one-time OAuth (a Spotify Developer app + browser sign-in).")
    info("Helper: tools/spotify_auth.py — it writes data/spotify_token.json.")
    if ask_yes("Run the Spotify auth helper now?", False):
        try:
            subprocess.run([str(VENV_PY), str(REPO / "tools" / "spotify_auth.py")])
            ok("Spotify auth helper finished.")
        except Exception as e:
            warn(f"Couldn't run the helper ({e}). Run it later: python tools/spotify_auth.py")
    else:
        info("Run it later: python tools/spotify_auth.py")

    # Only once Spotify is actually connected, ask what to play when Helios wakes (double-clap /
    # Ctrl+Alt+J). Saved to [startup].spotify_play; blank = just open Spotify, no specific playlist.
    # If it isn't connected, Helios won't open Spotify on wake at all (see app._open_app).
    try:
        from helios import spotify
        connected = spotify.configured()
    except Exception:
        connected = False
    if not connected:
        info("Spotify isn't connected yet — re-run setup after auth to pick a wake playlist.")
        return
    current = str(conf.startup_cfg().get("spotify_play", ""))
    play = ask_text(
        "Which playlist should Helios play when it wakes? (Spotify link or URI; blank = none):",
        current)
    set_settings(**{"startup.spotify_play": (play or "").strip()})
    if (play or "").strip():
        ok("Helios will play that on wake.")
    else:
        info("No wake playlist — Helios will just open Spotify when it wakes.")


def _setup_composio(info, ok, warn) -> None:
    info("Composio connects Gmail/GitHub/1000+ apps via a tool router.")
    info("1) Get an API key at https://composio.dev")
    info("2) Save it in config/composio_mcp.json (gitignored) per the Composio MCP setup.")
    warn("Composio wiring is guided, not automated — see the project README/handoff for details.")


# ---------------------------------------------------------------- wire-up helpers
def _write_generated_configs(conf, info, ok, warn) -> None:
    """Render config/mcp.json + claude_settings.json from the committed *.template files with this
    machine's paths (forward-slashed so the JSON needs no escaping)."""
    repo = REPO.as_posix()
    pyw = VENV_PYW.as_posix()
    cua = str(conf.cua_driver_bin()).replace("\\", "/")
    subs = {"__REPO__": repo, "__VENV_PYTHONW__": pyw, "__CUA_DRIVER_BIN__": cua}
    for name in ("mcp.json", "claude_settings.json"):
        tpl = conf.CONFIG_DIR / (name + ".template")
        if not tpl.exists():
            warn(f"Missing template {tpl.name}; skipping.")
            continue
        text = tpl.read_text(encoding="utf-8")
        for k, v in subs.items():
            text = text.replace(k, v)
        (conf.CONFIG_DIR / name).write_text(text, encoding="utf-8")
        ok(f"Wrote config/{name}")


def _register_scheduled_task(info, ok, warn) -> None:
    if os.name != "nt":
        warn("Auto-start registration is Windows-only; set up your own login item.")
        return
    pyw = str(VENV_PYW)
    script = str(REPO / "run_helios.pyw")
    ps = (
        f'$a = New-ScheduledTaskAction -Execute "{pyw}" -Argument \'"{script}"\' '
        f'-WorkingDirectory "{REPO}"; '
        '$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; '
        '$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries '
        '-StartWhenAvailable; '
        'Register-ScheduledTask -TaskName Helios -Action $a -Trigger $t -Settings $s -Force '
        '| Out-Null'
    )
    try:
        rc = subprocess.run(["powershell", "-NoProfile", "-Command", ps]).returncode
        if rc == 0:
            ok("Registered the 'Helios' scheduled task (starts at login).")
        else:
            warn("Couldn't register the scheduled task; you can start run_helios.pyw manually.")
    except Exception as e:
        warn(f"Scheduled-task registration failed ({e}).")


# ============================================================ entry point
def main() -> int:
    if "--run-wizard" in sys.argv or _in_repo_venv():
        try:
            return run_wizard()
        except KeyboardInterrupt:
            print("\nSetup cancelled.")
            return 1
    return bootstrap()


if __name__ == "__main__":
    sys.exit(main())
