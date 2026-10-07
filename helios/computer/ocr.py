"""OCR rung of the ladder: Windows' built-in OCR engine (Windows.Media.Ocr via winrt).

For surfaces UI Automation can't see into — canvases, images, remote desktops, some web views.
Offline, no models to download, ~0.1 s for a window on this laptop. Used on demand only (never
a continuous capture); the pixels live in memory for the one call and are never written to disk.
Reads screen pixels, so it only makes sense for what's actually visible: the foreground window
or a screen region.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import threading
import time

from . import policy_bridge
from .interface import Element

_engine = None
_engine_lock = threading.Lock()
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="helios-ocr")
_cache: dict[tuple, tuple[float, list[Element]]] = {}
_CACHE_SEC = 5.0
_MAX_SIDE = 9500              # Windows OCR's limit is 10000 px per side


def _preload() -> None:
    """Import (= load the DLLs of) everything OCR needs. The MCP server calls this at startup,
    before its stdio loop runs: on Windows, loading a DLL with its own C runtime blocks while
    another thread is in a blocking read on the stdin pipe (stack dump: stuck in `import numpy`),
    so a first import inside a tool call stalls until the next MCP message arrives."""
    import mss  # noqa: F401
    import numpy  # noqa: F401
    from winrt.windows.foundation import AsyncStatus  # noqa: F401
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap  # noqa: F401
    from winrt.windows.media.ocr import OcrEngine  # noqa: F401
    from winrt.windows.storage.streams import DataWriter  # noqa: F401


preload = _preload


def available() -> tuple[bool, str]:
    try:
        _preload()
    except Exception as e:
        return False, f"Windows OCR bindings not installed ({e.__class__.__name__})"
    try:
        # created on the OCR thread: a WinRT object made on a COM STA thread (comtypes/UIA
        # initialise one) and used from another thread deadlocks while the creator waits
        if _pool.submit(_get_engine).result(timeout=15) is None:
            return False, "no OCR language installed (Settings > Time & language > Language)"
    except Exception as e:
        return False, f"Windows OCR unavailable ({e.__class__.__name__}: {e})"
    return True, ""


def _get_engine():
    """Only ever called on the single OCR worker thread."""
    global _engine
    with _engine_lock:
        if _engine is None:
            from winrt.windows.media.ocr import OcrEngine
            _engine = OcrEngine.try_create_from_user_profile_languages()
        return _engine


def _grab(rect):
    """Screen pixels of rect (x, y, w, h) -> (bgra bytes, w, h). Physical pixels."""
    import mss
    x, y, w, h = rect
    factory = getattr(mss, "MSS", None) or mss.mss
    with factory() as s:
        shot = s.grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
        return bytes(shot.bgra), shot.width, shot.height


def _wait(op, timeout: float = 15.0):
    """Block on a WinRT IAsyncOperation by polling its status — no event loop or completion
    callback involved, so it behaves the same in any thread or process."""
    from winrt.windows.foundation import AsyncStatus
    deadline = time.monotonic() + timeout
    while op.status == AsyncStatus.STARTED:
        if time.monotonic() > deadline:
            try:
                op.cancel()
            except Exception:
                pass
            raise TimeoutError("Windows OCR didn't finish")
        time.sleep(0.005)
    if op.status != AsyncStatus.COMPLETED:
        raise RuntimeError(f"Windows OCR failed ({op.status})")
    return op.get_results()


def _recognize(bgra: bytes, w: int, h: int, scale: int):
    """Run the OCR engine (WinRT async op, polled); returns raw lines."""
    import numpy as np
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    img = np.frombuffer(bgra, dtype=np.uint8).reshape(h, w, 4)
    if scale > 1:
        img = np.ascontiguousarray(img.repeat(scale, axis=0).repeat(scale, axis=1))
    dw = DataWriter()
    dw.write_bytes(img.tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                 img.shape[1], img.shape[0])
    res = _wait(_get_engine().recognize_async(bmp))
    out = []
    for line in res.lines:
        rects = [wd.bounding_rect for wd in line.words]
        if not rects:
            continue
        x0 = min(r.x for r in rects); y0 = min(r.y for r in rects)
        x1 = max(r.x + r.width for r in rects); y1 = max(r.y + r.height for r in rects)
        out.append((line.text, (x0 / scale, y0 / scale, (x1 - x0) / scale, (y1 - y0) / scale)))
    return out


def read_region(rect, *, grab=None, recognize=None) -> list[Element]:
    """OCR lines inside a screen rect, in screen coordinates (cached a few seconds)."""
    x, y, w, h = (int(v) for v in rect)
    if w <= 0 or h <= 0:
        return []
    if recognize is None:
        _preload()
    bgra, gw, gh = (grab or _grab)((x, y, w, h))
    key = (x, y, gw, gh, hashlib.blake2b(bgra[::97], digest_size=12).digest())
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SEC:
        return hit[1]
    # small text reads much better upscaled (live test: "Search" at 2x vs "IN" at 1x)
    scale = 2 if max(gw, gh) * 2 <= _MAX_SIDE and gw * gh <= 2_400_000 else 1
    rec = recognize or (lambda b, a, c, s: _pool.submit(_recognize, b, a, c, s).result(timeout=20))
    lines = rec(bgra, gw, gh, scale)
    out = [Element(role="text", name=policy_bridge.clean(t, 200),
                   rect=(int(x + lx), int(y + ly), int(lw), int(lh)), source="ocr")
           for t, (lx, ly, lw, lh) in lines if t.strip()]
    _cache.clear()                          # keep only the latest read (no image or text hoard)
    _cache[key] = (time.monotonic(), out)
    return out


def text_of(lines: list[Element], limit: int = 3000) -> str:
    """OCR lines -> reading-order text (top-to-bottom, left-to-right), redacted and bounded."""
    rows = sorted(lines, key=lambda e: (round(e.rect[1] / 12), e.rect[0]))
    return policy_bridge.clean_block("\n".join(e.name for e in rows), limit)
