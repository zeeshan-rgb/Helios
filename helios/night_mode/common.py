"""Shared pieces for Night Mode tasks: the per-task result, the write guard, connectivity."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path

from .. import conf


@dataclass
class Result:
    """What one task did. The report keeps these apart (the blueprint: never blur them):
    completed = verified work done, observed = facts noticed, suggested = ideas for the user,
    approval = things waiting for the user's yes, failed = what went wrong."""
    status: str = "ok"                       # ok | failed | skipped
    summary: str = ""
    completed: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)
    suggested: list[str] = field(default_factory=list)
    approval: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    @classmethod
    def skipped(cls, why: str) -> "Result":
        return cls(status="skipped", summary=why)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("status", "summary", "completed", "observed",
                                             "suggested", "approval", "failed", "data")}


class CoreWriteRefused(PermissionError):
    pass


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_write(path: Path, text: str) -> Path:
    """The only way Night Mode writes files. Helios's own code folder is off limits (the
    blueprint: Night Mode MUST NOT modify Helios core source) — only its data/ and logs/."""
    path = Path(path)
    if _inside(path, conf.ROOT) and not (_inside(path, conf.DATA_DIR) or _inside(path, conf.LOGS_DIR)):
        raise CoreWriteRefused(f"Night Mode never writes inside Helios's code: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def online(timeout: float = 3.0) -> bool:
    """Best-effort internet check (TCP to public DNS resolvers; no data sent)."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 53), timeout=timeout):
                return True
        except OSError:
            continue
    return False
