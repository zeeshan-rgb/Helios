"""Voice phase 2: voice-character effects, pitch/rate compensation, wake-word fallback when the
model is missing, voice diagnostics, and the Antigravity session warm-up.

No audio device, no model files and no network: synthesis and the fake agy are stubbed.
"""

from __future__ import annotations

import numpy as np
import pytest

from helios.voice import effects
from helios.voice.tts import SR, Speaker
from helios.voice.wake import WakeWord


def _tone(seconds=1.0, amp=0.5):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype("float32")


# ---------------------------------------------------------------- effects
def test_apply_is_identity_when_neutral():
    x = _tone()
    assert np.array_equal(effects.apply(x, SR), x)


def test_stretch_changes_length_by_factor():
    x = _tone(1.0)
    assert effects.stretch(x, 1.25).size == int(round(x.size * 1.25))
    assert effects.stretch(x, 1.0).size == x.size


def test_reverb_adds_bounded_tail_without_clipping():
    x = _tone(1.0, amp=0.9)
    y = effects.reverb(x, SR, 0.6)
    assert y.size == x.size + int(effects._TAIL_S * SR)
    assert float(np.max(np.abs(y))) <= 0.951
    assert abs(float(y[-1])) < 1e-3                      # tail faded out, no click
    assert np.array_equal(effects.reverb(x, SR, 0.0), x)


def test_reverb_is_deterministic():
    x = _tone(0.5)
    assert np.array_equal(effects.reverb(x, SR, 0.3), effects.reverb(x, SR, 0.3))


class _FakeKokoro:
    def __init__(self):
        self.calls = []

    def create(self, text, voice, speed, lang):
        self.calls.append({"voice": voice, "speed": speed, "lang": lang})
        return _tone(1.0), SR


def test_pitch_keeps_speaking_rate_by_synthesizing_faster():
    s = Speaker("bm_lewis", 1.0, "en-gb", pitch=0.9, reverb=0.0)
    fake = _FakeKokoro()
    s._kokoro = fake
    out = s._synth("hello")
    assert fake.calls[0]["speed"] == pytest.approx(1.0 / 0.9)
    assert out.size == int(round(SR / 0.9))              # 1s synthesized fast, stretched by 1/0.9
    s.close()


def test_plain_voice_untouched_and_values_clamped():
    s = Speaker("am_onyx", 1.0, "en-us")
    fake = _FakeKokoro()
    s._kokoro = fake
    assert np.array_equal(s._synth("hi"), _tone(1.0))
    assert fake.calls[0] == {"voice": "am_onyx", "speed": 1.0, "lang": "en-us"}
    wild = Speaker("x", 1.0, "en-us", pitch=0.1, reverb=5)
    assert wild.pitch == 0.7 and wild.reverb == 1.0
    s.close(); wild.close()


def test_build_speaker_passes_character_settings():
    from helios.voice.engines import build_speaker
    spk = build_speaker({"tts_voice": "bm_fable", "tts_speed": 0.95, "tts_lang": "en-gb",
                         "tts_pitch": 0.9, "tts_reverb": 0.3})
    assert (spk.voice, spk.speed, spk.pitch, spk.reverb) == ("bm_fable", 0.95, 0.9, 0.3)
    spk.close()


# ---------------------------------------------------------------- wake word fallback
def test_missing_custom_wake_model_disables_cleanly(tmp_path):
    w = WakeWord(str(tmp_path / "hey_helios.onnx"))
    assert not w.available and "not found" in w.disabled_reason
    assert w.model_key == "hey_helios"
    assert w.detect(np.zeros(1280, dtype="int16")) is False
    w.warmup()                                            # logs once, never raises


@pytest.mark.parametrize("value", ["", "none", "None"])
def test_wake_word_can_be_turned_off(value):
    w = WakeWord(value)
    assert not w.available and "disabled" in w.disabled_reason


def test_pretrained_name_and_existing_custom_model_are_available(tmp_path):
    assert WakeWord("alexa").available
    model = tmp_path / "hey_helios.onnx"
    model.write_bytes(b"\x00")
    w = WakeWord(str(model))
    assert w.available and w.model_path == str(model)


def test_relative_custom_model_resolves_against_repo():
    from helios import conf
    w = WakeWord("data/wake/definitely_missing_model.onnx")
    assert w.model_path == str(conf.ROOT / "data/wake/definitely_missing_model.onnx")
    assert not w.available


def test_diagnostics_report_missing_wake_model_as_warning(tmp_path):
    from helios.voice import diagnostics
    status, detail = diagnostics.check_wake({"wake_word": str(tmp_path / "nope.onnx")})
    assert status == "warn" and "double-clap" in detail


def test_diagnostics_report_missing_kokoro_files(tmp_path, monkeypatch):
    from helios.voice import diagnostics
    monkeypatch.setattr(diagnostics.conf, "KOKORO_MODEL", tmp_path / "kokoro-v1.0.onnx")
    monkeypatch.setattr(diagnostics.conf, "KOKORO_VOICES", tmp_path / "voices-v1.0.bin")
    status, detail = diagnostics.check_tts({"tts_engine": "kokoro"})
    assert status == "fail" and "kokoro-v1.0.onnx" in detail


