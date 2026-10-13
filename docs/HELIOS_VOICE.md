# Helios — voice

```
double-clap / "Hey Helios" / Ctrl+Alt+H
  → chime → microphone (one 16 kHz stream, owned by the voice daemon)
  → RealtimeSTT capture: WebRTC + Silero endpointing, live text (tiny.en, 2 threads), final
    transcription started early during your pause
  → faster-whisper final text (base.en, CPU int8, vocabulary hint) + junk/confidence gate
  → voice lock (if enrolled) → local command or the brain (Antigravity)
  → Kokoro TTS (am_michael) → 6 s follow-up window, barge-in with "hey Helios"
```

## Speech-to-text settings — `[voice.stt]`

Preferred over the older flat `stt_*` keys, which still work.

| Key | Default | Meaning |
|---|---|---|
| `model` | `base.en` | `tiny.en` / `base.en` / `small.en` / `distil-small.en`; `base` / `small` are multilingual |
| `device` | `auto` | CUDA GPU if CTranslate2 sees one, else CPU. A GPU that fails to load falls back to CPU int8 |
| `compute_type` | `auto` | int8 on CPU, float16 on CUDA |
| `language` | `en` | `auto` detects (needs a multilingual model; `*.en` models are forced to English with a log line) |
| `beam_size` | `1` | Greedy decoding |
| `cpu_threads` | `0` | CTranslate2 default |
| `vad_filter` | `false` | faster-whisper's own VAD (capture already does endpointing) |
| `vocabulary` | `[]` | Extra words; also see "Vocabulary" below |
| `download_root` | — | Model folder. Default: `$HF_HOME/hub`, else `<data drive>/cache/hf/hub` |

* **Models:** stored in `D:\Helios\cache\hf\hub`. `conf.py` sets `HF_HOME` to that folder if the
  environment doesn't, so C: is never used.
* **Loading:** models load from the local cache first (`local_files_only`) and download only when
  missing. Measured 0.6–1.2 s instead of 0.8–16 s, and voice works offline.
* **Timestamps and language:** `Transcriber.transcribe_detailed()` returns text, the detected
  language and its probability, plus segment and word timestamps.
* **Clean shutdown:** the daemon's `close()` releases the model.

## Model choice — measured, not guessed

`tools/bench_stt.py` (i7-8565U, no CUDA; 8 Helios-style commands × 2 Windows voices × clean +
15 dB-noise = 32 clips; CPU int8, greedy):

| Model | WER clean | WER noisy | Median latency | Load | RAM |
|---|---|---|---|---|---|
| tiny.en | 4.0% | 5.5% | 0.59 s | 4.6 s* | +121 MB |
| **base.en** | **5.2%** | **5.2%** | **1.11 s** | 1.0 s | +139 MB |
| distil-small.en | 4.9% | 6.2% | 3.38 s | 44.5 s* | +267 MB |
| small.en | 4.7% | 4.7% | 3.65 s | 47.9 s* | +333 MB |

\*First load includes the download.

* **Most "errors" are formatting:** "5.30" for "five thirty", "3" for "three". The brain reads
  them fine.
* **The real errors were names:** every model misheard "Maqsusi"; tiny and distil also misheard
  "Polarion".
* **Decision: keep `base.en`.** It's as steady as `small.en` in noise at about a third of the
  latency, and better than `tiny.en` in noise. Larger models only pay off with a CUDA GPU (the
  hardware detector then suggests `small.en`).

## Vocabulary (measured gain)

Whisper's `initial_prompt` is built automatically (`stt.vocabulary_prompt`) from your
`stt_prompt`, built-in terms (Helios, Polarion, Antigravity), `[voice.stt] vocabulary` and the
names of your tracked projects (deduplicated, at most 40 terms). The live-text model gets the
same hint.

| base.en, 32 clips | WER | Project names right |
|---|---|---|
| Old prompt | 5.2% | 4 / 8 |
| + project names | **2.8%** | **8 / 8** |

## Diagnostics

`helios voice-check` reports:
* hardware (CPU, RAM, GPU, CUDA, suggested model);
* audio devices;
* TTS;
* STT (device, precision, language, beam, load and transcribe time, model folder; warns if
  models are on C:);
* capture engine, wake word and voice lock.

`helios voice-check --mic` also records 3 s (nothing saved) and reports level, peak, noise floor
and clipping, with a verdict: no sound (muted or blocked), clipping, noisy room, very quiet, or OK.

## Hardware detection — `helios/hardware.py`

CPU, cores/threads, RAM, GPUs and dedicated VRAM (registry), CUDA devices (CTranslate2), free
disk per drive, browsers and microphone, plus derived defaults (speech device/precision/model,
whether a GPU visual parser makes sense, which drive holds models). Fast (~1.5 s, no WMI).
