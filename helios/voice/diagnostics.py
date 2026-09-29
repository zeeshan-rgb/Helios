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
    engine = str(cfg.get("stt_engine", "faster-whisper")).lower()
    if engine != "faster-whisper":
        return "ok", f"{engine} (cloud) — not tested offline"
    try:
        from faster_whisper import WhisperModel
        t0 = time.time()
        WhisperModel(cfg.get("stt_model", "base.en"), device="cpu", compute_type="int8")
        where = os.environ.get("HF_HOME") or "~/.cache/huggingface"
        return "ok", f"faster-whisper {cfg.get('stt_model', 'base.en')} loaded in {time.time()-t0:.1f}s (cache: {where})"
    except Exception as e:
        return "fail", f"faster-whisper failed: {e}"


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


def run_checks() -> list[tuple[str, str, str]]:
    cfg = conf.voice_cfg()
    results: list[tuple[str, str, str]] = []
    results.append(("voice enabled", "ok" if cfg.get("enabled") else "warn",
                    "yes" if cfg.get("enabled") else "no — set [voice].enabled = true"))
    st, a, b = _devices()
    results.append(("audio devices", st, f"{a}; {b}".strip("; ")))
    results.append(("text-to-speech", *check_tts(cfg)))
    results.append(("speech-to-text", *check_stt(cfg)))
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