def test_clap_stands_in_for_missing_wake_word(tmp_path):
    from types import SimpleNamespace
    from helios.voice.daemon import VoiceDaemon
    missing = SimpleNamespace(clap_enabled=True, wake=WakeWord(str(tmp_path / "none.onnx")))
    assert VoiceDaemon._clap_is_wake(missing)
    model = tmp_path / "hey_helios.onnx"
    model.write_bytes(b"\x00")
    trained = SimpleNamespace(clap_enabled=True, wake=WakeWord(str(model)))
    assert not VoiceDaemon._clap_is_wake(trained)
    no_clap = SimpleNamespace(clap_enabled=False, wake=WakeWord(str(tmp_path / "none.onnx")))
    assert not VoiceDaemon._clap_is_wake(no_clap)


def test_talk_and_dictation_hotkeys_registered():
    import threading
    from types import SimpleNamespace
    from helios.voice.daemon import VoiceDaemon
    d = SimpleNamespace(cfg={"dictation_hotkey": "<ctrl>+<alt>+d"},
                        _dictation_req=threading.Event(), _talk_req=threading.Event())
    keys = VoiceDaemon._hotkey_map(d)
    assert set(keys) == {"<ctrl>+<alt>+d", "<ctrl>+<alt>+h"}
    keys["<ctrl>+<alt>+h"]()
    assert d._talk_req.is_set() and not d._dictation_req.is_set()
    d.cfg = {"talk_hotkey": ""}
    assert VoiceDaemon._hotkey_map(d) == {}


def test_double_clap_window_accepts_a_natural_pair():
    # 2026-09-30: the window was 1.5s (phase 2) but that let background noises pair up; the user
    # asked for less sensitivity, so it is now 1.0s — see tests/test_clap_sensitivity.py.
    from helios.voice.clap import ClapDetector
    c = ClapDetector(sensitivity=0.1)
    quiet = np.zeros(1280, dtype="int16")
    clap = np.zeros(1280, dtype="int16")
    clap[100:260] = 20000
    fired = False
    for frame in [quiet] * 10 + [clap] + [quiet] * 9 + [clap] + [quiet] * 3:    # ~0.8s apart
        fired = c.feed(frame) or fired
    assert fired


class _FakeVerifier:
    def __init__(self, enrolled=True, match=True, boom=False):
        self.enrolled, self.match, self.boom = enrolled, match, boom

    def is_enrolled(self):
        return self.enrolled

    def verify(self, audio):
        if self.boom:
            raise RuntimeError("model broke")
        return self.match, (0.82 if self.match else 0.41)


def _lock(on, verifier):
    from types import SimpleNamespace
    return SimpleNamespace(speaker_lock=on, _speaker=lambda: verifier, _warned_unenrolled=False)


@pytest.mark.parametrize("on,verifier,expected", [
    (False, _FakeVerifier(match=False), True),              # lock off: everyone
    (True, _FakeVerifier(enrolled=False, match=False), True),  # not enrolled yet: never lock out
    (True, _FakeVerifier(match=True), True),                # the owner
    (True, _FakeVerifier(match=False), False),              # someone else / the TV
    (True, _FakeVerifier(boom=True), False),                # verifier failure: fail closed
])
def test_voice_lock_decisions(on, verifier, expected):
    from helios.voice.daemon import VoiceDaemon
    assert VoiceDaemon._owner_voice(_lock(on, verifier), np.zeros(16000, "float32"), "command") is expected


def test_diagnostics_voice_lock_off_is_a_warning():
    from helios.voice import diagnostics
    status, detail = diagnostics.check_voice_lock({"speaker_verify": False})
    assert status == "warn" and "any voice" in detail


# ---------------------------------------------------------------- Antigravity warm-up
def test_warm_starts_session_and_skips_when_busy(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    import test_antigravity as ta
    script = tmp_path / "fake_agy.py"
    script.write_text(ta._FAKE_AGY, encoding="utf-8")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.json"))
    from helios import agy_cli
    base = tmp_path / "agy"
    for name, val in (("AGY_DIR", base), ("WORKSPACE", base / "workspace"),
                      ("RUNS_DIR", base / "runs"), ("MARKERS_DIR", base / "markers"),
                      ("SINK_FILE", base / "perm_sink.txt")):
        monkeypatch.setattr(agy_cli, name, val)
    monkeypatch.setitem(agy_cli.conf.SETTINGS, "antigravity", {"bin": str(script)})
    monkeypatch.setattr(agy_cli, "mcp_servers", lambda: {})
    import helios.agy_brain as ab
    monkeypatch.setattr(ab.agents, "orchestrator_brief", lambda: "")
    monkeypatch.setattr(ab.db, "get_state", lambda k: None)
    b = ab.AntigravityBrain(emit=lambda k, d: None)
    b._lock.acquire()
    b.warm()                                              # a turn is running: don't touch it
    assert b._session is None
    b._lock.release()
    b.warm()
    assert b._session is not None and b._session.alive()
    b.panic()
