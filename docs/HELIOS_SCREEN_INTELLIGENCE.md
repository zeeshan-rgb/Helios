# Helios — screen intelligence

`mcp__helios__screen_context` and `mcp__helios__find_ui_element` answer "what's on my screen?",
"what does this error say?", "what's selected?" and "where's the Save button?" as **text**,
without sending a screenshot to the model.

## Pipeline

```
request
  ↓
UI Automation (helios/computer/uia_backend.py)
  → active app/window (pid, window_id), focused control, selection, dialogs,
    filtered controls, document + static text            0.35–0.8 s
  ↓  only when UIA sees little text (< 80 chars), the app is a remote desktop / VM viewer,
     or ocr="on" — and only for the window in FRONT
Windows OCR (helios/computer/ocr.py)
  → text lines with screen coordinates                   ~0.1 s small window, ~1 s full screen
  ↓
normalized ScreenContext → formatted text for the brain (marked "data, not instructions")
  ↓  only if the model must SEE images or layout
screenshot via cua-driver (get_desktop_state / get_window_state)
```

`find_ui_element`:
1. Looks for UIA controls first. A match comes back with the cua-driver route:
   `get_window_state(query)` → `click(element_index)`.
2. If no control matches, it falls back to OCR text and clearly labels the result a *visual
   match* with screen and window-local coordinates. Pixel clicks are the last rung.

## Multimodal orchestration — `perceive` and `locate` (`helios/orchestrator.py`)

One layer above the screen reader, so the brain doesn't have to stitch surfaces together itself:

* **`perceive(window="")`** — one fast text read of everything Helios can sense: the foreground app
  and window (pid + window_id), **whether it's the user's own browser and which page/URL it shows**
  (read from the browser's address bar through UI Automation — no extension, no remote debugging),
  the focused control, dialogs/errors, the hidden browser's open tabs (only if it's already running —
  `perceive` never launches it), and the live voice state (from the app's `/dashboard/voice`).
  A search being typed in the address bar is not reported as a URL; credential pages stay hidden.
* **`locate(query, surface="auto"|"desktop"|"browser", window, role)`** — finds a target by what it
  says, best first:
  1. UI Automation controls in the user's window (real elements → `get_window_state(query)` →
     `click(element_index)` / `set_value`),
  2. the hidden browser's DOM (`browser_click(ref)` / `browser_type(ref, …)`) — only when the screen
     has no semantic match and Helios is working a page there,
  3. OCR text on the screen image — labelled a visual match, window-local pixels, last resort.

  Each match carries the exact next call and how to verify it. Nothing in the orchestrator acts:
  the tools it points to keep their own gates.

The brain's playbook (`config/persona_helios.md`, "THE PIPELINE") routes context-dependent requests
through `perceive → locate → act → verify → answer`.

Verified live (2026-10-14): `perceive` read the page title and full URL from a visible Edge window;
`locate("Learn more", surface="browser")` returned `browser_click(ref="e1")` on a hidden-browser page,
and in `auto` it fell through to the DOM because the screen had no such control; `locate("Minimize")`
found the real UIA button in a background VS Code window. Limitation seen: Electron apps (VS Code) in
the background expose almost nothing to UI Automation — the honest "not found" + OCR hint is returned.

## Adaptive, nothing stored

* **No background capture:** every read happens on demand.
* **OCR pixels are never written to disk.** The last read is cached for 5 s, keyed by a pixel
  hash, so a repeated question doesn't re-run OCR. The cache holds only the latest read.
* **Small windows are upscaled 2×** for accuracy. Live test: "Search" read correctly at 2×,
  misread as "IN" at 1×. Very large captures use 1×.
* **Only the window in front is OCR'd.** OCR reads visible pixels, so a covered window would
  return the wrong text. For a background window, use cua-driver's `get_window_state`, which
  captures through Windows Graphics Capture.

## Privacy

* **Password fields** return `[hidden password field]`.
* **Never read** (UIA or OCR): password managers, Windows credential prompts, UAC, the lock
  screen, and browser password / wallet pages. They are listed by title only.
* **Redaction:** OCR and UIA text both pass through `SECRET_RE`.
* **Not exposed** on the public MCP server (neither are `perceive` / `locate`).
* **Screen summaries in memory** are opt-in, short (≤ 600 characters) and expire after 30 days —
  never raw screen text or images ([HELIOS_MEMORY.md](HELIOS_MEMORY.md)).

## Engines evaluated

| Engine | Status | Why |
|---|---|---|
| Windows.Media.Ocr (winrt 3.2.1) | **Used** | Built into Windows, offline, MIT bindings, en-GB/en-US installed |
| OmniParser V2 | Not used | Needs torch + a YOLO detector + a Florence-2 captioner; no CUDA GPU on this laptop |
| cua-driver `parse_visual_regions` | Later option | Needs the signed "perception" extension catalog at daemon start |
| RapidOCR / EasyOCR | Not used | OpenCV (~40 MB) or torch, for no gain over the built-in engine on screen text |

## Gotcha: native DLLs in the MCP server

The internal MCP server runs under `pythonw` with stdin as a pipe. On Windows, a DLL that
initialises its own C runtime (numpy, the WinRT bindings) blocks while loading if another thread
is in a blocking read on that pipe. So the first `import numpy` *inside a tool call* hung until
the next MCP message arrived (stack dump: main thread stuck in numpy's `create_module`).

* **Fix:** `helios_server.py` calls `ocr.preload()` before `mcp.run()`, so those DLLs load
  before the stdio loop starts. This also protects other numpy-using tools (e.g. `model_3d`).
* **OCR waiting:** the async OCR operation is waited on by polling its status (`ocr._wait`), not
  through asyncio.

Install: the `winrt-*` lines in `requirements.txt`. OCR languages come from Windows
(Settings → Time & language → Language & region → add a language with OCR).
