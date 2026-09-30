"""Stability fixes (2026-09-30, user report: slow/garbled voice, notification storms, console
flashes, lag): VAD needs sustained speech to start, the capture keeps a pre-roll, STT logs what it
rejects, notifications launch hidden and are rate-limited, background work runs at low priority."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from helios import conf


# ------------------------------------------------------------------ VAD start

class _FakeSilero:
    def __init__(self, scores):
        self.scores = list(scores)

    def predict(self, win, frame_size=480):
        return self.scores.pop(0) if self.scores else 0.0

    def reset_states(self):
        pass


def _endpointer(scores, **kw):
    from helios.voice.vad import Endpointer
    e = Endpointer(**kw)
    e._vad = _FakeSilero(scores)
    return e


def _feed(e, n_windows):
    return e.feed(np.zeros(480 * n_windows, dtype="int16"))


def test_a_single_blip_does_not_start_an_utterance():
    e = _endpointer([0.9] + [0.0] * 40)          # e.g. the wake chime or a clap's echo
    _feed(e, 41)
    assert not e.started


def test_scattered_blips_do_not_add_up():
    e = _endpointer([0.9, 0.0, 0.0, 0.9, 0.0, 0.0, 0.9, 0.0, 0.0, 0.9, 0.0, 0.0] * 3)
    _feed(e, 36)
    assert not e.started


def test_sustained_speech_starts_and_then_ends_on_silence():
    e = _endpointer([0.9] * 10 + [0.0] * 30, silence_ms=800)
    assert _feed(e, 10) is False and e.started
    assert _feed(e, 30) is True


# ------------------------------------------------------------------ capture pre-roll

def test_capture_keeps_the_pre_roll():
    from helios.voice.daemon import VoiceDaemon
    frames = [np.full(1280, i, dtype="int16") for i in range(1, 12)]

    class Vad:
        started = False
        n = 0

        def reset(self):
            pass

        def feed(self, f):
            self.n += 1
            if self.n == 4:
                self.started = True            # speech confirmed on the 4th frame
            return self.n == 8

    class Mic:
        def flush(self):
            pass

        def read(self, t):
            return frames.pop(0)
    d = SimpleNamespace(vad=Vad(), mic=Mic(), _stop=threading.Event(), dormant=False, muted=False,
                        live_transcript=False, _mic_level=0.0, _vu_state=None, _announce_q=queue.Queue())
    audio = VoiceDaemon._capture(d, start_timeout=8, max_sec=30)
    first = round(float(audio[0]) * 32768)
    assert first == 1 and len(audio) == 8 * 1280        # frames 1-3 (pre-roll) + 4-8


# ------------------------------------------------------------------ STT rejection log

def test_stt_logs_what_it_rejects(monkeypatch):
    from helios.voice import stt
    logged = []
    monkeypatch.setattr(stt.conf, "log", lambda name, msg: logged.append(msg))
    t = stt.Transcriber()
    seg = SimpleNamespace(text=" open spotify", no_speech_prob=0.1, avg_logprob=-2.0)
    monkeypatch.setattr(t, "_transcribe_core", lambda audio: [seg])
    assert t.transcribe(np.ones(16000, dtype="float32")) == ""
    assert "stt rejected: 'open spotify' (no_speech 0.10, logprob -2.00)" in logged[-1]
    seg.avg_logprob = -1.1                             # accented speech: now accepted
    assert t.transcribe(np.ones(16000, dtype="float32")) == "open spotify"


# ------------------------------------------------------------------ notifications

@pytest.fixture
def real_notify(monkeypatch):
    import importlib
    from helios import notify
    notify = importlib.reload(notify)                  # undo conftest's stub for this test
    launched = []
    monkeypatch.setattr(notify, "_launch", lambda file="", command="": launched.append(file or command))
    notify._recent.clear()
    notify._sent.clear()
    return notify, launched


def test_toasts_are_deduplicated_and_rate_limited(real_notify, monkeypatch):
    notify, _ = real_notify
    shown = []
    monkeypatch.setattr(notify, "_allowed", notify._allowed)
    t = [1000.0]
    assert notify._allowed("A", "x", t[0]) is None
    assert notify._allowed("A", "x", t[0] + 5) == "duplicate"
    assert notify._allowed("B", "y", t[0] + 6) is None
    assert notify._allowed("C", "z", t[0] + 7) is None
    assert notify._allowed("D", "w", t[0] + 8) is None
    assert notify._allowed("E", "v", t[0] + 9) == "rate limit"      # 4 per minute
    assert notify._allowed("E", "v", t[0] + 75) is None             # window passed
    assert notify._allowed("A", "x", t[0] + 200) is None            # dedupe window passed


def test_toast_launcher_uses_no_window(monkeypatch):
    import importlib
    from helios import notify
    notify = importlib.reload(notify)
    seen = {}
    monkeypatch.setattr(notify.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    notify._launch(file="C:/tmp/t.ps1")
    assert seen["creationflags"] & notify.CREATE_NO_WINDOW
    assert "-WindowStyle" in seen["cmd"] and "Hidden" in seen["cmd"]


# ------------------------------------------------------------------ priorities

def test_background_work_runs_below_normal_priority(monkeypatch, tmp_path):
    import subprocess
    from helios import agy_cli, health
    assert health._BELOW_NORMAL == getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    seen = []

    class P:
        stderr = iter(())
        stdout = iter(())

        def poll(self):
            return 0
    monkeypatch.setattr(agy_cli.subprocess, "Popen", lambda *a, **kw: (seen.append(kw["creationflags"]), P())[1])
    handle = {"ws": tmp_path}
    agy_cli.AgySession(handle, prompt="x").start()           # one-shot = background
    agy_cli.AgySession(handle).start()                        # live session
    assert seen[0] & agy_cli.BELOW_NORMAL_PRIORITY and not seen[1] & agy_cli.BELOW_NORMAL_PRIORITY


def test_earcon_can_wait_for_the_chime(monkeypatch):
    from helios.voice import earcons
    monkeypatch.setattr(earcons, "_play", lambda sig: None)
    import time
    t0 = time.monotonic()
    earcons.wake(block=True)
    assert time.monotonic() - t0 >= earcons.WAKE_SEC
