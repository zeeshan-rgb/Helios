"""Orb launcher — picks the orb ENGINE and falls through on failure.

app._launch_orb spawns this file; which renderer actually runs is
`[orb] engine` in config/settings.toml:

  webview  (default) — three.js + React neural orb with UnrealBloom in a transparent
                       WebView2 window (helios/orb_webview.py + ui/orb.html + orb.js).
                       The pretty one. Trade-off: WebView2 transparency depends on GPU
                       compositing — if Windows drops it (battery saver, driver, RDP)
                       the orb renders as an OPAQUE BOX and can't detect that itself.
  layered            — pure-Win32 per-pixel-alpha layered window, PIL/numpy renderer
                       (helios/orb_layered.py). GPU-independent transparency, real glow.
                       Flip to this if the webview orb ever shows the opaque box.
  tk                 — the original tkinter color-key orb (helios/orb_overlay_tk.py).
                       No alpha, but bulletproof. Last resort.

Startup failure of an engine (missing dep, window creation error) falls through to the
next one, so there is ALWAYS an orb. All engines share the same contracts: SSE state
feed, click → POST /orb/toggle, drag → data/orb_pos.json, data/orb.pid = {pid, ctime},
fullscreen hide, watchdog exit when the app goes away.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helios import conf  # noqa: E402

_CHAINS = {
    "webview": ("webview", "layered", "tk"),
    "layered": ("layered", "tk"),
    "tk": ("tk",),
}


def main():
    engine = str(conf.SETTINGS.get("orb", {}).get("engine", "webview")).strip().lower()
    chain = _CHAINS.get(engine, _CHAINS["webview"])
    # If the webview engine's transparency self-check recently caught the opaque box, skip it
    # for a while (no white flash on every wake); a fresh attempt happens after ~6h — and the
    # flag clears itself the moment a check passes, so the three.js orb returns automatically.
    if chain[0] == "webview":
        try:
            import time as _t
            flag = conf.DATA_DIR / "orb_opaque.flag"
            if flag.exists() and _t.time() - flag.stat().st_mtime < 6 * 3600:
                conf.log("orb", "skipping webview engine (opaque-box flag < 6h old)")
                chain = chain[1:]
        except Exception:
            pass
    for name in chain:
        try:
            if name == "webview":
                from helios import orb_webview
                orb_webview.main()
            elif name == "layered":
                from helios import orb_layered
                orb_layered.main()
            else:
                from helios import orb_overlay_tk
                orb_overlay_tk.Orb().run()
            return
        except Exception as e:
            try:
                conf.log("orb", f"orb engine '{name}' failed ({e!r}) — falling back")
            except Exception:
                pass


if __name__ == "__main__":
    main()
