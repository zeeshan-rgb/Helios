"""Helios application: wires the brain, memory, permission broker, HTTP/SSE server,
global hotkeys, system tray, and the windows (reactive orb + dashboard) into one process.

The orb is Helios's ambient presence (small, always-on-top, top-right). Clicking it
toggles the dashboard (the full chat UI), which is hidden until summoned.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import threading
import urllib.request
import webbrowser

from . import conf, memory, notify, server
from .proc_util import kill_pid
from .brain_factory import build_brain
from .hotkeys import Hotkeys
from .permissions import PendingRegistry

_state = {"window": None, "orb_proc": None, "voice_proc": None, "httpd": None, "mode": "browser",
          "quit": threading.Event(), "icon": None, "dash_visible": False,
          "dormant": False, "hub": None}
_win_lock = threading.RLock()  # guards the dash_visible state across the dispatcher + readers
_win_ops: "queue.Queue" = queue.Queue()  # native-window operations, run on the dispatcher thread


# ---- window-op dispatcher --------------------------------------------------------------
# All native-window calls (show/hide/minimize/restore/evaluate_js) are funneled through ONE
# dedicated thread instead of being called straight from HTTP-handler / hotkey / tray threads.
# pywebview's WinForms backend already marshals each call to the GUI thread (via Invoke), but
# those calls BLOCK the caller until the GUI runs them — so calling them on an HTTP worker could
# hang that handler if the GUI thread is busy. The dispatcher makes every caller enqueue-and-
# return, and serializes the ops (single writer). See audit #7.
def _window_dispatcher() -> None:
    while True:
        op = _win_ops.get()
        if op is None:
            return
        try:
            op()
        except Exception as e:  # pragma: no cover
            conf.log("app", f"window op error: {e}")


def _dispatch(op) -> None:
    """Queue a window operation for the dispatcher thread (safe to call from any thread)."""
    _win_ops.put(op)


def _show_dashboard(show: bool) -> None:
    """Public entry (any thread): enqueue the show/hide so the caller never blocks on or races a
    native window call. The actual work runs on the dispatcher thread (_show_dashboard_impl)."""
    _dispatch(lambda: _show_dashboard_impl(show))


def _show_dashboard_impl(show: bool) -> None:
    win = _state["window"]
    if win is None:
        return
    hub = _state.get("hub")
    with _win_lock:
        try:
            if show:
                win.show()      # pywebview marshals to the GUI thread; window is created on_top=True
                try:
                    win.restore()
                except Exception:
                    pass
                _state["dash_visible"] = True
                # Replay the holographic open animation each time it's summoned (the page
                # isn't reloaded on show, so a one-time CSS load animation wouldn't recur).
                try:
                    win.evaluate_js("window.__heliosMaterialize && window.__heliosMaterialize()")
                except Exception:
                    pass
                if hub is not None:
                    hub.publish("control", {"action": "dashboard_shown"})  # orb hides itself
            else:
                win.hide()
                _state["dash_visible"] = False
                if hub is not None:
                    hub.publish("control", {"action": "dashboard_hidden"})  # orb reappears
        except Exception as e:  # pragma: no cover
            conf.log("app", f"show_dashboard error: {e}")


def _toggle_dashboard() -> None:
    """Toggle dashboard visibility. Reads the current state under the lock, then enqueues the
    show/hide — so two near-simultaneous toggles (orb click + hotkey) can't both act on a stale read."""
    with _win_lock:
        target = not _state.get("dash_visible")
    _show_dashboard(target)


def _perm_notifier(kind, data) -> None:
    """Notifier for the permission registry. Always broadcasts to the UI + voice daemon (hub). When
    voice ISN'T running to speak/answer the ask, also surface it on-screen: auto-open the dashboard
    on a pending permission and close it once resolved (only closing what we auto-opened)."""
    hub = _state.get("hub")
    if hub is not None:
        try:
            hub.publish(kind, data)
        except Exception:
            pass
    try:
        if kind == "permission" and not _voice_running():
            with _win_lock:
                already_visible = bool(_state.get("dash_visible"))
            _state["perm_pending"] = _state.get("perm_pending", 0) + 1
            if _state["perm_pending"] == 1 and not already_visible:
                _state["perm_auto_dash"] = True
            if _state.get("perm_auto_dash"):
                _show_dashboard(True)
        elif kind == "permission_resolved":
            if _state.get("perm_pending", 0) > 0:
                _state["perm_pending"] -= 1
            if _state.get("perm_pending", 0) == 0 and _state.pop("perm_auto_dash", False):
                _show_dashboard(False)
    except Exception as e:  # pragma: no cover
        conf.log("app", f"permission dashboard toggle error: {e}")


