#!/usr/bin/env pythonw
"""Helios entrypoint. Run with pythonw.exe for a windowless always-on process.

    pythonw run_helios.pyw

The .pyw extension makes Windows launch this with pythonw.exe (no console window). Under
the venv, pythonw.exe is a thin launcher stub that spawns the real base-Python child, so
Helios shows up as TWO processes in Task Manager — that is normal, not a leak/duplicate.
All the real wiring (server, brain, scheduler, hotkeys, orb overlay, bridges) lives in
helios.app.main(); this file only fixes up sys.path and calls it.
"""
import os
import sys

# Ensure this repo root is importable so `import helios...` works regardless of cwd
# (e.g. when launched from a Start-menu / autostart shortcut).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helios.app import main

if __name__ == "__main__":
    main()
