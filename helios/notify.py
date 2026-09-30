"""Windows desktop toast notifications for Helios (reminders, alerts, job-done).

Uses the `winotify` package's toast template, but launches its PowerShell helper with
CREATE_NO_WINDOW: winotify's own launcher only asks for a hidden window, which still flashes a
console on screen for a moment (from a windowless parent like Helios's pythonw). Toasts are also
rate-limited — an identical toast within DEDUPE_S is dropped and at most BURST toasts go out per
minute — and every toast (shown or suppressed) is logged to logs/notify.log so a noisy source is
easy to find. Best-effort: a failed toast never breaks the caller.
"""

from __future__ import annotations

import subprocess
import threading
import time

from . import conf

CREATE_NO_WINDOW = 0x08000000
DEDUPE_S = 120.0        # same title+message within this window: dropped
BURST = 4               # at most this many toasts per rolling minute

_lock = threading.Lock()
_recent: dict[tuple[str, str], float] = {}
_sent: list[float] = []


def _launch(file: str = "", command: str = "") -> None:
    """winotify's PowerShell launcher, minus the console flash."""
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden"]
    cmd += ["-File", file] if file else ["-Command", command]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)


def _allowed(title: str, message: str, now: float) -> str | None:
    """None = show it; otherwise why it was suppressed."""
    with _lock:
        for k, t in list(_recent.items()):
            if now - t > DEDUPE_S:
                del _recent[k]
        _sent[:] = [t for t in _sent if now - t < 60.0]
        key = (title, message)
        if key in _recent:
            return "duplicate"
        if len(_sent) >= BURST:
            return "rate limit"
        _recent[key] = now
        _sent.append(now)
    return None


def toast(title: str, message: str, *, sound: bool = True) -> None:
    """Show a Windows toast titled `title` with body `message` (capped at 250 chars). Imported
    lazily so the module loads where winotify is absent. Never raises."""
    message = (message or "")[:250]
    why = _allowed(title, message, time.monotonic())
    if why:
        conf.log("notify", f"suppressed ({why}): {title} — {message[:80]}")
        return
    conf.log("notify", f"toast: {title} — {message[:80]}")
    try:
        import winotify
        from winotify import Notification, audio
        winotify._run_ps = _launch            # hidden launcher (see module doc)
        n = Notification(app_id="Helios", title=title, msg=message)
        if sound:
            n.set_audio(audio.Default, loop=False)
        n.show()
    except Exception as e:  # pragma: no cover
        conf.log("notify", f"toast failed: {e}")
