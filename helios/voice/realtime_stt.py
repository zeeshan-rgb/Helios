"""RealtimeSTT capture engine ([voice] capture_engine = "realtimestt").

KoljaB/RealtimeSTT does the parts of listening that decide how fast Helios answers:
  * endpointing — WebRTC VAD for a fast start, Silero to confirm the end;
  * early transcription — the final transcription starts during the pause, before the end of
    speech is confirmed (thrown away if you keep talking), so the text is ready sooner;
  * live text — a small realtime model (tiny.en) re-transcribes the utterance every ~0.3 s.

Helios keeps everything else. The daemon still owns the ONE microphone stream and feeds it to
RealtimeSTT only while listening (use_microphone=False), so the double-clap, wake word, mute mode,
echo guard and voice lock work exactly as before. Both transcription steps are Helios's own
executors: the final text comes from Helios's already-loaded Transcriber (one copy of the model,
same junk/confidence gate), the live text from a tiny.en model capped at 2 CPU threads. So
RealtimeSTT starts no extra processes and no second microphone reader.

If RealtimeSTT isn't installed or fails to start, the daemon falls back to the built-in capture
(voice/vad.py) with a log line — voice never breaks because of it.
Install (deliberately --no-deps; the rest is already in the venv):
    pip install --no-deps RealtimeSTT==1.1.2 "silero-vad>=6.2.1"
    pip install halo==0.0.31
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from .. import conf

_PCM_SCALE = 32768.0


def available() -> tuple[bool, str]:
    """Can the RealtimeSTT engine run here? (False, why) when a piece is missing."""
    try:
        import RealtimeSTT  # noqa: F401
        from RealtimeSTT.core.silero_vad import find_silero_model_file
        import halo  # noqa: F401
        import webrtcvad  # noqa: F401
    except Exception as e:
        return False, f"RealtimeSTT not installed ({e.__class__.__name__}: {e})"
    try:
        if not find_silero_model_file("silero_vad.onnx"):
            return False, "Silero VAD model missing (pip install --no-deps silero-vad)"
    except Exception as e:
        return False, f"Silero VAD model check failed ({e})"
    return True, ""


class _ToVoiceLog(logging.Handler):
    """RealtimeSTT logs to a console the windowless voice daemon doesn't have — forward its
    warnings and errors to logs/voice.log instead."""

    def emit(self, record):
        try:
            conf.log("voice", f"RealtimeSTT {record.levelname.lower()}: {record.getMessage()[:300]}")
        except Exception:
            pass


def _route_library_log() -> None:
    lg = logging.getLogger("realtimestt")
    if not any(isinstance(h, _ToVoiceLog) for h in lg.handlers):
        lg.addHandler(_ToVoiceLog(level=logging.WARNING))


def _f32(audio) -> np.ndarray:
    a = np.asarray(audio).reshape(-1)
    if a.dtype != np.float32:
        a = a.astype("float32") / (_PCM_SCALE if np.issubdtype(a.dtype, np.integer) else 1.0)
    return a


def _result(text: str):
    from RealtimeSTT.transcription_engines.base import TranscriptionResult
    return TranscriptionResult(text=text or "")


class FinalExecutor:
    """RealtimeSTT's final transcription -> Helios's own Transcriber (shared model, same gate).
    Remembers the latest (time, audio, gated, raw) so the daemon gets the exact audio the text
    came from (for the voice lock) and both gated and raw text from ONE model run."""

    def __init__(self, stt, lock: threading.Lock):
        self.stt = stt
        self.lock = lock
        self._mu = threading.Lock()
        self.capture_id = 0     # set by RealtimeCapture.begin(); a run from an older capture is stale
        self.last: tuple[int, np.ndarray, str, str] | None = None

    def transcribe(self, audio, language=None, use_prompt=True, **_kw):
        cid = self.capture_id
        a = _f32(audio)
        with self.lock:
            gated, raw = self.stt.transcribe_both(a)
        with self._mu:
            self.last = (cid, a, gated, raw)
        return _result(raw)

    def result_for(self, capture_id: int):
        with self._mu:
            return self.last if self.last and self.last[0] == capture_id else None


class PartialExecutor:
    """Live text: a small faster-whisper model with a CPU-thread cap, greedy decoding."""

    def __init__(self, model: str = "tiny.en", threads: int = 2):
        self.model_name = model
        self.threads = max(1, int(threads))
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel
                    self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                               cpu_threads=self.threads)
                    conf.log("voice", f"realtime partial model '{self.model_name}' loaded")
        return self._model

    def warmup(self) -> None:
        self.transcribe(np.zeros(8000, dtype="float32"))

    def transcribe(self, audio, language=None, use_prompt=True, **_kw):
        a = _f32(audio)
        if a.size < 1600:
            return _result("")
        segments, _ = self._ensure().transcribe(a, language="en", beam_size=1,
                                                condition_on_previous_text=False,
                                                without_timestamps=True, vad_filter=False)
        return _result(" ".join(s.text.strip() for s in segments).strip())


class RealtimeCapture:
    """One RealtimeSTT recorder, fed by the daemon one utterance at a time.

    begin() -> feed(frame)... -> poll started/done -> result();  stop() forces the end (max
    length), cancel() abandons a capture (no speech in time, slept, muted)."""

    def __init__(self, stt, cfg: dict, stt_lock: threading.Lock, *, recorder_cls=None):
        self.cfg = cfg
        self.final = FinalExecutor(stt, stt_lock)
        self.partial = PartialExecutor(str(cfg.get("rt_partial_model", "tiny.en")),
                                       int(cfg.get("rt_partial_threads", 2)))
        self._recorder_cls = recorder_cls
        self.rec = None
        self.error = ""
        self._on_partial = None
        self._thread: threading.Thread | None = None
        self._returned: str | None = None
        self._capture_id = 0
        self._started = threading.Event()
        self._load_lock = threading.Lock()

    # ------------------------------------------------------------------ setup
    def load(self) -> bool:
        """Build the recorder (loads the partial model + VADs). Safe to call more than once."""
        with self._load_lock:
            if self.rec is not None:
                return True
            if self.error:
                return False
            try:
                cls = self._recorder_cls
                if cls is None:
                    from RealtimeSTT import AudioToTextRecorder as cls
                c = self.cfg
                silence = max(0.3, int(c.get("silence_ms", 800)) / 1000.0)
                # Start the final transcription ~1 s (its CPU time) before the pause would end
                # the utterance, so the text is ready when it does. Not before 450 ms of silence:
                # earlier, normal mid-sentence pauses keep throwing the work away.
                ms = int(silence * 1000)
                early = int(c.get("rt_early_ms", min(max(450, ms - 1000), max(0, ms - 200))))
                t = time.monotonic()
                self.rec = cls(
                    model=str(c.get("stt_model", "base.en")), language="en",
                    device="cpu", compute_type="int8",
                    use_microphone=False, spinner=False, no_log_file=True, level=logging.WARNING,
                    ensure_sentence_starting_uppercase=False, ensure_sentence_ends_with_period=False,
                    transcription_executor=self.final,
                    enable_realtime_transcription=bool(c.get("live_transcript", True)),
                    realtime_transcription_executor=self.partial,
                    realtime_processing_pause=float(c.get("rt_partial_pause", 0.3)),
                    init_realtime_after_seconds=0.3,
                    on_realtime_transcription_update=self._partial_cb,
                    silero_backend="raw_onnx",
                    silero_sensitivity=float(c.get("rt_silero_sensitivity", 0.4)),
                    silero_deactivity_detection=True,
                    webrtc_sensitivity=int(c.get("rt_webrtc_sensitivity", 3)),
                    post_speech_silence_duration=silence,
                    # RealtimeSTT 1.1.2 documents this in ms but compares it to SECONDS
                    # (core/recording.py) — pass seconds, or early transcription never fires.
                    early_transcription_on_silence=early / 1000.0,
                    min_length_of_recording=0.3, min_gap_between_recordings=0,
                    pre_recording_buffer_duration=1.0,
                    on_recording_start=self._started.set,
                )
                _route_library_log()
                if c.get("live_transcript", True):
                    self.partial.warmup()
                conf.log("voice", f"RealtimeSTT capture ready ({time.monotonic() - t:.1f}s; "
                                  f"silence {silence:.2f}s, early transcription {early} ms)")
                return True
            except Exception as e:
                self.error = f"{e.__class__.__name__}: {e}"
                self.rec = None
                conf.log("voice", f"RealtimeSTT unavailable ({self.error}) — using built-in capture")
                return False

    @property
    def ready(self) -> bool:
        return self.rec is not None

    # ------------------------------------------------------------------ one capture
    def _partial_cb(self, text):
        cb = self._on_partial
        if cb and text:
            try:
                cb(str(text).strip())
            except Exception:
                pass

    def _reset(self) -> None:
        """Forget anything left from the previous capture (a queued second utterance, the
        pre-roll tail) so it can't be glued onto this one."""
        r = self.rec
        r.start_recording_on_voice_activity = False
        r.stop_recording_on_voice_deactivity = False
        if getattr(r, "is_recording", False):
            try:
                r.recording_start_time = 0          # bypass the min-length guard on this stop
                r.stop()
            except Exception:
                pass
        q = getattr(r, "recorded_audio_queue", None)
        while q is not None and not q.empty():
            try:
                q.get_nowait()
            except Exception:
                break
        r.clear_audio_queue()
        lock = getattr(r, "frames_lock", None)
        if lock is not None:
            with lock:
                r.frames.clear()
                r.last_frames.clear()

    def begin(self, on_partial=None) -> None:
        self._join()
        self._reset()
        self._on_partial = on_partial
        self._returned = None
        self._started.clear()
        self._capture_id += 1
        self.final.capture_id = self._capture_id

        def run():
            try:
                self._returned = self.rec.text() or ""
            except Exception as e:  # pragma: no cover
                conf.log("voice", f"RealtimeSTT capture error: {e}")
                self._returned = ""
        self._thread = threading.Thread(target=run, name="helios-realtimestt", daemon=True)
        self._thread.start()

    def feed(self, frame: np.ndarray) -> None:
        self.rec.feed_audio(np.asarray(frame, dtype="int16").tobytes(), 16000)

    @property
    def started(self) -> bool:
        return self._started.is_set()

    @property
    def done(self) -> bool:
        """No capture running (finished, cancelled, or never begun)."""
        return self._thread is None or not self._thread.is_alive()

    def stop(self) -> None:
        """Force the end of the utterance (max length reached) — it still gets transcribed."""
        try:
            if getattr(self.rec, "is_recording", False):
                self.rec.recording_start_time = 0
                self.rec.stop()
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"RealtimeSTT stop failed: {e}")

    def cancel(self) -> None:
        """Abandon this capture: unblock text() without transcribing anything."""
        if self.rec is None:
            return
        self._on_partial = None
        self.rec.interrupt_stop_event.set()
        self._join()
        self.rec.interrupt_stop_event.clear()
        self.rec.was_interrupted.clear()
        self._reset()

    def wait(self, timeout: float) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def result(self):
        """(audio float32, gated text, raw text) for the finished capture, or None."""
        self._on_partial = None
        got = self.final.result_for(self._capture_id)
        if not got:
            return None
        _, audio, gated, raw = got
        return audio, gated, raw

    def _join(self) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            if self.rec is not None:
                self.rec.interrupt_stop_event.set()
            t.join(5)
            if self.rec is not None:
                self.rec.interrupt_stop_event.clear()
                self.rec.was_interrupted.clear()
        self._thread = None

    def close(self) -> None:
        r, self.rec = self.rec, None
        if r is None:
            return
        try:
            r.interrupt_stop_event.set()
            r.shutdown()
        except Exception:
            pass
