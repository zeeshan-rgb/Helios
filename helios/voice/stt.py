"""Speech-to-text via faster-whisper (CTranslate2 — int8 on CPU by default, float16 on a CUDA GPU).

One model is loaded once (lazily) and reused. transcribe() takes a float32 mono 16kHz numpy clip
(what the capture pipeline already produces) and returns clean text, applying confidence gates so a
cough / fan noise / silence doesn't get sent to the brain as a phantom command.

Configuration — `[voice.stt]` (preferred) or the older flat `[voice]` keys, which keep working:
    model = "base.en"        stt_model      tiny.en | base.en | small.en | distil-small.en | ...
    device = "auto"          stt_device     auto (CUDA if present, else CPU) | cpu | cuda
    compute_type = "auto"    stt_compute    auto (int8 on CPU, float16 on CUDA) | int8 | float16 ...
    language = "en"          stt_language   en | auto (detect — needs a multilingual model) | fr ...
    beam_size = 1                           1 = greedy (fastest; measured best value on this CPU)
    cpu_threads = 0                         0 = CTranslate2's default
    vad_filter = false                      faster-whisper's own VAD pass (capture already has VAD)
    prompt = "..."           stt_prompt     vocabulary hint (names Whisper would otherwise miss)
Models live in the Helios model cache on D: (model_root), never in C:'s user profile.
"""

from __future__ import annotations

import gc
import os
import re
import threading
from pathlib import Path

import numpy as np

from .. import conf


def model_root() -> str | None:
    """Where Whisper models are downloaded/loaded: [voice.stt] download_root, else $HF_HOME/hub,
    else <Helios data drive>/cache/hf/hub (D:\\Helios\\cache\\hf on this install) — so a launch
    without the HF_HOME environment variable still never fills C:."""
    stt = conf.voice_cfg().get("stt")
    if isinstance(stt, dict) and stt.get("download_root"):
        return str(stt["download_root"])
    hf = os.environ.get("HF_HOME")
    if hf:
        return str(Path(hf) / "hub")
    try:
        base = Path(conf.DATA_DIR).resolve().parent / "cache" / "hf"
        if base.exists():
            return str(base / "hub")
    except OSError:
        pass
    return None


def stt_settings(cfg: dict | None = None) -> dict:
    """[voice.stt] merged over the flat [voice] stt_* keys, with defaults."""
    c = cfg if cfg is not None else conf.voice_cfg()
    sub = c.get("stt") if isinstance(c.get("stt"), dict) else {}
    flat = {"provider": c.get("stt_engine"), "model": c.get("stt_model"), "device": c.get("stt_device"),
            "compute_type": c.get("stt_compute"), "language": c.get("stt_language"),
            "prompt": c.get("stt_prompt")}
    out = {"provider": "faster-whisper", "model": "base.en", "device": "auto", "compute_type": "auto",
           "language": "en", "beam_size": 1, "cpu_threads": 0, "vad_filter": False, "prompt": ""}
    out.update({k: v for k, v in flat.items() if v not in (None, "")})
    out.update({k: v for k, v in sub.items() if v not in (None, "")})
    out["provider"] = str(out["provider"]).strip().lower().replace("_", "-")
    return out


# Words from your world Whisper otherwise mangles (measured: "Maqsusi" heard as "Max Sousie" /
# "Maxuzzi", "Polarion" as "Polarian"). Project names are added automatically from the manifests.
_BUILTIN_VOCAB = ("Helios", "Polarion", "Antigravity")
_MAX_VOCAB_TERMS = 40


