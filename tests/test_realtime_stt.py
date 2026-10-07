"""RealtimeSTT capture: Helios feeds the one mic into the recorder only while listening, the final
text comes from Helios's own transcriber (one run -> gated + raw), live text reaches the HUD, a
listen with no speech is cancelled cleanly and leaves nothing behind for the next one, a too-long
utterance is cut and still transcribed, sleep/mute cancel it, and if the engine is missing or
breaks the daemon falls back to the built-in capture. A fake recorder stands in for the library
(no models, no audio device)."""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from helios.voice import realtime_stt
from helios.voice.daemon import VoiceDaemon
from helios.voice.stt import Transcriber

LOUD, QUIET = np.full(1280, 3000, "int16"), np.zeros(1280, "int16")


class FakeRecorder:
    """Energy-based stand-in for RealtimeSTT.AudioToTextRecorder (same attributes Helios uses)."""

    def __init__(self, **kw):
        self.kw = kw
        self.executor = kw["transcription_executor"]
        self.on_start = kw["on_recording_start"]
        self.on_partial = kw["on_realtime_transcription_update"]
        self.silence_frames = 4
        self.start_recording_on_voice_activity = False
        self.stop_recording_on_voice_deactivity = False
        self.is_recording = False
        self.recording_start_time = 0.0
        self.recorded_audio_queue = queue.Queue()
        self.frames, self.last_frames = [], []
        self.frames_lock = threading.RLock()
        self.interrupt_stop_event = threading.Event()
        self.was_interrupted = threading.Event()
        self._stopped = threading.Event()
        self._quiet = 0
        self.fed = 0
        self.shut = False

    def clear_audio_queue(self):
        pass

    def feed_audio(self, chunk, rate=16000):
        self.fed += 1
        a = np.frombuffer(chunk, dtype="int16")
        loud = np.abs(a).mean() > 500
        if not self.is_recording and loud and self.start_recording_on_voice_activity:
            self.is_recording, self._quiet = True, 0
            self.recording_start_time = time.time()
            self.on_start()
        if self.is_recording:
            with self.frames_lock:
                self.frames.append(a.copy())
            self.on_partial("partial words")
            self._quiet = 0 if loud else self._quiet + 1
            if self._quiet >= self.silence_frames:
                self.stop()

    def stop(self):
        if self.is_recording:
            with self.frames_lock:
                self.recorded_audio_queue.put(list(self.frames))
                self.frames = []
            self.is_recording = False
            self._stopped.set()

    def text(self):
        self.interrupt_stop_event.clear()
        self.start_recording_on_voice_activity = True
        while not self.interrupt_stop_event.is_set():
            try:
                frames = self.recorded_audio_queue.get(timeout=0.01)
                break
            except queue.Empty:
                continue
        else:
            self.was_interrupted.set()
            return ""
        audio = np.concatenate(frames)
        return self.executor.transcribe(audio, language="en", use_prompt=True).text

    def shutdown(self):
        self.shut = True


class FakeStt(Transcriber):
    def __init__(self, text="what time is it", gated_ok=True):
        super().__init__("fake")
        self.text, self.gated_ok, self.calls = text, gated_ok, 0

    def transcribe_both(self, audio, *, log_rejects=True):
        self.calls += 1
        self.last_audio = audio
        return (self.text if self.gated_ok else ""), self.text


@pytest.fixture
def cap(monkeypatch):
    monkeypatch.setattr(realtime_stt.PartialExecutor, "warmup", lambda self: None)
    stt = FakeStt()
    rt = realtime_stt.RealtimeCapture(stt, {"silence_ms": 1500, "live_transcript": True},
                                      threading.Lock(), recorder_cls=FakeRecorder)
    assert rt.load()
    return rt


def drive(rt, frames, *, start_timeout=1.0, max_frames=200):
    partials = []
    rt.begin(partials.append)
    t0 = time.monotonic()
    for i in range(max_frames):
        if rt.done:
            break
        rt.feed(frames[i] if i < len(frames) else QUIET)
        if not rt.started and time.monotonic() - t0 > start_timeout:
            rt.cancel()
            return None, partials
        time.sleep(0.002)
    assert rt.wait(5)
    return rt.result(), partials


# ------------------------------------------------------------------ the capture engine

