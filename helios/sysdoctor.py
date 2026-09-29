"""System doctor - a health snapshot + heuristic diagnosis for "why is my PC slow/stuttering/hot?".

Pulls CPU / RAM / swap / disk (psutil), GPU util+VRAM+temperature (nvidia-smi), battery + power plan
(Win32 / powercfg), and the top resource-hogging processes (aggregated by name). Then `_diagnose`
flags likely problems - pegged CPU, RAM paging, a hot/maxed GPU, thermal-throttle hints, a
power-saver plan or running on battery, disk pressure, and heavy background apps - so the brain can
answer naturally and suggest fixes. Read-only; no admin required (CPU temp needs a helper tool, so
it's reported best-effort).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import re
import subprocess
import time
from pathlib import Path

_NO_WINDOW = 0x08000000
_LHM_JSON = Path(__file__).resolve().parent.parent / "data" / "lhm.json"


def _lhm(max_age: float = 30.0) -> dict | None:
    """CPU temps + fan RPM from the elevated LibreHardwareMonitor helper (helios/lhm_sensors.py),
    if it's running and fresh. None if the helper isn't set up / not elevated."""
    try:
        d = json.loads(_LHM_JSON.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) > max_age:
            return None
        return d
    except Exception:
        return None


def _hvci_on() -> bool:
    """Is Memory Integrity (Core Isolation / HVCI) enabled? It blocks vulnerable kernel drivers,
    including LibreHardwareMonitor's WinRing0 - so CPU core temps + fan RPM can't be read while it's
    on, regardless of elevation. Used to explain *why* those sensors are missing."""
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity")
        try:
            v, _ = winreg.QueryValueEx(k, "Enabled")
            return v == 1
        finally:
            winreg.CloseKey(k)
    except Exception:
        return False


def _f(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ GPU (nvidia-smi)
def _gpus() -> list[dict]:
    try:
        q = ("name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
             "power.draw,power.limit,clocks_throttle_reasons.active")
        out = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=6, creationflags=_NO_WINDOW)
        gpus = []
        for line in out.stdout.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 5:
                continue
            gpus.append({"name": p[0], "util": _f(p[1]), "mem_used_mb": _f(p[2]),
                         "mem_total_mb": _f(p[3]), "temp_c": _f(p[4]),
                         "power_w": _f(p[5]) if len(p) > 5 else None,
                         "power_limit_w": _f(p[6]) if len(p) > 6 else None})
        return gpus
    except Exception:
        return []


# ------------------------------------------------------------------ battery / power
class _PWR(ctypes.Structure):
    _fields_ = [("ACLineStatus", wt.BYTE), ("BatteryFlag", wt.BYTE),
                ("BatteryLifePercent", wt.BYTE), ("SystemStatusFlag", wt.BYTE),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def _power() -> dict:
    """(percent, plugged-in). 255% / None means no battery or the call failed."""
    try:
        s = _PWR()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            pct = int(s.BatteryLifePercent)
            return {"percent": (None if pct == 255 else pct),
                    "plugged": bool(s.ACLineStatus == 1),
                    "has_battery": s.BatteryFlag != 128}
    except Exception:
        pass
    return {"percent": None, "plugged": True, "has_battery": False}


def _power_plan() -> str | None:
    try:
        out = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True,
                             timeout=4, creationflags=_NO_WINDOW)
        m = re.search(r"\(([^)]+)\)", out.stdout)
        return m.group(1).strip() if m else None
    except Exception:
        return None


# ------------------------------------------------------------------ snapshot
def snapshot(top_n: int = 6) -> dict:
    import psutil

    ncpu = psutil.cpu_count(logical=True) or 1
    procs = list(psutil.process_iter(["pid", "name"]))
    for p in procs:                       # prime per-process CPU counters
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    psutil.cpu_percent(None)
    io0 = psutil.disk_io_counters()
    t0 = time.time()
    time.sleep(0.7)                       # one sampling window for CPU + disk IO
    cpu_total = psutil.cpu_percent(None)
    io1 = psutil.disk_io_counters()
    dt = max(0.1, time.time() - t0)

    # aggregate top processes by NAME (so "chrome (12 procs)" reads naturally)
    agg: dict[str, dict] = {}
    for p in procs:
        try:
            with p.oneshot():
                name = (p.info.get("name") or "?")
                # "System Idle Process" (pid 0) is the IDLE counter - its CPU% is FREE CPU, not
                # usage; including it makes "top CPU" nonsense. Skip it.
                if p.info.get("pid") == 0 or name in ("System Idle Process", "Idle"):
                    continue
                c = p.cpu_percent(None)
                rss = p.memory_info().rss
        except Exception:
            continue
        a = agg.setdefault(name, {"cpu": 0.0, "rss": 0, "count": 0})
        a["cpu"] += c
        a["rss"] += rss
        a["count"] += 1
    for a in agg.values():
        a["cpu_pct"] = round(a["cpu"] / ncpu, 1)   # % of total CPU (psutil counts per-core)
        a["mem_gb"] = round(a["rss"] / 1e9, 2)
    top_cpu = sorted(agg.items(), key=lambda kv: kv[1]["cpu_pct"], reverse=True)[:top_n]
    top_mem = sorted(agg.items(), key=lambda kv: kv[1]["mem_gb"], reverse=True)[:top_n]

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    freq = psutil.cpu_freq()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"drive": part.device, "total_gb": round(u.total / 1e9, 1),
                          "free_gb": round(u.free / 1e9, 1), "percent": u.percent})
        except Exception:
            pass
    io_mb_s = None
    if io0 and io1:
        io_mb_s = round((io1.read_bytes + io1.write_bytes - io0.read_bytes - io0.write_bytes)
                        / dt / 1e6, 1)

    snap = {
        "cpu": {"percent": cpu_total, "cores": ncpu,
                "freq_mhz": round(freq.current) if freq else None,
                "freq_max_mhz": round(freq.max) if freq and freq.max else None},
        "memory": {"percent": vm.percent, "used_gb": round(vm.used / 1e9, 1),
                   "total_gb": round(vm.total / 1e9, 1), "available_gb": round(vm.available / 1e9, 1)},
        "swap": {"percent": sm.percent, "used_gb": round(sm.used / 1e9, 1)},
        "disks": disks, "disk_io_mb_s": io_mb_s,
        "gpus": _gpus(),
        "battery": _power(), "power_plan": _power_plan(),
        "process_count": len(procs),
        "top_cpu": [{"name": n, **v} for n, v in top_cpu],
        "top_mem": [{"name": n, **v} for n, v in top_mem],
    }
    # CPU temperature + fan RPM (needs the elevated LibreHardwareMonitor helper).
    lhm = _lhm()
    snap["cpu_temp_c"] = lhm.get("cpu_temp_c") if lhm else None
    snap["cpu_temp_max_c"] = lhm.get("cpu_temp_max_c") if lhm else None
    snap["cpu_temp_source"] = lhm.get("cpu_temp_source") if lhm else None
    snap["fans"] = (lhm.get("fans") or []) if lhm else []
    snap["lhm_gpu_temps"] = (lhm.get("gpu_temps") or {}) if lhm else {}
    snap["lhm_running"] = bool(lhm)          # helper is registered + writing fresh data
    snap["lhm_ok"] = bool(lhm)
    snap["hvci"] = _hvci_on()                # Memory Integrity blocks the kernel sensor driver
    snap["issues"] = _diagnose(snap)
    return snap


def _diagnose(s: dict) -> list[str]:
    """Heuristic findings - the doctor's read on what's likely hurting performance."""
    out = []
    cpu = s["cpu"]["percent"] or 0
    if cpu >= 85:
        out.append(f"CPU is pegged at {cpu:.0f}% - something is working it hard.")
    elif cpu >= 65:
        out.append(f"CPU is busy ({cpu:.0f}%).")
    top = s["top_cpu"][0] if s["top_cpu"] else None
    if top and top["cpu_pct"] >= 35:
        out.append(f"\"{top['name']}\" alone is using {top['cpu_pct']:.0f}% of the CPU"
                   + (f" across {top['count']} processes." if top['count'] > 1 else "."))
    mem = s["memory"]
    if mem["percent"] >= 90 or mem["available_gb"] < 1.0:
        out.append(f"RAM is {mem['percent']:.0f}% full ({mem['used_gb']}/{mem['total_gb']} GB, "
                   f"{mem['available_gb']} GB free) - Windows is likely paging to disk, a common "
                   f"cause of stutter.")
    if s["swap"]["percent"] >= 60:
        out.append(f"Heavy paging (swap {s['swap']['percent']:.0f}% used) - not enough free RAM.")
    tm = s["top_mem"][0] if s["top_mem"] else None
    if tm and tm["mem_gb"] >= 2.5 and tm["count"] >= 4:
        out.append(f"\"{tm['name']}\" is using {tm['mem_gb']} GB across {tm['count']} processes "
                   f"- closing some would free memory.")
    for d in s["disks"]:
        if d["percent"] >= 95:
            out.append(f"Drive {d['drive']} is {d['percent']:.0f}% full ({d['free_gb']} GB free) "
                       f"- low free space slows Windows down.")
    if s["disk_io_mb_s"] and s["disk_io_mb_s"] >= 80:
        out.append(f"Disk is busy ({s['disk_io_mb_s']} MB/s) - heavy disk activity can stutter.")
    for g in s["gpus"]:
        t = g.get("temp_c")
        if t and t >= 90:
            out.append(f"GPU ({g['name']}) is very hot at {t:.0f}°C - it's likely thermal-throttling.")
        elif t and t >= 84:
            out.append(f"GPU ({g['name']}) is running hot ({t:.0f}°C).")
        if g.get("util") and g["util"] >= 97:
            out.append(f"GPU is maxed out ({g['util']:.0f}%).")
    ct = s.get("cpu_temp_c")
    if s.get("cpu_temp_source") == "acpi":
        # A driver-free ACPI thermal zone (system-level), used because Memory Integrity blocks the
        # real per-core sensor. These are coarse and sometimes static/offset on laptops, so only
        # flag a genuinely dangerous reading and label it as approximate - don't cry wolf.
        if ct and ct >= 97:
            out.append(f"System thermal zone is very hot ({ct:.0f}°C, ACPI reading - approximate). "
                       f"Worth checking airflow/dust; for an exact CPU temp, see the note below.")
    elif ct and ct >= 95:
        out.append(f"CPU is very hot ({ct:.0f}°C) - likely thermal-throttling. Check airflow/dust, "
                   f"and consider a cooling pad or repaste.")
    elif ct and ct >= 88:
        out.append(f"CPU is running hot ({ct:.0f}°C) under this load.")
    fc, fm = s["cpu"]["freq_mhz"], s["cpu"]["freq_max_mhz"]
    if fc and fm and cpu >= 60 and fc < 0.6 * fm:
        out.append(f"CPU is running at {fc} MHz of {fm} MHz under load - possible thermal/power "
                   f"throttling.")
    bat = s["battery"]
    plan = (s["power_plan"] or "").lower()
    if bat.get("has_battery") and not bat.get("plugged"):
        out.append("Running on battery - Windows throttles performance to save power; plug in for "
                   "smoother performance.")
    if "saver" in plan or "power saver" in plan:
        out.append(f"Power plan is \"{s['power_plan']}\" - that caps performance; switch to "
                   f"Balanced or High performance.")
    if s["process_count"] >= 400:
        out.append(f"A lot is running ({s['process_count']} processes) - background apps may be "
                   f"competing for resources.")
    if not out:
        out.append("Nothing obviously wrong - CPU, RAM, GPU and temperatures all look healthy.")
    return out


def report(top_n: int = 6) -> str:
    """A readable health report (what the MCP tool returns to the brain)."""
    s = snapshot(top_n)
    L = []
    c = s["cpu"]
    L.append(f"CPU: {c['percent']:.0f}% of {c['cores']} cores"
             + (f" @ {c['freq_mhz']}/{c['freq_max_mhz']} MHz" if c.get("freq_max_mhz") else ""))
    m = s["memory"]
    L.append(f"RAM: {m['percent']:.0f}% used - {m['used_gb']}/{m['total_gb']} GB "
             f"({m['available_gb']} GB free); swap {s['swap']['percent']:.0f}%")
    for g in s["gpus"]:
        L.append(f"GPU: {g['name']} - {g['util']:.0f}% util, "
                 f"{g['mem_used_mb']:.0f}/{g['mem_total_mb']:.0f} MB VRAM, {g['temp_c']:.0f}°C"
                 + (f", {g['power_w']:.0f}W" if g.get("power_w") else ""))
    if not s["gpus"]:
        L.append("GPU: no NVIDIA GPU data (nvidia-smi unavailable)")
    if s.get("lhm_gpu_temps", {}).get("GPU Hot Spot"):
        L.append(f"   (GPU hot-spot {s['lhm_gpu_temps']['GPU Hot Spot']:.0f}°C)")
    if s.get("cpu_temp_c") is not None:
        src = " (ACPI thermal zone, system-level)" if s.get("cpu_temp_source") == "acpi" else ""
        L.append(f"CPU temp: {s['cpu_temp_c']:.0f}°C"
                 + (f" (hottest core {s['cpu_temp_max_c']:.0f}°C)"
                    if s.get('cpu_temp_max_c') and s['cpu_temp_max_c'] != s['cpu_temp_c'] else "")
                 + src)
    if s.get("fans"):
        L.append("Fans: " + ", ".join(f"{f['name']} {f['rpm']} RPM" for f in s["fans"]))
    # Explain a missing CPU temp / fans precisely: not-set-up vs blocked-by-Memory-Integrity.
    if s.get("cpu_temp_c") is None or not s.get("fans"):
        if not s.get("lhm_running"):
            L.append("CPU temp / fan RPM: unavailable - run tools/setup_lhm.ps1 as admin once "
                     "(LibreHardwareMonitor needs kernel access for those).")
        elif s.get("hvci"):
            missing = "Fan RPM" if s.get("cpu_temp_c") is not None else "CPU core temps + fan RPM"
            L.append(f"{missing}: blocked by Memory Integrity (Core Isolation). It blocks the "
                     "kernel sensor driver LibreHardwareMonitor needs, even when elevated. To read "
                     "them, turn Memory Integrity off (Windows Security > Device security > Core "
                     "isolation) and reboot - a security trade-off, your call. GPU temp above is "
                     "unaffected.")
        else:
            L.append("CPU temp / fan RPM: sensor helper is running but can't read the kernel "
                     "sensors (driver didn't load).")
    L.append("Disks: " + "; ".join(f"{d['drive']} {d['percent']:.0f}% full ({d['free_gb']} GB free)"
                                    for d in s["disks"])
             + (f"  |  IO {s['disk_io_mb_s']} MB/s" if s["disk_io_mb_s"] is not None else ""))
    bat = s["battery"]
    if bat.get("has_battery"):
        L.append(f"Power: {'plugged in' if bat['plugged'] else 'on battery'}"
                 + (f", {bat['percent']}%" if bat['percent'] is not None else "")
                 + (f"  |  plan: {s['power_plan']}" if s['power_plan'] else ""))
    elif s["power_plan"]:
        L.append(f"Power plan: {s['power_plan']}")
    L.append(f"Processes: {s['process_count']}")
    L.append("Top CPU: " + ", ".join(f"{p['name']} {p['cpu_pct']:.0f}%" for p in s["top_cpu"][:5]))
    L.append("Top RAM: " + ", ".join(f"{p['name']} {p['mem_gb']}GB"
                                      + (f"x{p['count']}" if p['count'] > 1 else "")
                                      for p in s["top_mem"][:5]))
    L.append("")
    L.append("FINDINGS:")
    L.extend(f"  - {i}" for i in s["issues"])
    return "\n".join(L)


if __name__ == "__main__":
    print(report())