def _open_app(name: str) -> None:
    """Launch an app/URI named in [startup].open_on_wake (e.g. 'spotify'). Best-effort."""
    name = (name or "").strip()
    if not name:
        return
    try:
        if name.lower() == "spotify":
            # Only open Spotify on wake if it's actually connected (the one-time OAuth in
            # tools/spotify_auth.py / onboarding wrote data/spotify_token.json). Otherwise a fresh
            # install — which ships open_on_wake=["spotify"] — would pop Spotify open on every
            # double-clap / Ctrl+Alt+J even though the user never set it up.
            try:
                from . import spotify
                connected = spotify.configured()
            except Exception:
                connected = False
            if not connected:
                conf.log("app", "wake: spotify not connected — skipping open-on-wake")
                return
            _open_spotify(conf.startup_cfg().get("spotify_play", ""))
        elif name.startswith(("http://", "https://")):
            webbrowser.open(name)
        else:
            os.startfile(name)            # a path, a registered protocol, or an app name
        conf.log("app", f"wake: opened {name}")
    except Exception as e:  # pragma: no cover
        conf.log("app", f"wake: could not open {name}: {e}")


def _spotify_uri(s: str) -> str:
    """Normalize a Spotify reference to a spotify: URI. Accepts an open.spotify.com link or an
    already-formed URI; returns it unchanged if it doesn't look like either."""
    import re
    s = (s or "").strip()
    m = re.search(r"open\.spotify\.com/(playlist|album|track|artist)/([A-Za-z0-9]+)", s)
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    return s


def _media_play() -> None:
    """Press the dedicated media PLAY key (VK_MEDIA_PLAY, 0xFA — NOT the play/pause toggle 0xB3) so a
    cold-opened Spotify starts playing without ever PAUSING an already-playing session. This is the
    fallback used only when the Spotify Web API isn't set up (see helios/spotify.py)."""
    try:
        import ctypes
        VK_MEDIA_PLAY = 0xFA
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY, 0, KEYEVENTF_KEYUP, 0)
    except Exception as e:  # pragma: no cover
        conf.log("app", f"media play key failed: {e}")


def _open_spotify(play: str = "") -> None:
    """Open Spotify and, if `play` is set (a playlist/album/track URI or link), start playing it.

    Preferred path: the Spotify Web API (helios/spotify.py) — it reliably switches to the exact
    playlist on the user's device even if something else is already playing. Set up once via
    tools/spotify_auth.py. Fallback (not authorized): open the URI + a single DEDICATED play key,
    which plays from a cold start and never pauses, but can't switch context mid-playback."""
    play = (play or "").strip()
    uri = _spotify_uri(play) if play else ""

    if uri:
        try:
            from . import spotify
            if spotify.configured():
                # play_context launches Spotify + waits for a device if needed; run it off-thread
                # so wake/_activate never blocks on it.
                threading.Thread(target=spotify.play_context, args=(uri,), daemon=True).start()
                conf.log("app", f"wake: spotify (Web API) -> {uri}")
                return
        except Exception as e:  # pragma: no cover
            conf.log("app", f"spotify Web API path failed, using fallback: {e}")

    # Fallback: open the app (or the specific URI) via the protocol handler.
    try:
        os.startfile(uri or "spotify:")
    except Exception:
        if play and "open.spotify.com" in play:
            webbrowser.open(play)
        else:
            webbrowser.open("https://open.spotify.com")
    if uri:
        # Cold-open: give Spotify time to launch + navigate, then a single DEDICATED play key
        # (0xFA, not the toggle) — starts a cold session without pausing an already-playing one.
        t = threading.Timer(5.0, _media_play)
        t.daemon = True
        t.start()
        conf.log("app", f"wake: spotify fallback (open + play key) -> {uri}")


