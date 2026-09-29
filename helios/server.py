"""Local HTTP + SSE server: serves the Helios chat UI and bridges it to the brain,
the memory engine, and the permission broker. Stdlib only.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import parse_qs, urlparse

from . import conf, db

UI_DIR = conf.ROOT / "helios" / "ui"
_PERM_WAIT = 120  # seconds the hook (and the user) has to decide before fail-safe deny
_MAX_BODY = 2 * 1024 * 1024  # cap request bodies (defend against a huge Content-Length)

# Settings the UI is allowed to write, with their expected type. Anything else is
# rejected so an attacker (or bug) can't rewrite arbitrary keys in settings.toml.
_SETTINGS_SPEC: dict[str, str] = {
    "router.auto": "bool", "router.triage": "bool",
    "router.light": "str", "router.medium": "str", "router.heavy": "str",
    "proactive.enabled": "bool",
    "proactive.battery_pct": "num", "proactive.disk_gb": "num",
    "proactive.downloads_count": "num", "proactive.quiet_start": "num",
    "proactive.quiet_end": "num",
    "claude.model": "str", "screen.interval_min": "num",
    # Voice stack. enabled is applied live by /voice/toggle; the rest take effect on the
    # next daemon (re)start — the UI notes this.
    "voice.enabled": "bool", "voice.wake_threshold": "num", "voice.stt_model": "str",
    "voice.tts_voice": "str", "voice.tts_speed": "num", "voice.tts_lang": "str",
    "voice.followup_sec": "num", "voice.speak_all": "bool", "voice.barge_in": "bool",
    "voice.live_transcript": "bool",
    # Startup behaviour (hidden/dormant + wake gesture). Take effect on next launch.
    "startup.hidden": "bool", "startup.wake_gesture": "str", "startup.clap_sensitivity": "num",
    # Orb: style reloads the orb page live; engine/size restart the orb process.
    "orb.style": "str", "orb.engine": "str", "orb.size": "num",
}


def _clean_settings_changes(changes: dict) -> dict:
    """Validate/coerce UI-supplied settings changes against the allowlist."""
    out: dict = {}
    for key, val in (changes or {}).items():
        kind = _SETTINGS_SPEC.get(key)
        if kind is None:
            raise ValueError(f"setting not allowed: {key}")
        if kind == "bool":
            out[key] = bool(val)
        elif kind == "num":
            try:
                f = float(val)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
            if f != f or f in (float("inf"), float("-inf")):
                raise ValueError(f"{key} must be finite")
            out[key] = int(f) if f.is_integer() else f
        else:  # str
            s = str(val)[:200]
            if any(c in s for c in "\n\r"):
                raise ValueError(f"{key} must be a single line")
            out[key] = s
    return out


class Hub:
    """Fan-out of brain/permission events to all connected SSE clients."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, data) -> None:
        msg = {"kind": kind, "data": data}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


