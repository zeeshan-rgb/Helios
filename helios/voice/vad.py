"""Utterance endpointing via Silero VAD (the ONNX model + wrapper bundled with openWakeWord).

After the wake word fires we keep recording until the user stops talking. This class turns the
stream of captured frames into a simple "has the utterance ended?" signal: it waits for speech to
begin, then ends the clip once it sees `silence_ms` of trailing quiet.

The bundled Silero VAD (openwakeword.vad.VAD) is the v4 model: it wants int16 input and keeps
LSTM state across calls, so we reset_states() at the start of every utterance and feed it in
aligned 30ms (480-sample @ 16kHz) windows.
"""

from __future__ import annotations

import threading

import numpy as np

from .. import conf

_WIN = 480          # 30ms @ 16kHz — Silero v4's recommended frame
_MS_PER_WIN = 30


class Endpointer:
    """Streaming speech endpoint detector. Feed int16 frames; ask if the utterance has ended."""

    def __init__(self, silence_ms: int = 800, on_thresh: float = 0.5, off_thresh: float = 0.35):
        self.silence_ms = int(silence_ms)
        self.on_thresh = float(on_thresh)
        self.off_thresh = float(off_thresh)
        self._vad = None
        self._acc = np.zeros(0, dtype="int16")
        self.started = False
        self.speech_ms = 0
        self.silence_run_ms = 0
        self.score = 0.0
        self._load_lock = threading.Lock()

    def _ensure(self):
        if self._vad is None:
            with self._load_lock:
                if self._vad is None:
                    from openwakeword.vad import VAD
                    self._vad = VAD()
        return self._vad

    def warmup(self) -> None:
        try:
            self._ensure().predict(np.zeros(_WIN, dtype="int16"), frame_size=_WIN)
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"vad warmup failed: {e}")

    def reset(self) -> None:
        """Begin a fresh utterance."""
        try:
            self._ensure().reset_states()
        except Exception:
            pass
        self._acc = np.zeros(0, dtype="int16")
        self.started = False
        self.speech_ms = 0
        self.silence_run_ms = 0
        self.score = 0.0

    def feed(self, frame_int16) -> bool:
        """Add a captured int16 frame. Returns True once the utterance has ended (trailing silence
        after speech). Never ends before speech has started."""
        vad = self._ensure()
        self._acc = np.concatenate([self._acc, np.asarray(frame_int16, dtype="int16").reshape(-1)])
        ended = False
        while len(self._acc) >= _WIN:
            win, self._acc = self._acc[:_WIN], self._acc[_WIN:]
            try:
                self.score = float(vad.predict(win, frame_size=_WIN))
            except Exception:
                self.score = 0.0
            if self.score >= self.on_thresh:
                self.started = True
                self.speech_ms += _MS_PER_WIN
                self.silence_run_ms = 0
            elif self.score < self.off_thresh:
                if self.started:
                    self.silence_run_ms += _MS_PER_WIN
                    if self.silence_run_ms >= self.silence_ms:
                        ended = True
            # scores between off and on thresholds are treated as "holding" — neither speech nor
            # confirmed silence — so a brief dip mid-word doesn't prematurely end the utterance.
        return ended
