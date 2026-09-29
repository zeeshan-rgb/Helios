"""Bridge between the voice daemon and the Helios app — pure HTTP + SSE, exactly like the orb.

The daemon never imports the brain; it drives it over the same local API the dashboard uses:
  - send_message(text)        POST /message        run a brain turn (fire-and-forget)
  - post_voice_state(...)     POST /voice/state    push listening/thinking/speaking to orb + UI
  - listen(on_event)          GET  /events (SSE)   receive model/token/tool/done/error to speak

All requests carry the shared conf.auth_token(); SSE passes it as ?token= (EventSource-style).
"""

from __future__ import annotations

import json
import threading
import urllib.request

from .. import conf


class AppBridge:
    def __init__(self):
        self.base = conf.BASE_URL

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "X-Auth-Token": conf.auth_token() or ""}

    def _post(self, path: str, payload: dict, timeout: float = 5.0) -> bool:
        try:
            req = urllib.request.Request(self.base + path,
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers=self._headers(), method="POST")
            urllib.request.urlopen(req, timeout=timeout).read()
            return True
        except Exception as e:
            conf.log("voice", f"POST {path} failed: {e}")
            return False

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=3) as r:
                return bool(json.loads(r.read() or b"{}").get("ok"))
        except Exception:
            return False

    def send_message(self, text: str) -> bool:
        return self._post("/message", {"text": text})

    def summon(self) -> bool:
        """Ask the app to come to the foreground (used as the double-clap wake gesture). The app's
        /summon also un-dormants: reveals the orb, opens the configured apps, shows the dashboard."""
        return self._post("/summon", {})

    def panic(self) -> bool:
        """Abort the in-flight brain turn (barge-in / stop). POST /panic -> brain.panic(): kills the
        `claude -p` tree, frees the single-flight lock so the next /message isn't rejected as busy."""
        return self._post("/panic", {})

    def respond_permission(self, rid: str, decision: str) -> bool:
        """Answer a pending permission ask by voice — POST /permission/respond (the same route the
        dashboard Approve/Deny buttons use). decision is 'allow' or 'deny'."""
        return self._post("/permission/respond", {"id": rid, "decision": decision})

    def post_voice_state(self, state: str, **fields) -> None:
        """Best-effort UI state push (orb + dashboard). Never blocks the voice loop on failure."""
        payload = {"state": state}
        payload.update(fields)
        threading.Thread(target=self._post, args=("/voice/state", payload, 3.0),
                         daemon=True).start()

    def post_level(self, state: str, level: float) -> None:
        """Push a live audio level (0..1) for the orb/HUD VU meter. Synchronous — call it from the
        dedicated VU thread (it self-throttles), NOT from the capture loop."""
        self._post("/voice/state", {"state": state, "level": round(float(level), 3)}, 2.0)

    def listen(self, on_event, stop_flag) -> None:
        """Run the SSE read loop, calling on_event(kind, data) per server event. Reconnects until
        stop_flag() returns True. Runs on its own thread (started by the daemon)."""
        url = self.base + "/events?token=" + (conf.auth_token() or "")
        while not stop_flag():
            try:
                with urllib.request.urlopen(url, timeout=40) as r:
                    for raw in r:
                        if stop_flag():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            msg = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        try:
                            on_event(msg.get("kind"), msg.get("data"))
                        except Exception as e:  # pragma: no cover
                            conf.log("voice", f"event handler error: {e}")
            except Exception:
                import time
                time.sleep(2.0)   # stream dropped — back off, then reconnect
