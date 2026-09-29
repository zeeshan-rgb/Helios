"""Open an application or website by name — ported from Helios-main actions/open_app.py
(2026-07-08 consolidation), Windows-only, with the website fix the user asked for.

Launch order:
  1. WEBSITE branch (the Helios-main bug fix): a curated site map + a looks-like-a-domain
     heuristic open the DEFAULT BROWSER via webbrowser.open. Before this branch existed,
     "open YouTube" fell through to the Start-menu search and typed 'youtube' into the
     search bar instead of opening the browser.
  2. PATH executables (chrome, code, steam...) -> subprocess.Popen.
  3. Protocol/URI handlers (ms-settings:, steam://...) -> start <uri>.
  4. Start-menu search: win key -> type name -> enter (the visible flow the user likes) —
     what launches Store/UWP apps (WhatsApp, Spotify, Discord...). Bails out if the
     panic flag is set: never type synthetic keystrokes after a Stop.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import webbrowser

from . import conf

_APP_ALIASES: dict[str, str] = {
    "chrome": "chrome", "google chrome": "chrome",
    "firefox": "firefox",
    "edge": "msedge",
    "brave": "brave",
    "opera": "opera",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "zoom": "Zoom",
    "teams": "msteams",
    "skype": "skype",
    "signal": "signal",
    "spotify": "Spotify",
    "vlc": "vlc",
    "netflix": "Netflix",
    "vscode": "code", "visual studio code": "code", "code": "code",
    "terminal": "wt",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "postman": "Postman",
    "git": "git-bash",
    "figma": "Figma",
    "blender": "blender",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "libreoffice": "soffice",
    "notepad": "notepad.exe", "textedit": "notepad.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "finder": "explorer.exe",
    "task manager": "taskmgr.exe",
    "settings": "ms-settings:",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "notion": "Notion",
    "obsidian": "Obsidian",
    "capcut": "CapCut",
    "steam": "steam",
    "epic": "EpicGamesLauncher", "epic games": "EpicGamesLauncher",
}

# Names that mean "a website" — opened in the default browser, NOT typed into Start.
# Desktop-app names deliberately absent (spotify/discord/whatsapp = the installed apps).
_SITES: dict[str, str] = {
    "youtube": "https://youtube.com",
    "youtube music": "https://music.youtube.com",
    "gmail": "https://mail.google.com",
    "google drive": "https://drive.google.com",
    "google docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com",
    "google maps": "https://maps.google.com",
    "maps": "https://maps.google.com",
    "github": "https://github.com",
    "reddit": "https://reddit.com",
    "twitter": "https://x.com", "x": "https://x.com",
    "twitch": "https://twitch.tv",
    "wikipedia": "https://wikipedia.org",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "amazon": "https://amazon.com",
    "ebay": "https://ebay.com",
    "linkedin": "https://linkedin.com",
    "facebook": "https://facebook.com",
    "whatsapp web": "https://web.whatsapp.com",
}

_TLDS = (".com", ".org", ".net", ".io", ".dev", ".ai", ".tv", ".gg", ".app", ".co",
         ".me", ".sh", ".xyz", ".edu", ".gov", ".info", ".nl", ".de", ".uk", ".fr")


def _website_url(raw: str) -> str | None:
    """URL when `raw` names a website; None when it's an app (or ambiguous -> app wins)."""
    key = raw.lower().strip().rstrip("/")
    if key.startswith(("http://", "https://")):
        return raw.strip()
    if key in _SITES:
        return _SITES[key]
    if key.startswith("www."):
        return "https://" + key
    # looks like a bare domain: single token with a real TLD tail (discord.com, wttr.in
    # stays out — keep the list conservative; unknown TLDs fall through to the app path)
    if " " not in key and "." in key and key.endswith(_TLDS):
        return "https://" + key
    return None


def _normalize(raw: str) -> str:
    key = raw.lower().strip()
    if key in _APP_ALIASES:
        return _APP_ALIASES[key]
    for alias_key, target in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return target
    return raw


def _launch_windows(app_name: str) -> bool:
    # tier 1: PATH executables
    if shutil.which(app_name) or shutil.which(app_name.split(".")[0]):
        try:
            subprocess.Popen(app_name, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)
            return True
        except Exception as e:
            print(f"[open_app] subprocess failed: {e}")

    # tier 2: protocol/URI handlers (ms-settings:, steam://, spotify:)
    if ":" in app_name:
        try:
            subprocess.Popen(f"start {app_name}", shell=True)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    # tier 3: Start-menu search — win, type, enter (the visible flow the user likes).
    # Panic check: NEVER emit synthetic keystrokes after a Stop.
    try:
        if conf.ABORT_FLAG.exists():
            print("[open_app] panic engaged - skipping Start-menu typing")
            return False
    except Exception:
        pass
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        time.sleep(2.5)
        return True
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")

    return False


def open_app(name: str) -> str:
    app_name = (name or "").strip()
    if not app_name:
        return "No application name provided."

    # Websites first — the fix for "open YouTube typed into the search bar".
    url = _website_url(app_name)
    if url:
        try:
            webbrowser.open(url)
            return f"Opened {url} in your browser."
        except Exception as e:
            return f"Could not open the browser for {url}: {e}"

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' -> '{normalized}'")
    try:
        if _launch_windows(normalized):
            return f"Opened {app_name}."
        if normalized.lower() != app_name.lower() and _launch_windows(app_name):
            return f"Opened {app_name}."
        return (f"Could not confirm that {app_name} launched. "
                f"It may still be loading, or it might not be installed.")
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"
