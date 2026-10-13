"""Phase 7 — speech-to-text: [voice.stt] config (old flat keys still work), CPU-first device auto
with a safe GPU fallback, models in the Helios cache on D:, English-only models never asked to
detect languages, timestamps + language detection, automatic vocabulary (project names), mic level
diagnostics, hardware-based defaults, clean shutdown. Fake models / fake audio only."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from helios import hardware
from helios.voice import diagnostics, engines, stt
from helios.voice.stt import Transcriber

_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ settings

def test_settings_merge_and_defaults():
    flat = {"stt_engine": "faster-whisper", "stt_model": "tiny.en", "stt_prompt": "Helios"}
    s = stt.stt_settings(flat)
    assert s["model"] == "tiny.en" and s["device"] == "auto" and s["beam_size"] == 1 and s["prompt"] == "Helios"
    s = stt.stt_settings({**flat, "stt": {"model": "base.en", "language": "auto", "device": "cpu"}})
    assert s["model"] == "base.en" and s["language"] == "auto" and s["device"] == "cpu"    # [voice.stt] wins
    assert stt.stt_settings({"stt_engine": "OpenAI"})["provider"] == "openai"


def test_device_auto(monkeypatch):
    import ctranslate2
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    assert stt.resolve_device("auto", "auto") == ("cpu", "int8")
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    assert stt.resolve_device("auto", "auto") == ("cuda", "float16")
    assert stt.resolve_device("cpu", "int8_float32") == ("cpu", "int8_float32")


def test_model_root_prefers_setting_then_hf_home(monkeypatch, tmp_path):
    monkeypatch.setattr(stt.conf, "voice_cfg", lambda: {"stt": {"download_root": "E:/models"}})
    assert stt.model_root() == "E:/models"
    monkeypatch.setattr(stt.conf, "voice_cfg", lambda: {})
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert stt.model_root() == str(tmp_path / "hf" / "hub")
    monkeypatch.delenv("HF_HOME")
    data = tmp_path / "Helios" / "data"
    (tmp_path / "Helios" / "cache" / "hf").mkdir(parents=True)
    data.mkdir(parents=True)
    monkeypatch.setattr(stt.conf, "DATA_DIR", data)
    assert stt.model_root() == str(tmp_path / "Helios" / "cache" / "hf" / "hub")


# ------------------------------------------------------------------ transcriber

class FakeModel:
    def __init__(self, name, **kw):
        self.name, self.kw, self.calls = name, kw, []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        word = SimpleNamespace(start=0.0, end=0.4, word=" Open", probability=0.93)
        segs = [SimpleNamespace(start=0.0, end=1.2, text=" Open Maqsusi.", words=[word],
                                no_speech_prob=0.05, avg_logprob=-0.2)]
        return iter(segs), SimpleNamespace(language="en", language_probability=0.98)


def loader_log():
    made = []

    def loader(name, **kw):
        if kw.get("device") == "cuda" and loader.fail_gpu:
            raise RuntimeError("CUDA driver version is insufficient")
        m = FakeModel(name, **kw)
        made.append(m)
        return m
    loader.fail_gpu, loader.made = False, made
    return loader


def test_loads_with_cache_on_d_and_settings(monkeypatch):
    monkeypatch.setattr(stt, "model_root", lambda: "D:/Helios/cache/hf/hub")
    ld = loader_log()
    tr = Transcriber("base.en", "Helios, Maqsusi", device="cpu", compute_type="int8", beam_size=2,
                     cpu_threads=3, loader=ld)
    assert tr.transcribe(np.zeros(16000, "float32")) == "Open Maqsusi."
    kw = ld.made[0].kw
    assert kw == {"device": "cpu", "compute_type": "int8", "download_root": "D:/Helios/cache/hf/hub",
                  "cpu_threads": 3, "local_files_only": True}            # no hub round-trip when cached
    call = ld.made[0].calls[-1]
    assert call["language"] == "en" and call["beam_size"] == 2 and call["initial_prompt"] == "Helios, Maqsusi"


def test_gpu_failure_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(stt, "model_root", lambda: None)
    ld = loader_log()
    ld.fail_gpu = True
    tr = Transcriber("small.en", device="cuda", compute_type="auto", loader=ld)
    tr._ensure()
    assert (tr.device, tr.compute_type) == ("cpu", "int8") and ld.made[0].kw["device"] == "cpu"


def test_english_only_models_never_detect_languages():
    assert Transcriber("base.en", language="auto").language == "en"
    assert Transcriber("base.en", language="hi").language == "en"
    assert Transcriber("small", language="auto").language is None             # detect
    assert Transcriber("small", language="hi").language == "hi"


def test_timestamps_language_and_close(monkeypatch):
    monkeypatch.setattr(stt, "model_root", lambda: None)
    ld = loader_log()
    tr = Transcriber("small", language="auto", loader=ld)
    out = tr.transcribe_detailed(np.zeros(16000, "float32"))
    assert out["text"] == "Open Maqsusi." and out["language"] == "en" and out["language_probability"] == 0.98
    assert out["segments"][0]["start"] == 0.0 and out["segments"][0]["words"][0]["word"] == "Open"
    assert ld.made[0].calls[-1]["word_timestamps"] is True and ld.made[0].calls[-1]["language"] is None
    assert tr.last_language == ("en", 0.98)
    tr.close()
    assert tr._model is None


def test_gate_still_works_with_new_engine(monkeypatch):
    monkeypatch.setattr(stt, "model_root", lambda: None)

    class Junk(FakeModel):
        def transcribe(self, audio, **kw):
            return iter([SimpleNamespace(start=0, end=1, text=" Thank you.", no_speech_prob=0.9, avg_logprob=-2.0)]), None
    tr = Transcriber("base.en", loader=lambda n, **k: Junk(n, **k))
    assert tr.transcribe(np.zeros(16000, "float32")) == ""
    assert tr.transcribe(np.zeros(16000, "float32"), gated=False) == "Thank you."


# ------------------------------------------------------------------ vocabulary

def test_vocabulary_prompt_adds_project_names_dedupes_and_caps():
    s = {"prompt": "Helios, Kokoro, Spotify", "vocabulary": ["LiveDoc", "helios"]}
    p = stt.vocabulary_prompt(s, project_names=["Maqsusi", "Napino Man.3", "Aegis", "Maqsusi"])
    terms = p.split(", ")
    assert terms[:3] == ["Helios", "Kokoro", "Spotify"] and "Polarion" in terms and "LiveDoc" in terms
    assert "Maqsusi" in terms and "Napino Man" in terms and terms.count("Maqsusi") == 1
    assert sum(t.lower() == "helios" for t in terms) == 1
    many = stt.vocabulary_prompt({"prompt": ""}, project_names=[f"Proj{i}" for i in range(100)])
    assert len(many.split(", ")) == stt._MAX_VOCAB_TERMS


def test_build_transcriber_uses_settings_and_vocabulary(monkeypatch):
    monkeypatch.setattr(stt, "vocabulary_prompt", lambda s: "Helios, Maqsusi")
    tr = engines.build_transcriber({"stt": {"model": "tiny.en", "device": "cpu", "beam_size": 3}})
    assert isinstance(tr, Transcriber) and tr.model_name == "tiny.en" and tr.beam_size == 3
    assert tr.initial_prompt == "Helios, Maqsusi"


def test_live_text_model_gets_the_same_vocabulary(monkeypatch):
    from helios.voice import realtime_stt
    monkeypatch.setattr(realtime_stt.PartialExecutor, "warmup", lambda self: None)
    rt = realtime_stt.RealtimeCapture(SimpleNamespace(initial_prompt="Helios, Maqsusi"), {}, None,
                                      recorder_cls=lambda **kw: SimpleNamespace(**kw))
    assert rt.partial.prompt == "Helios, Maqsusi"


# ------------------------------------------------------------------ mic diagnostics

def tone(db, n=48000, noise_db=-70, clip=False):
    rng = np.random.default_rng(1)
    amp = 10 ** (db / 20) * 32768 * 1.414
    x = amp * np.sin(np.linspace(0, 2 * np.pi * 220 * n / 16000, n))
    x[: n // 4] = 0                                                   # some pauses for the floor
    x = x + rng.normal(0, 10 ** (noise_db / 20) * 32768, n)
    if clip:
        x *= 50
    return np.clip(x, -32768, 32767).astype("int16")


@pytest.mark.parametrize("samples,status,word", [
    (np.zeros(48000, "int16"), "fail", "no sound"),
    (tone(-20, clip=True), "warn", "clipping"),
    (tone(-20, noise_db=-25), "warn", "noisy"),
    (tone(-45), "warn", "very quiet"),
    (tone(-18), "ok", "level"),
])
def test_mic_verdicts(samples, status, word):
    st, detail = diagnostics.mic_verdict(diagnostics.analyse_mic(samples))
    assert st == status and word in detail


def test_check_mic_with_fake_recorder_and_failure():
    assert diagnostics.check_mic(1, recorder=lambda s: tone(-18, n=16000))[0] == "ok"

    def broken(s):
        raise OSError("device unavailable")
    st, detail = diagnostics.check_mic(1, recorder=broken)
    assert st == "fail" and "device unavailable" in detail


def test_mic_test_only_when_asked(monkeypatch):
    called = []
    monkeypatch.setattr(diagnostics, "check_mic", lambda *a, **k: (called.append(1), ("ok", "x"))[1])
    for name in ("check_tts", "check_stt", "check_capture", "check_wake", "check_voice_lock", "check_hardware"):
        monkeypatch.setattr(diagnostics, name, lambda *a, **k: ("ok", "stub"))
    monkeypatch.setattr(diagnostics, "_devices", lambda: ("ok", "mic: x", "speaker: y"))
    names = [n for n, _s, _d in diagnostics.run_checks()]
    assert "mic levels" not in names and not called and "hardware" in names
    names = [n for n, _s, _d in diagnostics.run_checks(mic=True)]
    assert "mic levels" in names and called


# ------------------------------------------------------------------ hardware

def test_hardware_recommendations():
    cpu_only = {"cuda_devices": 0, "gpus": [{"name": "Intel UHD 620", "vram_gb": 1.0}], "disks": {"C": 12, "D": 54}}
    r = hardware.recommend(cpu_only)
    assert r == {"stt_device": "cpu", "stt_compute_type": "int8", "stt_model": "base.en",
                 "visual_parser": "off (no CUDA GPU with 6 GB+)", "model_disk": "D"}
    r = hardware.recommend({"cuda_devices": 1, "gpus": [{"name": "RTX 4070", "vram_gb": 12}], "disks": {"C": 50}})
    assert r["stt_device"] == "cuda" and r["stt_model"] == "small.en" and r["visual_parser"] == "available"
    assert r["model_disk"] == "C"


def test_hardware_summary_format():
    info = {"cpu": "Test CPU", "cores": 4, "threads": 8, "ram_gb": 16.0, "gpus": [], "cuda_devices": 0,
            "disks": {"D": 50.0}, "browsers": ["edge"], "mic": "Realtek"}
    info["recommended"] = hardware.recommend(info)
    out = hardware.summary(info)
    assert "Test CPU (4 cores / 8 threads)" in out and "GPU: none found" in out and "Mic: Realtek" in out
    assert "speech base.en on cpu (int8)" in out


# ------------------------------------------------------------------ shutdown + benchmark tool

def test_daemon_releases_the_speech_model_on_close():
    src = (_ROOT / "helios" / "voice" / "daemon.py").read_text(encoding="utf-8")
    close = src[src.index("    def close(self):"):]
    assert 'getattr(self.stt, "close"' in close


def test_benchmark_word_error_rate():
    spec = importlib.util.spec_from_file_location("bench_stt", _ROOT / "tools" / "bench_stt.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    assert b.wer("Check my Maqsusi project", "check my Maqsusi project.") == 0
    assert b.wer("Check my Maqsusi project", "Check my Max Sousie project") == 0.5
    assert b.wer("call at five thirty", "call at 5:30") == 0


def test_downloads_only_when_not_cached():
    calls = []

    def loader(name, **kw):
        calls.append(kw.get("local_files_only", False))
        if kw.get("local_files_only"):
            raise FileNotFoundError("not in cache")
        return FakeModel(name, **kw)
    m = stt.load_whisper(loader, "small.en", device="cpu")
    assert calls == [True, False] and m.name == "small.en"
