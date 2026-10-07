#!/usr/bin/env python3
"""Helios command line â€” `helios <command>`.

Installed on PATH by install.ps1 as a thin shim (bin/helios.cmd) that runs this file with the
install's own venv Python, so all deps are importable. Keep this dependency-light at import time.

Commands:
  onboard   run the setup wizard (connect a brain, voice, onboarding, apps)
  start     launch Helios in the background (if not already running)
  stop      ask a running Helios to quit
  restart   stop, then start
  update    update to the latest code (keeps venv/data/settings), then restart
  status    is Helios running?
  where     print the install folder
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Windows consoles default to cp1252, which can't encode the status glyphs (â— â—‹) we print â€” make
# stdout/stderr UTF-8 so `helios status`/`start` don't crash with UnicodeEncodeError.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
_SCRIPTS = "Scripts" if os.name == "nt" else "bin"
VENV_PYW = REPO / ".venv" / _SCRIPTS / ("pythonw.exe" if os.name == "nt" else "python")
VENV_PY = REPO / ".venv" / _SCRIPTS / ("python.exe" if os.name == "nt" else "python")  # for pip
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW (no console flash)


def _conf():
    sys.path.insert(0, str(REPO))
    from helios import conf
    return conf


def _is_running() -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(_conf().BASE_URL + "/health", timeout=3)
        return True
    except Exception:
        return False


def cmd_onboard(_args) -> int:
    """Run the interactive setup wizard in this console."""
    sys.path.insert(0, str(REPO))
    import setup
    return setup.run_wizard()


def cmd_status(_args) -> int:
    if _is_running():
        print(f"â— Helios is running at {_conf().BASE_URL}")
        return 0
    print("â—‹ Helios is not running.")
    return 1


def cmd_start(_args) -> int:
    if _is_running():
        print("Helios is already running.")
        return 0
    if not VENV_PYW.exists():
        print("! Helios isn't fully installed. Re-run the installer, then `helios onboard`.")
        return 1
    subprocess.Popen([str(VENV_PYW), str(REPO / "run_helios.pyw")],
                     cwd=str(REPO), creationflags=_NO_WINDOW, close_fds=True)
    print("Starting Heliosâ€¦  (summon with Ctrl+Alt+J)")
    return 0


def cmd_stop(_args) -> int:
    if not _is_running():
        print("Helios is not running.")
        return 0
    import urllib.request
    conf = _conf()
    req = urllib.request.Request(conf.BASE_URL + "/quit", method="POST",
                                 headers={"X-Auth-Token": conf.auth_token() or ""})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    time.sleep(1.5)
    if not _is_running():
        print("Helios stopped.")
        return 0
    print("Couldn't confirm shutdown â€” it may still be closing.")
    return 1


def cmd_restart(_args) -> int:
    cmd_stop(None)
    time.sleep(1.0)
    return cmd_start(None)


def cmd_where(_args) -> int:
    print(REPO)
    return 0


def cmd_memory(args) -> int:
    """Inspect what Helios remembers: `helios memory [list [category] | search <words> |
    forget <id>]`. The items are plain Markdown notes in the vault ([paths].vault)."""
    conf = _conf()
    from helios import memory_store
    args = list(args or [])
    action = args.pop(0).lower() if args else "list"
    if action == "list":
        cat = args[0].lower() if args else None
        if cat and cat not in memory_store.CATEGORIES:
            print(f"Unknown category. Use one of: {', '.join(memory_store.CATEGORIES)}")
            return 2
        print(memory_store.format_items(memory_store.items(cat)))
    elif action == "search" and args:
        print(memory_store.format_items(memory_store.recall(" ".join(args), limit=20)))
    elif action == "forget" and args:
        res = memory_store.forget(" ".join(args))
        if res["deleted"]:
            print(f"Forgotten: {res['deleted'][0]['text']}")
        elif res["candidates"]:
            print("Several items match — forget one by id:\n"
                  + memory_store.format_items(res["candidates"]))
        else:
            print("Nothing matching that is remembered.")
    else:
        print("usage: helios memory [list [category] | search <words> | forget <id or words>]")
        return 2
    print(f"\n(vault: {conf.vault_path()})")
    return 0


def cmd_learn(args) -> int:
    """Learning loop: `helios learn [YYYY-MM-DD] [--force] [--no-llm] | review | approve <id> |
    reject <id>`. Learning only adds memory items; new rules/skills wait for `approve`."""
    _conf()
    from helios import learning, memory_store
    args = list(args or [])
    action = args[0].lower() if args else ""
    if action == "review":
        found = learning.review()
        print(memory_store.format_items(found) if found else "No lessons are waiting for approval.")
    elif action in ("approve", "reject") and len(args) > 1:
        it = (learning.approve if action == "approve" else learning.reject)(args[1])
        print(f"{action.title()}d: {it['text']}" if it else "No pending lesson with that id.")
        if not it:
            return 1
    elif action in ("", "--force", "--no-llm") or re.fullmatch(r"\d{4}-\d{2}-\d{2}", action):
        day = action if re.fullmatch(r"\d{4}-\d{2}-\d{2}", action) else None
        res = learning.learn_from_daily(day, use_llm="--no-llm" not in args,
                                        force="--force" in args)
        print(learning.format_report(res))
        if any(r["status"] == "pending" for r in res.get("lessons", [])):
            print("\nReview with `helios learn review`, then `helios learn approve <id>`.")
    else:
        print("usage: helios learn [YYYY-MM-DD] [--force] [--no-llm] | review | "
              "approve <id> | reject <id>")
        return 2
    return 0


def cmd_voice_check(args) -> int:
    """Check every piece of the voice pipeline; --speak also plays a test line."""
    _conf()
    from helios.voice import diagnostics
    print("Helios voice check (loads the models — takes a few seconds)...\n")
    worst = "ok"
    for name, status, detail in diagnostics.run_checks():
        mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}[status]
        print(f"  {mark} {name:15s} {detail}")
        if status == "fail" or (status == "warn" and worst == "ok"):
            worst = status
    if args and "--speak" in args:
        print("\nSpeaking a test line through your speakers...")
        diagnostics.speak_test()
    print({"ok": "\nVoice is ready.", "warn": "\nVoice works, with warnings above.",
           "fail": "\nVoice has problems — see [FAIL] lines above."}[worst])
    return 1 if worst == "fail" else 0


def cmd_gemini_login(_args) -> int:
    """Sign Helios's private Gemini CLI home into a Google account (engine = "gemini")."""
    _conf()
    from helios import gemini_cli
    cmd = gemini_cli.command()
    if not cmd:
        print("! The Gemini CLI isn't installed. Run: npm install -g @google/gemini-cli")
        return 1
    gemini_cli.ensure_settings()
    gemini_cli.WORKSPACE.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GEMINI_CLI_HOME"] = str(gemini_cli.HOME)
    env.pop("GEMINI_CLI_SYSTEM_SETTINGS_PATH", None)
    print("Opening the Gemini CLI for Helios (its own sign-in, separate from your personal one).")
    print("Choose 'Sign in with Google', finish in your browser, then type /quit to come back.\n")
    rc = subprocess.call(cmd, env=env, cwd=str(gemini_cli.WORKSPACE))
    print("\nSigned in." if gemini_cli.signed_in() else
          "\n! No Google sign-in found yet — run `helios gemini-login` again.")
    return rc


