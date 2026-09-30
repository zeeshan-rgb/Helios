"""Orb engine "webview" — the three.js + React neural orb (separate process).

Renders helios/ui/orb.html + orb.js (a glowing 3D neural network with UnrealBloom) in a
TRANSPARENT, frameless, always-on-top pywebview window. All the look + reactivity lives
in orb.js (state via the app's SSE feed, toggle via POST /orb/toggle); this process just
owns the WINDOW:

  - transparent + frameless + always-on-top (periodic HWND_TOPMOST re-assert)
  - a tool window (off the taskbar / alt-tab)
  - draggable (pywebview drag region) with the dropped position remembered (data/orb_pos.json)
  - hides when a fullscreen app owns the monitor; exits if the app disappears
  - records data/orb.pid {pid, ctime} so the app can kill a genuine orphan (recycled-PID safe)

⚠️ KNOWN TRADE-OFF (why [orb].engine exists): WebView2 transparency rides on GPU
compositing, which Windows can silently drop (battery saver, driver change, RDP) — the
orb then renders as an OPAQUE BOX with no way to detect it from inside. If that happens,
set [orb].engine = "layered" in settings.toml (the pure-Win32 renderer, GPU-independent).
Transparency gotchas that MUST hold here: transparent=True + frameless + easy_drag=False,
NO SetWindowRgn, NO WS_EX_TRANSPARENT toggling, NO manual DPI awareness (see v0.3.x notes).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import sys
import threading
import time
import urllib.request

import webview

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helios import conf, proc_util  # noqa: E402

def _orb_size() -> int:
    try:
        return max(120, min(500, int(conf.SETTINGS.get("orb", {}).get("size", 190))))
    except Exception:
        return 190


SIZE = _orb_size()      # window size, px — [orb].size in settings.toml
MARGIN = 24
TITLE = "HELIOSORB"
_POS_FILE = conf.DATA_DIR / "orb_pos.json"
_PID_FILE = conf.DATA_DIR / "orb.pid"
# Written when the transparency self-check catches the opaque box; the dispatcher skips the
# webview engine while this is fresh (< ~6h) so the user doesn't see a white flash every wake.
# A later SUCCESSFUL check deletes it, so the three.js orb returns when compositing does.
OPAQUE_FLAG = conf.DATA_DIR / "orb_opaque.flag"

# ---- Win32 ----
_u = ctypes.windll.user32
_g = ctypes.windll.gdi32
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020   # click-through (hit-test passes to the window below)
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
_u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
MONITOR_DEFAULTTONEAREST = 2
_u.FindWindowW.restype = wintypes.HWND
_u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
_u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u.GetForegroundWindow.restype = wintypes.HWND
_u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u.MonitorFromWindow.restype = wintypes.HANDLE
_u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_u.GetMonitorInfoW.restype = wintypes.BOOL
_u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_u.GetWindowLongW.restype = ctypes.c_long
_u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_g.CreateEllipticRgn.restype = wintypes.HANDLE
_g.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
_u.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _set_topmost(hwnd):
    if hwnd:
        _u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def _virtual_bounds():
    g = _u.GetSystemMetrics
    return g(76), g(77), g(78), g(79)  # SM_*VIRTUALSCREEN (left, top, width, height)


def _fullscreen_on_primary(own_hwnd) -> bool:
    """True when the FOREGROUND window is a real fullscreen app covering its whole monitor (so the
    orb yields to it). Maximized windows stop at the work area, so they don't match."""
    fg = _u.GetForegroundWindow()
    if not fg or fg == own_hwnd:
        return False
    buf = ctypes.create_unicode_buffer(64)
    _u.GetClassNameW(fg, buf, 64)
    if buf.value in ("Progman", "WorkerW"):
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


# ---- position persistence ----
def _load_pos():
    sw = _u.GetSystemMetrics(0)
    default = (sw - SIZE - MARGIN, MARGIN)
    try:
        d = json.loads(_POS_FILE.read_text(encoding="utf-8"))
        x, y = int(d["x"]), int(d["y"])
        vx, vy, vw, vh = _virtual_bounds()
        return max(vx, min(x, vx + vw - SIZE)), max(vy, min(y, vy + vh - SIZE))
    except Exception:
        return default


