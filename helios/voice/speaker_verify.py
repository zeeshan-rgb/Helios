"""Speaker verification — enroll the user's voice once, then reject false wakes from others.

Uses resemblyzer (a lightweight speaker embedding model based on SoundNet) to extract
voiceprints. On first run, the user speaks "hey Helios" 3 times to build an enrollment template.
On each subsequent wake detection, we compare the detected audio's speaker embedding against
the template; the wake only passes if similarity is above threshold (default 0.70).

Enrollment is interactive: the daemon prompts "Say 'hey Helios' for enrollment" via TTS,
captures 3 samples, averages their embeddings, and saves to data/voice_model/speaker.npy.
"""

from __future__ import annotations

import os
import threading

import numpy as np

from .. import conf


class SpeakerVerifier:
    """Speaker verification: enroll once, verify on each wake."""

    def __init__(self, enrollment_path: str | None = None, threshold: float = 0.70):
        self.threshold = float(threshold)
        self.enrollment_path = enrollment_path or str(
            conf.DATA_DIR / "voice_model" / "speaker.npy"
        )
        self._enrollment = None  # cached speaker template (mean embedding)
        self._load_lock = threading.Lock()
        self._model = None  # the resemblyzer model (lazy-loaded)
        self._model_lock = threading.Lock()

    def _load_model(self):
        """Lazy-load the resemblyzer model on first use."""
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                try:
                    from resemblyzer import VoiceEncoder
                    # The constructor loads the pretrained weights itself (no separate .load()).
                    # verbose=False keeps it from printing to stdout (this runs in a windowless
                    # pythonw.exe process with no console to catch it).
                    self._model = VoiceEncoder(device="cpu", verbose=False)
                    conf.log("voice", "speaker encoder loaded (resemblyzer)")
                except ImportError:
                    conf.log("voice", "resemblyzer not installed; speaker verification disabled")
                    raise RuntimeError("resemblyzer required for speaker verification")
        return self._model

    def _load_enrollment(self):
        """Load the enrolled speaker template if it exists."""
        if self._enrollment is not None:
            return self._enrollment
        with self._load_lock:
            if self._enrollment is not None:
                return self._enrollment
            if os.path.exists(self.enrollment_path):
                try:
                    self._enrollment = np.load(self.enrollment_path)
                    conf.log("voice", f"speaker enrollment loaded ({self.enrollment_path})")
                    return self._enrollment
                except Exception as e:
                    conf.log("voice", f"failed to load speaker enrollment: {e}")
        return None

    def is_enrolled(self) -> bool:
        """Check if a speaker template exists."""
        return self._load_enrollment() is not None or os.path.exists(self.enrollment_path)

    def extract_embedding(self, audio_16k_float32: np.ndarray) -> np.ndarray | None:
        """Extract a speaker embedding (512-dim vector) from 16kHz float32 audio.

        Args:
            audio_16k_float32: Audio at 16kHz, float32, [-1, 1] range, or int16 array.

        Returns:
            The embedding vector, or None on error.
        """
        try:
            # Ensure float32 in [-1, 1]
            if audio_16k_float32.dtype == np.int16:
                audio = audio_16k_float32.astype("float32") / 32768.0
            else:
                audio = audio_16k_float32.astype("float32")

            if audio.ndim == 2:
                audio = audio.flatten()

            from resemblyzer import preprocess_wav
            # Trims silence + normalizes volume — makes a short "hey Helios" clip (mostly padded
            # with capture-window silence) a cleaner, more consistent input than the raw capture.
            audio = preprocess_wav(audio, source_sr=16000)
            if audio.size == 0:
                conf.log("voice", "embedding extraction: preprocessed audio is empty (all silence)")
                return None

            model = self._load_model()
            embedding = model.embed_utterance(audio)
            return embedding
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"embedding extraction error: {e}")
            return None

    def verify(self, audio_16k: np.ndarray, require_enrolled: bool = True) -> tuple[bool, float]:
        """Verify if this audio matches the enrolled speaker.

        Args:
            audio_16k: Audio at 16kHz (int16 or float32).
            require_enrolled: If True, fail if enrollment doesn't exist. If False, pass through.

        Returns:
            (verified, similarity_score) where verified is True if similarity >= threshold.
        """
        enrollment = self._load_enrollment()
        if enrollment is None:
            if require_enrolled:
                return False, 0.0
            else:
                return True, 1.0  # no enrollment → pass through

        probe_emb = self.extract_embedding(audio_16k)
        if probe_emb is None:
            return False, 0.0

        # Cosine similarity: dot product of normalized vectors
        enroll_norm = enrollment / np.linalg.norm(enrollment)
        probe_norm = probe_emb / np.linalg.norm(probe_emb)
        sim = float(np.dot(enroll_norm, probe_norm))

        verified = sim >= self.threshold
        return verified, sim

    def enroll(self, samples: list[np.ndarray]) -> bool:
        """Enroll from a list of audio samples (each 16kHz, int16 or float32).

        Averages embeddings and saves to enrollment_path.
        """
        if len(samples) == 0:
            conf.log("voice", "enrollment: no samples provided")
            return False

        embeddings = []
        for i, audio in enumerate(samples):
            emb = self.extract_embedding(audio)
            if emb is None:
                conf.log("voice", f"enrollment: failed to extract embedding for sample {i}")
                return False
            embeddings.append(emb)

        # Average the embeddings
        mean_emb = np.mean(embeddings, axis=0)

        # Ensure enrollment directory exists
        os.makedirs(os.path.dirname(self.enrollment_path), exist_ok=True)

        try:
            np.save(self.enrollment_path, mean_emb)
            self._enrollment = mean_emb  # cache it
            conf.log("voice", f"speaker enrollment saved ({len(samples)} samples) → "
                              f"{self.enrollment_path}")
            return True
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"enrollment save error: {e}")
            return False
