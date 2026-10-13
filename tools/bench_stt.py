"""Benchmark faster-whisper models for Helios's voice on THIS machine.

    .venv\\Scripts\\python.exe tools\\bench_stt.py [model ...] [--noisy] [--beam N]

Speaks a fixed set of Helios-style commands with the installed Windows voices (SAPI, written
straight to WAV — nothing plays), transcribes each clip with every model exactly the way Helios
does (CPU int8, English, greedy, the [voice] stt_prompt vocabulary), and reports word error rate
and latency. Results also go to data/bench/stt-<date>.json. Models download to the Helios model
cache on D: (never C:).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PHRASES = [
    "Hey Helios, what is on my screen right now?",
    "Open Chrome and search for the latest Polarion documentation.",
    "Find web development leads worldwide and show me the best three.",
    "Remind me to call the client at five thirty tomorrow evening.",
    "Check my Maqsusi project and tell me what changed overnight.",
    "Read this page and summarise it in two sentences.",
    "Turn the volume down to thirty percent and mute notifications.",
    "Draft a reply to the email from the bakery about the website quote.",
]


def norm_words(s: str) -> list[str]:
    s = s.lower().replace("summarize", "summarise").replace("30%", "thirty percent")
    s = re.sub(r"\b5:30\b", "five thirty", s)
    return re.findall(r"[a-z0-9']+", s)


def wer(ref: str, hyp: str) -> float:
    r, h = norm_words(ref), norm_words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def synth(voice: str, text: str, out: Path) -> None:
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
          "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
          f"$s.SetOutputToWaveFile('{out}', $f); $s.Speak(\"{text}\"); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def clips(noisy: bool):
    import numpy as np
    import soundfile as sf
    voices = ["Microsoft David Desktop", "Microsoft Zira Desktop"]
    tmp = Path(tempfile.mkdtemp(prefix="helios-bench-"))
    out = []
    rng = np.random.default_rng(7)
    for v in voices:
        for i, text in enumerate(PHRASES):
            p = tmp / f"{v.split()[1]}-{i}.wav"
            synth(v, text, p)
            audio, sr = sf.read(str(p), dtype="float32")
            out.append((f"{v.split()[1]}/clean", text, audio))
            if noisy:
                rms = float(np.sqrt(np.mean(audio ** 2))) or 1e-4
                noise = rng.normal(0, rms / 10 ** (15 / 20), audio.shape).astype("float32")   # ~15 dB SNR
                out.append((f"{v.split()[1]}/noisy", text, np.clip(audio + noise, -1, 1)))
    return out


def run(models: list[str], noisy: bool, beam: int) -> dict:
    from helios import conf
    from helios.voice.stt import model_root
    import psutil
    from faster_whisper import WhisperModel
    prompt = str(conf.voice_cfg().get("stt_prompt", "")).strip() or None
    data = clips(noisy)
    proc = psutil.Process()
    report = {"date": datetime.now().isoformat(timespec="seconds"), "beam": beam, "clips": len(data),
              "machine": _machine(), "models": {}}
    for name in models:
        rss0 = proc.memory_info().rss
        t = time.perf_counter()
        m = WhisperModel(name, device="cpu", compute_type="int8", download_root=model_root())
        load = time.perf_counter() - t
        m.transcribe(data[0][2], language="en", beam_size=beam)        # warm-up (not timed)
        lat, errs, rows = [], [], []
        for label, ref, audio in data:
            t = time.perf_counter()
            segs, _ = m.transcribe(audio, language="en", beam_size=beam, condition_on_previous_text=False,
                                   initial_prompt=prompt)
            hyp = " ".join(s.text.strip() for s in segs).strip()
            lat.append(time.perf_counter() - t)
            errs.append(wer(ref, hyp))
            rows.append({"clip": label, "ref": ref, "hyp": hyp, "wer": round(errs[-1], 3),
                         "sec": round(lat[-1], 3), "audio_sec": round(len(audio) / 16000, 2)})
        rss = (proc.memory_info().rss - rss0) / 1e6
        report["models"][name] = {
            "load_sec": round(load, 2), "mean_sec": round(sum(lat) / len(lat), 3),
            "p90_sec": round(sorted(lat)[int(0.9 * (len(lat) - 1))], 3),
            "rtf": round(sum(lat) / sum(r["audio_sec"] for r in rows), 3),
            "wer": round(sum(errs) / len(errs), 4),
            "wer_noisy": round(sum(r["wer"] for r in rows if "noisy" in r["clip"]) /
                               max(1, sum(1 for r in rows if "noisy" in r["clip"])), 4) if noisy else None,
            "rss_mb": round(rss), "rows": rows}
        del m
        print(f"{name:16} WER {report['models'][name]['wer']*100:5.1f}%  mean {report['models'][name]['mean_sec']:.2f}s "
              f"p90 {report['models'][name]['p90_sec']:.2f}s  RTF {report['models'][name]['rtf']:.2f}  "
              f"load {load:.1f}s  +{rss:.0f} MB", flush=True)
    out = conf.DATA_DIR / "bench"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"stt-{datetime.now():%Y%m%d-%H%M}.json"
    f.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"saved {f}")
    return report


def _machine() -> dict:
    try:
        from helios import hardware
        return hardware.detect(quick=True)
    except Exception:
        return {}


if __name__ == "__main__":
    args = sys.argv[1:]
    noisy = "--noisy" in args
    beam = int(args[args.index("--beam") + 1]) if "--beam" in args else 1
    names = [a for a in args if not a.startswith("--") and not a.isdigit()] or \
        ["tiny.en", "base.en", "distil-small.en", "small.en"]
    run(names, noisy, beam)