def _save_pos(x, y):
    try:
        _POS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _POS_FILE.write_text(json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
    except Exception:
        pass


# ---- pid file (recycled-PID-safe kill, see app._kill_stray_orb) ----
def _write_pid():
    try:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(json.dumps({"pid": os.getpid(), "ctime": proc_util.own_creation_time()}),
                             encoding="utf-8")
    except Exception:
        pass


def _clear_pid():
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


_stop = threading.Event()
_S = {"hwnd": 0, "opaque": False, "window": None, "dash_hidden": False, "asleep": False}   # shared between threads + main()


def _events():
    """Watch the app's SSE feed for dashboard show/hide so the orb can yield to it (orb.js handles
    its own look/state reactivity in-page; this is the one signal that needs the native window
    itself hidden, so it's handled here like the layered/tk engines' _events())."""
    url = conf.BASE_URL + "/events?token=" + (conf.auth_token() or "")
    while not _stop.is_set():
        try:
            with urllib.request.urlopen(url, timeout=40) as r:
                for raw in r:
                    if _stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        msg = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if msg.get("kind") == "control":
                        action = (msg.get("data") or {}).get("action")
                        if action == "dashboard_shown":
                            _S["dash_hidden"] = True
                        elif action == "dashboard_hidden":
                            _S["dash_hidden"] = False
                        elif action == "sleep":
                            _S["asleep"] = True
                        elif action == "wake":
                            _S["asleep"] = False
        except Exception:
            time.sleep(2.0)
        else:
            time.sleep(1.5)  # clean stream close — back off so we don't spin a reconnect loop


def _transparency_check():
    """Detect the opaque-box failure automatically. WebView2 transparency silently breaks
    when Windows drops GPU compositing; from inside the page there is NO signal. But from
    the outside there is: screenshot a corner patch of the window (the orb never draws in
    the corners), hide the window, screenshot again, show it back. If the two differ, the
    window is painting an opaque backdrop over the desktop → destroy the window so main()
    can raise and the dispatcher falls back to the layered engine."""
    time.sleep(6.0)                      # let webview + three.js settle
    hwnd = _S.get("hwnd")
    if not hwnd or _stop.is_set():
        return
    try:
        import mss
        import numpy as np
        r = wintypes.RECT()
        if not _u.GetWindowRect(hwnd, ctypes.byref(r)) or r.right <= r.left:
            return
        pad = 8
        boxes = [  # four 12px corner patches, just inside the window
            {"left": r.left + pad, "top": r.top + pad, "width": 12, "height": 12},
            {"left": r.right - pad - 12, "top": r.top + pad, "width": 12, "height": 12},
            {"left": r.left + pad, "top": r.bottom - pad - 12, "width": 12, "height": 12},
            {"left": r.right - pad - 12, "top": r.bottom - pad - 12, "width": 12, "height": 12},
        ]
        boxes = [b for b in boxes if b["left"] >= 0 and b["top"] >= 0]
        if not boxes:
            return
        with mss.mss() as sct:
            visible = [np.asarray(sct.grab(b), dtype=np.int16) for b in boxes]
            _u.ShowWindow(hwnd, SW_HIDE)
            time.sleep(0.25)
            behind = [np.asarray(sct.grab(b), dtype=np.int16) for b in boxes]
            _u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            _set_topmost(hwnd)
        diffs = [float(abs(v - h).mean()) for v, h in zip(visible, behind)]
        # transparent corners == the desktop behind them (diff ~0, minor AA noise).
        # An opaque box repaints every corner → all corners differ from the desktop.
        if min(diffs) > 10.0:
            conf.log("orb", f"webview orb is an OPAQUE BOX (corner diffs {['%.0f' % d for d in diffs]}) "
                            "— GPU compositing is off; falling back to the layered engine")
            try:  # remember the verdict so the dispatcher skips the white-box flash for a while
                OPAQUE_FLAG.write_text(str(time.time()), encoding="utf-8")
            except Exception:
                pass
            _S["opaque"] = True
            w = _S.get("window")
            if w is not None:
                try:
                    w.destroy()          # unblocks webview.start() in main()
                    return
                except Exception:
                    pass
            os._exit(3)                  # last resort: die so the app relaunches us fresh
        else:
            try:  # compositing works today — clear any stale bad verdict
                OPAQUE_FLAG.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass                             # never let the checker kill a WORKING orb


def _find_hwnd():
    hwnd = 0
    for _ in range(200):
        if _stop.is_set():
            return 0
        hwnd = _u.FindWindowW(None, TITLE)
        if hwnd:
            return hwnd
        time.sleep(0.1)
    return hwnd


def _manage():
    """Keep the orb on top, off the taskbar, fullscreen-aware, and remember where it's dragged.

    NOTE: we deliberately do NOT use SetWindowRgn to make it circular — clipping the window region
    breaks WebView2's DirectComposition transparency (the window goes opaque black). Click-through
    outside the disc is handled by _hittest() toggling WS_EX_TRANSPARENT instead, which keeps the
    transparency intact."""
    hwnd = _find_hwnd()
    if not hwnd:
        return
    _S["hwnd"] = hwnd
    try:
        _u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          _u.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW)  # off taskbar
    except Exception:
        pass
    last_pos = None
    hidden = False
    while not _stop.is_set():
        try:
            want_hidden = _fullscreen_on_primary(hwnd) or _S.get("dash_hidden") or _S.get("asleep")
            if want_hidden and not hidden:
                _u.ShowWindow(hwnd, SW_HIDE); hidden = True
            elif not want_hidden and hidden:
                _u.ShowWindow(hwnd, SW_SHOWNOACTIVATE); hidden = False; _set_topmost(hwnd)
            elif not want_hidden:
                _set_topmost(hwnd)
            if not hidden:
                rr = wintypes.RECT(); _u.GetWindowRect(hwnd, ctypes.byref(rr))
                pos = (rr.left, rr.top)
                if pos != last_pos:
                    last_pos = pos; _save_pos(*pos)  # remember where it was dragged
        except Exception:
            pass
        time.sleep(0.6)