def _activate() -> None:
    """Leave dormant (hidden) mode: reveal the orb, tell the voice daemon to wake, and open the
    configured apps. Idempotent — a no-op once already active. Triggered by the double-clap (via
    /summon), the tray, or the summon hotkey."""
    if not _state.get("dormant"):
        return
    _state["dormant"] = False
    conf.log("app", "waking from dormant")
    _launch_orb()  # bring the ambient orb to life (it wasn't started at boot)
    hub = _state.get("hub")
    if hub is not None:
        hub.publish("control", {"action": "wake"})  # daemon leaves dormant (tray/hotkey paths)
    for app_name in conf.startup_cfg().get("open_on_wake", []):
        _open_app(str(app_name))


def _sleep() -> None:
    """Power-menu 'Sleep': hide the dashboard, remove the orb, and go dormant — the mic keeps
    listening for the double-clap to wake again. The opposite of _activate()."""
    if _state.get("dormant"):
        return
    conf.log("app", "going to sleep (dormant)")
    _state["dormant"] = True
    _show_dashboard(False)  # hide the dashboard window
    _kill_orb()             # remove the orb
    hub = _state.get("hub")
    if hub is not None:
        hub.publish("control", {"action": "sleep"})  # daemon -> clap-only dormant mode


def _summon() -> None:
    """Bring the dashboard forward; if pywebview is unavailable (browser-fallback mode, window is
    None) open the dashboard URL in a browser. Used by the tray, the hotkey, AND the server's
    /summon route (so a second-instance summon — and the double-clap wake gesture — get here too).
    Wakes Helios first if it's dormant (reveals the orb + opens the configured apps)."""
    _activate()  # no-op unless dormant
    if _state.get("window") is not None:
        _show_dashboard(True)
    else:
        webbrowser.open(conf.BASE_URL)


def _minimize_dashboard() -> None:
    """Minimize the dashboard to the taskbar (the custom title bar's – button calls this via
    /window/minimize, since a frameless window has no native minimize button). Enqueued so the
    HTTP handler doesn't block on the marshaled win.minimize()."""
    def _impl():
        win = _state["window"]
        if win is None:
            return
        try:
            win.minimize()
        except Exception as e:  # pragma: no cover
            conf.log("app", f"minimize error: {e}")
    _dispatch(_impl)


def _resize_dashboard(w: int, h: int, fixx: str = "left", fixy: str = "top") -> None:
    """Resize the frameless dashboard (its custom edge/corner zones call this via /window/resize).
    fixx/fixy pick which edge stays anchored (the side opposite the one being dragged), mapped to
    pywebview's FixPoint so left/top drags don't need a separate move(). Enqueued on the window
    dispatcher so the HTTP handler never blocks on the marshaled win.resize(); the chosen size is
    remembered in data/dash_geom.json and restored at next launch."""
    w, h = max(420, int(w)), max(520, int(h))
    def _impl():
        win = _state["window"]
        if win is None:
            return
        try:
            from webview.window import FixPoint   # webview is imported lazily in _run_window
            fp = (FixPoint.NORTH if fixy == "top" else FixPoint.SOUTH) | \
                 (FixPoint.WEST if fixx == "left" else FixPoint.EAST)
            win.resize(w, h, fp)
        except Exception as e:  # pragma: no cover
            conf.log("app", f"resize error: {e}")
    _dispatch(_impl)
    try:
        conf.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (conf.DATA_DIR / "dash_geom.json").write_text(json.dumps({"w": w, "h": h}), encoding="utf-8")
    except Exception:
        pass


def _icon_path(ext: str) -> str:
    """Path to the branded Helios icon asset (helios/ui/helios.ico|png)."""
    return str(conf.ROOT / "helios" / "ui" / f"helios.{ext}")


def _icon_image():
    """Tray icon: the branded Helios mark (helios.png). Falls back to drawing the simple cyan
    ring+core if the asset is missing, so the tray never fails to appear."""
    from PIL import Image, ImageDraw
    try:
        p = _icon_path("png")
        if os.path.exists(p):
            return Image.open(p).convert("RGBA")
    except Exception as e:  # pragma: no cover
        conf.log("app", f"tray icon load failed, using fallback: {e}")
    img = Image.new("RGBA", (64, 64), (12, 15, 20, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 54, 54), outline=(77, 208, 225, 255), width=4)
    d.ellipse((26, 26, 38, 38), fill=(77, 208, 225, 255))
    return img


def _apply_window_icon() -> None:
    """Give the dashboard window (and so its taskbar button) the Helios icon, instead of the default
    Python icon. pywebview doesn't set a window icon on Windows, so we load the .ico as an HICON and
    WM_SETICON it onto the window found by its "Helios" title. Retries briefly because the native
    window may not exist the instant this is called. restype/argtypes are declared so the HICON/HWND
    aren't truncated on 64-bit."""
    import time
    ico = _icon_path("ico")
    if not os.path.exists(ico):
        return
    u = ctypes.windll.user32
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    u.LoadImageW.restype = wt.HANDLE
    u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
    u.FindWindowW.restype = wt.HWND
    u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    u.SendMessageW.restype = ctypes.c_long
    u.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_void_p, ctypes.c_void_p]
    try:
        hbig = u.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        hsmall = u.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    except Exception as e:  # pragma: no cover
        conf.log("app", f"window icon load failed: {e}")
        return
    for _ in range(40):  # ~4s of retries while the native window comes up
        hwnd = u.FindWindowW(None, "Helios")
        if hwnd:
            if hbig:
                u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hbig)
            if hsmall:
                u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hsmall)
            conf.log("app", "window icon applied")
            return
        time.sleep(0.1)


def _already_running() -> bool:
    try:
        with urllib.request.urlopen(conf.BASE_URL + "/health", timeout=1.0) as r:
            if r.status != 200:
                return False
            # Confirm it's actually Helios, not some unrelated service on this port.
            return bool(json.loads(r.read() or b"{}").get("ok"))
    except Exception:
        return False


