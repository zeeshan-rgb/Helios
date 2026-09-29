"""Speech-to-text via faster-whisper (CTranslate2, int8 on CPU — no PyTorch).

One model is loaded once and reused. transcribe() takes a float32 mono 16kHz numpy clip (what
the capture pipeline already produces) and returns clean text, applying confidence gates so a
cough / fan noise / silence doesn't get sent to the brain as a phantom command.
"""

from __future__ import annotations

import re
import threading

import numpy as np

from .. import conf

# faster-whisper exposes per-segment no_speech_prob (likelihood the audio is NOT speech) and
# avg_logprob (decoder confidence). Reject a clip that looks like non-speech or a low-confidence
# hallucination — Whisper otherwise happily emits "Thank you." / "you" for near-silence.
_NO_SPEECH_MAX = 0.6
_LOGPROB_MIN = -1.0
# Common Whisper hallucinations on silence/noise — drop if the whole transcript is just these.
_JUNK = {"", "you", "thank you", "thanks for watching", "thank you.", ".", "bye",
         "thanks for watching!", "okay", "so", "hmm", "uh", "um"}


class Transcriber:
    """Lazy-loaded faster-whisper wrapper."""

    def __init__(self, model: str = "base.en", initial_prompt: str = ""):
        self.model_name = model
        self.initial_prompt = (initial_prompt or "").strip()
        self._model = None
        self._load_lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            with self._load_lock:        # load once even if warmup + a real turn race
                if self._model is None:
                    from faster_whisper import WhisperModel
                    # int8 keeps it small + fast on CPU; download_root defaults to the HF cache.
                    self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
                    conf.log("voice", f"stt model '{self.model_name}' loaded")
        return self._model

    def warmup(self) -> None:
        try:
            self._ensure().transcribe(np.zeros(16000, dtype="float32"), beam_size=1)
            conf.log("voice", "stt warmed up")
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"stt warmup failed: {e}")

    def _transcribe_core(self, audio: np.ndarray):
        """Run the engine and return an iterable of segments, each exposing .text plus optional
        .no_speech_prob / .avg_logprob. The ONLY engine-specific step; cloud engines
        (voice/engines.py) override this. Default: faster-whisper."""
        m = self._ensure()
        segments, _ = m.transcribe(audio, language="en", beam_size=1,
                                   condition_on_previous_text=False,
                                   initial_prompt=self.initial_prompt if self.initial_prompt else None)
        return segments

    def transcribe(self, audio: np.ndarray, *, gated: bool = True) -> str:
        """Transcribe a float32 mono 16kHz clip. Returns "" if it doesn't look like real speech.

        gated=False skips the junk/confidence filter (used for dictation, where the user is
        deliberately speaking and we want the raw text even if short)."""
        try:
            audio = np.asarray(audio, dtype="float32").reshape(-1)
            if audio.size < 1600:  # < 0.1s — nothing to hear
                return ""
            segments = self._transcribe_core(audio)
            parts, kept = [], False
            for s in segments:
                if gated:
                    if getattr(s, "no_speech_prob", 0.0) > _NO_SPEECH_MAX:
                        continue
                    if getattr(s, "avg_logprob", 0.0) < _LOGPROB_MIN:
                        continue
                parts.append(s.text)
                kept = True
            text = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if gated and (not kept or text.lower().strip(" .,!?") in _JUNK):
                return ""
            return text
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"stt error: {e}")
            return ""


# ---- dictation helpers ----------------------------------------------------------------
_FILLERS = re.compile(r"\b(?:um+|uh+|er+|ah+|hmm+|like|you know|i mean|sort of|kind of)\b[,]?",
                      re.I)


def clean_for_dictation(text: str) -> str:
    """Tidy a dictated transcript: strip filler words, fix spacing, sentence-case the start."""
    t = _FILLERS.sub("", text)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)   # no space before punctuation
    t = re.sub(r"\s{2,}", " ", t).strip()
    if t:
        t = t[0].upper() + t[1:]
    return t
