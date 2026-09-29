"""Global (system-wide) hotkeys via pynput.

Two actions, both supplied by app.py: summon (default Ctrl+Alt+J -> bring up the chat
window) and panic (default Ctrl+Alt+Backspace -> emergency stop). The actual bindings are
read from [hotkeys] in settings.toml. pynput runs its own background listener thread, so
these fire even when Helios isn't focused.
"""

from __future__ import annotations

from pynput import keyboard

from . import conf


class Hotkeys:
    """Registers the global summon/panic hotkeys with pynput's GlobalHotKeys listener.

    `summon` and `panic` are zero-arg callables invoked from the listener thread when the
    corresponding chord is pressed."""

    def __init__(self, summon, panic):
        # Bindings come from settings.toml; the defaults match the documented Ctrl+Alt+J /
        # Ctrl+Alt+Backspace chords. pynput uses "<mod>+key" syntax for the keys.
        s = conf.SETTINGS.get("hotkeys", {})
        self._map = {
            s.get("summon", "<ctrl>+<alt>+j"): summon,
            s.get("panic", "<ctrl>+<alt>+<backspace>"): panic,
        }
        self._listener: keyboard.GlobalHotKeys | None = None

    def start(self) -> None:
        """Start the background hotkey listener. Failure (e.g. another app owns the chord)
        is logged but non-fatal — Helios still runs, just without global hotkeys."""
        try:
            self._listener = keyboard.GlobalHotKeys(self._map)
            self._listener.start()
            conf.log("hotkeys", f"registered: {list(self._map)}")
        except Exception as e:  # pragma: no cover
            conf.log("hotkeys", f"failed to register: {e}")

    def stop(self) -> None:
        """Stop the listener thread (best-effort; ignores errors during shutdown)."""
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
