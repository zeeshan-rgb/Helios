"""3D-model registry for the dashboard viewer: opaque ids -> .glb paths.

Ported from Helios-main webui/models.py (2026-07-08 consolidation). The /model/<id>
route serves ONLY paths registered here (no client-supplied paths, so no traversal
surface). Ids are deterministic path hashes, so a file keeps its URL across restarts
and rescans - and responses stay no-store because a re-exported model reuses its id.
GLB only: it is self-contained; a multi-file .gltf (+ .bin/textures) cannot resolve
through a single URL - helios/model_3d.py converts to GLB before showing.
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

MODELS_ROOT = Path.home() / "Downloads" / "Helios Work"   # scan root (tests monkeypatch)
_MAX_BYTES = 200 * 1024 * 1024
_SCAN_CAP = 40

_REG: dict[str, Path] = {}
_LOCK = threading.Lock()


def _mid(p: Path) -> str:
    return hashlib.sha1(str(p).lower().encode("utf-8")).hexdigest()[:16]


def register(path) -> dict | None:
    """Validate + register one .glb; return its viewer entry or None."""
    try:
        p = Path(str(path)).expanduser().resolve()
        if not p.is_file() or p.suffix.lower() != ".glb":
            return None
        st = p.stat()
        if st.st_size > _MAX_BYTES:
            return None
    except Exception:
        return None
    mid = _mid(p)
    with _LOCK:
        _REG[mid] = p
    name = p.parent.name if p.stem.lower() == "model" else p.stem
    return {"id": mid, "name": name, "url": f"/model/{mid}",
            "size": st.st_size, "mtime": int(st.st_mtime)}


def get_path(model_id: str) -> Path | None:
    with _LOCK:
        p = _REG.get(str(model_id))
    return p if p is not None and p.is_file() else None


def scan() -> list[dict]:
    """Register every .glb under MODELS_ROOT (2 levels deep), newest first."""
    items = []
    try:
        candidates = list(MODELS_ROOT.glob("*.glb")) + list(MODELS_ROOT.glob("*/*.glb"))
    except Exception:
        candidates = []
    for p in candidates:
        entry = register(p)
        if entry:
            items.append(entry)
    items.sort(key=lambda e: e["mtime"], reverse=True)
    return items[:_SCAN_CAP]
