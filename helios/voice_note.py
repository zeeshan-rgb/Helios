"""Spoken briefings as audio files — Helios's own voice (the configured TTS engine, Kokoro by
default) rendered to OGG/Opus, the format Telegram plays as a voice message.

Runs offline on this PC; nothing but the finished file leaves it (and only to the owner's own
Telegram chat, via channels.telegram_send_voice). Text is scrubbed before it's spoken.
"""

from __future__ import annotations

import io
import re

_SENT = re.compile(r"(?<=[.!?])\s+")
MAX_CHARS = 3500                 # ~3–4 minutes of speech; longer reports stay on the dashboard


def _speaker():
    from . import conf
    from .voice.engines import build_speaker
    return build_speaker(conf.voice_cfg())


def render(text: str, *, synth=None) -> bytes:
    """Speak `text` into OGG/Opus bytes (24 kHz mono). `synth(sentence) -> float32 samples` is
    injectable for tests; by default it's the configured speaker's own synthesis step."""
    import numpy as np
    import soundfile as sf
    from .executive import scrub
    from .voice.tts import SR, _spoken
    t = _spoken(scrub(text))[:MAX_CHARS]
    if not t:
        raise ValueError("nothing to say")
    if synth is None:
        synth = _speaker()._synth
    gap = np.zeros(int(SR * 0.25), dtype=np.float32)
    parts = []
    for s in (x.strip() for x in _SENT.split(t)):
        if s:
            parts += [np.asarray(synth(s), dtype=np.float32).reshape(-1), gap]
    audio = np.clip(np.concatenate(parts), -1.0, 1.0)
    buf = io.BytesIO()
    sf.write(buf, audio, SR, format="OGG", subtype="OPUS")
    return buf.getvalue()
