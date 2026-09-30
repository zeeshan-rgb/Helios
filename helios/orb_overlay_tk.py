"""Standalone reactive orb overlay (separate process).

Rendered with tkinter, not WebView2, because WebView2 can't paint a transparent overlay
on Windows (its DirectComposition surface ignores window transparency). tkinter's
`-transparentcolor` is a real GDI color-key, so the area *outside* the orb is truly
see-through and click-through.

The orb itself is an opaque circular sphere body with the neural mesh drawn on top, so the
WHOLE disc is a reliable click target (color-keyed gaps would otherwise pass clicks through).
It stays above every window (periodic HWND_TOPMOST) and hides only when a fullscreen app owns
the primary monitor.

Runs as its own process: streams state from the app's /events SSE feed and toggles the
dashboard by POSTing /orb/toggle. Exits on its own if the app goes away.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import math
import os
import sys
import threading
import time
import tkinter as tk
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helios import conf  # noqa: E402

KEY = "#000001"          # color-key -> transparent (orb never uses it)
SIZE = 170
MARGIN = 20
N = 46
DRAG_SLOP = 4            # px of movement that turns a click into a drag (no toggle)
_POS_FILE = conf.DATA_DIR / "orb_pos.json"   # remembers where you dropped the orb

# state colours [r,g,b], spin (rad/s), pulse (hz). The voice states reuse the dashboard's accent
# vocabulary: listening=green (live mic), speaking=bright cyan (Helios's voice), dictation=violet.
STATES = {
    "idle":      ((77, 208, 225), 0.22, 0.9),
    "thinking":  ((102, 224, 255), 0.65, 2.0),
    "acting":    ((214, 162, 99), 0.5, 1.6),
    "error":     ((224, 85, 107), 0.9, 3.0),
    "listening": ((126, 224, 160), 0.42, 1.5),
    "speaking":  ((102, 224, 255), 0.55, 1.9),
    "dictation": ((189, 170, 240), 0.48, 1.6),
}

# ---- Win32 (always-on-top + fullscreen detection) ----
_u = ctypes.windll.user32
GA_ROOT = 2
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
_u.GetAncestor.restype = wintypes.HWND
_u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
_u.GetForegroundWindow.restype = wintypes.HWND
_u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
MONITOR_DEFAULTTONEAREST = 2
_u.MonitorFromWindow.restype = wintypes.HANDLE  # HMONITOR — set type or it truncates on 64-bit
_u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_u.GetMonitorInfoW.restype = wintypes.BOOL


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _set_topmost(hwnd):
    if hwnd:
        _u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def _virtual_bounds():
    """(left, top, width, height) of the whole virtual desktop (all monitors)."""
    g = _u.GetSystemMetrics
    return g(76), g(77), g(78), g(79)  # SM_*VIRTUALSCREEN


def _fullscreen_on_primary(own_hwnd) -> bool:
    """True when the FOREGROUND window is a real fullscreen app (not just maximized) covering the
    entire monitor it sits on. Uses MonitorFromWindow + GetMonitorInfo (NOT GetSystemMetrics(0/1),
    which is only the primary monitor) so it's correct on multi-monitor setups and monitors at
    negative coordinates. Maximized windows stop at the work area (taskbar visible) so don't match.
    """
    fg = _u.GetForegroundWindow()
    if not fg or fg == own_hwnd:
        return False
    buf = ctypes.create_unicode_buffer(64)
    _u.GetClassNameW(fg, buf, 64)
    if buf.value in ("Progman", "WorkerW"):  # the desktop itself
        return False
    rect = wintypes.RECT()
    if not _u.GetWindowRect(fg, ctypes.byref(rect)):
        return False
    mon = _u.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)
    if not mon:
        return False
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _u.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor  # the actual rectangle of the monitor the foreground window is on
    return (rect.left <= m.left and rect.top <= m.top
            and rect.right >= m.right and rect.bottom >= m.bottom)


def _hex(r: float, g: float, b: float) -> str:
    r = int(max(0, min(255, r))); g = int(max(0, min(255, g))); b = int(max(0, min(255, b)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _sphere_nodes(n: int):
    nodes = []
    for i in range(n):
        y = 1 - (i / (n - 1)) * 2
        r = math.sqrt(max(0.0, 1 - y * y))
        phi = i * math.pi * (3 - math.sqrt(5))
        nodes.append((math.cos(phi) * r, y, math.sin(phi) * r))
    return nodes


def _edges(nodes):
    e = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            dx = nodes[i][0] - nodes[j][0]
            dy = nodes[i][1] - nodes[j][1]
            dz = nodes[i][2] - nodes[j][2]
            if dx * dx + dy * dy + dz * dz < 0.30:
                e.append((i, j))
    return e


_PID_FILE = conf.DATA_DIR / "orb.pid"


def _own_creation_time():
    """This process's creation time (FILETIME as a 64-bit int), or None. The app records this
    alongside our PID so it can verify the PID hasn't been recycled before killing it.
    NOTE: restype/argtypes MUST be declared — otherwise the HANDLE is truncated to 32-bit on
    64-bit Windows and GetProcessTimes silently fails (returns 0 -> None)."""
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        c = wintypes.FILETIME(); e = wintypes.FILETIME()
        kt = wintypes.FILETIME(); ut = wintypes.FILETIME()
        if k.GetProcessTimes(k.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                             ctypes.byref(kt), ctypes.byref(ut)):
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
    except Exception:
        pass
    return None


def _write_pid():
    """Record {pid, ctime} so app._kill_stray_orb can kill a genuine orphan without risking a
    recycled-PID kill of an unrelated process."""
    try:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(json.dumps({"pid": os.getpid(), "ctime": _own_creation_time()}),
                             encoding="utf-8")
    except Exception:
        pass


def _clear_pid():
    """Remove orb.pid on exit so a stale entry can't trigger a wrong-process kill next launch."""
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


