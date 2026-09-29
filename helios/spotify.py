"""Spotify Web API playback — reliably start a SPECIFIC playlist (e.g. KB) on wake.

The media Play/Pause key can only toggle whatever context is already playing, so it can't switch to a
particular playlist (and it pauses an already-playing session). This module uses the Web API instead:
it refreshes an OAuth token, finds the user's Spotify device (launching the desktop app if none is
active), and starts the given context_uri on it — regardless of what was playing.

Auth is OAuth 2.0 with PKCE (no client secret). Run `tools/spotify_auth.py` ONCE to authorize; it
writes {client_id, refresh_token} to data/spotify_token.json (gitignored). This module only reads/refreshes.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

from . import conf

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API = "https://api.spotify.com/v1"
_TOKEN_FILE = conf.DATA_DIR / "spotify_token.json"

_lock = threading.Lock()
_access = {"token": None, "exp": 0.0}   # cached bearer token + expiry (epoch seconds)


def _creds() -> dict:
    """{client_id, refresh_token} from data/spotify_token.json, or {} if not set up yet."""
    try:
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def configured() -> bool:
    c = _creds()
    return bool(c.get("client_id") and c.get("refresh_token"))


def _save_refresh(refresh_token: str) -> None:
    """Persist a rotated refresh token (Spotify may issue a new one on refresh)."""
    try:
        c = _creds()
        c["refresh_token"] = refresh_token
        _TOKEN_FILE.write_text(json.dumps(c), encoding="utf-8")
    except Exception as e:  # pragma: no cover
        conf.log("spotify", f"could not persist rotated refresh token: {e}")


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _access_token() -> str | None:
    """A valid bearer token, refreshing via the stored refresh token when needed."""
    with _lock:
        now = time.time()
        if _access["token"] and now < _access["exp"] - 30:
            return _access["token"]
        c = _creds()
        if not (c.get("client_id") and c.get("refresh_token")):
            return None
        try:
            tok = _post_form(_TOKEN_URL, {
                "grant_type": "refresh_token",
                "refresh_token": c["refresh_token"],
                "client_id": c["client_id"],
            })
        except Exception as e:
            conf.log("spotify", f"token refresh failed: {e}")
            return None
        _access["token"] = tok.get("access_token")
        _access["exp"] = now + int(tok.get("expires_in", 3600))
        new_rt = tok.get("refresh_token")
        if new_rt and new_rt != c.get("refresh_token"):
            _save_refresh(new_rt)
        return _access["token"]


def _api(method: str, path: str, body: dict | None = None) -> dict | None:
    token = _access_token()
    if not token:
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def _devices() -> list[dict]:
    try:
        d = _api("GET", "/me/player/devices") or {}
        return d.get("devices", []) or []
    except Exception as e:
        conf.log("spotify", f"devices lookup failed: {e}")
        return []


def play_context(context_uri: str, launch_wait: float = 9.0) -> bool:
    """Start `context_uri` (a playlist/album/artist URI) on the user's Spotify, switching context
    even if something else is playing. Launches the desktop app and waits for it to register as a
    device if none is active. Returns True if the play call was issued. Safe to call off-thread."""
    if not configured() or not context_uri:
        return False
    devices = _devices()
    if not devices:
        # Nothing to play on — launch the desktop app, then poll until it registers as a device.
        try:
            os.startfile("spotify:")
        except Exception:
            pass
        deadline = time.time() + launch_wait
        while time.time() < deadline and not devices:
            time.sleep(1.0)
            devices = _devices()
        if not devices:
            conf.log("spotify", "no Spotify device available; cannot play")
            return False
    dev = next((d for d in devices if d.get("is_active")), devices[0])
    did = dev.get("id")
    try:
        # device_id both targets AND activates the device, so this works even if it was idle.
        _api("PUT", f"/me/player/play?device_id={urllib.parse.quote(str(did))}",
             {"context_uri": context_uri})
        conf.log("spotify", f"playing {context_uri} on {dev.get('name')!r}")
        return True
    except Exception as e:
        conf.log("spotify", f"play failed: {e}")
        return False
