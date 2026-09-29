"""Elevated sensor helper — reads CPU temperatures + fan RPM via LibreHardwareMonitor and writes
them to data/lhm.json for the system doctor (helios/sysdoctor.py) to merge in.

CPU core temps and fan tach require Ring0 (kernel) access, so LibreHardwareMonitor's WinRing0 driver
only works ELEVATED. Helios itself runs un-elevated, so this helper is registered as its OWN
scheduled task at RunLevel=Highest (see tools/setup_lhm.ps1), mirroring the cua-driver daemon. It
loops, refreshing the JSON every few seconds; sysdoctor reads the file if it's fresh.

LibreHardwareMonitorLib.dll + HidSharp.dll live in data/lhm/ (downloaded from NuGet, gitignored).
Loaded via pythonnet (.NET Framework). No admin = the file still writes, just without the temps
that need Ring0.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helios import conf  # noqa: E402

_LHM_DIR = conf.DATA_DIR / "lhm"
_OUT = conf.DATA_DIR / "lhm.json"
_INTERVAL = 5.0

_computer = None


def _open_computer():
    """Load LibreHardwareMonitor via pythonnet and open the CPU/motherboard/controller/GPU groups."""
    global _computer
    import clr
    # Load LHM's dependency assemblies BY PATH first — the CLR won't probe data/lhm/ for them, so
    # if LHM tries to bind System.Memory etc. lazily it fails with "cannot find the file". Loading
    # them up front (LoadFrom) puts them in the AppDomain so LHM resolves them.
    for dep in ("System.Runtime.CompilerServices.Unsafe.dll", "System.Numerics.Vectors.dll",
                "System.Buffers.dll", "System.Memory.dll", "HidSharp.dll"):
        p = _LHM_DIR / dep
        if p.exists():
            try:
                clr.AddReference(str(p))
            except Exception as e:
                conf.log("lhm", f"dep load warn {dep}: {e}")
    clr.AddReference(str(_LHM_DIR / "LibreHardwareMonitorLib.dll"))
    from LibreHardwareMonitor.Hardware import Computer
    c = Computer()
    c.IsCpuEnabled = True
    c.IsMotherboardEnabled = True   # Super I/O fans
    c.IsControllerEnabled = True    # embedded-controller fans (laptops)
    c.IsGpuEnabled = True           # GPU temp (bonus; nvidia-smi already covers NVIDIA)
    c.Open()
    _computer = c
    return c


def read_sensors() -> dict:
    """One refresh: returns {cpu_temp_c, cpu_temp_max_c, cpu_temps{}, fans[], gpu_temps{}, elevated}."""
    from LibreHardwareMonitor.Hardware import HardwareType, SensorType
    c = _computer or _open_computer()
    cpu_temps, gpu_temps, fans = {}, {}, []

    def visit(hw):
        try:
            hw.Update()
        except Exception:
            return
        for sub in hw.SubHardware:
            visit(sub)
        for s in hw.Sensors:
            try:
                if s.Value is None:
                    continue
                v = round(float(s.Value), 1)
            except Exception:
                continue
            if s.SensorType == SensorType.Temperature:
                if hw.HardwareType == HardwareType.Cpu:
                    cpu_temps[str(s.Name)] = v
                elif "Gpu" in str(hw.HardwareType):
                    gpu_temps[str(s.Name)] = v
            elif s.SensorType == SensorType.Fan and v > 0:
                fans.append({"name": str(s.Name), "rpm": int(v)})

    for hw in c.Hardware:
        visit(hw)

    # Pick a representative CPU temp: prefer a package/Tctl/Tdie sensor, else the hottest core.
    pkg = None
    for k, v in cpu_temps.items():
        kl = k.lower()
        if "package" in kl or "tctl" in kl or "tdie" in kl or kl == "cpu":
            pkg = v
            break
    cpu_max = max(cpu_temps.values()) if cpu_temps else None
    cpu_temp = pkg if pkg is not None else cpu_max
    source = "lhm" if cpu_temp is not None else None
    # Driver-free fallback: ACPI thermal zone via WMI (no WinRing0, so it survives Memory
    # Integrity — but needs admin, which this elevated helper has). Often a system/CPU-ish temp;
    # not all laptops expose it. Fans have no such fallback (they need the kernel driver).
    if cpu_temp is None:
        acpi = _acpi_temp()
        if acpi is not None:
            cpu_temp, source = acpi, "acpi"
    return {
        "ts": time.time(),
        "cpu_temp_c": cpu_temp,
        "cpu_temp_max_c": cpu_max,
        "cpu_temp_source": source,
        "cpu_temps": cpu_temps,
        "gpu_temps": gpu_temps,
        "fans": fans,
        "elevated": bool(cpu_temps),   # CPU temps via LHM only populate with Ring0/admin
    }


def _acpi_temp():
    """ACPI thermal-zone temperature via WMI (root/wmi MSAcpi_ThermalZoneTemperature). No kernel
    driver, so it isn't blocked by Memory Integrity; needs admin (this helper is elevated). Returns
    the hottest sane zone in °C, or None if the firmware doesn't expose it."""
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
             "-ErrorAction Stop).CurrentTemperature"],
            capture_output=True, text=True, timeout=10, creationflags=0x08000000)
        vals = []
        for tok in out.stdout.split():
            if tok.strip().isdigit():
                c = int(tok) / 10.0 - 273.15
                if 20 < c < 110:
                    vals.append(round(c, 1))
        return max(vals) if vals else None
    except Exception:
        return None


def _write(data: dict):
    try:
        _OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = _OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, _OUT)
    except Exception as e:  # pragma: no cover
        conf.log("lhm", f"write failed: {e}")


def main():
    once = "--once" in sys.argv
    try:
        _open_computer()
    except Exception as e:
        conf.log("lhm", f"could not load LibreHardwareMonitor: {e}")
        _write({"ts": time.time(), "error": str(e)[:200], "fans": [], "cpu_temps": {}})
        return
    conf.log("lhm", "sensor helper started")
    while True:
        try:
            data = read_sensors()
            _write(data)
            if once:
                print(json.dumps(data, indent=2))
                return
        except Exception as e:  # pragma: no cover
            conf.log("lhm", f"read error: {e}")
        time.sleep(_INTERVAL)


if __name__ == "__main__":
    main()