# ---- uninstall ---------------------------------------------------------------
def _run_ps(script: str) -> None:
    """Run a short PowerShell snippet, swallowing errors (best-effort system tweaks)."""
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       creationflags=_NO_WINDOW, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _remove_from_user_path(bin_dir: Path) -> None:
    """Drop `bin_dir` from the USER Path env var (reverses install.ps1's PATH append)."""
    t = str(bin_dir).rstrip("\\").replace("'", "''")
    _run_ps(
        "$t = '" + t + "';"
        "$cur = [Environment]::GetEnvironmentVariable('Path','User');"
        "if ($cur) {"
        " $new = (($cur -split ';') | Where-Object { $_ -and ($_.TrimEnd('\\') -ine $t) }) -join ';';"
        " [Environment]::SetEnvironmentVariable('Path', $new, 'User');"
        "}"
    )


def _kill_under_dir(root: Path) -> None:
    """Force-kill the app's background **pythonw** processes under `root` (orb / voice daemon /
    main app) so they release locks on the folder we're about to delete. The uninstaller itself
    runs as python.exe (and its command line is the CLI), so a pythonw-only kill that also skips
    anything matching `helios_cli` can never terminate us."""
    r = str(root).replace("'", "''")
    _run_ps(
        "$root = '" + r + "';"
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" |"
        " Where-Object { $_.CommandLine -and ($_.CommandLine -like ('*' + $root + '*'))"
        " -and ($_.CommandLine -notlike '*helios_cli*') } |"
        " ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )


def _purge_user_data(conf) -> None:
    """Delete the Obsidian vault + faster-whisper model cache (opt-in only). Best effort; never
    touches the rest of the HF cache or the pip cache."""
    try:
        vault = conf.vault_path()
        if vault and Path(vault).exists():
            shutil.rmtree(vault, ignore_errors=True)
            print(f"  removed Obsidian vault: {vault}")
    except Exception as e:
        print(f"  (could not remove vault: {e})")
    hf_home = os.environ.get("HF_HOME")
    hub = Path(os.environ.get("HUGGINGFACE_HUB_CACHE")
               or (Path(hf_home) / "hub" if hf_home
                   else Path.home() / ".cache" / "huggingface" / "hub"))
    try:
        if hub.exists():
            for d in hub.glob("models--*faster-whisper*"):
                shutil.rmtree(d, ignore_errors=True)
                print(f"  removed model cache: {d.name}")
    except Exception as e:
        print(f"  (could not clean model cache: {e})")


def _spawn_self_delete(root: Path) -> None:
    """Detached .cmd that waits for THIS process (+ the helios.cmd shim) to exit, then removes the
    install dir. Needed because we're running from inside the dir we want to delete."""
    target = str(root)
    script = (
        "@echo off\r\n"
        "set /a n=0\r\n"
        ":retry\r\n"
        "ping 127.0.0.1 -n 3 >nul\r\n"          # ~2s delay (timeout is unreliable when detached)
        'rmdir /s /q "' + target + '" 2>nul\r\n'
        'if not exist "' + target + '" goto done\r\n'
        "set /a n+=1\r\n"
        "if %n% lss 10 goto retry\r\n"
        ":done\r\n"
        'del "%~f0" 2>nul\r\n'
    )
    fd, path = tempfile.mkstemp(suffix=".cmd", prefix="helios-uninstall-")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    subprocess.Popen(["cmd", "/c", path], creationflags=0x00000008, close_fds=True)  # DETACHED_PROCESS


def cmd_uninstall(args) -> int:
    args = args or []
    # Safety: never delete a dev checkout â€” uninstall is only for installs created by install.ps1.
    if (REPO / ".git").is_dir():
        print("Refusing to uninstall: this looks like a development checkout (.git present), not an")
        print(f"installed copy. `helios uninstall` is for installs from install.ps1.\n    {REPO}")
        return 1
    skip_confirm = "--yes" in args or "-y" in args
    purge = "--purge" in args
    keep_data = "--keep-data" in args
    conf = _conf()

    print(f"This removes Helios from:\n    {REPO}")
    print("  - deletes the install folder, the 'Helios' scheduled task, and the 'helios' PATH entry")
    print("  - Python stays installed")
    if not skip_confirm:
        if input("Proceed with uninstall? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 1

    if purge:
        do_purge = True
    elif keep_data:
        do_purge = False
    else:
        try:
            vault = conf.vault_path()
        except Exception:
            vault = "(your Obsidian vault)"
        print("\nAlso delete your Obsidian vault (Helios's memory) and downloaded model caches?")
        print(f"  vault: {vault}")
        print("  Recommended only if you're NOT reinstalling Helios.")
        do_purge = input("Delete that data too? [y/N] ").strip().lower() in ("y", "yes")

    print("\nUninstalling...")
    try:
        cmd_stop(None)
    except Exception:
        pass
    _kill_under_dir(REPO)
    try:
        subprocess.run(["schtasks", "/delete", "/tn", "Helios", "/f"],
                       creationflags=_NO_WINDOW, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    print("  removed scheduled task (if present)")
    _remove_from_user_path(REPO / "bin")
    print("  removed the 'helios' PATH entry")
    if do_purge:
        _purge_user_data(conf)
    else:
        print("  kept your Obsidian vault + model caches")
    _spawn_self_delete(REPO)
    print(f"\nDone - {REPO} will be removed in a few seconds.")
    print("Open a NEW terminal afterward (the 'helios' command is gone). Python was left installed.")
    return 0


# ---- update -----------------------------------------------------------------
def _update_git() -> int:
    """Update a source (git) checkout: fast-forward pull, preserving the user's settings.toml
    (it's tracked but locally edited via the settings UI). Refuses SAFELY if the working tree has
    other uncommitted changes or has diverged from the remote â€” the user commits/stashes and retries."""
    git = shutil.which("git")
    if not git:
        print("! git not found on PATH â€” can't update a source checkout. Install Git, or reinstall.")
        return 1
    settings = REPO / "config" / "settings.toml"
    backup = None
    if settings.exists():
        fd, backup = tempfile.mkstemp(suffix=".toml", prefix="helios-settings-")
        os.close(fd)
        shutil.copyfile(settings, backup)
        # Revert local settings edits so the fast-forward pull isn't blocked by them.
        subprocess.run([git, "-C", str(REPO), "checkout", "--", "config/settings.toml"])
    print("Fetching latestâ€¦")
    rc = subprocess.run([git, "-C", str(REPO), "pull", "--ff-only"]).returncode
    if rc != 0:
        if backup:
            shutil.copyfile(backup, settings)   # restore the user's settings verbatim
            os.remove(backup)
        print("\n! Update stopped: `git pull --ff-only` failed (uncommitted changes or a diverged")
        print("  branch). Commit or stash your changes, then run `helios update` again.")
        return rc
    if backup:
        # Merge the user's saved values back onto the (possibly updated) committed default.
        merged = subprocess.run([str(VENV_PY), str(REPO / "tools" / "merge_settings.py"),
                                 "--base", str(settings), "--user", backup,
                                 "--out", str(settings)]).returncode
        if merged != 0:
            shutil.copyfile(backup, settings)   # merge failed -> keep the user's settings verbatim
        os.remove(backup)
    if VENV_PY.exists():
        print("Updating dependenciesâ€¦")
        subprocess.run([str(VENV_PY), "-m", "pip", "install", "-r", str(REPO / "requirements.txt")])
    return 0


def _update_installer() -> int:
    """Update a zip install by re-running the installer (it downloads the latest code, preserves
    .venv + data, and merges settings). Needs the repo reachable (public, or via a configured token)."""
    branch = os.environ.get("HELIOS_BRANCH", "cua-driver")
    repo_dir = str(REPO).replace("'", "''")
    ps = ("$env:HELIOS_DIR='" + repo_dir + "'; $env:HELIOS_BRANCH='" + branch + "';"
          "irm https://raw.githubusercontent.com/NotTimPunt/jarvis/" + branch + "/install.ps1 | iex")
    print(f"Re-running the installer (branch '{branch}')â€¦")
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", ps]).returncode


def cmd_update(_args) -> int:
    """Update Helios to the latest code, KEEPING your .venv, data, and settings, then restart.

    Auto-detects the install type: a git checkout is updated with `git pull` (works on a private
    repo via your git auth); a zip install re-runs install.ps1. Helios is stopped first so no
    files/DLLs are locked during the update."""
    was_running = _is_running()
    print("Stopping Heliosâ€¦")
    try:
        cmd_stop(None)
    except Exception:
        pass
    _kill_under_dir(REPO)          # kill any stray background pythonw holding a lock
    time.sleep(1.0)

    is_git = (REPO / ".git").is_dir()
    rc = _update_git() if is_git else _update_installer()
    if rc != 0:
        print("Update failed â€” Helios was NOT restarted.")
        return rc

    print("Update complete.")
    if was_running:
        return cmd_start(None)
    print("Run `helios start` to launch the updated Helios.")
    return 0


def cmd_projects(args) -> int:
    """Project manifests: `helios projects [list [--all] | add <folder> [--name N] [--force] |
    changes [name] [--hours N] | check [name] [--only CHECK] | health [name] [--run] |
    show <name> | discover [--days N] | pick <n...>]`."""
    conf = _conf()
    from helios import projects
    args = list(args or [])
    action = args.pop(0).lower() if args else "list"

    def opt(flag, default=None):
        if flag in args:
            i = args.index(flag)
            val = args[i + 1] if i + 1 < len(args) else default
            del args[i:i + 2]
            return val
        return default

    try:
        if action == "list":
            print(projects.format_list(include_inactive="--all" in args))
        elif action == "add" and args:
            name, force = opt("--name"), "--force" in args
            args = [a for a in args if a != "--force"]
            f = projects.add(" ".join(args), name, overwrite=force)
            print(f"Wrote {f}\n\n{f.read_text(encoding='utf-8')}\nReview and edit it; "
                  "`helios projects check <name>` runs its health checks.")
        elif action == "discover":
            days = int(opt("--days", 60))
            print(f"Looking for folders you've worked in over the last {days} days "
                  "(read-only; this can take a minute)...\n")
            found = projects.discover_recent(days)
            (conf.DATA_DIR / "projects_discovered.json").write_text(
                json.dumps(found, indent=1), encoding="utf-8")
            print(projects.format_discovered(found))
            print("\nAdd the ones you want:  helios projects pick 1 3 5")
        elif action == "pick" and args:
            saved = conf.DATA_DIR / "projects_discovered.json"
            if not saved.exists():
                print("Run `helios projects discover` first.")
                return 1
            found = json.loads(saved.read_text(encoding="utf-8"))
            for num in args:
                if not num.isdigit() or not 1 <= int(num) <= len(found):
                    print(f"  {num}: not in the list")
                    continue
                e = found[int(num) - 1]
                try:
                    f = projects.add(e["path"])
                    print(f"  {num}: added {e['path']} -> {f.name}")
                except (ValueError, FileExistsError) as err:
                    print(f"  {num}: skipped {e['path']} ({err})")
        elif action == "changes":
            hours = float(opt("--hours", 24))
            print(projects.format_changes(projects.all_changes(hours, " ".join(args) or None)))
        elif action == "check":
            only = opt("--only")
            print(projects.format_checks(projects.run_checks(" ".join(args) or None, only)))
        elif action == "health":
            from helios import health
            run = "--run" in args
            name = " ".join(a for a in args if a != "--run") or None
            if run:
                print("Running checks, dependency checks and probes (this can take a few minutes)...")
            print("PROJECT HEALTH\n")
            print(health.format_report(health.run_all(name) if run else health.reports(name)))
        elif action == "show" and args:
            p = projects.get(" ".join(args))
            if not p:
                print("No such project.")
                return 1
            print(json.dumps({k: v for k, v in p.items()} | {"state": projects.state(p)},
                             indent=1, default=str)[:6000])
        else:
            print(cmd_projects.__doc__.split(":", 1)[1].strip())
            return 2
    except (KeyError, ValueError, FileExistsError, projects.Busy) as e:
        print(f"Error: {e}")
        return 1
    print(f"\n(manifests: {conf.projects_dir()})")
    return 0


def cmd_night(args) -> int:
    """Night Mode: `helios night [status | run [--only task1,task2] | report [night] | stop |
    on | off]`. `run` runs every scheduled task right now (a manual run)."""
    conf = _conf()
    from helios import night_mode
    args = list(args or [])
    action = args.pop(0).lower() if args else "status"
    if action == "status":
        print(night_mode.status_text())
    elif action == "run":
        only = None
        if "--only" in args:
            i = args.index("--only")
            only = [t.strip() for t in (args[i + 1] if i + 1 < len(args) else "").split(",") if t.strip()]
        print("Running Night Mode now (this can take several minutes)...")
        rec = night_mode.run_night(only=only)
        if rec.get("status") in ("busy", "duplicate"):
            print(f"Not started: {rec['reason']}.")
            return 1
        print(night_mode.latest_report(rec["night"]))
        print(f"(report: {rec.get('report')})")
    elif action == "report":
        print(night_mode.latest_report(args[0] if args else None))
    elif action == "stop":
        from helios.night_mode import scheduler as night
        night.request_stop()
        print("Stop requested — a running Night Mode run stops before its next task.")
    elif action in ("on", "off"):
        conf.update_settings({"night_mode.enabled": action == "on"})
        print(f"Night Mode is now {action.upper()} (restart Helios if it's running).")
    else:
        print(cmd_night.__doc__.split(":", 1)[1].strip())
        return 2
    return 0


def cmd_mcp(args) -> int:
    """Helios's public MCP server: `helios mcp [config | tools | log | antigravity]`. `config`
    prints the JSON for an MCP client; `antigravity` prints the `agy mcp add` command."""
    conf = _conf()
    args = list(args or [])
    action = args[0].lower() if args else "config"
    py = VENV_PYW                   # windowless, like the internal server (no console flash)
    server = REPO / "mcp" / "helios_public_server.py"
    if action == "config":
        entry = {"mcpServers": {"helios": {"command": py.as_posix(), "args": [server.as_posix()]}}}
        print(json.dumps(entry, indent=2))
        print("\nPaste this into your MCP client's config (Antigravity: its mcp_config.json).\n"
              "Only the curated public tools are exposed; every call goes through Helios's "
              "permission gate. See docs/HELIOS_MCP.md.")
    elif action == "tools":
        import importlib.util
        spec = importlib.util.spec_from_file_location("helios_public_server", server)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        c = conf._section("mcp_public")
        print(f"Public MCP server: {'ON' if c.get('enabled', True) else 'OFF'}"
              f"{' (read-only)' if c.get('read_only') else ''}")
        for name, (internal, kind) in mod.TOOLS.items():
            off = " — DISABLED" if name in (c.get("disabled_tools") or []) or \
                (c.get("read_only") and kind != "read") else ""
            print(f"  {name:20} {kind:6} (policy: {internal}){off}")
    elif action == "antigravity":
        # The supported direction only: Antigravity -> MCP -> Helios public server. Registered with
        # Antigravity's own `agy mcp add` (its global MCP config, shared by the IDE and the CLI).
        # Named helios-public, never "helios": that name is the internal server inside Helios's
        # own brain sessions and must not be shadowed.
        agy = str(conf.SETTINGS.get("antigravity", {}).get("bin") or "agy")
        print("Register Helios's public MCP server with Antigravity (IDE + agy CLI):\n")
        print(f'  "{agy}" mcp add helios-public "{py.as_posix()}" "{server.as_posix()}"\n')
        print("Check:   agy mcp list        Remove:  agy mcp remove helios-public")
        print("Or, for one workspace only, put the `helios mcp config` JSON (renamed to "
              "helios-public) in <workspace>\\.agents\\mcp_config.json.")
        print("Full guide: docs/ANTIGRAVITY_MCP.md")
    elif action == "log":
        f = conf.LOGS_DIR / "mcp_public.log"
        print("\n".join(f.read_text(encoding="utf-8").splitlines()[-30:]) if f.exists()
              else "No public MCP calls yet.")
    else:
        print(cmd_mcp.__doc__.split(":", 1)[1].strip())
        return 2
    return 0


def cmd_leads(args) -> int:
    """Lead finder: `helios leads [list [status] | show <id> | contacted <id> | won <id> |
    lost <id> | dismiss <id> | run [service ...] | rates]`. status: new (default), contacted,
    won, lost, dismissed, all. Helios never contacts leads — it drafts the pitch, you send it."""
    _conf()
    from helios import leads
    args = list(args or [])
    action = args.pop(0).lower() if args else "list"
    if action == "list":
        st = args[0].lower() if args else "new"
        print(leads.format_leads(leads.all_leads(None if st == "all" else st)))
    elif action == "show" and args:
        d = leads.get(args[0])
        print(leads.format_leads([d], verbose=True) if d else "No lead with that id.")
    elif action in ("contacted", "won", "lost", "dismiss") and args:
        d = leads.set_status(args[0], "dismissed" if action == "dismiss" else action)
        print(f"{d['title']} -> {d['status']}" if d else "No lead with that id.")
    elif action == "run":
        names = [a for a in args if a in leads.SERVICES] or None
        print("Searching (a few minutes per service)...")
        for r in leads.run(names):
            print(f"{r['service']}: " + (f"FAILED — {r['error']}" if r["error"] else
                  f"{len(r['new'])} new, {len(r['duplicates'])} known, {len(r['dropped'])} dropped ({r['seconds']}s)"))
            for d in r["new"]:
                print("  + " + leads.format_leads([d]))
    elif action == "rates":
        for s in leads.services():
            r = leads.rates(s)
            print(f"{s:18} " + "  ".join(f"{t}: {leads.currency()} {lo:,}-{hi:,}" for t, (lo, hi) in r.items()))
    else:
        print(cmd_leads.__doc__.split(":", 1)[1].strip())
        return 2
    return 0


def cmd_usage(args) -> int:
    """AI token usage: `helios usage [days]` (default 7)."""
    _conf()
    from helios import usage
    days = int(args[0]) if args and str(args[0]).isdigit() else 7
    print(usage.format_summary(days))
    return 0


def cmd_polarion(args) -> int:
    """Polarion, read-only: `helios polarion [status | token <local|server> | projects [local|server]
    | search <project> [query...] [--server] | item <project> <id> [--server]]`. The token is
    typed at a hidden prompt here — never pasted into chat."""
    _conf()
    from helios import polarion
    args = list(args or [])
    inst = "server" if "--server" in args else "local"
    args = [a for a in args if a not in ("--server", "--local")]
    action = args.pop(0).lower() if args else "status"
    try:
        if action == "status":
            print(polarion.format_status(polarion.status()))
        elif action == "token" and args and args[0] in polarion.INSTANCES:
            import getpass
            print(f"Create a personal access token in Polarion ({args[0]}): your avatar > "
                  "My Account > Personal Access Token. Paste it below (nothing will show).")
            polarion.save_token(args[0], getpass.getpass("token: "))
            print(f"Saved to config/secrets.toml [polarion] {args[0]}_token.")
            print(polarion.format_status([r for r in polarion.status() if r["instance"] == args[0]]))
        elif action == "projects":
            print(polarion.format_projects(polarion.projects(args[0] if args else inst)))
        elif action == "search" and args:
            print(polarion.format_items(polarion.search(inst, args[0], " ".join(args[1:]))))
        elif action == "item" and len(args) >= 2:
            print(polarion.format_item(polarion.item(inst, args[0], args[1])))
        else:
            print(cmd_polarion.__doc__.split(":", 1)[1].strip())
            return 2
    except (polarion.PolarionError, ValueError) as e:
        print(e)
        return 1
    return 0


def cmd_gmail(args) -> int:
    """Gmail: `helios gmail [status | setup <client.json> | login | logout | business [list | add
    <email|@domain> | remove <x>] | inbox [query...] | check | drafts | show <n> | send <n> |
    discard <n>]`. Helios drafts replies to business contacts only; nothing is sent without you."""
    _conf()
    from helios import gmail, jobs
    args = list(args or [])
    action = args.pop(0).lower() if args else "status"
    try:
        if action == "status":
            print(gmail.status_text())
        elif action == "setup" and args:
            gmail.setup_client(" ".join(args).strip('"'))
            print("OAuth client saved to config/secrets.toml [gmail]. You can delete the downloaded "
                  "file now.\nNext: helios gmail login")
        elif action == "login":
            print("Opening Google sign-in in your browser — sign in and allow access "
                  "(read, draft and send; never permanent delete)...")
            me = gmail.login(open_browser=None)
            print(f"Connected as {me}.")
            if not jobs.get("gmail_replies"):
                every = str(gmail.cfg().get("check_every") or "every 30m")
                jobs.add("gmail_replies", every, name="gmail_replies", source="gmail login")
                print(f"Scheduled: check for client emails {every} (helios jobs to change).")
            print("Add your clients:  helios gmail business add client@acme.com   (or @acme.com)")
        elif action == "logout":
            gmail.logout()
            print("Signed out and the Google token revoked.")
        elif action == "business":
            sub = args.pop(0).lower() if args else "list"
            if sub == "add" and args:
                print(f"Business contact added: {gmail.add_business(args[0])}")
            elif sub == "remove" and args:
                print("Removed." if gmail.remove_business(args[0]) else
                      "Not in your list (entries from settings.toml / leads are edited there).")
            else:
                items = gmail.business_entries()
                print("\n".join(f"- {k}  ({v})" for k, v in sorted(items.items()))
                      or "No business contacts yet — helios gmail business add client@acme.com")
        elif action == "inbox":
            print(gmail.format_messages(gmail.search(" ".join(args) or "in:inbox", 15)))
        elif action == "check":
            print("Checking unread mail from business contacts...")
            out = gmail.check()
            print(f"{len(out['drafted'])} reply draft(s), {out['skipped']} skipped.")
            for e in out["errors"]:
                print(f"  error: {e}")
            if out["drafted"]:
                print("\n" + gmail.format_pending(out["drafted"]))
        elif action == "drafts":
            print(gmail.format_pending(gmail.pending()))
        elif action == "show" and args:
            print(gmail.format_pending([gmail._pick(args[0])], verbose=True))
        elif action == "send" and args:
            d = gmail._pick(args[0])
            print(gmail.format_pending([d], verbose=True))
            print("(If you edited it in Gmail, your edited version is what gets sent.)")
            if input(f"Send this reply to {d['to']}? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Not sent.")
                return 0
            r = gmail.send_draft(args[0])
            print(f"Sent to {', '.join(r['recipients'])}.")
        elif action == "discard" and args:
            d = gmail.discard_draft(args[0])
            print(f"Discarded the draft to {d['to']}.")
        else:
            print(cmd_gmail.__doc__.split(":", 1)[1].strip())
            return 2
    except (gmail.GmailError, jobs.JobError, ValueError) as e:
        print(e)
        return 1
    return 0


def cmd_security(args) -> int:
    """Security self-check: `helios security` — is every protection in place? Read-only."""
    _conf()
    from helios import security
    checks = security.self_check()
    print("HELIOS SECURITY CHECK\n")
    print(security.format_checks(checks))
    print("\nDetails: docs/SECURITY.md")
    return 1 if any(s == "fail" for _, s, _ in checks) else 0


def cmd_jobs(args) -> int:
    """Scheduled jobs: `helios jobs [list | add <type> "<schedule>" [--project P] [--topics a,b]
    [--days N] [--name N] | enable <job> | disable <job> | remove <job> | run <job> |
    history [job]]`. Types: project_health, research, learn, morning_briefing, night_mode.
    Schedules: 'daily 07:30', 'weekdays 09:00', 'weekly mon 09:00', 'hourly', 'every 30m',
    'every 2h', 'once 2026-10-02T09:00'."""
    _conf()
    from helios import jobs
    args = list(args or [])
    action = args.pop(0).lower() if args else "list"

    def opt(flag):
        if flag in args:
            i = args.index(flag)
            v = args[i + 1] if i + 1 < len(args) else ""
            del args[i:i + 2]
            return v
        return None

    try:
        if action == "list":
            print(jobs.format_jobs())
        elif action == "add" and len(args) >= 2:
            name, project, topics, days = opt("--name"), opt("--project"), opt("--topics"), opt("--days")
            a = {"project": project, "days": int(days) if days else None,
                 "topics": [t.strip() for t in topics.split(",") if t.strip()] if topics else None}
            j = jobs.add(args[0], " ".join(args[1:]), name=name, args=a)
            print(f"Added #{j['id']} {j['name']} — next run {jobs._when(j['next_run'])}")
        elif action in ("enable", "disable") and args:
            j = jobs.set_enabled(" ".join(args), action == "enable")
            print(f"{j['name']}: {'on' if j['enabled'] else 'off'} · next: {jobs._when(j['next_run'])}")
        elif action == "remove" and args:
            print(f"Removed {jobs.remove(' '.join(args))['name']}.")
        elif action == "run" and args:
            print("Running now...")
            r = jobs.run_now(" ".join(args))
            print(f"{r['job']}: {r['status']} — {r['summary']}")
        elif action == "history":
            print(jobs.format_history(jobs.history(" ".join(args) or None)))
        else:
            print(cmd_jobs.__doc__.split(":", 1)[1].strip())
            return 2
    except jobs.JobError as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_briefing(args) -> int:
    """Morning briefing: `helios briefing [--spoken] [--fresh] [YYYY-MM-DD]`. --spoken prints the
    short read-aloud version; --fresh rebuilds today's from the latest results."""
    _conf()
    from helios import briefing
    args = list(args or [])
    day = next((a for a in args if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a)), None)
    if "--fresh" in args:
        out = briefing.prepare()
        print(out["spoken"] if "--spoken" in args else out["text"])
        print(f"(saved: {out['path']})")
        return 0
    print(briefing.latest_text(day, spoken_version="--spoken" in args))
    return 0


def cmd_research(args) -> int:
    """Research: `helios research [topics | run [topic ...] | findings [topic] [--days N] |
    show <id> | keep <id>]`. `keep` copies one finding into memory (findings never go there
    on their own)."""
    _conf()
    from helios import research
    args = list(args or [])
    action = args.pop(0).lower() if args else "findings"
    days = None
    if "--days" in args:
        i = args.index("--days")
        days = float(args[i + 1]) if i + 1 < len(args) else None
        del args[i:i + 2]
    if action == "topics":
        last = research._state().get("last", {})
        for t in research.topics():
            proj = f" (project {t['project']})" if t["project"] else ""
            print(f"- {t['topic']}{proj}: last {last.get(t['topic'].lower(), 'never')[:16]}")
        print(f"\nnext run: {', '.join(t['topic'] for t in research.pick())}")
    elif action == "run":
        names = [" ".join(args)] if args else None
        print("Researching (about a minute or two per topic)...")
        print(research.format_run(research.run(names)))
        print(f"\n(library: {research.library()})")
    elif action == "findings":
        print(research.format_findings(research.findings(" ".join(args) or None, days=days),
                                       verbose=True))
    elif action == "show" and args:
        f = research.get(args[0])
        print(research.format_findings([f], verbose=True) if f else "No such finding.")
    elif action == "keep" and args:
        res = research.keep(args[0])
        print(f"{res['status']}: {res.get('reason') or res['item']['text'][:120]}")
    else:
        print(cmd_research.__doc__.split(":", 1)[1].strip())
        return 2
    return 0


_COMMANDS = {
    "onboard": cmd_onboard, "setup": cmd_onboard,
    "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
    "update": cmd_update,
    "status": cmd_status, "where": cmd_where, "uninstall": cmd_uninstall,
    "gemini-login": cmd_gemini_login,
    "voice-check": cmd_voice_check,
    "memory": cmd_memory,
    "learn": cmd_learn,
    "projects": cmd_projects,
    "night": cmd_night,
    "research": cmd_research,
    "briefing": cmd_briefing,
    "mcp": cmd_mcp,
    "jobs": cmd_jobs,
    "security": cmd_security,
    "leads": cmd_leads,
    "usage": cmd_usage,
    "polarion": cmd_polarion,
    "gmail": cmd_gmail,
}

_USAGE = ("Helios â€” usage: helios <command>\n"
          "  onboard   run the setup wizard (connect a brain, voice, apps)\n"
          "  start     launch Helios in the background\n"
          "  stop      ask Helios to quit\n"
          "  restart   stop, then start\n"
          "  update    update to the latest code, keeping venv/data/settings, then restart\n"
          "  status    is Helios running?\n"
          "  where     print the install folder\n"
          "  gemini-login  sign Helios's Gemini brain into your Google account\n"
          "  voice-check   check mic/speaker, TTS, speech-to-text and wake word (--speak to hear it)\n"
          "  memory        list / search / forget what Helios remembers\n"
          "  learn         learn lessons from today's conversations; review / approve / reject\n"
          "  projects      list / add / changes / check / health of your configured projects\n"
          "  night         Night Mode: status / run now / last report / on / off\n"
          "  research      research topics / run now / findings / keep one in memory\n"
          "  briefing      today's morning briefing (--spoken for the read-aloud version)\n"
          "  mcp           public MCP server for other apps: config snippet / tools / call log\n"
          "  jobs          scheduled jobs: list / add / enable / disable / remove / run / history\n"
          "  security      security self-check: is every protection in place?\n"
          "  leads         worldwide paid-work leads with price ranges: list / show / won / run\n"
          "  usage         AI tokens used per day and what for\n"
          "  polarion      read-only Polarion (local + server): status / token / projects / search / item\n"
          "  gmail         Gmail: setup / login / business contacts / inbox / reply drafts / send\n"
          "  uninstall remove Helios (folder, task, PATH); --purge also deletes vault + caches")


def main() -> int:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    fn = _COMMANDS.get(cmd)
    if not fn:
        print(_USAGE)
        return 0 if cmd in ("", "help", "-h", "--help") else 2
    return fn(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
