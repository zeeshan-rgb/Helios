"""Neural-network orb — a per-pixel-alpha layered window (separate process).

Rendered into a Win32 LAYERED window via UpdateLayeredWindow, NOT tkinter and NOT
WebView2. Why: WebView2 transparency depends on GPU compositing (it randomly became an
opaque box on this machine — see handoff), and tkinter's color-key can't do alpha at all
(hard 1-bit edges, no glow). UpdateLayeredWindow is pre-DWM Win32: every pixel carries
its own alpha regardless of GPU state, so the orb gets real soft glow, and Windows
hit-tests the alpha for us — clicks on fully transparent pixels fall through to whatever
is underneath (true click-through outside the orb, which the tkinter orb couldn't have).

The picture: a fibonacci-sphere neural net, slowly turning, depth-fogged on the far
side, with signal pulses travelling along the synapses — rare drifting sparks when idle,
cascades while thinking, VU-synced while speaking. Rendered with PIL + numpy (both
already runtime deps) at 2× supersample with an additive bloom pass, ~30 fps live /
halved when idle, skipped entirely while hidden behind a fullscreen app.

Behaviour contract is identical to the other orb engines: streams state from /events SSE,
click toggles the dashboard (POST /orb/toggle), drag moves it (position saved), stays
topmost, hides for fullscreen apps, writes data/orb.pid = {pid, ctime}, and exits when
the app goes away. Selected via [orb].engine = "layered" (see helios/orb_overlay.py, the
engine dispatcher — this is the GPU-independent fallback behind the three.js orb).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import math
import os
import random
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helios import conf  # noqa: E402

def _orb_size() -> int:
    try:
        return max(120, min(500, int(conf.SETTINGS.get("orb", {}).get("size", 190))))
    except Exception:
        return 190


SIZE = _orb_size()       # window (and frame buffer) size, px — [orb].size in settings.toml
DISC_R = round(SIZE * 0.32)   # radius of the orb disc (the reliable click target)
SS = 2                   # supersample factor for the renderer
MARGIN = 20
N = 72                   # neural-net nodes
EDGE_D2 = 0.23           # link nodes closer than sqrt(this) on the unit sphere
DRAG_SLOP = 4            # px of movement that turns a click into a drag (no toggle)
_POS_FILE = conf.DATA_DIR / "orb_pos.json"   # remembers where you dropped the orb
_PID_FILE = conf.DATA_DIR / "orb.pid"

# state -> ([r,g,b], spin rad/s, pulse hz, sparks/s). Same colour vocabulary as the
# dashboard's synapse dot + voice HUD: listening=green, speaking=bright cyan,
# dictation=violet, acting=warm, error=red.
STATES = {
    "idle":      ((77, 208, 225), 0.22, 0.9, 0.4),
    "thinking":  ((102, 224, 255), 0.85, 2.0, 8.0),
    "acting":    ((214, 162, 99), 0.6, 1.6, 5.0),
    "error":     ((224, 85, 107), 0.95, 3.0, 6.0),
    "listening": ((126, 224, 160), 0.45, 1.5, 1.5),
    "speaking":  ((102, 224, 255), 0.6, 1.9, 2.0),
    "dictation": ((189, 170, 240), 0.5, 1.6, 2.0),
}
LIVE_STATES = ("thinking", "acting", "error", "listening", "speaking", "dictation")


# ============================================================================
# Pure geometry / pixel helpers (unit-tested in tests/test_orb.py — no Win32).
# ============================================================================

def sphere_nodes(n: int):
    """n points spread over the unit sphere (fibonacci lattice)."""
    nodes = []
    for i in range(n):
        y = 1 - (i / (n - 1)) * 2
        r = math.sqrt(max(0.0, 1 - y * y))
        phi = i * math.pi * (3 - math.sqrt(5))
        nodes.append((math.cos(phi) * r, y, math.sin(phi) * r))
    return nodes


def sphere_edges(nodes, max_d2: float = EDGE_D2):
    """Index pairs of nodes closer than sqrt(max_d2) — the synapses."""
    e = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            dx = nodes[i][0] - nodes[j][0]
            dy = nodes[i][1] - nodes[j][1]
            dz = nodes[i][2] - nodes[j][2]
            if dx * dx + dy * dy + dz * dz < max_d2:
                e.append((i, j))
    return e


def project(p, ang: float, tilt: float, cx: float, cy: float, radius: float):
    """Rotate about Y by `ang`, tilt about X, apply mild perspective.
    Returns (x, y, depth) with depth in [0, 1] (1 = nearest the viewer)."""
    ca, sa = math.cos(ang), math.sin(ang)
    x = p[0] * ca - p[2] * sa
    z = p[0] * sa + p[2] * ca
    y = p[1]
    ct, st = math.cos(tilt), math.sin(tilt)
    y2 = y * ct - z * st
    z2 = y * st + z * ct
    persp = 1 / (1.9 - z2)
    return cx + x * radius * persp, cy + y2 * radius * persp, (z2 + 1) / 2


def premultiply_bgra(rgba):
    """Straight-alpha RGBA uint8 (H,W,4) -> premultiplied BGRA bytes for
    UpdateLayeredWindow (which requires premultiplied alpha, BGRA byte order)."""
    import numpy as np
    a = rgba[..., 3:4].astype(np.uint16)
    out = rgba.copy()
    # premultiply RGB by alpha (integer math, rounds like GDI does)
    out[..., :3] = ((rgba[..., :3].astype(np.uint16) * a + 127) // 255).astype(rgba.dtype)
    out = out[..., [2, 1, 0, 3]]                    # RGBA -> BGRA
    return out.tobytes()


class PulseField:
    """Signal pulses travelling along edges. Deterministic given a seeded rng."""

    def __init__(self, n_edges: int, rng: random.Random | None = None):
        self.n_edges = n_edges
        self.rng = rng or random.Random()
        self.active: list[list] = []     # [edge_index, t 0..1, speed]
        self._spawn_acc = 0.0

    def step(self, dt: float, rate: float):
        """Advance pulses; spawn ~rate new ones per second (fractional accumulator).
        Advance-then-filter guarantees every surviving pulse has t < 1 (a t ≥ 1 would
        hand the renderer a negative sin() alpha)."""
        for p in self.active:
            p[1] += p[2] * dt
        self.active = [p for p in self.active if p[1] < 1.0]
        if not self.n_edges:
            return
        self._spawn_acc += rate * dt
        while self._spawn_acc >= 1.0 and len(self.active) < 48:
            self._spawn_acc -= 1.0
            self.active.append([self.rng.randrange(self.n_edges), 0.0,
                                0.9 + self.rng.random() * 1.4])
        # sub-1 remainder: spawn probabilistically so low rates still fire sometimes
        if self._spawn_acc > 0 and self.rng.random() < self._spawn_acc and len(self.active) < 48:
            self._spawn_acc = 0.0
            self.active.append([self.rng.randrange(self.n_edges), 0.0,
                                0.9 + self.rng.random() * 1.4])


class Renderer:
    """Draws one orb frame (premultiplied BGRA bytes). Pure PIL/numpy — no Win32,
    so it's importable and testable headless."""

    def __init__(self, size: int = SIZE, ss: int = SS, n: int = N):
        import numpy as np
        self.np = np
        self.size = size
        self.big = size * ss
        self.ss = ss
        self.nodes = sphere_nodes(n)
        self.edges = sphere_edges(self.nodes)
        self.pulses = PulseField(len(self.edges))
        # -- precomputed layers at FINAL resolution (premultiplied float32, 0..1).
        #    Only the crisp line/node drawing happens supersampled; bloom + compositing
        #    run at final size (4× cheaper — keeps a frame well under the 33ms budget).
        self.k = size / 224.0            # scale factor: pixel constants tuned at 224px
        disc_r = round(size * 0.32)
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        c = size / 2
        dist = np.sqrt((xx - c) ** 2 + (yy - c) ** 2)
        # dark translucent disc body = contrast + a solid click target (alpha>0 catches
        # the mouse; the soft edge fades clicks out with the pixels)
        body = np.clip((disc_r - dist) / (5.0 * self.k), 0.0, 1.0) * 0.82
        self.disc_a = body.astype(np.float32)                       # alpha profile
        self.disc_rgb = np.array([10, 13, 18], dtype=np.float32) / 255.0
        # halo glow profile: soft ring energy just outside the disc, fading outward
        halo = np.exp(-((dist - disc_r * 0.92) / (24.0 * self.k)) ** 2)
        halo[dist < disc_r * 0.55] *= 0.35
        self.halo_a = (halo * 0.5).astype(np.float32)

    def frame(self, color, ang, wobble, breath, level, spark_rate, dt) -> bytes:
        """color=(r,g,b) 0..255; breath = 0..1 pulse wave; level = voice VU 0..1."""
        np = self.np
        from PIL import Image, ImageDraw, ImageFilter
        big, ss = self.big, self.ss
        cx = cy = big / 2
        # perspective compresses lateral reach to ~0.6R, so the base radius runs well
        # past the disc for the net to actually fill it
        R = big * 0.46 * (0.95 + 0.05 * breath) * (1 + 0.14 * level)
        tilt = 0.42 + 0.07 * wobble
        pts = [project(p, ang, tilt, cx, cy, R) for p in self.nodes]
        r8, g8, b8 = [int(c) for c in color]

        # ---- crisp layer: synapses + nodes + travelling pulses (straight alpha) ----
        crisp = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(crisp)
        for (i, j) in sorted(self.edges, key=lambda e: (pts[e[0]][2] + pts[e[1]][2])):
            a, b = pts[i], pts[j]
            depth = (a[2] + b[2]) / 2
            alpha = int((0.10 + 0.38 * depth) * 255)
            d.line([a[0], a[1], b[0], b[1]], fill=(r8, g8, b8, alpha), width=ss)
        self.pulses.step(dt, spark_rate)
        for e_idx, t, _v in self.pulses.active:
            i, j = self.edges[e_idx]
            a, b = pts[i], pts[j]
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            glow = max(0.0, math.sin(t * math.pi))
            rad = 2.1 * ss * self.k
            d.ellipse([x - rad, y - rad, x + rad, y + rad],
                      fill=(min(255, r8 + 90), min(255, g8 + 80), min(255, b8 + 80),
                            int(230 * glow)))
        for p in sorted(pts, key=lambda p: p[2]):
            depth = p[2]
            rad = (0.9 + 2.1 * depth) * ss * self.k
            col = (min(255, int(r8 + 55 * depth)), min(255, int(g8 + 40 * depth)),
                   min(255, int(b8 + 40 * depth)), int((0.38 + 0.62 * depth) * 255))
            d.ellipse([p[0] - rad, p[1] - rad, p[0] + rad, p[1] + rad], fill=col)

        # ---- premultiply at the supersampled size, THEN downscale (premultiplied
        #      resize is fringe-free; straight-alpha resize bleeds black at edges) ----
        cr_big = np.asarray(crisp, dtype=np.float32) * (1.0 / 255.0)
        cr_big[..., :3] *= cr_big[..., 3:4]
        small = Image.fromarray((cr_big * 255).astype(np.uint8)).resize(
            (self.size, self.size), Image.LANCZOS)
        cs = np.asarray(small, dtype=np.float32) * (1.0 / 255.0)      # premult, final res
        # bloom = blurred copy added on top (premult add = true "plus lighter"); two box
        # blurs ≈ a gaussian at a fraction of the cost
        bloom = small.filter(ImageFilter.BoxBlur(3)).filter(ImageFilter.BoxBlur(3))
        bl = np.asarray(bloom, dtype=np.float32) * (1.0 / 255.0)

        out = np.empty_like(cs)
        # disc body first (it is the backdrop the net sits on)
        col = np.array([r8, g8, b8], dtype=np.float32) / 255.0
        out[..., 3] = self.disc_a
        out[..., :3] = self.disc_rgb[None, None, :] * self.disc_a[..., None]
        # net + bloom, additively
        out += cs
        out += bl * 0.7
        # state-coloured halo breathing around the disc (stronger with voice level).
        # Slightly super-luminous on purpose (premult RGB > A) — UpdateLayeredWindow
        # composes src + dst*(1-A), so this reads as a real additive glow on the desktop.
        halo_k = (0.30 + 0.25 * breath + 0.55 * level)
        out[..., :3] += (self.halo_a * halo_k)[..., None] * col[None, None, :]
        out[..., 3] += self.halo_a * halo_k * 0.75
        np.clip(out, 0.0, 1.0, out)

        arr = (out * 255).astype(np.uint8)   # premultiplied RGBA — swap to BGRA
        return arr[..., [2, 1, 0, 3]].tobytes()


