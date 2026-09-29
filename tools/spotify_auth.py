"""One-time Spotify authorization for Helios (OAuth 2.0 with PKCE — no client secret needed).

WHY: lets Helios reliably start a specific playlist (your KB playlist) on the double-clap wake,
regardless of what's currently playing. The media key can only toggle the current context.

SETUP (do this once):
  1. Go to https://developer.spotify.com/dashboard  -> Create app (any name/description).
     - Redirect URI: add EXACTLY   http://127.0.0.1:8888/callback
     - Which API/SDKs: tick "Web API". Save.
     - Copy the app's Client ID.
  2. Run this script with Helios's venv python, e.g. (in the Helios prompt, prefix with !):
        ! C:\\Users\\Tim\\helios\\.venv\\Scripts\\python.exe C:\\Users\\Tim\\helios\\tools\\spotify_auth.py
  3. Paste the Client ID when asked, then approve in the browser that opens.
  4. It writes data/spotify_token.json (gitignored). Restart Helios — double-clap now plays KB.

Scopes: user-read-playback-state user-modify-playback-state (read devices + control playback).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REDIRECT = "http://127.0.0.1:8888/callback"
PORT = 8888
SCOPE = "user-read-playback-state user-modify-playback-state"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / "data" / "spotify_token.json"

_result = {"code": None, "error": None}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        if q.path != "/callback":
            self.send_response(404); self.end_headers(); return
        params = urllib.parse.parse_qs(q.query)
        _result["code"] = (params.get("code") or [None])[0]
        _result["error"] = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Authorized — you can close this tab and return to Helios."
               if _result["code"] else f"Authorization failed: {_result['error']}")
        self.wfile.write(f"<html><body style='font:16px sans-serif;padding:40px'>{msg}</body></html>"
                         .encode())

    def log_message(self, *a):  # silence the default stderr logging
        pass


def _pkce():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def main():
    existing = {}
    try:
        existing = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    client_id = (existing.get("client_id") or "").strip()
    if not client_id:
        client_id = input("Paste your Spotify app Client ID: ").strip()
    if not client_id:
        print("No Client ID — aborting.")
        return

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    auth = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPE,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    })

    httpd = http.server.HTTPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=httpd.handle_request, daemon=True).start()  # serve exactly one request
    print("\nOpening your browser to authorize Spotify...")
    print("If it doesn't open, paste this URL manually:\n" + auth + "\n")
    webbrowser.open(auth)

    # Wait for the callback (handle_request fills _result then the thread exits).
    import time
    for _ in range(300):  # ~5 min
        if _result["code"] or _result["error"]:
            break
        time.sleep(1.0)
    try:
        httpd.server_close()
    except Exception:
        pass

    if _result["error"] or not _result["code"]:
        print(f"Authorization failed: {_result['error'] or 'no code received'}")
        return

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": _result["code"],
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read().decode())
    except Exception as e:
        print(f"Token exchange failed: {e}")
        return

    refresh = tok.get("refresh_token")
    if not refresh:
        print(f"No refresh token in response: {tok}")
        return

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"client_id": client_id, "refresh_token": refresh}),
                          encoding="utf-8")
    print(f"\n[OK] Saved {TOKEN_FILE}. Restart Helios - the double-clap will now play your KB playlist.")


if __name__ == "__main__":
    main()
