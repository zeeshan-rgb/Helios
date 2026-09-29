"""Shared configuration, paths, and constants for Helios.

Central, import-time config hub used by every other module (brain.py, db.py, the
app, the served UI, and the PreToolUse hook). Resolves all on-disk paths relative
to the repo ROOT, loads config/settings.toml + config/secrets.toml, exposes the
server HOST/PORT/BASE_URL, the VERSION string, and the per-install CSRF auth token.
Designed to never raise at import time — it runs under pythonw with no console, so
a bad config falls back to safe defaults rather than crashing the whole app.
"""

from __future__ import annotations

import os
import secrets as _secrets
import shutil
import threading
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the repo root
CONFIG_DIR = ROOT / "config"
LOGS_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
HOOKS_DIR = ROOT / "hooks"
ABORT_FLAG = LOGS_DIR / "abort.flag"
SCREEN_LOCK = LOGS_DIR / "screen.lock"   # cross-process: which agent currently drives input
YOLO_FLAG = LOGS_DIR / "yolo.flag"       # cross-process: YOLO mode on -> hook auto-approves the
                                         # normal Approve/Deny prompts for the current chat (the
                                         # SSRF / ~/.claude / panic hard-rails still apply). Default
                                         # OFF: cleared at app boot + every new-chat/panic boundary.
# Per-install bearer token shared between the app, the served UI, and the PreToolUse
# hook. Defeats CSRF from web pages (which can't read this file or our headers).
AUTH_TOKEN_FILE = DATA_DIR / ".session_token"

# The Claude Code CLI (the brain). brain.py shells out to this as `claude -p`, which
# runs headless on the user's subscription (no API key, free). Must NOT be swapped for
# direct api.anthropic.com calls — third-party use of Claude-Code creds gets gated to paid.
# Resolved by _detect_claude_bin() AFTER settings load (so an explicit config wins); the
# fallback is the per-user default location, not a hardcoded "the user" path.
CLAUDE_BIN: str = ""   # assigned below, once SETTINGS is available

LOGS_DIR.mkdir(parents=True, exist_ok=True)  # ensure logs/ exists before log() is ever called

# Version is read from a plain-text VERSION file at the repo root (single source of truth,
# shared with the UI); fall back to 0.0.0 if it's missing or unreadable.
try:
    VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
except Exception:
    VERSION = "0.0.0"