def test_capture_returns_gated_raw_and_the_exact_audio(cap):
    got, partials = drive(cap, [QUIET] * 3 + [LOUD] * 10)
    audio, gated, raw = got
    assert gated == raw == "what time is it"
    assert audio.dtype == np.float32 and audio.size == 14 * 1280      # 10 loud + 4 silent frames
    assert np.allclose(audio[:1280], 3000 / 32768.0)                   # int16 -> float32 scaled
    assert cap.final.stt.calls == 1 and "partial words" in partials


def test_recorder_settings(cap):
    kw = cap.rec.kw
    assert kw["use_microphone"] is False and kw["spinner"] is False and kw["no_log_file"] is True
    assert kw["transcription_executor"] is cap.final                   # Helios's own model
    assert kw["realtime_transcription_executor"] is cap.partial        # capped tiny.en
    assert kw["post_speech_silence_duration"] == 1.5
    assert kw["early_transcription_on_silence"] == 0.5                 # SECONDS (library quirk)
    assert kw["silero_backend"] == "raw_onnx" and kw["device"] == "cpu"
    assert kw["ensure_sentence_ends_with_period"] is False


@pytest.mark.parametrize("silence,early", [(800, 0.45), (1500, 0.5), (2500, 1.5), (500, 0.3)])
def test_early_transcription_default(monkeypatch, silence, early):
    monkeypatch.setattr(realtime_stt.PartialExecutor, "warmup", lambda self: None)
    rt = realtime_stt.RealtimeCapture(FakeStt(), {"silence_ms": silence}, threading.Lock(),
                                      recorder_cls=FakeRecorder)
    rt.load()
    assert rt.rec.kw["early_transcription_on_silence"] == pytest.approx(early)


def test_no_speech_cancels_cleanly_and_next_capture_works(cap):
    got, _ = drive(cap, [QUIET] * 500, start_timeout=0.1)
    assert got is None and cap.done
    assert not cap.rec.interrupt_stop_event.is_set()
    got, _ = drive(cap, [LOUD] * 5)
    assert got and got[1] == "what time is it"


def test_leftover_recording_is_not_glued_onto_the_next(cap):
    drive(cap, [LOUD] * 5)
    cap.rec.recorded_audio_queue.put([np.full(1280, 9999, "int16")])   # a queued tail utterance
    got, _ = drive(cap, [LOUD] * 5)
    assert not np.any(got[0] > 0.3)                                     # 9999/32768 never shows up


def test_stop_forces_the_end_and_still_transcribes(cap):
    cap.rec.silence_frames = 10 ** 6                                    # never ends on its own
    cap.begin()
    for _ in range(6):
        cap.feed(LOUD)
    cap.stop()
    assert cap.wait(5)
    assert cap.result()[1] == "what time is it"


def test_result_ignores_an_older_capture(cap):
    drive(cap, [LOUD] * 5)
    cap.begin()
    cap.cancel()
    assert cap.result() is None


def test_load_failure_falls_back(monkeypatch):
    def boom(**kw):
        raise RuntimeError("no silero model")
    rt = realtime_stt.RealtimeCapture(FakeStt(), {}, threading.Lock(), recorder_cls=boom)
    assert rt.load() is False and not rt.ready and "no silero model" in rt.error
    assert rt.load() is False                                           # doesn't retry forever


def test_close_shuts_the_recorder_down(cap):
    rec = cap.rec
    cap.close()
    assert rec.shut and not cap.ready


