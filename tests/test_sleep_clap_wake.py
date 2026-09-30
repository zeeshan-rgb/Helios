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
    import queue
    d = SimpleNamespace(vad=_Vad(), _stop=threading.Event(), dormant=dormant, muted=False,
                        live_transcript=False, _mic_level=0.0, _vu_state=None,
                        _announce_q=queue.Queue())
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


@pytest.fixture
def voice_store(monkeypatch):
    from helios import conf
    store = {"enabled": True, "muted": False}

    def update(ch):
        for k, v in ch.items():
            store[k.split(".", 1)[1]] = v
    monkeypatch.setattr(conf, "update_settings", update)
    monkeypatch.setattr(conf, "voice_cfg", lambda: dict(store))
    return store


def test_mic_off_mutes_instead_of_killing(app_env, voice_store):
    app, calls, alive, hub = app_env
    toggle = app._make_voice_toggle(hub)
    alive["v"] = True
    assert toggle(False) is False
    assert calls["killed"] == 0 and voice_store["muted"] is True        # listener stays up
    assert ("control", {"action": "mute"}) in calls["published"]
    assert ("voice", {"state": "off"}) in calls["published"]
    assert toggle(True) is True and voice_store["muted"] is False
    assert ("control", {"action": "unmute"}) in calls["published"]
    assert calls["launched"] == 0                                       # no heavy restart
    assert toggle() is False                                            # no intent: flip (off)


def test_mic_off_with_no_listener_starts_it_muted(app_env, voice_store):
    app, calls, alive, hub = app_env
    toggle = app._make_voice_toggle(hub)
    assert toggle(False) is False
    assert calls["launched"] == 1 and voice_store["muted"] is True      # so a clap can wake it


def test_muted_daemon_wakes_and_unmutes_on_clap():
    from helios.voice.daemon import VoiceDaemon
    events = []

    class Clap:
        def reset(self, grace=0.0):
            events.append("reset")
    d = SimpleNamespace(dormant=False, muted=True, clap=Clap(),
                        bridge=SimpleNamespace(set_voice=lambda on: events.append(("set_voice", on)),
                                               summon=lambda: events.append("summon")))
    VoiceDaemon._wake_up(d, "double-clap")
    assert d.muted is False and d.dormant is False
    assert ("set_voice", True) in events and "summon" in events


def test_mute_control_events():
    from helios.voice.daemon import VoiceDaemon
    states = []
    d = SimpleNamespace(dormant=False, muted=False, clap=SimpleNamespace(reset=lambda grace=0.0: None),
                        tts=SimpleNamespace(stop=lambda: None), _turn_complete=threading.Event())
    d._set_state = lambda s, **k: states.append(s)
    VoiceDaemon._on_event(d, "control", {"action": "mute"})
    assert d.muted is True and states[-1] == "off"
    VoiceDaemon._on_event(d, "control", {"action": "unmute"})
    assert d.muted is False and states[-1] == "idle"


def test_orb_hides_on_sleep_and_is_reused_on_wake(app_env, monkeypatch):
    app, calls, alive, _ = app_env
    killed, launched = [], []
    monkeypatch.setattr(app, "_kill_orb", lambda: killed.append(1))
    monkeypatch.setattr(app, "_launch_orb", lambda: launched.append(1))
    monkeypatch.setattr(app, "_orb_alive", lambda: True)
    monkeypatch.setattr(app, "_open_app", lambda name: None)
    app._sleep()
    assert killed == [] and ("control", {"action": "sleep"}) in calls["published"]
    app._activate()
    assert launched == [] and ("control", {"action": "wake"}) in calls["published"]