class Orb:
    def __init__(self):
        self.nodes = _sphere_nodes(N)
        self.edges = _edges(self.nodes)
        self.state = "idle"
        self.voice_active = False   # a voice interaction owns the orb (overrides brain idle/think)
        self.cur = [77.0, 208.0, 225.0]
        self.spin = 0.22
        self.pulse = 0.9
        self.level = 0.0          # live mic/TTS level from the voice daemon (0..1)
        self.level_smooth = 0.0
        self.ang = 0.0
        self.t = 0.0
        self._err_until = 0.0
        self.hwnd = 0
        self.hidden = False
        self.hidden_dash = False    # dashboard is open — set by _events(), applied in _watch_window()
        self.asleep = False         # Helios is asleep — hide (the process stays up, no relaunch)
        self._drag = None

        # Record our PID (+ creation time) so a freshly-launched app can kill an orphaned orb
        # from a previous run before spawning a new one (otherwise quick restarts leave orbs
        # stacked, because the health-watchdog only self-exits after ~20s offline). See
        # app._launch_orb / _kill_stray_orb.
        _write_pid()

        # Make this process DPI-aware BEFORE creating the Tk root, so tkinter's coordinates and
        # the Win32 GetSystemMetrics/SetWindowPos values agree under display scaling (otherwise
        # the orb mis-positions on a scaled display).
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.root.config(bg=KEY)
        sw = self.root.winfo_screenwidth()
        x, y = self._load_pos(sw - SIZE - MARGIN, MARGIN)
        self.root.geometry(f"{SIZE}x{SIZE}+{x}+{y}")
        self.cv = tk.Canvas(self.root, width=SIZE, height=SIZE, bg=KEY,
                            highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        # Click toggles the dashboard; drag (past DRAG_SLOP) moves the orb.
        self.cv.bind("<Button-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_release)

        # Opaque sphere body behind the mesh so the WHOLE orb is a reliable click target.
        # (With color-key transparency, the gaps between mesh lines/nodes click straight
        # through to the desktop — drawing a solid disc gives a proper hitbox.)
        c = SIZE / 2
        DR = 44
        self.cv.create_oval(c - DR, c - DR, c + DR, c + DR,
                            fill="#0a0d12", outline="#4dd0e1", width=2)
        self.edge_items = [self.cv.create_line(0, 0, 0, 0, fill=KEY, width=2) for _ in self.edges]
        self.node_items = [self.cv.create_oval(0, 0, 0, 0, fill=KEY, outline="") for _ in self.nodes]

        threading.Thread(target=self._events, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---- networking ----
    def _toggle(self):
        try:
            req = urllib.request.Request(conf.BASE_URL + "/orb/toggle", data=b"{}",
                                         headers={"Content-Type": "application/json",
                                                  "X-Auth-Token": conf.auth_token() or ""},
                                         method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass

    # ---- drag to move / click to toggle ----
    def _on_press(self, e):
        self._drag = {"mx": e.x_root, "my": e.y_root,
                      "ox": self.root.winfo_x(), "oy": self.root.winfo_y(),
                      "moved": False}

    def _on_drag(self, e):
        d = self._drag
        if not d:
            return
        dx, dy = e.x_root - d["mx"], e.y_root - d["my"]
        if abs(dx) > DRAG_SLOP or abs(dy) > DRAG_SLOP:
            d["moved"] = True
        if d["moved"]:
            self.root.geometry(f"+{d['ox'] + dx}+{d['oy'] + dy}")

    def _on_release(self, e):
        d, self._drag = self._drag, None
        if not d:
            return
        if d["moved"]:
            self._save_pos()   # remember where it was dropped
        else:
            self._toggle()     # a real click, not a drag

    def _load_pos(self, dx, dy):
        """Saved drop position, clamped to stay on the visible desktop; else default."""
        try:
            saved = json.loads(_POS_FILE.read_text(encoding="utf-8"))
            x, y = int(saved["x"]), int(saved["y"])
            vx, vy, vw, vh = _virtual_bounds()
            x = max(vx, min(x, vx + vw - SIZE))
            y = max(vy, min(y, vy + vh - SIZE))
            return x, y
        except Exception:
            return dx, dy

    def _save_pos(self):
        try:
            _POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _POS_FILE.write_text(
                json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}),
                encoding="utf-8")
        except Exception:
            pass

    def _events(self):
        url = conf.BASE_URL + "/events?token=" + (conf.auth_token() or "")
        while True:
            try:
                with urllib.request.urlopen(url, timeout=40) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            msg = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        k = msg.get("kind")
                        if k == "voice":
                            # Voice owns the orb during a voice interaction (listening / speaking /
                            # dictation are states brain events can't express).
                            vs = (msg.get("data") or {}).get("state")
                            if vs in ("idle", "off", "disabled"):
                                self.voice_active = False
                                self.state = "idle"
                            elif vs in ("listening", "speaking", "thinking", "dictation"):
                                self.voice_active = True
                                self.state = vs
                            elif vs == "error":
                                self.voice_active = True
                                self.state = "error"
                                self._err_until = time.time() + 1.6
                            d = msg.get("data") or {}
                            if isinstance(d.get("level"), (int, float)):
                                self.level = max(0.0, min(1.0, float(d["level"])))  # live mic/TTS VU
                        elif k == "model":
                            if not self.voice_active:  # voice manages its own think->speak lifecycle
                                self.state = "thinking"
                        elif k == "tool":
                            self.state = "acting"      # PC control is meaningful in any mode
                        elif k == "done":
                            if not self.voice_active:  # voice stays 'speaking' until playback drains
                                self.state = "idle"
                        elif k == "error":
                            self.state = "error"
                            self._err_until = time.time() + 1.6
                        elif k == "control":
                            action = (msg.get("data") or {}).get("action")
                            if action == "dashboard_shown":
                                self.hidden_dash = True
                            elif action == "dashboard_hidden":
                                self.hidden_dash = False
                            elif action == "sleep":
                                self.asleep = True
                            elif action == "wake":
                                self.asleep = False
            except Exception:
                # Stream dropped. We no longer know the real turn state, so reset to idle —
                # otherwise the orb stays stuck pulsing 'thinking'/'acting' forever after a drop.
                self.state = "idle"
                self.voice_active = False
                time.sleep(2.0)
            else:
                # Clean end of the SSE iterator (server closed the stream) — reset, and back off
                # too: without a delay a server that accepts-then-closes spins a tight reconnect
                # loop hammering /events and pinning a CPU.
                self.state = "idle"
                self.voice_active = False
                time.sleep(1.5)

    def _watchdog(self):
        """Exit if the app disappears (so we don't linger as an orphan overlay)."""
        misses = 0
        while True:
            time.sleep(5.0)
            try:
                with urllib.request.urlopen(conf.BASE_URL + "/health", timeout=3) as r:
                    json.loads(r.read() or b"{}")
                misses = 0
            except Exception:
                misses += 1
                if misses >= 4:  # ~20s gone -> quit
                    _clear_pid()  # os._exit bypasses atexit/finally — unlink the pid file first
                    os._exit(0)

    # ---- window manager: stay on top, yield to fullscreen ----
    def _watch_window(self):
        try:
            if not self.hwnd:
                self.hwnd = _u.GetAncestor(self.root.winfo_id(), GA_ROOT) or self.root.winfo_id()
            fs = _fullscreen_on_primary(self.hwnd)
            want_hidden = fs or self.hidden_dash or self.asleep
            if want_hidden and not self.hidden:
                self.root.withdraw()
                self.hidden = True
            elif not want_hidden and self.hidden:
                self.root.deiconify()
                self.hidden = False
                _set_topmost(self.hwnd)
            elif not want_hidden:
                _set_topmost(self.hwnd)  # re-assert so nothing buries it
        except Exception:
            pass
        self.root.after(600, self._watch_window)

    # ---- render ----
    def _project(self, p, R, cx, cy):
        ca, sa = math.cos(self.ang), math.sin(self.ang)
        x = p[0] * ca - p[2] * sa
        z = p[0] * sa + p[2] * ca
        y = p[1]
        tilt = 0.42
        ct, st = math.cos(tilt), math.sin(tilt)
        y2 = y * ct - z * st
        z2 = y * st + z * ct
        persp = 1 / (1.9 - z2)
        return cx + x * R * persp, cy + y2 * R * persp, (z2 + 1) / 2

    def _tick(self):
        if self.hidden:  # don't burn CPU behind a fullscreen game
            self.root.after(200, self._tick)
            return
        # Idle is a slow calm pulse — redraw at ~17fps then; only spin up to ~30fps while the
        # orb is actively reacting (thinking/acting/error). Halves idle CPU on an always-on overlay.
        live = self.state in ("thinking", "acting", "error", "listening", "speaking", "dictation")
        interval = 33 if live else 60
        dt = interval / 1000.0
        self.t += dt
        if self.state == "error" and time.time() > self._err_until:
            self.state = "idle"
        tc, tspin, tpulse = STATES.get(self.state, STATES["idle"])
        for i in range(3):
            self.cur[i] += (tc[i] - self.cur[i]) * 0.15
        self.spin += (tspin - self.spin) * 0.1
        self.pulse += (tpulse - self.pulse) * 0.1
        self.ang += self.spin * dt
        # Live audio level: smooth toward the latest, then decay so a missed update fades out.
        self.level_smooth += (self.level - self.level_smooth) * 0.3
        self.level *= 0.9

        cx = cy = SIZE / 2
        pulse = 0.92 + 0.08 * math.sin(self.t * self.pulse * math.pi)
        R = SIZE * 0.30 * pulse * (1 + 0.22 * self.level_smooth)   # swell with the voice
        cr, cg, cb = self.cur
        pts = [self._project(p, R, cx, cy) for p in self.nodes]

        edim = _hex(cr * 0.5, cg * 0.5, cb * 0.5)
        for item, (i, j) in zip(self.edge_items, self.edges):
            a, b = pts[i], pts[j]
            self.cv.coords(item, a[0], a[1], b[0], b[1])
            self.cv.itemconfig(item, fill=edim)
        for item, p in zip(self.node_items, pts):
            d = p[2]
            rad = 1.4 + 2.3 * d
            col = _hex(cr + 55 * d, cg + 35 * d, cb + 35 * d)
            self.cv.coords(item, p[0] - rad, p[1] - rad, p[0] + rad, p[1] + rad)
            self.cv.itemconfig(item, fill=col)

        self.root.after(interval, self._tick)

    def run(self):
        self._tick()
        self._watch_window()
        self.root.mainloop()


if __name__ == "__main__":
    try:
        Orb().run()
    except Exception:
        pass
    finally:
        _clear_pid()  # normal exit (mainloop ended) — don't leave a stale pid file behind