def vocabulary_prompt(settings: dict | None = None, *, project_names=None) -> str:
    """The Whisper vocabulary hint: your stt prompt + built-in terms + [voice.stt] vocabulary +
    the names of your tracked projects. Benchmark (tools/bench_stt.py, base.en, 32 clips): word
    error 5.2% -> 2.8% and project names 4/8 -> 8/8 once the names were in the hint."""
    s = settings if settings is not None else stt_settings()
    terms: list[str] = []

    def add(t):
        t = re.sub(r"\s+", " ", str(t or "")).strip(" ,.")
        if t and t.lower() not in {x.lower() for x in terms}:
            terms.append(t)
    for t in re.split(r"\s*,\s*", str(s.get("prompt") or "")):
        add(t)
    for t in _BUILTIN_VOCAB:
        add(t)
    vocab = s.get("vocabulary") or []
    for t in (vocab if isinstance(vocab, list) else re.split(r"\s*,\s*", str(vocab))):
        add(t)
    if project_names is None:
        try:
            from .. import projects
            project_names = [p.get("name", "") for p in projects.load_all()[0] if p.get("active", True)]
        except Exception:
            project_names = []
    for name in project_names:
        add(re.sub(r"[^\w\s'-]+\d*$", "", str(name)))   # "Napino Man.3" -> "Napino Man"
    return ", ".join(terms[:_MAX_VOCAB_TERMS])


def load_whisper(loader, name: str, **kw):
    """Load from the local model cache first (no network round-trip to the Hugging Face hub —
    measured 0.6 s vs 0.8-16 s, and it works offline); download only when it isn't cached yet."""
    try:
        return loader(name, local_files_only=True, **kw)
    except Exception as first:
        conf.log("voice", f"stt model '{name}' not in the local cache ({first.__class__.__name__}) — downloading")
        return loader(name, **kw)


def resolve_device(device: str = "auto", compute_type: str = "auto") -> tuple[str, str]:
    device = (device or "auto").lower()
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    ct = (compute_type or "auto").lower()
    if ct == "auto":
        ct = "float16" if device == "cuda" else "int8"
    return device, ct

# faster-whisper exposes per-segment no_speech_prob (likelihood the audio is NOT speech) and
# avg_logprob (decoder confidence). Reject a clip that looks like non-speech or a low-confidence
# hallucination — Whisper otherwise happily emits "Thank you." / "you" for near-silence.
_NO_SPEECH_MAX = 0.7
_LOGPROB_MIN = -1.25   # a little looser than Whisper's -1.0 default: accented English scores lower
# Common Whisper hallucinations on silence/noise — drop if the whole transcript is just these.
_JUNK = {"", "you", "thank you", "thanks for watching", "thank you.", ".", "bye",
         "thanks for watching!", "okay", "so", "hmm", "uh", "um"}


