"""Ambient mode — monitor windows for events, alert, or click through them.

"Watch for the build to finish" / "Watch for a Continue button and click it" — the voice daemon
polls the FOREGROUND window's UI Automation tree on a cadence and triggers on matching conditions.
Runs entirely inside the voice daemon process via the `uiautomation` package (Windows UIA/COM) —
NOT via the mcp__computer__* tools, which only exist inside a live Claude brain turn and aren't
reachable from this standalone background thread.

Two trigger polarities, chosen by whether a click_target is set:
  - Plain watch (no click_target): fires when the condition text DISAPPEARS from the window
    (e.g. "Building…" going away means the build finished). Re-arms if the text comes back.
  - Click watch (click_target set): fires when the condition text APPEARS (e.g. a "Continue"
    dialog popping up), clicks the named element, then removes itself (one-shot — a watch that
    re-clicked every poll while a dialog lingered would be a bug, not a feature).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

from . import conf

_MAX_WALK_DEPTH = 8
_MAX_WALK_NODES = 800
_CLICKABLE_TYPES = frozenset({
    "ButtonControl", "MenuItemControl", "HyperlinkControl", "ListItemControl",
    "CheckBoxControl", "RadioButtonControl", "TabItemControl",
})


class Watch:
    """A single ambient watch: poll the foreground window for a condition."""

    def __init__(self, watch_id: str, pattern: str, condition: str, interval_sec: float = 2.0,
                 action: str = "voice", click_target: str | None = None):
        self.watch_id = watch_id
        self.pattern = pattern          # window title/process regex or substring; ".*" = any window
        self.condition = condition      # text to watch for (appear/disappear depending on mode)
        self.interval_sec = float(interval_sec)
        self.action = action            # "voice", "notify", "dashboard" (alert delivery for plain watches)
        self.click_target = click_target  # element name to click when condition appears (one-shot)
        self.created_at = time.monotonic()
        self.last_checked = 0.0
        self.triggered = False          # plain-watch armed/fired state (toggles on disappear/reappear)

    def to_dict(self) -> dict:
        return {
            "id": self.watch_id,
            "pattern": self.pattern,
            "condition": self.condition,
            "interval_sec": self.interval_sec,
            "action": self.action,
            "click_target": self.click_target,
            "created_at": self.created_at,
        }


class AmbientMonitor:
    """Background monitor: poll the foreground window, alert or click on matches."""

    def __init__(self, on_trigger: Callable[[Watch, str], None] | None = None):
        self.watches: dict[str, Watch] = {}
        self.on_trigger = on_trigger  # callback: (watch, message) when triggered
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._next_watch_id = 1
        self._warned_unavailable = False  # log the "uiautomation missing" warning once, not every poll

    def add_watch(self, pattern: str, condition: str, interval_sec: float = 2.0,
                  action: str = "voice", click_target: str | None = None) -> str:
        """Register a watch. Returns the watch ID."""
        watch_id = f"watch_{self._next_watch_id}"
        self._next_watch_id += 1

        watch = Watch(watch_id, pattern, condition, interval_sec, action, click_target)
        with self._lock:
            self.watches[watch_id] = watch

        conf.log("ambient", f"watch added: {watch_id} (pattern={pattern!r}, condition={condition!r}, "
                            f"click_target={click_target!r})")
        return watch_id

    def remove_watch(self, watch_id: str) -> bool:
        """Unregister a watch. Returns True if it existed."""
        with self._lock:
            if watch_id in self.watches:
                del self.watches[watch_id]
                conf.log("ambient", f"watch removed: {watch_id}")
                return True
        return False

    def clear_watches(self) -> None:
        """Remove all watches."""
        with self._lock:
            self.watches.clear()
        conf.log("ambient", "all watches cleared")

    def get_watches(self) -> list[dict]:
        """List all active watches."""
        with self._lock:
            return [w.to_dict() for w in self.watches.values()]

    def current_window_pattern(self) -> str:
        """Foreground window's title right now, escaped for exact matching — used so a watch
        created via voice command tracks THAT window, not whatever happens to be focused later.
        Falls back to ".*" (any window) if the title can't be read."""
        info = self._get_window_info()
        if info and info.get("title"):
            return re.escape(info["title"])
        return ".*"

    def start(self) -> None:
        """Start the polling thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="AmbientMonitor")
        self._thread.start()
        conf.log("ambient", "monitor started")

    def stop(self) -> None:
        """Stop the polling thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        conf.log("ambient", "monitor stopped")

    def _run(self) -> None:
        """Thread entry: initialize COM for this thread (uiautomation/comtypes needs it), then loop."""
        try:
            import uiautomation as auto
        except ImportError:
            conf.log("ambient", "uiautomation not installed; ambient monitor disabled")
            return
        try:
            with auto.UIAutomationInitializerInThread():
                self._loop()
        except Exception as e:  # pragma: no cover
            conf.log("ambient", f"monitor thread crashed: {e}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    watches = list(self.watches.values())

                now = time.monotonic()
                for watch in watches:
                    if now - watch.last_checked < watch.interval_sec:
                        continue
                    watch.last_checked = now
                    self._check_watch(watch)

                self._stop.wait(0.2)  # yield, check for stop
            except Exception as e:  # pragma: no cover
                conf.log("ambient", f"monitor error: {e}")
                self._stop.wait(1.0)

    def _check_watch(self, watch: Watch) -> None:
        """Poll one watch: get the foreground window, match pattern, check condition, act."""
        try:
            info = self._get_window_info()
            if info is None:
                return

            if not (self._match_pattern(info["title"], watch.pattern) or
                    self._match_pattern(info["process"], watch.pattern)):
                return

            condition_found = any(watch.condition.lower() in t.lower() for t in info["texts"])

            if watch.click_target:
                # Click-on-appear, one-shot: fire once the condition text shows up, then remove
                # this watch (a lingering dialog would otherwise get re-clicked every poll).
                if condition_found:
                    self._fire_click(watch, info)
                return

            # Plain watch: fire on DISAPPEARANCE (e.g. "Building…" going away = build finished).
            if not condition_found and not watch.triggered:
                watch.triggered = True
                msg = f"{watch.condition} — no longer showing in {info['title'] or info['process']}"
                conf.log("ambient", f"watch triggered: {watch.watch_id} ({msg})")
                if self.on_trigger:
                    self.on_trigger(watch, msg)
            elif condition_found:
                watch.triggered = False  # re-arm: it came back, so a future disappearance fires again

        except Exception as e:  # pragma: no cover
            conf.log("ambient", f"watch error ({watch.watch_id}): {e}")

    def _fire_click(self, watch: Watch, info: dict) -> None:
        matches = [(name, ctrl) for name, ctrl in info["clickables"]
                  if watch.click_target.lower() in name.lower()]
        if not matches:
            msg = f"{watch.condition} appeared, but no button matching {watch.click_target!r} was found"
            conf.log("ambient", f"watch {watch.watch_id}: {msg}")
            if self.on_trigger:
                self.on_trigger(watch, msg)
            self.remove_watch(watch.watch_id)
            return

        # Prefer an exact (case-insensitive) name match over a mere substring hit.
        exact = [m for m in matches if m[0].lower() == watch.click_target.lower()]
        name, ctrl = exact[0] if exact else matches[0]
        try:
            ctrl.Click(simulateMove=False)
            msg = f"clicked {name!r} ({watch.condition})"
            conf.log("ambient", f"watch {watch.watch_id}: {msg}")
        except Exception as e:
            msg = f"found {name!r} but the click failed: {e}"
            conf.log("ambient", f"watch {watch.watch_id}: {msg}")
        if self.on_trigger:
            self.on_trigger(watch, msg)
        self.remove_watch(watch.watch_id)   # one-shot: done regardless of click success

    def _get_window_info(self) -> dict | None:
        """Foreground window's title/process + a bounded walk of its UI tree: all element names
        (for condition text matching) and clickable elements (for the click_target lookup)."""
        try:
            import uiautomation as auto
        except ImportError:
            if not self._warned_unavailable:
                conf.log("ambient", "uiautomation not installed; watches will never fire")
                self._warned_unavailable = True
            return None

        try:
            win = auto.GetForegroundControl()
            if win is None:
                return None
            title = win.Name or ""
            process = ""
            try:
                import psutil
                process = psutil.Process(win.ProcessId).name()
            except Exception:
                pass

            texts: list[str] = []
            clickables: list[tuple[str, object]] = []

            def walk(ctrl, depth: int) -> None:
                if depth > _MAX_WALK_DEPTH or len(texts) > _MAX_WALK_NODES:
                    return
                try:
                    name = ctrl.Name
                    if name:
                        texts.append(name)
                        if ctrl.ControlTypeName in _CLICKABLE_TYPES:
                            clickables.append((name, ctrl))
                except Exception:
                    pass
                try:
                    for child in ctrl.GetChildren():
                        walk(child, depth + 1)
                except Exception:
                    pass

            walk(win, 0)
            return {"title": title, "process": process, "texts": texts, "clickables": clickables}
        except Exception as e:  # pragma: no cover
            conf.log("ambient", f"window read error: {e}")
            return None

    @staticmethod
    def _match_pattern(text: str, pattern: str) -> bool:
        """Match a window title/process against a pattern (regex, falling back to substring)."""
        if not text or not pattern:
            return False
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return pattern.lower() in text.lower()
