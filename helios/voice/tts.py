"""Text-to-speech: Kokoro v1.0 (ONNX) with sentence-streaming playback.

The win for perceived latency is to NOT wait for the whole reply: as the brain streams tokens,
we cut the text into sentences and synthesize+play each one while the next still streams. So
Helios starts talking a beat after the first sentence lands, not after the full answer.

Design:
  - One always-open sounddevice OutputStream (24kHz mono float32). Its callback pulls samples
    from a thread-safe buffer; silence when empty. Keeping it open is cheap and lets stop() cut
    playback instantly (barge-in / panic) by just clearing the buffer.
  - A synth worker thread pulls text segments off a queue, runs Kokoro, and appends the samples.
  - Streaming text in: begin() -> feed(delta) -> flush(). feed() carves complete sentences out
    of the running buffer (and skips ```fenced code``` — never read code aloud); flush() speaks
    the trailing remainder. speak(text) is the one-shot convenience (errors / short lines).

on_speaking(cb) fires cb(True) when audio starts and cb(False) when it fully drains — the daemon
uses that to gate the mic (half-duplex echo control) and to post listening/speaking UI state.
"""

from __future__ import annotations

import queue
import re
import threading

import numpy as np

from .. import conf

SR = 24000          # Kokoro output sample rate
_BLOCK = 1200       # output callback block (~50ms) — small enough that stop() is near-instant


