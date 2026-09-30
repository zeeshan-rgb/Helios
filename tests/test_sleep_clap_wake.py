"""Sleep -> double-clap wake must work every time: a listen in progress is cancelled the moment
Helios is put to sleep, Sleep restarts a switched-off voice listener (booting dormant), and the
mic button's request is idempotent so a stale button can't switch the listener off."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from helios.voice.daemon import VoiceDaemon


class _Vad:
    started = True

    def reset(self):
        pass

    def feed(self, frame):
        return False          # speech never ends on its own (like a noisy room)


class _Mic:
    def __init__(self, d, sleep_after):
        self.d, self.reads, self.sleep_after = d, 0, sleep_after

    def flush(self):
        pass

    def read(self, timeout):
        self.reads += 1
        if self.reads == self.sleep_after:
            self.d.dormant = True             # the user pressed Sleep mid-listen
        return np.full(1280, 500, dtype="int16")


def _daemon(sleep_after=3, dormant=False):
    d = SimpleNamespace(vad=_Vad(), _stop=threading.Event(), dormant=dormant,
                        live_transcript=False, _mic_level=0.0, _vu_state=None)
    d.mic = _Mic(d, sleep_after)
    return d


def test_sleep_cancels_a_listen_in_progress():
    d = _daemon(sleep_after=3)
    assert VoiceDaemon._capture(d, start_timeout=8, max_sec=30) is None
    assert d.mic.reads <= 4                      # stopped right away, not after 30 s
    assert d._vu_state is None


def test_capture_started_while_asleep_is_not_cancelled():
    # Spoken permission answers are captured while dormant; they must not be cut off.
    d = _daemon(sleep_after=10 ** 9, dormant=True)
    d._stop_after = 0
    orig = d.mic.read

    def read(timeout):
        if d.mic.reads >= 5:
            d._stop.set()
        return orig(timeout)
    d.mic.read = read
    audio = VoiceDaemon._capture(d, start_timeout=8, max_sec=30)
    assert audio is not None and len(audio) > 0


@pytest.mark.parametrize("hidden,env,expected", [
    (False, "0", True),     # app asleep -> boot dormant even without [startup].hidden
    (True, "1", False),     # app awake -> never dormant
    (True, None, True), (False, None, False),
])
def test_boot_dormant(hidden, env, expected):
    assert VoiceDaemon._boot_dormant(hidden, env) is expected


# ------------------------------------------------------------------ app side

@pytest.fixture
def app_env(monkeypatch):
    from helios import app, conf
    calls = {"launched": 0, "killed": 0, "settings": [], "published": []}
    alive = {"v": False}

    def launch():
        calls["launched"] += 1
        calls["awake_env"] = "0" if app._state.get("dormant") else "1"
        alive["v"] = True
        return True

    def kill():
        calls["killed"] += 1
        alive["v"] = False
    monkeypatch.setattr(app, "_voice_alive", lambda: alive["v"])
    monkeypatch.setattr(app, "_launch_voice", launch)
    monkeypatch.setattr(app, "_kill_voice", kill)
    monkeypatch.setattr(app, "_show_dashboard", lambda show: None)
    monkeypatch.setattr(app, "_kill_orb", lambda: None)
    monkeypatch.setattr(conf, "update_settings", lambda ch: calls["settings"].append(ch))
    hub = SimpleNamespace(publish=lambda k, d: calls["published"].append((k, d)))
    monkeypatch.setitem(app._state, "hub", hub)
    monkeypatch.setitem(app._state, "dormant", False)
    return app, calls, alive, hub


def test_sleep_restarts_a_switched_off_listener_in_dormant_mode(app_env):
    app, calls, alive, _ = app_env
    app._sleep()
    assert calls["launched"] == 1 and calls["awake_env"] == "0"
    assert {"voice.enabled": True} in calls["settings"]
    assert ("control", {"action": "sleep"}) in calls["published"]


def test_sleep_leaves_a_running_listener_alone(app_env):
    app, calls, alive, _ = app_env
    alive["v"] = True
    app._sleep()
    assert calls["launched"] == 0


def test_mic_button_is_idempotent(app_env):
    app, calls, alive, hub = app_env
    toggle = app._make_voice_toggle(hub)
    alive["v"] = True
    assert toggle(True) is True and calls["killed"] == 0      # stale "off" button asking for on
    assert toggle(False) is False and calls["killed"] == 1
    assert toggle(False) is False and calls["killed"] == 1    # already off: nothing to do
    assert toggle(True) is True and calls["launched"] == 1
    assert toggle() is False                                  # no intent given: flip