# ============================================================================
# Win32 plumbing
# ============================================================================
_u = ctypes.windll.user32
_g = ctypes.windll.gdi32
_k = ctypes.windll.kernel32

GA_ROOT = 2
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_NOZORDER = 0x2, 0x1, 0x10, 0x4
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x8, 0x80, 0x8000000
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1
SW_HIDE, SW_SHOWNA = 0, 8
WM_TIMER, WM_DESTROY, WM_CLOSE = 0x0113, 0x2, 0x10
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE, WM_CAPTURECHANGED = 0x201, 0x202, 0x200, 0x215
MONITOR_DEFAULTTONEAREST = 2

_u.GetAncestor.restype = wintypes.HWND
_u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
_u.GetForegroundWindow.restype = wintypes.HWND
_u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u.MonitorFromWindow.restype = wintypes.HANDLE   # HMONITOR — declare or it truncates on 64-bit
_u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_u.GetMonitorInfoW.restype = wintypes.BOOL
_u.DefWindowProcW.restype = ctypes.c_ssize_t
_u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u.CreateWindowExW.restype = wintypes.HWND
_g.CreateDIBSection.restype = wintypes.HANDLE
_g.CreateCompatibleDC.restype = wintypes.HANDLE
_u.GetDC.restype = wintypes.HANDLE
# Handle-taking calls MUST have argtypes: without them ctypes converts Python ints through a
# 32-bit C int, and any HWND/HDC ≥ 2^31 (perfectly normal on 64-bit Windows) raises
# "OverflowError: int too long to convert". The first runs only worked because Windows happened
# to hand out small handle values.
_k.GetModuleHandleW.restype = wintypes.HANDLE
_k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_u.LoadCursorW.restype = wintypes.HANDLE
_u.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u.SetTimer.restype = ctypes.c_size_t
_u.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
_u.SetCapture.restype = wintypes.HWND
_u.SetCapture.argtypes = [wintypes.HWND]
_u.GetDC.argtypes = [wintypes.HWND]
_u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HANDLE]
_g.CreateCompatibleDC.argtypes = [wintypes.HANDLE]
_g.SelectObject.restype = wintypes.HANDLE
_g.SelectObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_g.CreateDIBSection.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.UINT,
                                ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_u.UpdateLayeredWindow.restype = wintypes.BOOL
