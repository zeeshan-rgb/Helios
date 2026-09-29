"""Windows desktop toast notifications for Helios (reminders, alerts, job-done).

Thin wrapper over the `winotify` package. Used by scheduler.py and others to surface
proactive pings to the user. Best-effort: any failure (missing dep, toast service issues) is
swallowed and logged so a failed notification never breaks the caller.
"""

from __future__ import annotations

from . import conf


def toast(title: str, message: str, *, sound: bool = True) -> None:
    """Show a Windows toast titled `title` with body `message`. Imported lazily so the
    module loads even where winotify is absent. `message` is capped at 250 chars (Windows
    truncates long toasts anyway). Never raises — failures are logged and ignored."""
    try:
        from winotify import Notification, audio
        # app_id "Helios" groups all our toasts under one identity in the Action Center.
        n = Notification(app_id="Helios", title=title, msg=message[:250])
        if sound:
            n.set_audio(audio.Default, loop=False)
        n.show()
    except Exception as e:  # pragma: no cover
        conf.log("notify", f"toast failed: {e}")
