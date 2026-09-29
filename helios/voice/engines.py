"""Pluggable TTS/STT engine selection for the voice daemon.

Kokoro (TTS) + faster-whisper (STT) remain the local, no-cloud default. This module adds cloud
engines (OpenAI, Groq) behind the SAME Speaker/Transcriber interfaces by overriding only the one
engine-specific step each class exposes:
  - Speaker._synth(text) -> float32 @ 24kHz   (all playback/sentence-streaming stays in tts.py)
  - Transcriber._transcribe_core(audio) -> [segment]  (all junk/confidence gating stays in stt.py)

Cloud audio is decoded (soundfile, already a dep) and resampled to the fixed 24kHz playback rate;
cloud STT loses faster-whisper's per-segment confidence, so a neutral pseudo-segment is returned
and the engine-agnostic junk-list + min-size gate still apply.

Selection: [voice].tts_engine (kokoro|openai|groq) and [voice].stt_engine (faster-whisper|openai|
groq). Provider keys come from config/secrets.toml [<provider>] via helios.llm.make_client.
"""

from __future__ import annotations

import io

import numpy as np

from .. import conf, llm
from .stt import Transcriber
from .tts import SR, Speaker

# Sensible cloud defaults (overridable via [voice].tts_<provider>_model / _voice / stt_<provider>_model).
_TTS_DEFAULTS = {
    "openai": {"model": "tts-1", "voice": "alloy"},
    "groq": {"model": "playai-tts", "voice": "Fritz-PlayAI"},
}
_STT_DEFAULTS = {
    "openai": {"model": "whisper-1"},
    "groq": {"model": "whisper-large-v3-turbo"},
}


# --------------------------------------------------------------------------- audio utils
def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear-resample a 1-D float32 signal from `src` Hz to `dst` Hz (numpy only — no new dep)."""
    x = np.asarray(x, dtype="float32").reshape(-1)
    if src == dst or x.size == 0:
        return x
    n = int(round(x.size * dst / src))
    if n <= 0:
        return x
    xp = np.linspace(0.0, 1.0, num=x.size, endpoint=False)
    xnew = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(xnew, xp, x).astype("float32")


def _decode_to_24k(raw: bytes) -> np.ndarray:
    """Decode cloud TTS audio bytes (request WAV) -> mono float32 @ 24kHz for the output stream."""
    import soundfile as sf
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    data = np.asarray(data, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)   # downmix to mono
    return _resample(data, sr, SR)


def _encode_wav(audio: np.ndarray, sr: int) -> bytes:
    """Encode a float32 mono clip to 16-bit PCM WAV bytes for cloud STT upload."""
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, np.asarray(audio, dtype="float32").reshape(-1), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class _Seg:
    """A pseudo-segment for cloud STT (which doesn't report per-segment confidence)."""

    def __init__(self, text: str):
        self.text = text
        self.no_speech_prob = 0.0
        self.avg_logprob = 0.0


# --------------------------------------------------------------------------- cloud TTS
class CloudSpeaker(Speaker):
    """OpenAI/Groq TTS over the OpenAI-compatible audio.speech endpoint. Reuses all of Speaker's
    streaming playback; only synthesis is swapped."""

    def __init__(self, provider: str, cfg: dict):
        d = _TTS_DEFAULTS.get(provider, _TTS_DEFAULTS["openai"])
        voice = cfg.get(f"tts_{provider}_voice") or d["voice"]
        super().__init__(voice=voice, speed=float(cfg.get("tts_speed", 1.0)),
                         lang=cfg.get("tts_lang", "en-us"))
        self.provider = provider
        self.model = cfg.get(f"tts_{provider}_model") or d["model"]
        self._client = None

    def _client_(self):
        if self._client is None:
            self._client = llm.make_client(self.provider)
        return self._client

    def _ensure_model(self):
        return self._client_()

    def warmup(self) -> None:
        try:
            self._ensure_stream()   # don't spend an API call on boot; first reply validates creds
            conf.log("voice", f"{self.provider} tts ready")
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"tts warmup failed: {e}")

    def _synth(self, text: str):
        client = self._client_()
        resp = client.audio.speech.create(model=self.model, voice=self.voice,
                                          input=text, response_format="wav")
        raw = resp.read() if hasattr(resp, "read") else getattr(resp, "content", b"")
        return _decode_to_24k(raw)


# --------------------------------------------------------------------------- cloud STT
class CloudTranscriber(Transcriber):
    """OpenAI/Groq STT over the OpenAI-compatible audio.transcriptions endpoint. Reuses Transcriber's
    junk/confidence gating; only the recognition step is swapped."""

    def __init__(self, provider: str, cfg: dict):
        d = _STT_DEFAULTS.get(provider, _STT_DEFAULTS["openai"])
        super().__init__(model=cfg.get(f"stt_{provider}_model") or d["model"])
        self.provider = provider
        self._client = None

    def _client_(self):
        if self._client is None:
            self._client = llm.make_client(self.provider)
        return self._client

    def warmup(self) -> None:
        conf.log("voice", f"{self.provider} stt ready")  # nothing to preload

    def _transcribe_core(self, audio):
        client = self._client_()
        f = io.BytesIO(_encode_wav(audio, 16000))
        f.name = "audio.wav"   # the SDK uses the filename suffix to pick the content type
        resp = client.audio.transcriptions.create(model=self.model_name, file=f, language="en")
        return [_Seg(getattr(resp, "text", "") or "")]


# --------------------------------------------------------------------------- factories
def build_speaker(cfg: dict) -> Speaker:
    """Construct the configured TTS engine (default: local Kokoro)."""
    engine = str(cfg.get("tts_engine", "kokoro")).strip().lower()
    if engine in ("openai", "groq"):
        return CloudSpeaker(engine, cfg)
    return Speaker(cfg.get("tts_voice", "bm_george"),
                   float(cfg.get("tts_speed", 1.0)), cfg.get("tts_lang", "en-gb"),
                   pitch=float(cfg.get("tts_pitch", 1.0)),
                   reverb=float(cfg.get("tts_reverb", 0.0)))


def build_transcriber(cfg: dict) -> Transcriber:
    """Construct the configured STT engine (default: local faster-whisper)."""
    engine = str(cfg.get("stt_engine", "faster-whisper")).strip().lower()
    if engine in ("openai", "groq"):
        return CloudTranscriber(engine, cfg)
    return Transcriber(cfg.get("stt_model", "base.en"),
                       initial_prompt=cfg.get("stt_prompt", ""))