class Transcriber:
    """Lazy-loaded faster-whisper wrapper."""

    def __init__(self, model: str = "base.en", initial_prompt: str = "", *, device: str = "auto",
                 compute_type: str = "auto", language: str = "en", beam_size: int = 1,
                 cpu_threads: int = 0, vad_filter: bool = False, loader=None):
        self.model_name = model
        self.initial_prompt = (initial_prompt or "").strip()
        self.device, self.compute_type = device, compute_type
        lang = (language or "en").strip().lower()
        if model.endswith(".en") and lang != "en":
            conf.log("voice", f"stt: '{model}' is English-only — language '{lang}' ignored (use a "
                              "multilingual model such as 'base' or 'small' for other languages)")
            lang = "en"
        self.language = None if lang == "auto" else lang
        self.beam_size = max(1, int(beam_size or 1))
        self.cpu_threads = max(0, int(cpu_threads or 0))
        self.vad_filter = bool(vad_filter)
        self.last_language: tuple[str, float] | None = None
        self._loader = loader                 # tests inject a fake WhisperModel factory
        self._model = None
        self._load_lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            with self._load_lock:        # load once even if warmup + a real turn race
                if self._model is None:
                    self._model = self._load()
        return self._model

    def _load(self):
        loader = self._loader
        if loader is None:
            from faster_whisper import WhisperModel as loader
        device, ct = resolve_device(self.device, self.compute_type)
        kw = {"device": device, "compute_type": ct, "download_root": model_root(),
              "cpu_threads": self.cpu_threads}
        try:
            m = load_whisper(loader, self.model_name, **kw)
        except Exception as e:
            if device == "cpu":
                raise
            # a GPU that can't run it (driver / VRAM / missing cuDNN) must not take voice down
            conf.log("voice", f"stt on {device} failed ({e}) — falling back to CPU int8")
            device, ct = "cpu", "int8"
            m = load_whisper(loader, self.model_name, **{**kw, "device": device, "compute_type": ct})
        self.device, self.compute_type = device, ct
        conf.log("voice", f"stt model '{self.model_name}' loaded ({device}, {ct}, "
                          f"cache {kw['download_root'] or 'default'})")
        return m

    def warmup(self) -> None:
        try:
            self._ensure().transcribe(np.zeros(16000, dtype="float32"), beam_size=1,
                                      language=self.language or "en")
            conf.log("voice", "stt warmed up")
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"stt warmup failed: {e}")

    def close(self) -> None:
        """Release the model (clean shutdown / switching models)."""
        with self._load_lock:
            self._model = None
        gc.collect()

    def _run(self, audio: np.ndarray, word_timestamps: bool = False):
        m = self._ensure()
        segments, info = m.transcribe(audio, language=self.language, beam_size=self.beam_size,
                                      condition_on_previous_text=False, vad_filter=self.vad_filter,
                                      word_timestamps=word_timestamps,
                                      initial_prompt=self.initial_prompt if self.initial_prompt else None)
        if info is not None and getattr(info, "language", None):
            self.last_language = (info.language, round(float(getattr(info, "language_probability", 0) or 0), 2))
        return segments, info

    def _transcribe_core(self, audio: np.ndarray):
        """Run the engine and return an iterable of segments, each exposing .text plus optional
        .no_speech_prob / .avg_logprob. The ONLY engine-specific step; cloud engines
        (voice/engines.py) override this. Default: faster-whisper."""
        return self._run(audio)[0]

    def transcribe_detailed(self, audio: np.ndarray, *, word_timestamps: bool = True) -> dict:
        """Text + detected language + segment (and word) timestamps — for dictation, subtitles,
        or "what did I say at the start?". Ungated."""
        segments, info = self._run(np.asarray(audio, dtype="float32").reshape(-1), word_timestamps)
        segs = []
        for s in segments:
            row = {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            if word_timestamps and getattr(s, "words", None):
                row["words"] = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip(),
                                 "prob": round(w.probability, 2)} for w in s.words]
            segs.append(row)
        return {"text": re.sub(r"\s+", " ", " ".join(x["text"] for x in segs)).strip(),
                "language": getattr(info, "language", self.language),
                "language_probability": round(float(getattr(info, "language_probability", 0) or 0), 2),
                "segments": segs}

    def transcribe(self, audio: np.ndarray, *, gated: bool = True) -> str:
        """Transcribe a float32 mono 16kHz clip. Returns "" if it doesn't look like real speech.

        gated=False skips the junk/confidence filter (used for dictation, where the user is
        deliberately speaking and we want the raw text even if short)."""
        g, raw = self.transcribe_both(audio, log_rejects=gated)
        return g if gated else raw

    def transcribe_both(self, audio: np.ndarray, *, log_rejects: bool = True) -> tuple[str, str]:
        """One engine run -> (gated text, raw text). The RealtimeSTT capture uses this so a
        command (gated) and a dictation / spoken yes-no (raw) never need a second pass."""
        try:
            audio = np.asarray(audio, dtype="float32").reshape(-1)
            if audio.size < 1600:  # < 0.1s — nothing to hear
                return "", ""
            segments = self._transcribe_core(audio)
            parts, every, kept, dropped = [], [], False, []
            for s in segments:
                every.append(s.text)
                nsp, lp = getattr(s, "no_speech_prob", 0.0), getattr(s, "avg_logprob", 0.0)
                if nsp > _NO_SPEECH_MAX or lp < _LOGPROB_MIN:
                    dropped.append(f"{s.text.strip()!r} (no_speech {nsp:.2f}, logprob {lp:.2f})")
                    continue
                parts.append(s.text)
                kept = True
            raw = re.sub(r"\s+", " ", " ".join(every)).strip()
            text = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if not kept or text.lower().strip(" .,!?") in _JUNK:
                # Say what was thrown away, so "not understood" can be tuned instead of guessed at.
                if log_rejects:
                    conf.log("voice", "stt rejected: " + ("; ".join(dropped) if dropped else repr(text)))
                return "", raw
            return text, raw
        except Exception as e:  # pragma: no cover
            conf.log("voice", f"stt error: {e}")
            return "", ""


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
