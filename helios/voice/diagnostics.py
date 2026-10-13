"""Voice diagnostics (`helios voice-check`): is every piece of the voice pipeline ready?

Each check returns (name, status, detail) with status "ok" | "warn" | "fail". Checks load the real
engines (so they take a few seconds) but never start the voice daemon or touch the running app.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .. import conf


def _devices() -> tuple[str, str, str]:
    try:
        import sounddevice as sd
        ins = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
        outs = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
        if not ins:
            return "fail", "no microphone found", ""
        if not outs:
            return "fail", "no speaker found", ""
        din, dout = sd.default.device
        return "ok", f"mic: {sd.query_devices(din)['name']}", f"speaker: {sd.query_devices(dout)['name']}"
    except Exception as e:
        return "fail", f"audio device query failed: {e}", ""


def check_tts(cfg: dict) -> tuple[str, str]:
    engine = str(cfg.get("tts_engine", "kokoro")).lower()
    if engine != "kokoro":
        return "ok", f"{engine} (cloud) — not tested offline"
    missing = [p.name for p in (conf.KOKORO_MODEL, conf.KOKORO_VOICES) if not p.exists()]
    if missing:
        return "fail", f"Kokoro model files missing in {conf.VOICE_DIR}: {', '.join(missing)}"
    try:
        from .engines import build_speaker
        spk = build_speaker(cfg)
        t0 = time.time()
        audio = spk._synth("Helios voice check.")
        took = time.time() - t0
        spk.close()
        secs = len(audio) / 24000
        return "ok", (f"Kokoro voice={cfg.get('tts_voice', 'bm_george')} speed={cfg.get('tts_speed', 1)} "
                      f"pitch={cfg.get('tts_pitch', 1.0)} reverb={cfg.get('tts_reverb', 0.0)} — "
                      f"{secs:.1f}s of audio in {took:.1f}s")
    except Exception as e:
        return "fail", f"Kokoro failed: {e}"


def check_stt(cfg: dict) -> tuple[str, str]:
    from .stt import Transcriber, model_root, stt_settings
    s = stt_settings(cfg)
    if s["provider"] != "faster-whisper":
        return "ok", f"{s['provider']} (cloud) — not tested offline"
    try:
        import numpy as np
        t0 = time.time()
        tr = Transcriber(str(s["model"]), device=str(s["device"]), compute_type=str(s["compute_type"]),
                         language=str(s["language"]), beam_size=int(s["beam_size"]))
        tr._ensure()
        load = time.time() - t0
        t0 = time.time()
        tr._run(np.zeros(32000, dtype="float32"))
        root = model_root() or "default Hugging Face cache"
        lang = tr.language or "auto-detect"
        detail = (f"faster-whisper {s['model']} on {tr.device} ({tr.compute_type}), language {lang}, "
                  f"beam {tr.beam_size} — loaded {load:.1f}s, 2 s of audio in {time.time() - t0:.2f}s "
                  f"(models: {root})")
        tr.close()
        on_c = str(root).upper().startswith("C:")
        return ("warn", detail + " — models are on C:, set HF_HOME to a D: folder") if on_c else ("ok", detail)
    except Exception as e:
        return "fail", f"faster-whisper failed: {e}"


def analyse_mic(samples, rate: int = 16000) -> dict:
    """Level / peak / clipping / noise floor of a mono int16 recording."""
    import numpy as np
    x = np.asarray(samples, dtype="float32").reshape(-1) / 32768.0
    if x.size == 0:
        return {"rms_db": -120.0, "peak_db": -120.0, "clip_pct": 0.0, "floor_db": -120.0}
    db = lambda v: float(20 * np.log10(max(float(v), 1e-6)))  # noqa: E731
    frame = max(1, rate // 20)                                # 50 ms frames
    n = x.size // frame
    frames = x[: n * frame].reshape(n, frame) if n else x.reshape(1, -1)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return {"rms_db": round(db(np.sqrt(np.mean(x ** 2))), 1), "peak_db": round(db(np.max(np.abs(x))), 1),
            "clip_pct": round(float(np.mean(np.abs(x) >= 0.999)) * 100, 3),
            "floor_db": round(db(np.percentile(frame_rms, 10)), 1)}


def mic_verdict(m: dict) -> tuple[str, str]:
    detail = (f"level {m['rms_db']} dBFS, peak {m['peak_db']} dBFS, noise floor {m['floor_db']} dBFS, "
              f"clipping {m['clip_pct']}%")
    if m["peak_db"] < -60:
        return "fail", f"no sound at all ({detail}) — mic muted, blocked in Windows privacy settings, or wrong device"
    if m["clip_pct"] > 0.1:
        return "warn", f"clipping ({detail}) — lower the mic level in Windows sound settings"
    if m["floor_db"] > -35:
        return "warn", f"noisy room or hum ({detail}) — speech will be harder to pick out"
    if m["peak_db"] < -30:
        return "warn", f"very quiet ({detail}) — raise the mic level or speak closer (did you speak during the test?)"
    return "ok", detail


def check_mic(seconds: float = 3.0, recorder=None) -> tuple[str, str]:
    """Record a few seconds from the default mic (nothing is saved) and judge the levels."""
    try:
        if recorder is None:
            import sounddevice as sd
            rec = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="int16")
            sd.wait()
            samples = rec[:, 0]
        else:
            samples = recorder(seconds)
    except Exception as e:
        return "fail", f"couldn't record from the microphone: {e}"
    return mic_verdict(analyse_mic(samples))


def check_hardware() -> tuple[str, str]:
    try:
        from .. import hardware
        info = hardware.detect(quick=True)
        r = info["recommended"]
        gpu = info["gpus"][0]["name"] if info["gpus"] else "no GPU found"
        return "ok", (f"{info['cpu']}, {info['ram_gb']} GB RAM, {gpu}, CUDA {info['cuda_devices']} — "
                      f"suggested speech model {r['stt_model']} on {r['stt_device']}")
    except Exception as e:
        return "warn", f"hardware detection failed: {e}"


def check_capture(cfg: dict) -> tuple[str, str]:
    engine = str(cfg.get("capture_engine", "builtin")).strip().lower()
    if engine != "realtimestt":
        return "ok", "built-in (Silero endpointing)"
    from .realtime_stt import available
    ok, why = available()
    if not ok:
        return "warn", f"realtimestt selected but {why} — the built-in capture is used instead"
    try:
        from .realtime_stt import PartialExecutor
        t0 = time.time()
        p = PartialExecutor(str(cfg.get("rt_partial_model", "tiny.en")),
                            int(cfg.get("rt_partial_threads", 2)))
        p.warmup()
        return "ok", (f"RealtimeSTT — early transcription + live text "
                      f"({p.model_name} loaded in {time.time() - t0:.1f}s)")
    except Exception as e:
        return "warn", f"RealtimeSTT live-text model failed ({e}) — built-in capture used"


def check_wake(cfg: dict) -> tuple[str, str]:
    from .wake import WakeWord
    w = WakeWord(cfg.get("wake_word", "hey_jarvis"), float(cfg.get("wake_threshold", 0.5)))
    if not w.available:
        return "warn", (f"{w.disabled_reason} — until then, a double-clap wakes Helios and starts "
                        "listening")
    try:
        import numpy as np
        from .wake import FRAME
        w._ensure().predict(np.zeros(FRAME, dtype="int16"))
        return "ok", f"wake word '{w.model_key}' loaded (threshold {w.threshold})"
    except Exception as e:
        return "fail", f"wake word failed: {w.disabled_reason or e}"


def check_voice_lock(cfg: dict) -> tuple[str, str]:
    from .speaker_verify import SpeakerVerifier
    v = SpeakerVerifier(threshold=float(cfg.get("speaker_verify_threshold", 0.70)))
    enrolled = v.is_enrolled()
    try:
        import resemblyzer  # noqa: F401
        engine = True
    except Exception:
        engine = False
    if not cfg.get("speaker_verify"):
        hint = "say 'enroll my voice' after the talk hotkey" if engine else "install resemblyzer first"
        return "warn", f"off — Helios responds to any voice ({hint})"
    if not engine:
        return "fail", "on, but resemblyzer isn't installed — voice commands will be rejected"
    if not enrolled:
        return "warn", "on, but no voiceprint yet — accepting all voices until you enroll"
    return "ok", f"on — only your enrolled voice is accepted (threshold {v.threshold:.2f})"


def run_checks(*, mic: bool = False) -> list[tuple[str, str, str]]:
    cfg = conf.voice_cfg()
    results: list[tuple[str, str, str]] = []
    results.append(("voice enabled", "ok" if cfg.get("enabled") else "warn",
                    "yes" if cfg.get("enabled") else "no — set [voice].enabled = true"))
    results.append(("hardware", *check_hardware()))
    st, a, b = _devices()
    results.append(("audio devices", st, f"{a}; {b}".strip("; ")))
    if mic and st == "ok":
        results.append(("mic levels", *check_mic()))
    results.append(("text-to-speech", *check_tts(cfg)))
    results.append(("speech-to-text", *check_stt(cfg)))
    results.append(("speech capture", *check_capture(cfg)))
    results.append(("wake word", *check_wake(cfg)))
    results.append(("voice lock", *check_voice_lock(cfg)))
    s = conf.startup_cfg()
    results.append(("other controls", "ok",
                    f"talk to Helios={cfg.get('talk_hotkey', '<ctrl>+<alt>+h')}, "
                    f"wake gesture={s.get('wake_gesture', 'none')}, dashboard="
                    f"{conf.SETTINGS.get('hotkeys', {}).get('summon', '?')}, dictation into the "
                    f"focused field={cfg.get('dictation_hotkey', 'off')}"))
    return results


def speak_test(text: str = "Hello. I am Helios. Voice check complete.") -> None:
    """Play a line through the configured voice on the default speaker (blocks until done)."""
    from .engines import build_speaker
    spk = build_speaker(conf.voice_cfg())
    spk.speak(text)
    time.sleep(0.3)
    while spk.is_speaking():
        time.sleep(0.1)
    spk.close()
