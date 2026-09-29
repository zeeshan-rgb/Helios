"""Microphone capture: one 16kHz mono int16 input stream feeding the whole pipeline.

16kHz is exactly what openWakeWord, Silero VAD, and Whisper all want, so we capture there once
and never resample. The stream's callback drops fixed 1280-sample (80ms) frames into a queue the
daemon drains; wake/VAD/STT all consume from that single source.

`mute()` makes the callback discard frames — that's the half-duplex echo guard: while Helios is
speaking we throw mic audio away so it can't hear itself (and `flush()` clears the short tail
that lands between "playback done" and "mic re-armed").
"""

from __future__ import annotations

import queue

import numpy as np

from .. import conf

RATE = 16000
FRAME = 1280   # 80ms — openWakeWord's frame size; VAD re-chunks internally to 30ms


class Microphone:
    """Threaded mic capture into a frame queue. Single producer (PortAudio), single consumer."""

    def __init__(self, maxframes: int = 200):
        self._q: queue.Queue = queue.Queue(maxsize=maxframes)
        self._stream = None
        self._muted = False

    def _callback(self, indata, frames, time_info, status):  # PortAudio thread
        if self._muted:
            return
        try:
            self._q.put_nowait(np.array(indata[:, 0], dtype="int16"))
        except queue.Full:
            # Consumer fell behind (shouldn't on an 80ms cadence) — drop the oldest, keep newest.
            try:
                self._q.get_nowait()
                self._q.put_nowait(np.array(indata[:, 0], dtype="int16"))
            except queue.Empty:
                pass

    def start(self) -> bool:
        """Open the input stream. Returns False (and logs) if no usable mic — the daemon then
        runs degraded rather than crashing the whole app."""
        if self._stream is not None:
            return True
        try:
            import sounddevice as sd
            self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                          blocksize=FRAME, callback=self._callback)
            self._stream.start()
            conf.log("voice", f"microphone open ({RATE}Hz, {FRAME}-sample frames)")
            return True
        except Exception as e:
            conf.log("voice", f"microphone unavailable: {e}")
            self._stream = None
            return False

    def read(self, timeout: float = 0.5):
        """Next captured int16 frame, or None on timeout."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def mute(self, on: bool) -> None:
        self._muted = on
        if on:
            self.flush()

    def flush(self) -> None:
        """Drop any buffered frames (e.g. the echo tail after Helios finishes speaking)."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