def _summon_running_instance() -> bool:
    """Ask the already-running instance to bring its native window forward (POST /summon).
    Returns True on success. Used instead of opening the dashboard URL in a web browser."""
    try:
        req = urllib.request.Request(
            conf.BASE_URL + "/summon", data=b"{}",
            headers={"Content-Type": "application/json",
                     "X-Auth-Token": conf.auth_token() or ""},
            method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        return True
    except Exception:
        return False


def main() -> None:
    if _already_running():
        conf.log("app", "another instance is already running; summoning it instead")
        # Bring the running instance's native window forward. Only if it can't be reached
        # do we fall back to opening the dashboard URL in a web browser.
        if not _summon_running_instance():
            webbrowser.open(conf.BASE_URL)
        return

    conf.ensure_auth_token()  # local CSRF token shared with the UI + the hook
    memory.ensure_vault()
    # Declare a distinct AppUserModelID so Windows treats Helios as its own app (correct taskbar
    # grouping + lets our window icon, not pythonw's, represent it on the taskbar).
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NotTimPunt.Helios")
    except Exception:
        pass

    hub = server.Hub()
    _state["hub"] = hub
    # YOLO mode (all-permissions) is per-chat and must never survive a restart — clear it at boot
    # before the server can take any tool call.
    try:
        conf.YOLO_FLAG.unlink(missing_ok=True)
    except Exception:
        pass
    # Boot hidden/dormant if configured: no orb, dashboard hidden, mic listening only for the
    # wake gesture. The first wake (double-clap / tray / hotkey) reveals everything.
    _state["dormant"] = bool(conf.startup_cfg().get("hidden", False))
    perms = PendingRegistry(notifier=_perm_notifier)  # broadcasts + auto-opens dashboard when no voice
    brain = build_brain(emit=hub.publish, perms=perms)   # Claude brain or lite brain, per [brain].engine
    if hasattr(brain, "warm"):   # e.g. start the Antigravity session so the first reply is fast
        threading.Thread(target=brain.warm, daemon=True, name="brain-warm").start()
    try:
        _state["httpd"] = server.start(
            brain, perms, hub,
            toggle=_toggle_dashboard,
            summon=_summon,
            minimize=_minimize_dashboard,
            close=lambda: _show_dashboard(False),
            voice_toggle=_make_voice_toggle(hub),
            sleep=_sleep,
            resize=_resize_dashboard)
    except OSError as e:
        conf.log("app", f"could not bind {conf.BASE_URL} ({e}); is Helios already running?")
        notify.toast("Helios failed to start", f"Port {conf.PORT} is in use. {e}")
        return

    from .side_agent import SideAgentPool
    max_par = int(conf.SETTINGS.get("agents", {}).get("max_parallel", 3))
    pool = SideAgentPool(emit=hub.publish, max_parallel=max_par)

    from .missions import MissionManager
    missions = MissionManager(emit=hub.publish)

    from .workflows import WorkflowManager
    workflows = WorkflowManager(pool, hub.publish)
    _state["httpd"].app["workflows"] = workflows   # server /workflow/* routes drive it (run-now etc.)

    from .scheduler import Scheduler
    Scheduler(brain, pool, missions, workflows, hub.publish).start()

    tg = conf.SETTINGS.get("telegram", {})
    if tg.get("token"):
        if not tg.get("allowed_ids"):
            conf.log("app", "WARNING: telegram token set but allowed_ids is empty — "
                            "bridge will reject ALL messages until you add your id.")
        from .telegram_bridge import TelegramBridge
        TelegramBridge(brain, tg["token"], tg.get("allowed_ids", []), perms=perms).start()

    def panic():
        brain.panic()
        pool.panic()
        missions.panic()  # kill every running mission supervisor tree
        workflows.panic()  # kill any in-flight workflow step process
        # Everyone's killed now — free the screen lock immediately (don't wait for the
        # ~20s stale-takeover; a force-killed MCP server can't release it itself).
        try:
            conf.SCREEN_LOCK.unlink(missing_ok=True)
        except Exception:
            pass

    def quit_app(*_):
        conf.log("app", "quit requested")
        _state["quit"].set()
        # Tear everything down so we don't leak headless claude -p trees / the listening socket.
        try:
            panic()  # kills brain + side-agent + mission process trees and clears SCREEN_LOCK
        except Exception as e:  # pragma: no cover
            conf.log("app", f"quit panic error: {e}")
        httpd = _state.get("httpd")
        if httpd is not None:
            try:
                httpd.shutdown()      # stop serve_forever (unblocks the serving thread)
                httpd.server_close()  # release the listening socket so a fast restart can rebind
            except Exception:
                pass
        try:
            if _state["icon"]:
                _state["icon"].stop()
        except Exception:
            pass
        _kill_orb()    # real orb interpreter (orb.pid) + launcher stub, and unlink orb.pid
        _kill_voice()  # voice daemon (voice.pid) + launcher stub, and unlink voice.pid
        w = _state.get("window")
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def restart_orb():
        """Settings changed ([orb] engine/size need a new process — the window size and the
        renderer are fixed at orb launch). Bounce the orb, but only bring one back if an orb
        was actually up (don't conjure an orb while dormant/asleep)."""
        pid, ctime = _read_orb_pid()
        was_up = _orb_pid_is_live_orb(pid, ctime) or _state.get("orb_proc") is not None
        _kill_orb()
        if was_up and not _state.get("dormant"):
            _launch_orb()

    # Register the power-menu Shut down action now that quit_app exists (route reads it live).
    if _state.get("httpd") is not None:
        _state["httpd"].app["quit"] = quit_app
        _state["httpd"].app["orb_restart"] = restart_orb   # /settings applies [orb] changes live

    Hotkeys(_summon, panic).start()
    _start_tray(_summon, brain, panic, quit_app)
    # Kick the cua-driver daemon off-thread: it's a synchronous subprocess.run(timeout=25)
    # whose result we ignore, so it must not gate orb/window cold-start.
    threading.Thread(target=_ensure_cua_driver, daemon=True).start()
    if not _state.get("dormant"):
        _launch_orb()    # when dormant we stay fully hidden — the orb appears only on wake
    else:
        conf.log("app", "started dormant (hidden) — orb withheld until the wake gesture")
    _launch_voice()      # standalone voice daemon (handles the wake gesture; no-op if voice off)

    conf.log("app", "Helios started")
    _run_window()


def _ensure_cua_driver() -> None:
    """Make sure the elevated cua-driver daemon (backs computer-use) is running. It autostarts
    at logon at RunLevel=Highest; kick it best-effort in case it isn't up yet. The daemon is what
    lets background UI Automation drive even elevated apps (Calculator/Settings) without admin UAC."""
    import subprocess
    exe = str(conf.cua_driver_bin())
    if not os.path.exists(exe):
        conf.log("app", "cua-driver.exe not found; computer-use will be unavailable")
        return
    try:
        subprocess.run([exe, "autostart", "kick"], capture_output=True, timeout=25,
                       creationflags=0x08000000)
        conf.log("app", "cua-driver daemon kicked")
    except Exception as e:  # pragma: no cover
        conf.log("app", f"cua-driver kick failed (non-fatal): {e}")


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _proc_creation_time(pid: int) -> int | None:
    """Creation time (FILETIME as a 64-bit int) of a live PID, or None if it can't be opened.
    Used to confirm a recorded orb PID hasn't been recycled to an unrelated process. restype/
    argtypes MUST be set or the HANDLE is truncated to 32-bit on 64-bit Windows and the calls fail."""
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


def _read_pid(path) -> tuple[int | None, int | None]:
    """(pid, creation_time) recorded as JSON in a {pid, ctime} file, or (None, None). Shared by the
    orb (data/orb.pid) and the voice daemon (data/voice.pid)."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        ct = d.get("ctime")
        return int(d["pid"]), (int(ct) if ct is not None else None)
    except Exception:
        return None, None


def _read_orb_pid() -> tuple[int | None, int | None]:
    """(pid, creation_time) the orb recorded in data/orb.pid as JSON, or (None, None)."""
    return _read_pid(conf.DATA_DIR / "orb.pid")


def _pid_is_live(pid, ctime) -> bool:
    """True only if `pid` is alive AND its creation time matches what we recorded — i.e. it is
    genuinely our child (orb / voice daemon), not a recycled PID now owned by an unrelated process.
    Without a recorded ctime we can't verify, so we refuse to kill (fail safe)."""
    if not pid or pid == os.getpid() or ctime is None:
        return False
    return _proc_creation_time(pid) == ctime


# Back-compat alias (older call sites in the orb kill paths).
_orb_pid_is_live_orb = _pid_is_live


def _kill_stray_orb() -> None:
    """Kill an orphaned orb from a previous run before spawning a fresh one — but ONLY when the
    recorded PID is verifiably still that orb (creation-time match). A fast restart can orphan the
    old orb (its health-watchdog only self-exits ~20s after the server goes away), stacking a stale
    orb under the new one. Windows recycles PIDs and orb.pid persists across crashes, so a bare-PID
    kill could terminate an unrelated process — hence the identity check; otherwise just clear it."""
    import signal
    pid, ctime = _read_orb_pid()
    try:
        if _orb_pid_is_live_orb(pid, ctime):
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
            conf.log("app", f"killed orphaned orb pid {pid}")
        else:
            (conf.DATA_DIR / "orb.pid").unlink(missing_ok=True)  # stale/recycled — clear it
    except Exception:
        pass


def _kill_orb() -> None:
    """Kill the orb on quit: the real interpreter PID it recorded (taskkill /T reaches its child
    tree — the venv pythonw Popen handle is only the launcher stub) AND the stub handle, then
    remove data/orb.pid so the next start can't act on a stale entry."""
    pid, ctime = _read_orb_pid()
    if _orb_pid_is_live_orb(pid, ctime):
        kill_pid(pid)  # real orb interpreter + its child tree
    op = _state.get("orb_proc")
    if op is not None:
        try:
            op.terminate()
        except Exception:
            pass
    try:
        (conf.DATA_DIR / "orb.pid").unlink(missing_ok=True)
    except Exception:
        pass


def _launch_orb() -> None:
    """Launch the transparent orb as its own tkinter process.

    WebView2 can't render a transparent overlay on Windows, so the orb is a separate
    tkinter window (real GDI color-key transparency). It talks to us over the local
    server (SSE state in, /orb/toggle out) and exits on its own if we disappear.
    """
    import subprocess
    import sys
    _kill_stray_orb()  # clear any orphan from a previous run so orbs don't stack
    exe = sys.executable
    pyw = exe.replace("python.exe", "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw  # no console flash (handoff gotcha: spawned procs need pythonw)
    script = str(conf.ROOT / "helios" / "orb_overlay.py")
    CREATE_NO_WINDOW = 0x08000000
    try:
        _state["orb_proc"] = subprocess.Popen(
            [exe, script], cwd=str(conf.ROOT), creationflags=CREATE_NO_WINDOW)
    except Exception as e:  # pragma: no cover
        conf.log("app", f"orb overlay failed to launch: {e}")


# ---- voice daemon (separate process, like the orb) ------------------------------------
def _voice_running() -> bool:
    """True if our voice daemon is genuinely alive (PID + creation-time verified)."""
    pid, ctime = _read_pid(conf.VOICE_PID_FILE)
    return _pid_is_live(pid, ctime)


def _kill_stray_voice() -> None:
    """Kill an orphaned voice daemon from a previous run before spawning a fresh one — only when
    the recorded PID verifiably is still ours (creation-time match), else just clear the stale file.
    Same recycled-PID safety as the orb."""
    pid, ctime = _read_pid(conf.VOICE_PID_FILE)
    try:
        if _pid_is_live(pid, ctime):
            kill_pid(pid)
            conf.log("app", f"killed orphaned voice daemon pid {pid}")
        else:
            conf.VOICE_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _kill_voice() -> None:
    """Kill the voice daemon (its recorded interpreter PID + its child tree, plus the launcher
    stub) and clear data/voice.pid."""
    pid, ctime = _read_pid(conf.VOICE_PID_FILE)
    if _pid_is_live(pid, ctime):
        kill_pid(pid)
    vp = _state.get("voice_proc")
    if vp is not None:
        try:
            vp.terminate()
        except Exception:
            pass
    _state["voice_proc"] = None
    try:
        conf.VOICE_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _launch_voice() -> bool:
    """Launch the voice daemon as its own process (heavy audio/ML libs stay out of this process).
    No-op (returns False) if voice is disabled in settings or the Kokoro model assets are missing."""
    import subprocess
    import sys
    if not conf.voice_cfg().get("enabled", False):
        conf.log("app", "voice disabled in settings; not launching daemon")
        return False
    if not conf.KOKORO_MODEL.exists() or not conf.KOKORO_VOICES.exists():
        conf.log("app", f"voice enabled but Kokoro assets missing ({conf.VOICE_DIR}); skipping")
        return False
    _kill_stray_voice()
    exe = sys.executable
    pyw = exe.replace("python.exe", "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw  # no console flash (handoff gotcha)
    script = str(conf.ROOT / "helios" / "voice" / "daemon.py")
    CREATE_NO_WINDOW = 0x08000000
    env = dict(os.environ)
    # A daemon (re)launched while the app is awake (e.g. the dashboard mic toggle) must not boot
    # dormant just because [startup].hidden is set — that left it waiting for a wake gesture.
    env["HELIOS_VOICE_AWAKE"] = "0" if _state.get("dormant") else "1"
    try:
        _state["voice_proc"] = subprocess.Popen(
            [exe, script], cwd=str(conf.ROOT), creationflags=CREATE_NO_WINDOW, env=env)
        conf.log("app", "voice daemon launched")
        return True
    except Exception as e:  # pragma: no cover
        conf.log("app", f"voice daemon failed to launch: {e}")
        return False


def _make_voice_toggle(hub):
    """Build the /voice/toggle callback: flip the daemon on/off at runtime (dashboard mic button).
    Returns the resulting running state so the UI can update immediately."""
    def toggle() -> bool:
        if _voice_running() or _state.get("voice_proc") is not None:
            _kill_voice()
            try:
                conf.update_settings({"voice.enabled": False})  # button is the source of truth
            except Exception:
                pass
            hub.publish("voice", {"state": "off"})
            conf.log("app", "voice daemon stopped (toggle)")
            return False
        try:
            conf.update_settings({"voice.enabled": True})
        except Exception:
            pass
        return _launch_voice()
    return toggle


def _start_tray(summon, brain, panic, quit_app) -> None:
    def run():
        try:
            import pystray
            from pystray import MenuItem as item
            menu = pystray.Menu(
                item("Open Helios", lambda *_: summon(), default=True),
                item("New conversation", lambda *_: brain.new_conversation()),
                item("Panic stop", lambda *_: panic()),
                item("Quit", quit_app),
            )
            icon = pystray.Icon("Helios", _icon_image(), f"Helios v{conf.VERSION}", menu)
            _state["icon"] = icon
            icon.run()
        except Exception as e:  # pragma: no cover
            conf.log("app", f"tray failed: {e}")

    threading.Thread(target=run, daemon=True).start()


def _run_window() -> None:
    """Reactive orb (always-on-top, top-right) + a hidden dashboard it toggles. Falls back
    to a plain browser window if pywebview is unavailable."""
    try:
        import webview

        # Only the element literally clicked counts as a drag handle — so the custom title
        # bar (.pywebview-drag-region) moves the window, but the buttons inside it still
        # register clicks instead of dragging. (Default walks up the DOM, which would make
        # every child of the bar a drag handle too.)
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True

        # Large, centered window with breathing room on every edge (see the open-animation
        # sketches). frameless=True drops the native Windows title bar (we draw our own
        # Helios-style one in the UI); easy_drag=False so only the drag-region moves it,
        # not clicks on the content.
        u = ctypes.windll.user32
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        W = max(720, int(sw * 0.78))
        H = max(560, int(sh * 0.80))
        # Restore a size the user picked before (the resize grip persists it); clamp to monitor + min.
        try:
            geom = json.loads((conf.DATA_DIR / "dash_geom.json").read_text(encoding="utf-8"))
            W = max(420, min(int(sw), int(geom.get("w", W))))
            H = max(520, min(int(sh), int(geom.get("h", H))))
        except Exception:
            pass
        x, y = (sw - W) // 2, (sh - H) // 2
        dash = webview.create_window(
            "Helios", conf.BASE_URL, x=x, y=y, width=W, height=H,
            min_size=(420, 520), background_color="#0c0f14", hidden=True,
            resizable=True,         # resizable (also driven by our custom bottom-right grip)
            on_top=True,            # float above all other apps (like the orb)
            frameless=True,         # no native title bar — we render our own
            easy_drag=False,        # drag only via the .pywebview-drag-region title bar
        )
        _state["window"] = dash
        _state["mode"] = "webview"

        # Brand the window + its taskbar button with the Helios icon (pywebview doesn't on Windows).
        # Once now (the window exists even while hidden) and again on each show, so it always sticks.
        threading.Thread(target=_apply_window_icon, daemon=True).start()
        try:
            dash.events.shown += lambda: threading.Thread(
                target=_apply_window_icon, daemon=True).start()
        except Exception:
            pass

        def on_closing():
            if _state["quit"].is_set():
                return True
            _show_dashboard(False)  # hide to the orb instead of quitting
            return False

        dash.events.closing += on_closing

        # Start the window-op dispatcher now that the window exists (see _window_dispatcher):
        # all show/hide/minimize/evaluate_js go through it instead of HTTP/hotkey/tray threads.
        threading.Thread(target=_window_dispatcher, daemon=True).start()

        # The orb is a separate tkinter process (see _launch_orb); it can't live as a
        # pywebview window because WebView2 won't render a transparent overlay.
        webview.start()
    except Exception as e:
        conf.log("app", f"pywebview unavailable ({e}); using browser window")
        _state["mode"] = "browser"
        webbrowser.open(conf.BASE_URL)
        _state["quit"].wait()


if __name__ == "__main__":
    main()
