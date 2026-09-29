"""Shared Windows process-tree kill helpers.

Used by every place that spawns a `claude -p` child (brain, side agents, missions) so the
teardown logic lives in ONE spot and can't drift. `taskkill /F /T` kills the whole tree,
which matters because the venv pythonw stub spawns a base-python child AND the CLI spawns
its own helpers/MCP servers — a plain proc.kill() would orphan those.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000  # don't flash a console when spawning taskkill
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def kill_tree(proc: subprocess.Popen) -> None:
    """Force-kill a process and its whole child tree by Popen handle. No-op if already dead.
    Falls back to proc.kill() if taskkill is unavailable."""
    if proc and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def kill_pid(pid: int) -> None:
    """Force-kill a process tree by PID (when we don't hold the Popen — e.g. a worker spawned in
    another process). taskkill /T no-ops on an already-gone PID, so this is safe on stale PIDs."""
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def creation_time(pid: int) -> int | None:
    """Creation time (FILETIME as a 64-bit int) of a live PID, or None if it can't be opened.

    Pairs a recorded PID with its start time so a recycled PID (Windows reuses them) now owned by
    an unrelated process can't be mistaken for ours and killed. restype/argtypes MUST be set or the
    HANDLE truncates to 32-bit on 64-bit Windows and the calls silently fail. (Same technique the
    orb uses for orb.pid; centralized here for the voice daemon too.)"""
    try:
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = wt.HANDLE
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
        k.GetProcessTimes.restype = wt.BOOL
        k.CloseHandle.argtypes = [wt.HANDLE]
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            c = wt.FILETIME(); e = wt.FILETIME(); kt = wt.FILETIME(); ut = wt.FILETIME()
            if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                     ctypes.byref(kt), ctypes.byref(ut)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


def own_creation_time() -> int | None:
    """This process's creation time (FILETIME 64-bit int), or None. Recorded next to a PID file so
    the launcher can verify the PID hasn't been recycled before killing a presumed-orphan."""
    return creation_time(os.getpid())
