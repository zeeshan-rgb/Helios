"""What this PC can do — CPU, RAM, GPU/VRAM, CUDA, free disk, microphone, browsers — and the
defaults that follow from it (speech model device/precision, whether GPU-only features make sense).

Fast (registry + psutil, no PowerShell/WMI): safe to call at startup and from voice-check.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

_GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
_BROWSERS = {
    "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
             r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
    "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
}


def _cpu_name() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        return platform.processor() or "unknown CPU"


def _gpus() -> list[dict]:
    """Display adapters from the registry: name + dedicated VRAM (GB, 0 = shared/integrated)."""
    out = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS) as root:
            for i in range(64):
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
                        vram = 0
                        for val in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                            try:
                                raw = winreg.QueryValueEx(k, val)[0]
                                vram = int.from_bytes(raw, "little") if isinstance(raw, bytes) else int(raw)
                                break
                            except OSError:
                                continue
                except OSError:
                    continue
                if name and not any(g["name"] == name for g in out) and "Basic Display" not in name:
                    out.append({"name": name, "vram_gb": round(vram / 1024 ** 3, 1)})
    except Exception:
        pass
    return out


def cuda_devices() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def _disks() -> dict:
    out = {}
    for d in ("C:\\", "D:\\", "E:\\"):
        if os.path.exists(d):
            try:
                out[d[0]] = round(shutil.disk_usage(d).free / 1024 ** 3, 1)
            except OSError:
                pass
    return out


def _mic() -> str:
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        return str(dev.get("name") or "")
    except Exception:
        return ""


def _browsers() -> list[str]:
    return [b for b, paths in _BROWSERS.items() if any(Path(os.path.expandvars(p)).exists() for p in paths)]


def recommend(info: dict) -> dict:
    """Defaults that follow from the hardware (used where a setting is 'auto')."""
    cuda = info.get("cuda_devices", 0) > 0
    big_gpu = any(g.get("vram_gb", 0) >= 6 for g in info.get("gpus", []))
    return {
        "stt_device": "cuda" if cuda else "cpu",
        "stt_compute_type": "float16" if cuda else "int8",
        # measured on an i7-8565U (tools/bench_stt.py): base.en is the latency/accuracy sweet spot
        # on a 4-core laptop CPU; bigger models only pay off with CUDA.
        "stt_model": "small.en" if cuda else "base.en",
        "visual_parser": "available" if (cuda and big_gpu) else "off (no CUDA GPU with 6 GB+)",
        "model_disk": "D" if "D" in info.get("disks", {}) else "C",
    }


def detect(quick: bool = False) -> dict:
    import psutil
    info = {
        "cpu": _cpu_name(), "cores": psutil.cpu_count(logical=False) or 0,
        "threads": psutil.cpu_count(logical=True) or 0,
        "ram_gb": round(psutil.virtual_memory().total / 1024 ** 3, 1),
        "gpus": _gpus(), "cuda_devices": cuda_devices(), "disks": _disks(),
        "browsers": _browsers(),
    }
    if not quick:
        info["mic"] = _mic()
    info["recommended"] = recommend(info)
    return info


def summary(info: dict | None = None) -> str:
    i = info or detect()
    gpu = ", ".join(f"{g['name']}" + (f" ({g['vram_gb']} GB)" if g.get("vram_gb") else "") for g in i["gpus"]) or "none found"
    disks = ", ".join(f"{d}: {free} GB free" for d, free in i["disks"].items())
    r = i["recommended"]
    lines = [f"CPU: {i['cpu']} ({i['cores']} cores / {i['threads']} threads) · RAM {i['ram_gb']} GB",
             f"GPU: {gpu} · CUDA devices: {i['cuda_devices']}",
             f"Disk: {disks}",
             f"Browsers: {', '.join(i['browsers']) or 'none'}" + (f" · Mic: {i['mic'] or 'none'}" if "mic" in i else ""),
             f"Defaults: speech {r['stt_model']} on {r['stt_device']} ({r['stt_compute_type']}), "
             f"models on {r['model_disk']}:, visual parser {r['visual_parser']}"]
    return "\n".join(lines)