class Handler(BaseHTTPRequestHandler):
    """Per-request handler for every UI/API route.

    One instance per request (ThreadingHTTPServer runs each in its own thread). Every
    non-bootstrap route is gated by three checks, in this order: _host_ok (loopback Host,
    anti DNS-rebinding) -> _origin_ok (own Origin, anti-CSRF, POST only) -> _authed (local
    token). The shared app dependencies (brain/perms/hub/toggle/summon) hang off the server
    via the `app` property below.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence default logging
        pass

    # ---- helpers ----
    @property
    def app(self):
        # The dict wired up in start(): brain, perms, hub, and optional toggle/summon callbacks.
        return self.server.app  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra_headers: dict | None = None) -> None:
        """Write a complete response (status + headers + body). Swallows write errors so a
        client that disconnected mid-response can't crash the handler thread."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_body(self) -> bytes:
        """Read (and fully drain) the request body once. Draining matters even for routes that
        don't use the body (/panic, /orb/toggle, /summon, /window/*): on an HTTP/1.1 keep-alive
        connection an unread body corrupts the NEXT request. Caps the kept bytes at _MAX_BODY."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return b""
        if n <= 0:
            return b""
        data = self.rfile.read(min(n, _MAX_BODY))
        rest = n - len(data)
        while rest > 0:  # discard any excess so keep-alive stays in sync
            chunk = self.rfile.read(min(rest, 65536))
            if not chunk:
                break
            rest -= len(chunk)
        return data if n <= _MAX_BODY else b""

    def _json_body(self) -> dict:
        """Parse the already-drained request body as JSON, returning {} on anything malformed."""
        try:
            return json.loads(getattr(self, "_raw_body", b"") or b"{}")
        except Exception:
            return {}

    # ---- auth / anti-CSRF ----
    def _host_ok(self) -> bool:
        """Reject requests whose Host isn't loopback (defeats DNS-rebinding)."""
        host = (self.headers.get("Host") or "").strip().lower()
        return host in {f"127.0.0.1:{conf.PORT}", f"localhost:{conf.PORT}", f"[::1]:{conf.PORT}"}

    def _origin_ok(self) -> bool:
        """If an Origin is present it must be our own (blocks cross-site fetches)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-CORS request; the token check still applies
        o = origin.strip().lower().rstrip("/")
        return o in {f"http://127.0.0.1:{conf.PORT}", f"http://localhost:{conf.PORT}",
                     f"http://[::1]:{conf.PORT}"}  # match the IPv6 loopback _host_ok accepts

    def _request_token(self) -> str:
        tok = self.headers.get("X-Auth-Token")
        if tok:
            return tok.strip()
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        # EventSource can't set headers -> accept the token as a query param.
        return (parse_qs(urlparse(self.path).query).get("token", [""])[0] or "").strip()

    def _authed(self) -> bool:
        """True only if the request carries the live local auth token. Uses
        secrets.compare_digest (constant-time) to avoid leaking the token via timing."""
        want = conf.auth_token()
        got = self._request_token()
        return bool(want) and bool(got) and secrets.compare_digest(got, want)

    # ---- routes ----
    def do_GET(self):
        """Route GETs: public bootstrap routes (UI files, /health) need no token; every other
        GET (state reads + the /events SSE stream) requires _authed(). SSE can't send headers,
        so /events authenticates via the ?token= query param (see _request_token)."""
        if not self._host_ok():
            return self._send(403, b'{"error":"bad host"}')
        path = urlparse(self.path).path
        # Public bootstrap routes (no token needed).
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/orb":
            # The transparent orb overlay (separate process) loads this; token is injected like /.
            return self._serve_file("orb.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            ctype = ("text/css" if name.endswith(".css")
                     else "application/javascript" if name.endswith(".js")
                     else "application/octet-stream")
            return self._serve_file(name, ctype)
        if path == "/health":
            return self._send(200, b'{"ok":true}')
        # Everything below requires the local token.
        if not self._authed():
            return self._send(401, b'{"error":"unauthorized"}')
        if path == "/busy":
            busy = self.app["brain"].busy()
            return self._send(200, json.dumps({"busy": busy}).encode())
        if path == "/version":
            return self._send(200, json.dumps({"version": conf.VERSION}).encode())
        if path == "/conversations":
            return self._send(200, json.dumps({"items": db.list_conversations()}).encode())
        if path == "/settings":
            s = conf.SETTINGS
            return self._send(200, json.dumps({
                "router": {k: s.get("router", {}).get(k) for k in ("auto", "light", "medium", "heavy")},
                "proactive": {k: s.get("proactive", {}).get(k) for k in
                              ("enabled", "battery_pct", "disk_gb", "downloads_count", "quiet_start", "quiet_end")},
                "model": s.get("claude", {}).get("model"),
                "tone": db.get_state("tone") or "",
                "hotkeys": s.get("hotkeys", {}),
                "voice": {k: s.get("voice", {}).get(k) for k in
                          ("enabled", "wake_word", "wake_threshold", "stt_model", "tts_voice",
                           "tts_speed", "tts_lang", "followup_sec", "speak_all", "barge_in",
                           "live_transcript", "dictation_hotkey")},
                "startup": {k: s.get("startup", {}).get(k) for k in
                            ("hidden", "wake_gesture", "open_on_wake", "clap_sensitivity")},
                "orb": {"style": s.get("orb", {}).get("style", "bloom"),
                        "engine": s.get("orb", {}).get("engine", "webview"),
                        "size": s.get("orb", {}).get("size", 190)},
                "allowlist": conf.autonomy_allow(),
                "yolo": conf.YOLO_FLAG.exists(),   # live runtime flag, so the toggle inits right on reload
                "version": conf.VERSION,
            }).encode())
        if path == "/conversation":
            cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            return self._send(200, json.dumps({"messages": db.conversation_messages(cid)}).encode())
        if path == "/missions":
            return self._send(200, json.dumps({"items": db.list_missions(30)}).encode())
        if path == "/mission":
            raw = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"bad id"}')
            m = db.get_mission(mid)
            if not m:
                return self._send(404, b'{"error":"no mission"}')
            return self._send(200, json.dumps({
                "mission": m, "agents": db.mission_agents(mid),
                "log": db.read_mission_log(mid),
            }).encode())
        if path == "/workflows":
            return self._send(200, json.dumps({"items": db.list_workflows()}).encode())
        if path == "/workflow":
            raw = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            try:
                wid = int(raw)
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"bad id"}')
            wf = db.get_workflow(wid)
            if not wf:
                return self._send(404, b'{"error":"no workflow"}')
            try:
                spec = json.loads(wf["spec"])
            except Exception:
                spec = {}
            return self._send(200, json.dumps({
                "workflow": wf, "spec": spec, "runs": db.list_workflow_runs(wid, 20),
            }).encode())
        # ---- 3D models (registry-backed: opaque ids only, GLB only; ported from Helios-main) ----
        if path == "/models":
            from . import models3d
            return self._send(200, json.dumps({"items": models3d.scan()}).encode())
        if path.startswith("/model/"):
            from . import models3d
            p = models3d.get_path(path[len("/model/"):])
            if p is None:
                return self._send(404, b'{"error":"no model"}')
            try:
                body = p.read_bytes()
            except Exception:
                return self._send(404, b'{"error":"no model"}')
            # no-store: ids are path hashes, so a re-exported model reuses its URL
            return self._send(200, body, "model/gltf-binary",
                              extra_headers={"Cache-Control": "no-store"})
        if path == "/events":
            return self._events()
        return self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        """Route POSTs (all state-changing actions). Adds the _origin_ok anti-CSRF check on
        top of the Host + token checks because POSTs are the ones a malicious page could try
        to forge. /message runs the brain turn off-thread so the request returns immediately."""
        if not self._host_ok():
            return self._send(403, b'{"error":"bad host"}')
        if not self._origin_ok():
            return self._send(403, b'{"error":"bad origin"}')
        if not self._authed():
            return self._send(401, b'{"error":"unauthorized"}')
        self._raw_body = self._read_body()  # drain once; _json_body() parses this
        path = urlparse(self.path).path
        if path == "/message":
            body = self._json_body()
            text = (body.get("text") or "").strip()
            steer = bool(body.get("steer"))   # dashboard mid-turn message -> steer the running turn
            if text:
                threading.Thread(target=self.app["brain"].run_turn,
                                 args=(text,), kwargs={"steer": steer}, daemon=True).start()
            return self._send(200, b'{"ok":true}')
        if path == "/permission/ask":
            body = self._json_body()
            perms = self.app["perms"]
            rid = perms.create(body.get("tool", ""), body.get("input", {}), sink=body.get("sink", ""))
            decision = perms.wait(rid, _PERM_WAIT)
            return self._send(200, json.dumps({"decision": decision}).encode())
        if path == "/permission/respond":
            body = self._json_body()
            ok = self.app["perms"].resolve(body.get("id", ""), body.get("decision", "deny"))
            return self._send(200, json.dumps({"ok": ok}).encode())
        if path == "/open":
            url = (self._json_body().get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                import webbrowser
                webbrowser.open(url)
                return self._send(200, b'{"ok":true}')
            return self._send(400, b'{"ok":false}')
        if path == "/model/show":
            # The model_3d MCP tool (separate process) hands a finished GLB to the dashboard:
            # register it (opaque id) + fan out a `model3d` chat-card SSE frame.
            from . import models3d
            body = self._json_body()
            entry = models3d.register(str(body.get("path", "")))
            if not entry:
                return self._send(400, b'{"error":"not a servable glb"}')
            png = body.get("png", "")
            if not (isinstance(png, str) and png.startswith("data:image/png;base64,")):
                png = ""
            self.app["hub"].publish("model3d", {
                "id": entry["id"], "name": entry["name"], "url": entry["url"],
                "size": entry["size"], "verts": int(body.get("verts") or 0),
                "faces": int(body.get("faces") or 0), "png": png,
            })
            return self._send(200, json.dumps(entry).encode())
        if path == "/voice/state":
            # The standalone voice daemon pushes its state (idle/listening/thinking/speaking/
            # dictation/...) here; we fan it out as a 'voice' SSE event so the orb + dashboard
            # react. transcript carries the recognized command so the UI can show "you said…".
            body = self._json_body()
            data = {"state": str(body.get("state", "") or "")[:24]}
            for k in ("transcript", "text", "partial"):
                if k in body:
                    data[k] = str(body.get(k) or "")[:2000]
            if "level" in body:   # live audio level (0..1) for the orb/HUD VU meter
                try:
                    data["level"] = max(0.0, min(1.0, float(body.get("level"))))
                except (TypeError, ValueError):
                    pass
            self.app["hub"].publish("voice", data)
            return self._send(200, b'{"ok":true}')
        if path == "/voice/toggle":
            # The dashboard mic button flips the voice daemon on/off at runtime (no restart).
            cb = self.app.get("voice_toggle")
            running = False
            if cb:
                try:
                    running = bool(cb())
                except Exception as e:  # pragma: no cover
                    conf.log("server", f"voice toggle error: {e}")
            return self._send(200, json.dumps({"running": running}).encode())
        if path == "/sleep":
            # Power menu -> Sleep: hide the dashboard + remove the orb, go dormant (the voice
            # daemon keeps listening for the double-clap to wake again).
            cb = self.app.get("sleep")
            if cb:
                try:
                    cb()
                except Exception as e:  # pragma: no cover
                    conf.log("server", f"sleep error: {e}")
            return self._send(200, b'{"ok":true}')
        if path == "/quit":
            # Power menu -> Shut down: tear Helios down completely (won't return until relaunched).
            # Run off-thread + answer first: quit_app stops this very server, which would otherwise
            # deadlock if called inline on the serving thread.
            cb = self.app.get("quit")
            if cb:
                self._send(200, b'{"ok":true}')
                threading.Thread(target=cb, daemon=True).start()
                return
            return self._send(200, b'{"ok":false}')
        if path == "/panic":
            self.app["brain"].panic()
            return self._send(200, b'{"ok":true}')
        if path == "/new":
            self.app["brain"].new_conversation()
            return self._send(200, b'{"ok":true}')
        if path == "/yolo":
            # All-permissions toggle for the current chat. A file flag (logs/yolo.flag) the
            # PreToolUse hook reads; the hard-rails (panic/SSRF/~.claude) still apply. Brain
            # resets clear it on every new-chat/panic boundary; app boot clears it too.
            state = str(self._json_body().get("state", "") or "").strip().lower()
            on = state == "on"
            try:
                if on:
                    conf.YOLO_FLAG.parent.mkdir(parents=True, exist_ok=True)
                    conf.YOLO_FLAG.write_text("on", encoding="utf-8")
                else:
                    conf.YOLO_FLAG.unlink(missing_ok=True)
            except Exception as e:  # pragma: no cover
                conf.log("server", f"yolo toggle error: {e}")
            self.app["hub"].publish("yolo", {"on": on})   # sync every open surface
            return self._send(200, json.dumps({"on": on}).encode())
        if path == "/orb/toggle":
            # The standalone orb overlay (separate process) clicks through to here.
            t = self.app.get("toggle")
            if t:
                try:
                    t()
                except Exception as e:  # pragma: no cover
                    conf.log("server", f"orb toggle error: {e}")
            return self._send(200, b'{"ok":true}')
        if path == "/summon":
            # A second app launch (shortcut/task) hits this so it brings the native window
            # forward instead of opening the dashboard URL in a web browser.
            s = self.app.get("summon")
            if s:
                try:
                    s()
                except Exception as e:  # pragma: no cover
                    conf.log("server", f"summon error: {e}")
            return self._send(200, b'{"ok":true}')
        if path in ("/window/minimize", "/window/close"):
            # The frameless dashboard has no native title bar, so its custom Helios-style
            # bar drives the window through these: minimize -> taskbar, close -> hide to orb.
            cb = self.app.get("minimize" if path.endswith("minimize") else "close")
            if cb:
                try:
                    cb()
                except Exception as e:  # pragma: no cover
                    conf.log("server", f"window {path} error: {e}")
            return self._send(200, b'{"ok":true}')
        if path == "/window/resize":
            # Custom edge/corner resize zones (the frameless window has no native sizing border).
            # fixx/fixy say which edge stays put (the side opposite the one being dragged).
            body = self._json_body()
            try:
                w, h = int(body.get("width")), int(body.get("height"))
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"bad size"}')
            fixx = "right" if str(body.get("fixx")) == "right" else "left"
            fixy = "bottom" if str(body.get("fixy")) == "bottom" else "top"
            cb = self.app.get("resize")
            if cb:
                try:
                    cb(w, h, fixx, fixy)
                except Exception as e:  # pragma: no cover
                    conf.log("server", f"window resize error: {e}")
            return self._send(200, b'{"ok":true}')
        if path == "/conversation/load":
            self.app["brain"].set_session(self._json_body().get("id", ""))
            return self._send(200, b'{"ok":true}')
        if path == "/settings":
            body = self._json_body()
            try:
                changes = body.get("changes") or {}
                orb_prev = dict(conf.SETTINGS.get("orb", {}))   # snapshot BEFORE the write
                if changes:
                    changes = _clean_settings_changes(changes)
                    if "orb.engine" in changes and changes["orb.engine"] not in ("webview", "layered", "tk"):
                        raise ValueError("orb.engine must be webview, layered or tk")
                    if "orb.size" in changes:
                        changes["orb.size"] = int(max(120, min(500, changes["orb.size"])))
                    conf.update_settings(changes)
                if "tone" in body:
                    db.set_state("tone", str(body.get("tone") or "")[:500])
                # Orb engine/size need a fresh orb PROCESS (window size + renderer are fixed at
                # launch); a bare style change only needs the orb page to reload. The UI sends the
                # orb keys on every save, so only act when a value actually CHANGED.
                def _orb_changed(key, default):
                    return key in changes and changes[key] != orb_prev.get(key.split(".")[1], default)
                if _orb_changed("orb.engine", "webview") or _orb_changed("orb.size", 190):
                    cb = self.app.get("orb_restart")
                    if cb:
                        threading.Thread(target=cb, daemon=True).start()
                elif _orb_changed("orb.style", "bloom"):
                    self.app["hub"].publish("control", {"action": "orb_reload"})
                return self._send(200, b'{"ok":true}')
            except ValueError as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}).encode())
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}).encode())
        if path == "/workflow/save":
            # Create or update a workflow from the editor. Validate the spec, then denormalize its
            # schedule/next_run (like a routine) so the scheduler can poll it cheaply.
            from . import workflows as _wf
            from datetime import datetime as _dt
            body = self._json_body()
            spec = body.get("spec") or {}
            ok, err = _wf.validate_spec(spec)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": err}).encode())
            name = str(spec.get("name") or "")[:120]
            sched = _wf.schedule_of(spec)
            next_run = None
            if sched:
                from . import sched_util
                try:
                    next_run = sched_util.next_run_after(sched, _dt.now()).isoformat(timespec="seconds")
                except Exception:
                    next_run = None
            spec_json = json.dumps(spec)
            wid = body.get("id")
            if wid:
                try:
                    wid = int(wid)
                except (TypeError, ValueError):
                    return self._send(400, b'{"error":"bad id"}')
                db.update_workflow(wid, name, spec_json, sched, next_run)
            else:
                wid = db.add_workflow(name, spec_json, sched, next_run, enabled=True)
            return self._send(200, json.dumps({"ok": True, "id": wid}).encode())
        if path == "/workflow/run":
            try:
                wid = int(self._json_body().get("id"))
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"bad id"}')
            mgr = self.app.get("workflows")
            run_id = mgr.run_async(wid, "manual") if mgr else None
            return self._send(200, json.dumps({"ok": run_id is not None, "run_id": run_id}).encode())
        if path == "/workflow/enabled":
            body = self._json_body()
            try:
                wid = int(body.get("id"))
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"bad id"}')
            db.set_workflow_enabled(wid, bool(body.get("enabled")))
            return self._send(200, b'{"ok":true}')
        if path == "/workflow/delete":
            try:
                wid = int(self._json_body().get("id"))
            except (TypeError, ValueError):
                return self._send(400, b'{"error":"bad id"}')
            return self._send(200, json.dumps({"ok": db.delete_workflow(wid)}).encode())
        return self._send(404, b'{"error":"not found"}')

    def _serve_file(self, name: str, ctype: str):
        # Guard against path traversal via crafted /static/ names.
        try:
            p = (UI_DIR / name).resolve()
            p.relative_to(UI_DIR.resolve())
        except Exception:
            return self._send(404, b"not found", "text/plain")
        if not p.exists() or not p.is_file():
            return self._send(404, b"not found", "text/plain")
        data = p.read_bytes()
        if name.endswith(".html"):
            # Inject the auth token ONLY into the quoted placeholder ("__HELIOS_TOKEN__"),
            # as a JSON string literal. Replacing the bare token would also clobber the JS
            # identifier `window.__HELIOS_TOKEN__`, leaving the client with an empty token.
            tok = json.dumps(conf.auth_token() or "")
            data = data.replace(b'"__HELIOS_TOKEN__"', tok.encode())
            # Inject the orb style so orb.html loads the right renderer immediately (no-op for
            # index.html, which has no such placeholder).
            style = json.dumps(str(conf.SETTINGS.get("orb", {}).get("style", "glow")))
            data = data.replace(b'"__ORB_STYLE__"', style.encode())
            # Never let WebView2 cache a token-bearing page (stale token -> 401).
            return self._send(200, data, ctype, extra_headers={"Cache-Control": "no-store"})
        # Don't let WebView2's persistent cache serve a stale app.js / style.css after a UI change
        # (the files are tiny + local, so skipping the cache costs nothing and avoids "my fix
        # didn't load" ghosts — e.g. a new button rendering from fresh HTML but with no handler).
        return self._send(200, data, ctype, extra_headers={"Cache-Control": "no-store"})

    def _events(self):
        """Server-Sent Events stream: subscribes to the Hub and forwards every brain/permission
        event to this client as `data:` frames. On a 15s idle it emits a `: ping` comment — this
        both keeps the connection alive and surfaces a dropped client (the write raises, we
        unsubscribe). Always unsubscribes in the finally so dead clients don't accumulate."""
        q = self.app["hub"].subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            while True:
                try:
                    msg = q.get(timeout=15)
                    payload = json.dumps(msg).encode("utf-8")
                    self.wfile.write(b"data: " + payload + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # heartbeat / disconnect detector
                self.wfile.flush()
        except Exception:
            pass
        finally:
            self.app["hub"].unsubscribe(q)


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with SO_REUSEADDR so a fast Stop/Start of the Helios scheduled task
    can immediately rebind the port instead of hitting 'address already in use', and daemon
    request threads so they never block interpreter shutdown."""
    allow_reuse_address = True
    daemon_threads = True


def start(brain, perms, hub, toggle=None, summon=None,
          minimize=None, close=None, voice_toggle=None, sleep=None,
          resize=None) -> ThreadingHTTPServer:
    """Boot the HTTP/SSE server on a daemon thread and return the running server.

    brain    — the LLM brain (run_turn/panic/busy/session/conversation control).
    perms    — the permission broker the /permission/* routes drive (mirrors the hook flow).
    hub      — the Hub the /events SSE stream fans out from.
    toggle   — optional callback for /orb/toggle (show/hide the native dashboard window).
    summon   — optional callback for /summon (always bring the native window forward; a 2nd
               app launch calls this instead of opening the dashboard URL in a browser).
    minimize — optional callback for /window/minimize (frameless window's custom – button).
    close    — optional callback for /window/close (frameless window's custom ✕ → hide to orb).
    voice_toggle — optional callback for /voice/toggle (start/stop the voice daemon); returns
               True if voice is now running.
    sleep    — optional callback for /sleep (power menu: hide dashboard + orb, go dormant).
               The /quit callback (power menu: full shutdown) is wired into app['quit'] after
               start() returns, because quit_app needs the pool/missions created later in main().
    """
    conf.ensure_auth_token()  # must exist before we serve the (token-injected) UI
    httpd = _Server((conf.HOST, conf.PORT), Handler)
    httpd.app = {"brain": brain, "perms": perms, "hub": hub,  # type: ignore[attr-defined]
                 "toggle": toggle, "summon": summon,
                 "minimize": minimize, "close": close, "voice_toggle": voice_toggle,
                 "sleep": sleep, "resize": resize,
                 "quit": None}  # quit is wired post-init (needs quit_app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    conf.log("server", f"listening on {conf.BASE_URL}")
    return httpd
