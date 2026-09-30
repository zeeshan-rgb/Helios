"""Token usage log: every Antigravity call records its input/output tokens, so the cost of chat,
Night Mode, research and the lead finder is measured instead of guessed (`helios usage`).

One JSON line per call in data/usage.jsonl: {ts, label, in, out}. The label's first part (before
':') is the purpose — chat, research, leads, complete_json (lesson extraction / triage), agent
(background agents). Never raises; the log is trimmed to the last ~60 days.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import conf

_lock = threading.Lock()
_KEEP_DAYS = 60


def _file() -> Path:
    return conf.DATA_DIR / "usage.jsonl"


def record(label: str, tokens_in, tokens_out) -> None:
    try:
        i, o = int(tokens_in or 0), int(tokens_out or 0)
        if not (i or o):
            return
        line = json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                           "label": str(label or "other")[:60], "in": i, "out": o})
        with _lock:
            f = _file()
            f.parent.mkdir(parents=True, exist_ok=True)
            with f.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as e:  # pragma: no cover
        conf.log("usage", f"record failed: {e}")


def _rows(days: int) -> list[dict]:
    f = _file()
    if not f.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("ts", "") >= cutoff:
            out.append(r)
    return out


def purpose(label: str) -> str:
    return (label or "other").split(":", 1)[0]


def summary(days: int = 7) -> dict:
    """{day: {purpose: {"calls", "in", "out"}}} for the last `days` days."""
    out: dict = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "in": 0, "out": 0}))
    for r in _rows(days):
        d = out[r["ts"][:10]][purpose(r["label"])]
        d["calls"] += 1
        d["in"] += int(r.get("in", 0))
        d["out"] += int(r.get("out", 0))
    return {k: dict(v) for k, v in sorted(out.items())}


def trim() -> None:
    try:
        keep = _rows(_KEEP_DAYS)
        with _lock:
            _file().write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")
    except Exception:
        pass


def format_summary(days: int = 7) -> str:
    s = summary(days)
    if not s:
        return "No AI usage recorded yet."
    lines = []
    total_in = total_out = 0
    for day, by in s.items():
        di = sum(v["in"] for v in by.values())
        do = sum(v["out"] for v in by.values())
        total_in, total_out = total_in + di, total_out + do
        parts = ", ".join(f"{p} {v['calls']}x {(v['in'] + v['out']) / 1000:.0f}k"
                          for p, v in sorted(by.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"])))
        lines.append(f"{day}: {(di + do) / 1000:,.0f}k tokens ({di / 1000:,.0f}k in / {do / 1000:,.0f}k out) — {parts}")
    lines.append(f"\nLast {days} day(s): {(total_in + total_out) / 1e6:.2f}M tokens "
                 f"(avg {(total_in + total_out) / max(1, len(s)) / 1000:,.0f}k per active day)")
    return "\n".join(lines)
