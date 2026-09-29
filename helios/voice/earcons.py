"""Tiny audio earcons — short, soft tones so the user gets feedback without looking at the screen.

Generated as numpy sine waves (with a soft second harmonic + attack/release envelope) and played
through sounddevice. Kept brief and quiet so they feel like a polished UI cue, not a beep. Played
when NOT speaking (wake fires before listening; "didn't catch that" after a failed transcribe), so
they don't collide with Kokoro TTS.
"""

from __future__ import annotations

import threading

import numpy as np

from .. import conf

SR = 24000


def _build(notes, vol: float = 0.22) -> np.ndarray:
    """notes = [(freq_hz, seconds), ...] played in sequence; returns a normalized float32 waveform."""
    segs = []
    for freq, dur in notes:
        n = max(1, int(SR * dur))
        t = np.arange(n) / SR
        w = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * freq * 2 * t)
        env = np.ones(n)
        a, r = int(SR * 0.008), int(SR * 0.035)
        if a:
            env[:a] = np.linspace(0, 1, a)
        if r:
            env[-r:] = np.linspace(1, 0, r)
        segs.append(w * env)
    sig = np.concatenate(segs).astype("float32")
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig * (vol / peak)).astype("float32")


# A bright two-note rise = "I'm listening"; a soft two-note fall = "didn't catch that".
_WAKE = _build([(660, 0.07), (988, 0.10)], vol=0.22)
_MISS = _build([(520, 0.08), (392, 0.13)], vol=0.16)


def _play(sig: np.ndarray) -> None:
    def run():
        try:
            import sounddevice as sd
            sd.play(sig, SR)
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"earcon play failed: {e}")
    threading.Thread(target=run, daemon=True).start()


def wake() -> None:
    """Play the wake chime (just after the wake word fires)."""
    _play(_WAKE)


def miss() -> None:
    """Play the 'didn't catch that' chime (after a failed/empty transcription)."""
    _play(_MISS)