def load() -> dict:
    """Load and merge config into a single dict: settings.toml overlaid with secrets.toml.

    secrets.toml (gitignored) holds tokens/keys and is merged on top of settings.toml;
    dict-valued sections present in both are merged key-by-key rather than replaced
    wholesale. Returns {} (not None) on any failure so callers can safely .get() into it.
    """
    try:
        with (CONFIG_DIR / "settings.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        # A malformed/missing settings.toml must not crash the whole app at import
        # (it runs under pythonw with no console). Fall back to defaults below.
        data = {}
    secrets = CONFIG_DIR / "secrets.toml"   # gitignored — tokens etc.
    if secrets.exists():
        try:
            with secrets.open("rb") as fh:
                for k, v in tomllib.load(fh).items():
                    if isinstance(v, dict) and isinstance(data.get(k), dict):
                        data[k].update(v)
                    else:
                        data[k] = v
        except Exception:
            pass
    return data


SETTINGS = load()


def _section(name: str) -> dict:
    """Return the named [section] table from SETTINGS, or {} if missing/not a table."""
    v = SETTINGS.get(name)
    return v if isinstance(v, dict) else {}


def _detect_claude_bin() -> str:
    """Resolve the Claude Code CLI path without hardcoding a user name.

    Order: explicit config ([claude].bin or [brain].claude_bin) → the per-user default
    install location (~/.local/bin/claude.exe, where the user's lives) → PATH lookup. Falls back
    to the per-user default string even if absent so callers always have a path to try.
    Never raises (runs at import under pythonw with no console)."""
    try:
        cfg = _section("claude").get("bin") or _section("brain").get("claude_bin")
        if cfg:
            return str(cfg)
        default = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
        if default.exists():
            return str(default)
        found = shutil.which("claude.exe") or shutil.which("claude")
        if found:
            return found
        return str(default)
    except Exception:
        return str(Path.home() / ".local" / "bin" / "claude.exe")


CLAUDE_BIN = _detect_claude_bin()


def cua_driver_bin() -> Path:
    """Resolve the cua-driver.exe path (computer-use engine) without hardcoding a user name.
    Explicit config ([computer].driver_bin) wins; else the per-user default install location."""
    cfg = _section("computer").get("driver_bin")
    if cfg:
        return Path(cfg)
    return (Path.home() / "AppData" / "Local" / "Programs" / "Cua"
            / "cua-driver" / "bin" / "cua-driver.exe")


def brain_cfg() -> dict:
    """The [brain] section (engine/provider/model/base_url/... for the tiered brain) as a dict."""
    return dict(SETTINGS.get("brain", {}))


def brain_engine() -> str:
    """Which brain engine to run: 'claude' (the full claude -p brain), 'antigravity' (the official
    agy CLI, signed in with a Google account), 'gemini' (the Gemini CLI) or 'lite' (native
    OpenAI-compatible loop). Defaults to 'claude' so an unconfigured install behaves as before."""
    return str(_section("brain").get("engine", "claude")).strip().lower() or "claude"


def provider_cfg(name: str) -> dict:
    """Return a provider's merged config+secrets table (e.g. provider_cfg('openai') ->
    {'api_key': ..., 'base_url': ...}). secrets.toml [name] is merged over settings by load()."""
    return _section(name)


def anthropic_api_key() -> str | None:
    """The Anthropic API key for the claude -p brain, if the user chose the API-key auth path
    (vs subscription login). From [anthropic].api_key (secrets) or the ANTHROPIC_API_KEY env.
    Returns None when unset — in which case claude -p uses the logged-in subscription (free)."""
    key = _section("anthropic").get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    return str(key).strip() if key else None


def claude_env() -> dict:
    """Environment dict for spawning the claude CLI. Inherits the current env and injects
    ANTHROPIC_API_KEY only when configured (so the subscription-login path is untouched)."""
    env = dict(os.environ)
    key = anthropic_api_key()
    if key:
        env["ANTHROPIC_API_KEY"] = key
    return env


# Local web server binding. Defaults: loopback-only host, port 8769. BASE_URL is the
# address the served UI and PreToolUse hook use to reach the app. PORT falls back to the
# default if settings.toml has a non-integer value.
HOST: str = str(_section("server").get("host", "127.0.0.1"))
try:
    PORT: int = int(_section("server").get("port", 8769))
except (TypeError, ValueError):
    PORT = 8769
BASE_URL = f"http://{HOST}:{PORT}"


def vault_path() -> Path:
    """Path to the Obsidian vault Helios uses for long-term memory (memory.py writes here,
    brain.py grants the CLI --add-dir access to it). Defaults to ~/Documents/Obsidian Vaults/LocalAI."""
    p = _section("paths").get("vault")
    return Path(p) if p else (Path.home() / "Documents" / "Obsidian Vaults" / "LocalAI")


def workspace_path() -> Path:
    """Working directory the claude -p process runs in (cwd for each brain turn).
    Defaults to the user's home directory."""
    return Path(_section("paths").get("workspace") or str(Path.home()))


def autonomy_allow() -> list[str]:
    """Tool/action patterns the user has pre-approved (no confirmation prompt)."""
    return list(SETTINGS.get("autonomy", {}).get("allow", []))


def autonomy_always_ask() -> list[str]:
    """Tool/action patterns that must always prompt the user, even if otherwise allowed."""
    return list(SETTINGS.get("autonomy", {}).get("always_ask", []))


def autonomy_mode() -> str:
    """Permission posture chosen during onboarding: '' (normal policy) or 'blank' (a blank-slate
    install that confirms EVERYTHING not explicitly in the allow-list). Default normal."""
    return str(SETTINGS.get("autonomy", {}).get("mode", "")).strip().lower()


def router_cfg() -> dict:
    """The [router] section (model-tier names like light/medium/heavy) as a plain dict."""
    return dict(SETTINGS.get("router", {}))


def proactive_cfg() -> dict:
    """The [proactive] section (settings for unprompted/scheduled behaviour) as a plain dict."""
    return dict(SETTINGS.get("proactive", {}))


def voice_cfg() -> dict:
    """The [voice] section (wake word / STT / TTS / follow-up settings) as a plain dict."""
    return dict(SETTINGS.get("voice", {}))


def startup_cfg() -> dict:
    """The [startup] section (boot hidden/dormant + wake-gesture behaviour) as a plain dict."""
    return dict(SETTINGS.get("startup", {}))


# On-disk locations for the voice stack. Kokoro model assets live in data/voices/ (gitignored,
# 325MB+28MB — copied in once, never committed). The daemon records its PID here like the orb so
# the app can kill a genuine orphan from a previous run (creation-time verified) before relaunching.
VOICE_DIR = DATA_DIR / "voices"
KOKORO_MODEL = VOICE_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = VOICE_DIR / "voices-v1.0.bin"
VOICE_PID_FILE = DATA_DIR / "voice.pid"


def reload() -> None:
    """Re-read settings.toml into SETTINGS so live changes take effect without restart."""
    global SETTINGS
    SETTINGS = load()


_SETTINGS_LOCK = threading.Lock()


def update_settings(changes: dict) -> None:
    """Apply {'section.key': value} changes to settings.toml (preserving comments), then reload.
    Serialized under a lock and written atomically (temp file + os.replace) so two concurrent
    POST /settings can't lose each other's updates or leave a half-written, corrupt TOML."""
    import tomlkit  # parses+dumps TOML preserving comments/formatting (unlike stdlib tomllib)
    p = CONFIG_DIR / "settings.toml"
    with _SETTINGS_LOCK:
        doc = tomlkit.parse(p.read_text(encoding="utf-8"))
        for dotted, val in changes.items():
            if "." not in dotted:
                continue  # keys must be "section.key"; skip anything not addressing a section
            sec, key = dotted.split(".", 1)
            if sec not in doc:
                doc[sec] = tomlkit.table()
            doc[sec][key] = val
        tmp = p.with_suffix(".toml.tmp")
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(tmp, p)  # atomic swap — a crash mid-write can't corrupt settings.toml
        reload()


# In-process cache of the CSRF auth token so we don't re-read the file on every request.
_auth_token_cache: str | None = None


def ensure_auth_token() -> str:
    """Create (once) and return the local auth token. Call at app startup.

    Idempotent: if data/.session_token already exists it is reused (so the token stays
    stable across restarts and matches what the UI/hook already hold); otherwise a fresh
    32-byte url-safe token is generated, persisted, and chmod'd 0600 (best-effort; no-op
    on Windows). This token is the shared CSRF secret defeating cross-site requests.
    """
    global _auth_token_cache
    AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        existing = ""
    if existing:
        _auth_token_cache = existing
        return existing
    tok = _secrets.token_urlsafe(32)
    AUTH_TOKEN_FILE.write_text(tok, encoding="utf-8")
    try:
        import os
        os.chmod(AUTH_TOKEN_FILE, 0o600)
    except Exception:
        pass
    _auth_token_cache = tok
    return tok


def auth_token() -> str | None:
    """Read the current auth token (None if the app hasn't created one yet)."""
    global _auth_token_cache
    if _auth_token_cache:
        return _auth_token_cache
    try:
        _auth_token_cache = (AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip() or None)
    except Exception:
        _auth_token_cache = None
    return _auth_token_cache


def log(name: str, msg: str) -> None:
    """Lightweight append logger -> logs/<name>.log."""
    import time
    try:
        with (LOGS_DIR / f"{name}.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass
