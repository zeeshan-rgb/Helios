"""Extraction cache: one small JSON file per (URL, mode) under data/web_cache/.

Stores the EXTRACTED result (clean text + metadata + provenance), never raw HTML, never
screenshots or cookies. Entries expire (default 24 h) and the cache is pruned to a size cap.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .. import conf

MAX_ENTRIES = 2000


def cache_dir() -> Path:
    d = conf.DATA_DIR / "web_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize(url: str) -> str:
    """Scheme/host lower-cased, fragment dropped, trailing slash trimmed (path kept as is)."""
    s = urlsplit(url.strip())
    path = s.path.rstrip("/") or "/"
    return urlunsplit((s.scheme.lower(), s.netloc.lower(), path, s.query, ""))


def _path(url: str, mode: str) -> Path:
    h = hashlib.sha256(f"{normalize(url)}|{mode}".encode("utf-8")).hexdigest()[:32]
    return cache_dir() / f"{h}.json"


def get(url: str, mode: str, max_age: float) -> dict | None:
    p = _path(url, mode)
    try:
        entry = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if max_age <= 0 or time.time() - entry.get("saved_at", 0) > max_age:
        return None
    result = entry.get("result") or {}
    result["cached"] = True
    return result


def put(url: str, mode: str, result: dict) -> None:
    p = _path(url, mode)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"saved_at": time.time(), "url": normalize(url), "result": result},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    prune()


def prune(max_entries: int = MAX_ENTRIES) -> int:
    files = sorted(cache_dir().glob("*.json"), key=lambda f: f.stat().st_mtime)
    extra = files[:-max_entries] if len(files) > max_entries else []
    for f in extra:
        f.unlink(missing_ok=True)
    return len(extra)


def clear() -> int:
    n = 0
    for f in cache_dir().glob("*.json"):
        f.unlink(missing_ok=True)
        n += 1
    return n