_u.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HANDLE, ctypes.c_void_p,
                                   ctypes.c_void_p, wintypes.HANDLE, ctypes.c_void_p,
                                   wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_u.RegisterClassW.restype = wintypes.WORD
_u.RegisterClassW.argtypes = [ctypes.c_void_p]
_u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                               wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, wintypes.HWND, wintypes.HANDLE, wintypes.HANDLE,
                               ctypes.c_void_p]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HANDLE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BLENDFUNCTION(ctypes.Structure):
    # BYTE = c_ubyte — c_byte is SIGNED and rejects the 255 constant-alpha
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


def _virtual_bounds():
    """(left, top, width, height) of the whole virtual desktop (all monitors)."""
    gm = _u.GetSystemMetrics
    return gm(76), gm(77), gm(78), gm(79)  # SM_*VIRTUALSCREEN


def _fullscreen_on_monitor(own_hwnd) -> bool:
    """True when the FOREGROUND window is a real fullscreen app covering the entire
    monitor it sits on (maximized windows stop at the work area, so they don't match).
    MonitorFromWindow-based → correct on multi-monitor + negative coordinates."""
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
    m = mi.rcMonitor
    return (rect.left <= m.left and rect.top <= m.top
            and rect.right >= m.right and rect.bottom >= m.bottom)


