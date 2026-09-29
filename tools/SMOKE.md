# Helios — manual smoke checklist

The automated suite (`tools/run_tests.ps1`) only covers Helios's **pure decision functions**
(permissions / router / voice intent). Everything below needs the **live system** — the running
app, a real mic + speakers, cua-driver, and `claude` — so it **cannot** be automated or run in CI.
Walk it by hand after meaningful changes to the voice stack, the persona, or the brain.

Reliable restart first (see `handoff.md` → "How to run"): Stop the `Helios` task → kill stray
`*\helios\*` python procs → confirm `http://127.0.0.1:8769/health` is down → Start → confirm a FRESH
`Helios started` is the last line of `logs/app.log`. Tail `logs/voice.log` while testing voice.

## 1. Voice intents (say "hey Helios" first, then the line)
Expect: wake chime, a spoken reply, the dashboard HUD shows the transcript + state.

- [ ] "what time is it"  → speaks the time
- [ ] "what day is it"  → speaks the date
- [ ] "set a timer for two minutes"  → confirms; toast fires when it elapses
- [ ] "remind me to stretch in ten minutes"  → confirms the reminder
- [ ] "what's playing"  → reports current media (or "nothing playing")
- [ ] "play"  / "pause"  / "next track"  → media control responds
- [ ] "turn the volume up" / "set volume to 30 percent"  → volume changes
- [ ] "open spotify"  → app launches
- [ ] "take a screenshot"  → captures
- [ ] "what's the weather in Miami"  → web lookup, spoken summary
- [ ] "tell me a joke"  → light chat reply
- [ ] "why is my Nitro stuttering"  → calls system_health, leads with the likely cause
- [ ] "thanks" (as a hot-mic follow-up, no wake word)  → treated as ambient, NOT sent
- [ ] long answer, then pause ~1s mid-question  → Helios waits (silence_ms = 1500), doesn't cut in

## 2. Barge-in (requires `[voice].barge_in = true`, then restart)
- [ ] Ask for something long ("read me a short story"). Mid-reply, say **"hey Helios"** → speech
      cuts within a beat, ack chime fires, HUD → listening, your new command is captured + answered.
- [ ] Barge during THINKING (before any audio): ask something slow, say "hey Helios" before it
      speaks → the in-flight turn aborts, new command captured.
- [ ] Check `logs/voice.log` shows `barge-in (score …)`; check no orphaned `claude -p`:
      `Get-CimInstance Win32_Process -Filter "Name='claude.exe'"` → none left over from the killed turn.
- [ ] Set `barge_in = false`, restart → speaking is uninterruptible again (mic muted while talking),
      everything else identical.

## 3. Computer-use + the recovery policy (persona)
- [ ] "open Settings and go to Sound"  → Settings opens to the Sound page (predict→verify loop).
- [ ] "set the output volume to 50 percent"  → confirms by a FINAL capture of the actual slider/value,
      not just that a click landed.
- [ ] Fail-then-recover: give a task whose obvious first approach is blocked (e.g. an app that needs
      foreground, or a dialog in the way). Confirm Helios:
  - names/uses ONE recovery move (retry-in-foreground / replan / substitute — e.g. falls back to a
    `launch_app("ms-settings:…")` deep link or a hotkey),
  - bounds itself to ~3 attempts on the sub-goal,
  - ESCALATES with a clear "here's where I'm stuck, sir" instead of looping the same failing action.

## 4. Permission gating (don't auto-approve the dangerous stuff)
- [ ] Ask Helios to read `.env` or delete a pre-existing file  → an Approve/Deny prompt appears;
      Deny → the action does not happen, file intact.
- [ ] Ask it to send an email  → prompts before sending (outbound is always ask-first).
- [ ] Panic key (Ctrl+Alt+Backspace) mid-action  → stops immediately, no orphaned procs.

## 5. YOLO mode (top-bar amber ⚡ toggle — all permissions for the current chat)
- [ ] Click the YOLO button → it turns amber + a "YOLO" badge appears by the brand. Ask for a
      normally-gated action (write a `.ps1`, run `Remove-Item` on a throwaway file, delete a
      pre-existing file) → it proceeds with NO Approve/Deny prompt.
- [ ] While YOLO is on, the hard-rails still bite: ask it to fetch `http://127.0.0.1:8769/…` (SSRF)
      or write under `C:\Users\Tim\.claude\` → still refused.
- [ ] Click "New conversation" (the + button) → YOLO turns itself off (button dims, badge gone).
      Same after a panic. It never survives a restart.

## 6. Resizable window (drag any edge or corner)
- [ ] Drag each EDGE (top/bottom/left/right) and each CORNER → the window resizes and the OPPOSITE
      edge/corner stays put (left-edge drag grows leftward, top-edge upward, etc.). Won't go below the
      min size; chat + composer reflow. The SE corner shows a subtle grip glyph.
- [ ] Sanity at your display scaling (125/150%): the window tracks the cursor 1:1 (no drift).
- [ ] Resize, then fully restart Helios → the window comes back at the size you left it
      (persisted to `data/dash_geom.json`).
- [ ] (Known gap) OS Aero-Snap / drag-to-top-to-maximize is NOT supported (native-only) — that's expected.

## 7. Mid-turn steering (type while Helios is working — it folds it in)
- [ ] Ask a multi-step task ("plan a weekend in Italy"). While it's still answering, type "also
      route through Sicily" and hit Enter → a "✚ added — folding that in…" chip appears, Helios says
      "Noted — folding that in, sir," and the answer comes back WITH Sicily included.
- [ ] Ask something long; mid-reply type "play [song] on Spotify" → it does the side task and
      continues. (Note: this is preempt-and-merge, not literal parallelism — the first attempt's
      partial text may be left truncated above the new answer; that's expected.)
- [ ] The panic button still hard-stops a turn; no orphaned `claude -p` procs after a steer
      (`Get-CimInstance Win32_Process -Filter "Name='claude.exe'"`).

## 8. Lite brain (engine = "lite", a non-Claude provider) — v0.7.0
Set `[brain].engine = "lite"` + a provider/model with a key in `secrets.toml` (or run `python setup.py`
and pick OpenRouter/OpenAI/Gemini/Ollama), then restart. The dashboard/orb/voice are unchanged.
- [ ] Plain chat streams token-by-token, ends cleanly, and is logged to history (reload the chat → it's there).
- [ ] "what's my system health" → calls the `system_health` tool (a "using system_health…" chip), reports specifics.
- [ ] "make a file test.txt on my desktop saying hello" → an Approve/Deny prompt appears (write is gated);
      Approve → file created; Deny → not created, and Helios adapts.
- [ ] A read-only shell ask ("list my running processes") runs with NO prompt; a mutating one
      ("delete a throwaway file") prompts first.
- [ ] "remember that I prefer tea over coffee" → saved (memory_append); ask later in a NEW chat "what do
      I prefer to drink" → recalls it (proves memory write-back + digest work without Claude installed).
- [ ] Panic mid-reply → the stream stops immediately; no hang.
- [ ] (No web-search key set) ask for current web info → it asks you for a URL, then `web_fetch` reads it.

## 9. Setup wizard (`python setup.py`) — v0.7.0
Best run on a throwaway clone/VM (it writes settings/secrets and can register the scheduled task).
- [ ] Fresh clone → `python setup.py` builds `.venv`, installs deps, re-launches into the styled wizard.
- [ ] Brain section: each provider writes the right `[brain]`/secrets; Headless Claude offers the
      account sign-in; lite providers ask for key + model (Ollama lists installed models).
- [ ] Voice "set up now?" → choosing Kokoro offers the model download; cloud engines collect/reuse keys.
- [ ] Onboarding: blank → asks-for-everything (verify a normally-auto action prompts); default/full/manual
      write `Profile.md` and the chosen autonomy allow-list.
- [ ] Obsidian is required (can't finish without a vault); Telegram/Spotify/Composio each skippable.
- [ ] `config/mcp.json` + `claude_settings.json` are generated with THIS machine's paths (no `C:\Users\Tim`).
- [ ] Re-running setup keeps things working (idempotent); Helios still launches + reaches `/health`.

## 8. v0.9.0 dashboard + neural orb (visual — Tim's eyes required)
- [ ] Summon (Ctrl+Alt+J) → the new quiet-luxe-2.0 dashboard: glass title bar, capsule composer,
      canvas synapse dot next to "Helios" (barely breathing when idle).
- [ ] Empty state shows the LIVING neural emblem (small rotating net, sparks) — not a static circle.
- [ ] Send a message → composer ripples once; title-bar hairline drifts cyan→teal while working;
      synapse dot + emblem quicken. All motion stops when the reply lands.
- [ ] Ask something tool-heavy ("what's my battery and disk space") → a live tool timeline appears
      under the streaming reply ("Reading the screen…" etc.), then collapses to "N steps · Xs";
      clicking the chip re-expands it.
- [ ] History / Missions / Workflows / Settings all open in the right-hand DRAWER (chat stays
      visible, dimmed). Esc / scrim click / ✕ closes; ← appears inside workflow detail/editor and
      mission detail. Workflow list→detail→editor→save round-trips; mission detail live-updates.
- [ ] Σ token counter in the title bar → click → stats popover (totals, est. cost, turns, per-turn log).
- [ ] Permission prompt still a centered dialog; Approve/Deny both work.
- [ ] ORB: fully transparent outside the disc (windows visible + CLICKABLE through the glow corners),
      neural net spins with soft glow, sparks travel the links. Click → dashboard toggles; drag →
      moves + position survives a restart; state colours: thinking≈bright cyan, acting≈warm,
      listening≈green, speaking≈cyan, error≈red flash. Fullscreen video → orb hides; back → returns.
- [ ] Reduced-motion (Settings → Accessibility → animation off): canvases render still frames,
      hairline/holo/ripple animations skipped, UI fully usable.