class Speaker:
    """Streaming Kokoro speaker. Thread-safe; one instance per daemon."""

    def __init__(self, voice: str = "bm_george", speed: float = 1.0, lang: str = "en-gb",
                 pitch: float = 1.0, reverb: float = 0.0):
        self.voice = voice
        self.speed = float(speed)
        self.lang = lang
        self.pitch = min(max(float(pitch or 1.0), 0.7), 1.3)   # voice-character effect (effects.py)
        self.reverb = min(max(float(reverb or 0.0), 0.0), 1.0)
        self._kokoro = None                      # lazy — model load is ~1s
        self._segq: queue.Queue = queue.Queue()  # text segments awaiting synthesis
        self._pcm: list[np.ndarray] = []         # queued sample chunks (ready to play)
        self._cur: np.ndarray | None = None      # chunk currently being drained by the callback
        self._cur_i = 0
        self._remaining = 0                      # samples still queued+playing (drives is_speaking)
        self._synthesizing = False
        self._lock = threading.Lock()        # guards the playback buffer (held briefly)
        self._mlock = threading.Lock()       # guards the one-time model load (held during load)
        self._stream = None
        self._gen = 0                            # bumped by stop() so stale synth results are dropped
        self._speaking = False
        self._on_speaking = None                 # callback(bool): speaking started / stopped
        self._out_level = 0.0                     # RMS of the audio currently playing (0..1), for VU
        self._buf = ""                           # streaming text not yet cut into sentences
        self._in_code = False                    # inside a ```fenced``` block (don't speak it)
        threading.Thread(target=self._worker, daemon=True, name="tts-synth").start()

    # ------------------------------------------------------------------ lifecycle
    def on_speaking(self, cb) -> None:
        """Register callback(bool) fired when playback starts (True) / fully drains (False)."""
        self._on_speaking = cb

    def _ensure_model(self):
        if self._kokoro is None:
            with self._mlock:                # warmup thread + synth worker can race here
                if self._kokoro is None:
                    from kokoro_onnx import Kokoro  # heavy import — defer to first use
                    self._kokoro = Kokoro(str(conf.KOKORO_MODEL), str(conf.KOKORO_VOICES))
        return self._kokoro

    def _ensure_stream(self):
        if self._stream is None:
            import sounddevice as sd
            self._stream = sd.OutputStream(samplerate=SR, channels=1, dtype="float32",
                                           blocksize=_BLOCK, callback=self._cb)
            self._stream.start()

    def warmup(self) -> None:
        """Load the model and JIT a tiny synth so the first real reply isn't slow. Discards audio."""
        try:
            self._ensure_model()
            self._synth("Ready.")
            self._ensure_stream()
            conf.log("voice", "tts warmed up")
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"tts warmup failed: {e}")

    def _synth(self, text: str):
        """Synthesize `text` -> a float32 numpy waveform at SR (24kHz). The ONLY engine-specific
        step; pluggable cloud engines (voice/engines.py) override this. Default: Kokoro v1.0."""
        k = self._ensure_model()
        # A deeper pitch stretches the audio by 1/pitch, so synthesize that much faster first to
        # keep the configured speaking rate (Kokoro accepts speed 0.5..2.0).
        speed = min(max(self.speed / self.pitch, 0.5), 2.0)
        samples, _ = k.create(text, voice=self.voice, speed=speed, lang=self.lang)
        return self._character(samples)

    def _character(self, samples):
        """Apply the configured voice-character effect (identity by default)."""
        if abs(self.pitch - 1.0) < 1e-3 and self.reverb <= 0:
            return samples
        from .effects import apply
        return apply(samples, SR, pitch=self.pitch, reverb_wet=self.reverb)

    # ------------------------------------------------------------------ playback callback
    def _cb(self, outdata, frames, time_info, status):  # sounddevice OutputStream callback
        out = outdata
        i = 0
        with self._lock:
            while i < frames:
                if self._cur is None or self._cur_i >= len(self._cur):
                    if self._pcm:
                        self._cur = self._pcm.pop(0)
                        self._cur_i = 0
                    else:
                        break
                n = min(frames - i, len(self._cur) - self._cur_i)
                out[i:i + n, 0] = self._cur[self._cur_i:self._cur_i + n]
                self._cur_i += n
                self._remaining -= n
                i += n
            done = (self._remaining <= 0 and not self._pcm and self._cur is None
                    or (self._remaining <= 0 and self._cur is not None and self._cur_i >= len(self._cur)))
        if i < frames:
            out[i:, 0] = 0.0  # underrun / nothing to play -> silence
        # Track the output level (RMS of what we just played) so the orb/HUD can pulse while speaking.
        if i > 0:
            self._out_level = float(np.sqrt(np.mean(out[:i, 0] ** 2)))
        else:
            self._out_level *= 0.6
        # Fire the stop transition once the buffer is fully drained AND nothing is being synthesized.
        if i < frames:
            self._maybe_stopped()

    def _maybe_stopped(self):
        with self._lock:
            empty = self._remaining <= 0 and not self._pcm and not self._synthesizing \
                    and self._segq.empty()
            transition = self._speaking and empty
            if transition:
                self._speaking = False
        if transition and self._on_speaking:
            try:
                self._on_speaking(False)
            except Exception:
                pass

    def _begin_speaking(self):
        fire = False
        with self._lock:
            if not self._speaking:
                self._speaking = True
                fire = True
        if fire and self._on_speaking:
            try:
                self._on_speaking(True)
            except Exception:
                pass

    # ------------------------------------------------------------------ synthesis worker
    def _worker(self):
        while True:
            gen, text = self._segq.get()
            if gen != self._gen:
                continue  # stale (stop() was called after this was queued) — drop it
            with self._lock:
                self._synthesizing = True
            try:
                samples = self._synth(text)
                samples = (np.asarray(samples, dtype="float32").reshape(-1)
                           if samples is not None else None)
            except Exception as e:  # pragma: no cover
                conf.log("voice", f"tts synth error: {e}")
                samples = None
            with self._lock:
                self._synthesizing = False
                if samples is not None and gen == self._gen and samples.size:
                    self._ensure_stream()
                    self._pcm.append(samples)
                    self._remaining += samples.size
            if samples is not None and samples.size:
                self._begin_speaking()
            else:
                self._maybe_stopped()

    def _enqueue(self, text: str):
        text = text.strip()
        if text:
            self._segq.put((self._gen, text))

    # ------------------------------------------------------------------ public speak API
    def speak(self, text: str) -> None:
        """One-shot: speak a whole string now (used for errors / short status lines)."""
        self.begin()
        self.feed(text)
        self.flush()

    def begin(self) -> None:
        """Start a fresh streamed response (clears any half-assembled sentence state)."""
        self._buf = ""
        self._in_code = False

    def feed(self, delta: str) -> None:
        """Add a chunk of streamed reply text; speak whole sentences as they complete."""
        if not delta:
            return
        self._buf += delta
        self._drain_sentences(final=False)

    def flush(self) -> None:
        """End of reply: speak whatever sentence tail is left."""
        self._drain_sentences(final=True)
        leftover = _spoken(self._buf)
        self._buf = ""
        if leftover:
            self._enqueue(leftover)

    def _drain_sentences(self, final: bool):
        """Pull complete, speakable sentences out of self._buf, skipping fenced code blocks."""
        while True:
            if self._in_code:
                end = self._buf.find("```")
                if end == -1:
                    if not final:
                        return
                    self._buf = ""   # unterminated code fence at end-of-reply: drop it
                    return
                self._buf = self._buf[end + 3:]
                self._in_code = False
                continue
            fence = self._buf.find("```")
            m = _first_stop(self._buf)
            # Whichever comes first: a code fence opening, or a sentence boundary.
            if fence != -1 and (m is None or fence < m.start()):
                head, self._buf = self._buf[:fence], self._buf[fence + 3:]
                spoken = _spoken(head)
                if spoken:
                    self._enqueue(spoken)
                self._in_code = True
                continue
            if m is not None:
                cut = m.end()
                sent, self._buf = self._buf[:cut], self._buf[cut:]
                spoken = _spoken(sent)
                if spoken:
                    self._enqueue(spoken)
                continue
            # No boundary. Force-flush an over-long run-on so we don't sit silent on a long sentence.
            if not final and fence == -1 and len(self._buf) > 240:
                sp = self._buf.rfind(" ", 0, 240)
                if sp > 80:
                    head, self._buf = self._buf[:sp], self._buf[sp:]
                    spoken = _spoken(head)
                    if spoken:
                        self._enqueue(spoken)
                    continue
            return

    # ------------------------------------------------------------------ control / state
    def stop(self) -> None:
        """Cut playback immediately and drop all pending/queued audio (barge-in / panic)."""
        with self._lock:
            self._gen += 1            # invalidate in-flight synth + queued segments
            self._pcm.clear()
            self._cur = None
            self._cur_i = 0
            self._remaining = 0
            self._buf = ""
            self._in_code = False
        try:
            while True:
                self._segq.get_nowait()
        except queue.Empty:
            pass
        self._maybe_stopped()

    def level(self) -> float:
        """Current output loudness (RMS 0..1) — drives the orb/HUD pulse while Helios speaks."""
        return self._out_level if self._speaking else 0.0

    def is_speaking(self) -> bool:
        with self._lock:
            return (self._speaking or self._remaining > 0 or bool(self._pcm)
                    or self._synthesizing or not self._segq.empty())

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None


# Sentence boundary: ., !, ? (optionally quoted/closing-bracketed) followed by whitespace/end,
# OR a newline. Good enough for natural prosody chunking without a full NLP sentence splitter.
# (Decimals/versions like "3.14" or "v1.0" already never match here — their period is followed by a
# digit, not whitespace — so only abbreviations/initials need the guard below.)
_SENT_END = re.compile(r'[.!?]+["\')\]]*(?=\s|$)|\n+')

# Period-ending tokens that are abbreviations, not sentence ends — don't break TTS after these.
_ABBREV = {"dr", "mr", "mrs", "ms", "st", "jr", "sr", "prof", "gen", "sen", "rep", "gov", "vs",
           "no", "etc", "e.g", "i.e", "al", "fig", "approx", "dept", "inc", "ltd", "co", "mt"}
_WORD_BEFORE_DOT = re.compile(r"([A-Za-z][A-Za-z.]*)$")


def _false_stop(buf: str, m: "re.Match") -> bool:
    """True if boundary m is a period that's really an abbreviation or a single initial ('Dr.',
    'e.g.', 'J. Smith') rather than a real sentence end. '!'/'?'/newline boundaries are never false."""
    if m.group()[:1] != ".":
        return False                      # only '.'-ended runs can be abbreviations
    before = buf[:m.start()]
    tail = _WORD_BEFORE_DOT.search(before)
    if not tail:
        return False
    word = tail.group(1).rstrip(".").lower()
    if word in _ABBREV:
        return True
    if len(word) == 1 and word.isalpha():         # single initial like 'J.' — preceded by space/start
        pre = before[:tail.start()]
        return pre == "" or pre[-1].isspace()
    return False


def _first_stop(buf: str):
    """First real sentence boundary in buf, skipping abbreviation/initial false stops (see
    _false_stop). Returns the re.Match or None. Advances past each false stop so it can't loop."""
    pos = 0
    while True:
        m = _SENT_END.search(buf, pos)
        if m is None or not _false_stop(buf, m):
            return m
        pos = m.end()

_URL = re.compile(r'https?://\S+')
_MD_LINK = re.compile(r'\[([^\]]+)\]\((?:https?://)?[^)]+\)')
_INLINE_CODE = re.compile(r'`([^`]+)`')
_BOLD = re.compile(r'\*{1,3}([^*]+)\*{1,3}')
_HEAD = re.compile(r'^\s{0,3}#{1,6}\s*', re.M)
_BULLET = re.compile(r'^\s*[-*•]\s+', re.M)
# Symbols/emoji TTS mangles; keep letters, digits, basic punctuation, currency/percent/&.
_KEEP = re.compile(r"[^0-9A-Za-zÀ-ɏ .,!?;:'\"()$%&/-]")


def _spoken(text: str) -> str:
    """Turn a chunk of markdown reply text into something natural to read aloud."""
    if not text:
        return ""
    t = _MD_LINK.sub(r"\1", text)         # [label](url) -> label
    t = _URL.sub("the link", t)            # bare URLs -> "the link"
    t = _INLINE_CODE.sub(r"\1", t)         # `code` -> code (drop backticks)
    t = _BOLD.sub(r"\1", t)                # **bold** -> bold
    t = _HEAD.sub("", t)                   # drop markdown headers
    t = _BULLET.sub("", t)                 # drop list bullets
    t = _KEEP.sub(" ", t)                  # strip emoji / odd symbols
    t = re.sub(r"\s+", " ", t).strip()
    return t
