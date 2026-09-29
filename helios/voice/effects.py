"""Optional voice-character effects applied after synthesis ([voice].tts_pitch / tts_reverb).

numpy only (no new dependency). Designed for sentence-streamed playback: each sentence is processed
on its own, so the reverb tail is kept short to avoid audible gaps between sentences.

Pitch works with the synthesizer: a pitch factor p < 1 stretches the audio by 1/p (lower and
longer), so the synthesizer is asked to speak 1/p faster first — the net result is the same
speaking rate at a deeper pitch (see Speaker._synth).
"""

from __future__ import annotations

import numpy as np

_TAIL_S = 0.3          # reverb tail kept per sentence
_IR_S = 0.9            # impulse response length
_PREDELAY_S = 0.02


def stretch(x: np.ndarray, factor: float) -> np.ndarray:
    """Linear-interpolation resample to len(x)*factor samples (factor > 1 lowers the pitch)."""
    x = np.asarray(x, dtype="float32").reshape(-1)
    n = int(round(x.size * factor))
    if x.size == 0 or n <= 1 or abs(factor - 1.0) < 1e-6:
        return x
    src = np.linspace(0.0, 1.0, num=x.size, endpoint=False)
    dst = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(dst, src, x).astype("float32")


def _impulse(sr: int) -> np.ndarray:
    rng = np.random.default_rng(7)          # fixed seed: the voice sounds the same every time
    n = int(_IR_S * sr)
    t = np.arange(n, dtype="float32") / sr
    ir = rng.standard_normal(n).astype("float32") * np.exp(-t * 5.5)
    pre = int(_PREDELAY_S * sr)
    ir = np.concatenate([np.zeros(pre, dtype="float32"), ir])
    return ir / (np.sqrt(np.sum(ir ** 2)) + 1e-9)


def reverb(x: np.ndarray, sr: int, wet: float) -> np.ndarray:
    """Hall-style reverb via FFT convolution with a synthetic decaying-noise impulse."""
    x = np.asarray(x, dtype="float32").reshape(-1)
    wet = float(min(max(wet, 0.0), 1.0))
    if wet <= 0 or x.size == 0:
        return x
    ir = _impulse(sr)
    n = x.size + ir.size - 1
    size = 1 << (n - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:n].astype("float32")
    keep = x.size + int(_TAIL_S * sr)
    y = y[:keep]
    dry = np.concatenate([x, np.zeros(keep - x.size, dtype="float32")])
    # Fade the kept tail out so the cut isn't audible.
    fade = min(int(_TAIL_S * sr), keep)
    if fade > 0:
        y[-fade:] *= np.linspace(1.0, 0.0, fade, dtype="float32")
    out = (1.0 - wet * 0.5) * dry + wet * y * (np.max(np.abs(x)) / (np.max(np.abs(y)) + 1e-9))
    peak = float(np.max(np.abs(out)))
    return out / peak * 0.95 if peak > 0.95 else out


def apply(x: np.ndarray, sr: int, pitch: float = 1.0, reverb_wet: float = 0.0) -> np.ndarray:
    """Pitch (factor < 1 = deeper) then reverb. Identity when both are neutral."""
    y = np.asarray(x, dtype="float32").reshape(-1)
    if pitch and abs(pitch - 1.0) > 1e-3:
        y = stretch(y, 1.0 / pitch)
    if reverb_wet > 0:
        y = reverb(y, sr, reverb_wet)
    return y