def test_available_reports_what_is_missing(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "RealtimeSTT":
            raise ImportError("No module named 'RealtimeSTT'")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    ok, why = realtime_stt.available()
    assert not ok and "RealtimeSTT" in why


# ------------------------------------------------------------------ one transcription, two texts

class SegStt(Transcriber):
    def __init__(self, segs):
        super().__init__("fake")
        self.segs = segs

    def _transcribe_core(self, audio):
        return [SimpleNamespace(text=t, no_speech_prob=n, avg_logprob=lp) for t, n, lp in self.segs]


def test_transcribe_both_gates_once():
    audio = np.zeros(16000, "float32")
    s = SegStt([(" Open Spotify", 0.1, -0.3), (" uh", 0.9, -2.0)])
    assert s.transcribe_both(audio) == ("Open Spotify", "Open Spotify uh")
    assert s.transcribe(audio) == "Open Spotify" and s.transcribe(audio, gated=False) == "Open Spotify uh"
    junk = SegStt([(" Thank you.", 0.2, -0.4)])
    assert junk.transcribe_both(audio) == ("", "Thank you.")
    assert SegStt([]).transcribe_both(np.zeros(100, "float32")) == ("", "")


# ------------------------------------------------------------------ daemon integration

class _Mic:
    def __init__(self, frames, d=None, sleep_at=None):
        self.frames, self.i, self.d, self.sleep_at = list(frames), 0, d, sleep_at

    def flush(self):
        pass

    def read(self, timeout):
        self.i += 1
        if self.sleep_at and self.i == self.sleep_at:
            self.d.dormant = True
        time.sleep(0.002)
        return self.frames.pop(0) if self.frames else QUIET


def _daemon(rt, frames, **kw):
    posted = []
    d = SimpleNamespace(rt=rt, _rt_last=None, _stop=threading.Event(), dormant=False, muted=False,
                        live_transcript=True, _mic_level=0.0, _vu_state=None,
                        _announce_q=queue.Queue(), _stt_lock=threading.Lock(), stt=None,
                        bridge=SimpleNamespace(post_voice_state=lambda s, **f: posted.append((s, f))))
    d.mic = _Mic(frames, d, kw.get("sleep_at"))
    d.posted = posted
    return d


def test_daemon_capture_uses_realtimestt_and_skips_a_second_transcription(cap):
    d = _daemon(cap, [LOUD] * 8)
    audio = VoiceDaemon._capture(d, start_timeout=2, max_sec=10)
    assert audio is not None and audio is d._rt_last[0]
    d.stt = SimpleNamespace(transcribe=lambda *a, **k: pytest.fail("ran the model twice"))
    assert VoiceDaemon._transcribe(d, audio) == "what time is it"
    assert VoiceDaemon._transcribe(d, audio, gated=False) == "what time is it"
    assert ("listening", {"partial": "partial words"}) in d.posted
    assert d._vu_state is None and cap.final.stt.calls == 1


def test_daemon_gated_vs_raw(cap):
    cap.final.stt.gated_ok = False                                      # e.g. a mumble
    d = _daemon(cap, [LOUD] * 8)
    audio = VoiceDaemon._capture(d, start_timeout=2, max_sec=10, state="permission")
    assert VoiceDaemon._transcribe(d, audio) == ""                      # command: rejected
    assert VoiceDaemon._transcribe(d, audio, gated=False) == "what time is it"   # yes/no: raw


def test_daemon_no_speech_times_out(cap):
    d = _daemon(cap, [])
    t = time.monotonic()
    assert VoiceDaemon._capture(d, start_timeout=0.2, max_sec=10) is None
    assert time.monotonic() - t < 2 and cap.done


def test_daemon_sleep_cancels_the_listen(cap):
    cap.rec.silence_frames = 10 ** 6
    d = _daemon(cap, [LOUD] * 100, sleep_at=4)
    assert VoiceDaemon._capture(d, start_timeout=2, max_sec=10) is None
    assert d.mic.i <= 6 and cap.done


def test_daemon_max_length_cuts_and_transcribes(cap):
    cap.rec.silence_frames = 10 ** 6
    d = _daemon(cap, [LOUD] * 10 ** 4)
    audio = VoiceDaemon._capture(d, start_timeout=2, max_sec=0.3)
    assert audio is not None and VoiceDaemon._transcribe(d, audio) == "what time is it"


def test_daemon_falls_back_when_the_engine_breaks(cap):
    def broken(frame):
        raise RuntimeError("worker died")
    cap.feed = broken
    d = _daemon(cap, [LOUD] * 8)
    assert VoiceDaemon._capture(d, start_timeout=2, max_sec=10) is None
    assert d.rt is None                                                  # next capture: built-in


def test_daemon_without_engine_uses_builtin(monkeypatch):
    d = _daemon(None, [])
    d.vad = SimpleNamespace(started=False, reset=lambda: None, feed=lambda f: False)
    assert VoiceDaemon._capture(d, start_timeout=0.05, max_sec=1) is None
    calls = []
    d.stt = SimpleNamespace(transcribe=lambda a, gated=True: calls.append(gated) or "hi")
    assert VoiceDaemon._transcribe(d, np.zeros(10, "float32"), gated=False) == "hi" and calls == [False]


def test_daemon_selects_engine_from_settings(monkeypatch):
    import helios.voice.daemon as dm
    src = open(dm.__file__, encoding="utf-8").read()
    assert 'c.get("capture_engine", "builtin")' in src                  # opt-in, safe default
    from helios import conf
    assert conf.voice_cfg().get("capture_engine") in ("realtimestt", "builtin")
