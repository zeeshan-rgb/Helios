"""Wake-word detection via openWakeWord (pretrained "hey Helios", ONNX — no PyTorch).

openWakeWord ships a pretrained `hey_jarvis_v0.1` model plus the shared melspectrogram +
embedding feature extractors (downloaded once into the package's resources/models dir). We feed
it 80ms / 1280-sample int16 frames at 16kHz and watch the per-model score cross a threshold.

Speaker verification (optional): on each wake, verify the speaker matches an enrolled template.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from .. import conf

FRAME = 1280   # openWakeWord wants 80ms frames @ 16kHz


class WakeWord:
    """Thin wrapper over an openWakeWord Model scoped to a single wake phrase.

    Optionally verifies speaker identity on each detection.
    """

    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.5,
                 enable_speaker_verify: bool = False, speaker_verify_threshold: float = 0.70):
        # A custom model is a path to a .onnx file (relative paths resolve against the repo root);
        # openWakeWord keys its scores by the file stem, so track that separately.
        model = (model or "").strip()
        self.model_path = model
        # Why the wake word can't run ("" = it can). Voice keeps working without it: the double-clap
        # gesture, the summon hotkey and push-to-talk still wake Helios.
        self.disabled_reason = ""
        if not model or model.lower() == "none":
            self.disabled_reason = "wake word disabled in settings"
        elif model.lower().endswith(".onnx"):
            p = Path(model)
            self.model_path = str(p if p.is_absolute() else conf.ROOT / p)
            model = p.stem
            if not Path(self.model_path).exists():
                self.disabled_reason = (f"custom wake model not found: {self.model_path} "
                                        "(train it, then place it there)")
        self.model_key = model
        self.threshold = float(threshold)
        self.enable_speaker_verify = bool(enable_speaker_verify)
        self.speaker_verify_threshold = float(speaker_verify_threshold)
        self._model = None
        self._score = 0.0
        self._speaker_verifier = None
        self._load_lock = threading.Lock()  # the warmup thread + main loop both lazy-load
        self._recent_frames = []  # buffer for speaker verification

    @property
    def available(self) -> bool:
        return not self.disabled_reason

    def _ensure(self):
        if self.disabled_reason:
            raise RuntimeError(self.disabled_reason)
        if self._model is None:
            with self._load_lock:        # double-checked: load exactly once despite the race
                if self._model is None:
                    from openwakeword.model import Model
                    # Ensure the feature + wakeword models exist (no-op if already downloaded).
                    try:
                        from openwakeword.utils import download_models
                        download_models([self.model_key])
                    except Exception:
                        pass
                    try:
                        self._model = Model(wakeword_models=[self.model_path],
                                            inference_framework="onnx")
                    except Exception as e:
                        # Don't retry (and re-log) on every 80ms frame; report once.
                        self.disabled_reason = f"wake model failed to load: {e}"
                        conf.log("voice", f"wake word unavailable — {self.disabled_reason}")
                        raise
                    conf.log("voice", f"wake word '{self.model_key}' loaded "
                                      f"(threshold {self.threshold})")
        return self._model

    def _ensure_speaker_verifier(self):
        """Lazy-load the speaker verifier."""
        if self._speaker_verifier is None and self.enable_speaker_verify:
            try:
                from .speaker_verify import SpeakerVerifier
                self._speaker_verifier = SpeakerVerifier(threshold=self.speaker_verify_threshold)
            except Exception as e:
                conf.log("voice", f"speaker verification unavailable: {e}")
                self.enable_speaker_verify = False
        return self._speaker_verifier

    def warmup(self) -> None:
        if self.disabled_reason:
            conf.log("voice", f"wake word unavailable — {self.disabled_reason}; "
                              "a double-clap wakes Helios and starts listening instead")
            return
        try:
            self._ensure().predict(np.zeros(FRAME, dtype="int16"))
            if self.enable_speaker_verify:
                self._ensure_speaker_verifier()
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"wake warmup failed: {e}")

    def detect(self, frame_int16, threshold: float | None = None) -> bool:
        """Feed one 1280-sample int16 frame. Returns True the instant the score crosses threshold.

        If speaker verification is enabled, also checks that the speaker matches the enrolled
        template. `threshold` overrides the instance default for this call."""
        if self.disabled_reason:
            return False          # reported once at warmup; never spam the log per frame
        try:
            # Buffer for speaker verification (keep ~1.5s = ~20 frames)
            self._recent_frames.append(np.array(frame_int16, dtype="int16"))
            if len(self._recent_frames) > 20:
                self._recent_frames.pop(0)

            scores = self._ensure().predict(frame_int16)
            self._score = float(scores.get(self.model_key, 0.0))
            thr = self.threshold if threshold is None else threshold

            detected = self._score >= thr
            if detected and self.enable_speaker_verify:
                verifier = self._ensure_speaker_verifier()
                if verifier is not None and verifier.is_enrolled():
                    # Concatenate recent frames for verification
                    audio = np.concatenate(self._recent_frames).astype("float32") / 32768.0
                    verified, sim = verifier.verify(audio)
                    if not verified:
                        conf.log("voice", f"wake detected but speaker mismatch (sim={sim:.2f})")
                        return False

            return detected
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"wake detect error: {e}")
            return False

    @property
    def score(self) -> float:
        return self._score

    def reset(self) -> None:
        """Clear the model's internal feature buffers after a detection so the next wake starts
        clean (otherwise the tail of the just-detected audio can immediately re-trigger)."""
        try:
            if self._model is not None:
                self._model.reset()
        except Exception:
            pass
        self._recent_frames.clear()

    def is_speaker_verification_available(self) -> bool:
        """Check if speaker verification is active and enrolled."""
        if not self.enable_speaker_verify:
            return False
        verifier = self._ensure_speaker_verifier()
        return verifier is not None and verifier.is_enrolled()

    def get_speaker_verifier(self):
        """Get the speaker verifier instance for enrollment."""
        return self._ensure_speaker_verifier()
