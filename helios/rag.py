"""Local retrieval primitives for RAG: embed text via Ollama, chunk docs, cosine score.

All embedding is done locally against an Ollama server (nomic-embed-text) — no cloud
call, so retrieval works offline and costs nothing. This module is intentionally just
the building blocks (embed / chunk / similarity / file-read); the index store and query
orchestration live in the caller.
"""

from __future__ import annotations

import json
import math
import re
import urllib.request
from pathlib import Path

# Local Ollama embeddings endpoint (default loopback port) — must be running.
OLLAMA_EMBED = "http://127.0.0.1:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"

TEXT_EXT = {".txt", ".md", ".markdown", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv",
            ".html", ".css", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sh", ".ps1",
            ".toml", ".yaml", ".yml", ".rst", ".tex", ".ini", ".cfg", ".log", ".pdf"}


def embed_one(text: str) -> list[float]:
    """Return the embedding vector for `text` from the local Ollama server.

    Input is truncated to 8000 chars (model context guard). Raises on connection
    failure or non-200 — callers should handle Ollama being down. Blocks up to 60s.
    """
    body = json.dumps({"model": EMBED_MODEL, "prompt": text[:8000]}).encode()
    req = urllib.request.Request(OLLAMA_EMBED, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["embedding"]


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Split text into ~`size`-char chunks with `overlap` chars carried between them.

    Overlap preserves context that would otherwise be cut at a chunk boundary. Blank
    runs of 3+ newlines are collapsed first; empty chunks are dropped. The step is
    max(1, size-overlap) so it always advances even if overlap >= size.
    """
    text = re.sub(r"\n{3,}", "\n\n", text or "")
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i + size])
        i += max(1, size - overlap)   # guard against non-advancing step
    return [c.strip() for c in chunks if c.strip()]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 for empty/mismatched/degenerate."""
    # Different lengths => embeddings from different models/versions; not comparable.
    # (zip would silently truncate and return a bogus score.)
    if not a or not b or len(a) != len(b):
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def read_text(p: Path) -> str:
    """Extract plain text from a file for indexing; "" on any failure.

    PDFs are parsed via pypdf (imported lazily so it's only required when a PDF is
    actually read); everything else is read as UTF-8 with undecodable bytes replaced.
    See TEXT_EXT for the extensions the caller treats as indexable.
    """
    try:
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
