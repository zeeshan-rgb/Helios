# JARVIS v0.9.0 — Dashboard + Neural Orb Revamp (design)

**Approved by Tim 2026-07-02** ("mix of living instrument and quiet-luxe 2.0"; layered-window orb;
slide-over drawer; all four alive elements; "go ahead and implement everything").

## Direction

**"Living instrument, quiet luxe craft."** Calm refined surfaces at rest; the *intelligence* is what
animates, never the chrome. Nothing moves when idle.

## Invariants (must not change)

- Server routes, SSE protocol (`model/token/tool/status/usage/done/error/permission/mission/
  workflow/voice/memory/yolo`), permission flow, YOLO, steering, `.pywebview-drag-region`,
  `#resizeedges` all-edge resize mechanics, quoted `"__JARVIS_TOKEN__"` injection, `?v=`
  cache-busting, `Cache-Control: no-store`, vanilla JS (no build step).
- `server.py` / `app.py` / `brain.py` untouched. `app._launch_orb` keeps spawning
  `jarvis/orb_overlay.py`; PID contract (`data/orb.pid` = `{pid, ctime}`) unchanged.

## Dashboard

- **Tokens 2.0** (`style.css` rebuilt): deeper surface ladder, glass elevation (backdrop blur +
  1px inset top highlight), tightened type scale, cyan signature kept + a cyan→teal gradient
  reserved for "working" moments, mono = Cascadia Code (ships with Win11). One ease, durations
  140/220/360ms, `prefers-reduced-motion` kills all motion.
- **Drawer instead of modals**: History / Missions / Workflows / Settings live in ONE right-hand
  slide-over drawer (~430px, glass, 220ms), chat dims under a scrim but stays visible. Views keep
  their existing inner element IDs so the port is mechanical. Permission prompts STAY a centered
  focused dialog (urgent + blocking), restyled with the summary in mono.
- **Alive elements** (all four):
  1. *Live tool timeline* — `tool` SSE events render as friendly rows in an activity strip attached
     to the streaming reply ("▸ Reading a file…"); collapses to a "N steps · Xs" chip on `done`,
     click to re-expand. Replaces the per-tool chat chips.
  2. *Neural empty-state emblem* — canvas neural net (same visual language as the orb), breathes on
     idle, reacts to brain/voice states.
  3. *State choreography* — header status dot becomes a micro synapse-cluster canvas; while a turn
     runs an accent glow drifts along the title-bar hairline; composer pulses once on send.
  4. *Session stats popover* — the token counter becomes a click popover: session tokens, est.
     cost, turn count, per-turn mini-log (model · in→out · duration).

## Orb (rewrite: `jarvis/orb_overlay.py`)

Pure-ctypes **Win32 per-pixel-alpha layered window** (`WS_EX_LAYERED|TOPMOST|TOOLWINDOW|NOACTIVATE`
+ `UpdateLayeredWindow`, premultiplied BGRA from a PIL/numpy renderer; both already runtime deps).
Rationale: works regardless of GPU compositing (the WebView2 transparency failure mode), true
soft-alpha glow (tkinter can't), and clicks pass through fully-transparent pixels automatically.

- **Look**: ~72-node fibonacci-sphere neural net, slow rotation + axis wobble, depth-fogged far
  side, additive node glow sprites, anti-aliased synapses (2× supersample + bloom layer), signal
  pulses traveling along edges (rare when idle, cascades when thinking, VU-synced when speaking),
  soft state-colored halo breathing around the disc.
- **Behavior kept 1:1** from the tkinter orb: SSE state feed + the 7 states/colors, VU smoothing,
  click→`/orb/toggle`, drag→`data/orb_pos.json`, periodic topmost re-assert, fullscreen-app hide,
  `{pid, ctime}` PID file, health watchdog self-exit, DPI awareness, idle frame-rate halving.
- **Fallback**: old tkinter orb preserved as `jarvis/orb_overlay_tk.py`; if layered-window setup
  fails, `main()` falls back to it.

## Testing

New pytest module for the orb's pure functions (projection, edge gen, pulse field, premultiply);
existing 166 tests stay green; `node --check app.js`; isolated-server render check; SMOKE.md
additions; live restart + `mss` screenshot of the orb over the desktop to verify real transparency.

**Version:** 0.9.0.
