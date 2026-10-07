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
* **Not exposed** on the public MCP server.

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