def _hittest():
    """Make outside-the-disc click-through without a window region (which would kill transparency):
    poll the cursor and flip WS_EX_TRANSPARENT on when it's outside the orb's circle, off when over
    it. So clicks on the orb toggle/drag, clicks on the transparent corners pass to the desktop."""
    pt = wintypes.POINT()
    transparent = None
    while not _stop.is_set():
        hwnd = _S.get("hwnd")
        if hwnd:
            try:
                _u.GetCursorPos(ctypes.byref(pt))
                r = wintypes.RECT(); _u.GetWindowRect(hwnd, ctypes.byref(r))
                cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
                rad = (r.right - r.left) * 0.46
                inside = (pt.x - cx) ** 2 + (pt.y - cy) ** 2 <= rad * rad
                want = not inside  # transparent (click-through) only when the cursor is off the disc
                if want != transparent:
                    transparent = want
                    ex = _u.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    ex = (ex | WS_EX_TRANSPARENT) if want else (ex & ~WS_EX_TRANSPARENT)
                    _u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            except Exception:
                pass
        time.sleep(0.05)


def _watchdog():
    """Exit if the app disappears, so we don't linger as an orphan overlay."""
    misses = 0
    while not _stop.is_set():
        time.sleep(5.0)
        try:
            with urllib.request.urlopen(conf.BASE_URL + "/health", timeout=3) as r:
                json.loads(r.read() or b"{}")
            misses = 0
        except Exception:
            misses += 1
            if misses >= 4:  # ~20s gone
                _clear_pid()
                os._exit(0)


def main():
    # NOTE: do NOT set process DPI awareness here — the working transparent overlay (verified) ran
    # without it, and forcing per-monitor awareness before pywebview can break the transparency.
    # WebView2's OWN default background must be transparent too (ARGB hex) — without this the
    # control paints an opaque white backdrop under the page even when the page is transparent.
    # Must be in the env BEFORE the WebView2 environment is created.
    os.environ.setdefault("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "00000000")
    # Give the orb its OWN WebView2 user-data folder: with the default folder it JOINS the
    # dashboard's already-running browser/GPU process, whose compositing was negotiated for an
    # OPAQUE window — a prime suspect for the "transparent one day, opaque box the next" history
    # (it depended on which window claimed the shared browser process first).
    os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", str(conf.DATA_DIR / "orb_webview2"))
    _write_pid()
    x, y = _load_pos()
    threading.Thread(target=_manage, daemon=True).start()
    threading.Thread(target=_events, daemon=True).start()
    # NOTE: _hittest (WS_EX_TRANSPARENT click-through) is intentionally NOT started — toggling that
    # extended style breaks WebView2's DirectComposition transparency (opaque box). The orb window
    # is small, so its transparent corners eating clicks is an acceptable trade for staying see-through.
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_transparency_check, daemon=True).start()
    try:
        _S["window"] = webview.create_window(
            TITLE, conf.BASE_URL + "/orb", x=x, y=y, width=SIZE, height=SIZE,
            frameless=True, on_top=True, transparent=True, easy_drag=False)
        webview.start()
        if _S.get("opaque"):   # the checker killed the window: compositing is off right now
            raise RuntimeError("WebView2 transparency broken (opaque box)")
    except Exception as e:  # pragma: no cover
        conf.log("orb", f"webview orb failed: {e}")
        raise            # let the dispatcher (orb_overlay.py) fall back to the layered engine
    finally:
        _stop.set()
        _clear_pid()


if __name__ == "__main__":
    try:
        main()
    finally:
        _clear_pid()