def _own_creation_time():
    """This process's creation time (FILETIME as 64-bit int), or None — recorded next
    to our PID so the app can verify the PID wasn't recycled before killing it.
    restype/argtypes MUST be declared or the HANDLE truncates on 64-bit and this
    silently returns None."""
    try:
        _k.GetCurrentProcess.restype = wintypes.HANDLE
        _k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        _k.GetProcessTimes.restype = wintypes.BOOL
        c = wintypes.FILETIME(); e = wintypes.FILETIME()
        kt = wintypes.FILETIME(); ut = wintypes.FILETIME()
        if _k.GetProcessTimes(_k.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                              ctypes.byref(kt), ctypes.byref(ut)):
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
    except Exception:
        pass
    return None


def _write_pid():
    try:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(json.dumps({"pid": os.getpid(), "ctime": _own_creation_time()}),
                             encoding="utf-8")
    except Exception:
        pass


def _clear_pid():
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


class Orb:
    def __init__(self):
        self.renderer = Renderer()
        self.state = "idle"
        self.voice_active = False   # a voice interaction owns the orb (overrides brain idle/think)
        self.cur = [77.0, 208.0, 225.0]
        self.spin = 0.22
        self.pulse_hz = 0.9
        self.spark_rate = 0.4
        self.level = 0.0            # live mic/TTS level from the voice daemon (0..1)
        self.level_smooth = 0.0
        self.ang = 0.0
        self.t = 0.0
        self._err_until = 0.0
        self.hidden = False
        self.hidden_dash = False    # dashboard is open — set by _events(), applied in _tick()
        self._drag = None
        self._frame_no = 0
        self._last_tick = time.monotonic()

        _write_pid()

        # DPI awareness BEFORE the window exists so coordinates agree under scaling.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                _u.SetProcessDPIAware()
            except Exception:
                pass

        # ---- window ----
        hinst = _k.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._on_message)   # keep a ref or the GC eats the thunk
        wc = _WNDCLASS()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = _u.LoadCursorW(None, 32512)    # IDC_ARROW
        wc.lpszClassName = "HeliosNeuralOrb"
        if not _u.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError()
        sw = _u.GetSystemMetrics(0)
        self.x, self.y = self._load_pos(sw - SIZE - MARGIN, MARGIN)
        self.hwnd = _u.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "HeliosNeuralOrb", "Helios Orb", WS_POPUP,
            self.x, self.y, SIZE, SIZE, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError()

        # ---- one reusable 32-bit DIB the renderer blits into ----
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = SIZE, -SIZE      # negative = top-down rows
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        screen = _u.GetDC(None)
        self.hdc_mem = _g.CreateCompatibleDC(screen)
        self.bits = ctypes.c_void_p()
        self.hbmp = _g.CreateDIBSection(screen, ctypes.byref(bmi), 0,
                                        ctypes.byref(self.bits), None, 0)
        _u.ReleaseDC(None, screen)
        if not self.hbmp or not self.bits:
            raise ctypes.WinError()
        _g.SelectObject(self.hdc_mem, self.hbmp)

        _u.ShowWindow(self.hwnd, SW_SHOWNA)
        self._blit(self._render_frame(1 / 30))
        _u.SetTimer(self.hwnd, 1, 33, None)

        threading.Thread(target=self._events, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---- frame ----
    def _render_frame(self, dt) -> bytes:
        if self.state == "error" and time.time() > self._err_until:
            self.state = "idle"
        tc, tspin, tpulse, trate = STATES.get(self.state, STATES["idle"])
        for i in range(3):
            self.cur[i] += (tc[i] - self.cur[i]) * 0.15
        self.spin += (tspin - self.spin) * 0.1
        self.pulse_hz += (tpulse - self.pulse_hz) * 0.1
        self.spark_rate += (trate - self.spark_rate) * 0.15
        self.t += dt
        self.ang += self.spin * dt
        self.level_smooth += (self.level - self.level_smooth) * 0.3
        self.level *= 0.9
        breath = 0.5 + 0.5 * math.sin(self.t * self.pulse_hz * math.pi)
        wobble = math.sin(self.t * 0.31)
        return self.renderer.frame(self.cur, self.ang, wobble, breath,
                                   self.level_smooth, self.spark_rate, dt)

    def _blit(self, frame: bytes):
        ctypes.memmove(self.bits, frame, len(frame))
        pt_dst = wintypes.POINT(self.x, self.y)
        pt_src = wintypes.POINT(0, 0)
        size = _SIZE(SIZE, SIZE)
        bf = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        _u.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pt_dst), ctypes.byref(size),
                               self.hdc_mem, ctypes.byref(pt_src), 0, ctypes.byref(bf),
                               ULW_ALPHA)

    def _tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self._last_tick)
        self._last_tick = now
        self._frame_no += 1
        # housekeeping every ~0.6s: yield to fullscreen apps, re-assert topmost
        if self._frame_no % 18 == 0:
            fs = _fullscreen_on_monitor(self.hwnd)
            want_hidden = fs or self.hidden_dash
            if want_hidden and not self.hidden:
                _u.ShowWindow(self.hwnd, SW_HIDE)
                self.hidden = True
            elif not want_hidden and self.hidden:
                _u.ShowWindow(self.hwnd, SW_SHOWNA)
                self.hidden = False
            if not self.hidden:
                _u.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if self.hidden:
            return                                   # don't burn CPU behind a game
        # idle = calm → render every other timer tick (~15fps); live states get ~30fps
        if self.state not in LIVE_STATES and self._frame_no % 2:
            return
        self._blit(self._render_frame(dt))

    # ---- input (drag to move / click to toggle) ----
    def _cursor(self):
        pt = wintypes.POINT()
        _u.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_LBUTTONDOWN:
                mx, my = self._cursor()
                self._drag = {"mx": mx, "my": my, "ox": self.x, "oy": self.y, "moved": False}
                _u.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE and self._drag:
                mx, my = self._cursor()
                dx, dy = mx - self._drag["mx"], my - self._drag["my"]
                if abs(dx) > DRAG_SLOP or abs(dy) > DRAG_SLOP:
                    self._drag["moved"] = True
                if self._drag["moved"]:
                    self.x, self.y = self._drag["ox"] + dx, self._drag["oy"] + dy
                    _u.SetWindowPos(hwnd, None, self.x, self.y, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                return 0
            if msg == WM_LBUTTONUP:
                drag, self._drag = self._drag, None
                _u.ReleaseCapture()
                if drag:
                    if drag["moved"]:
                        self._save_pos()
                    else:  # a real click, not a drag — don't block the pump on HTTP
                        threading.Thread(target=self._toggle, daemon=True).start()
                return 0
            if msg == WM_CAPTURECHANGED:
                self._drag = None
                return 0
            if msg in (WM_CLOSE, WM_DESTROY):
                _u.PostQuitMessage(0)
                return 0
        except Exception:
            pass  # a broken frame must never kill the message pump
        return _u.DefWindowProcW(hwnd, msg, wparam, lparam)

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
            _POS_FILE.write_text(json.dumps({"x": self.x, "y": self.y}), encoding="utf-8")
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
                            # Voice owns the orb during a voice interaction (listening /
                            # speaking / dictation are states brain events can't express).
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
                                self.level = max(0.0, min(1.0, float(d["level"])))
                        elif k == "model":
                            if not self.voice_active:  # voice manages its own lifecycle
                                self.state = "thinking"
                        elif k == "tool":
                            self.state = "acting"      # PC control matters in any mode
                        elif k == "done":
                            if not self.voice_active:  # voice stays 'speaking' till drained
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
            except Exception:
                # Stream dropped — we no longer know the real turn state, reset to idle
                # (otherwise the orb pulses 'thinking' forever after a drop).
                self.state = "idle"
                self.voice_active = False
                time.sleep(2.0)
            else:
                # Clean close: reset + back off, or an accept-then-close server spins a
                # tight reconnect loop that pins a CPU.
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
                    _clear_pid()  # os._exit bypasses finally — unlink the pid file first
                    os._exit(0)

    def run(self):
        msg = wintypes.MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))


def main():
    # Raises on setup failure (missing numpy/PIL, window creation) so the dispatcher in
    # orb_overlay.py can fall through to the next engine.
    orb = Orb()
    try:
        orb.run()
    finally:
        _clear_pid()  # normal exit — don't leave a stale pid file behind


if __name__ == "__main__":
    main()
