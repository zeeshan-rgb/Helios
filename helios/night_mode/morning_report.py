"""The night report: written after every night run (and for a missed night), into the vault at
Night Reports/<night>.md so it's readable in Obsidian. Phase 10 turns it into the spoken morning
briefing. Sections stay separate — completed / observed / suggested / needs approval / failed /
skipped — and only verified results are ever listed as completed."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .. import memory_store
from .common import safe_write

SECTIONS = (("completed", "Completed"), ("observed", "Observed"), ("suggested", "Suggested"),
            ("approval", "Needs your approval"), ("failed", "Failed"))


def report_path(night: str) -> Path:
    return memory_store.vault() / "Night Reports" / f"{night}.md"


def _t(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return "?"


def collect(record: dict) -> dict:
    """Merge every task's lists into the report sections (approvals de-duplicated by lesson id)."""
    out = {k: [] for k, _ in SECTIONS}
    out["skipped"] = []
    approval_ids: set[str] = set()
    for name, t in (record.get("tasks") or {}).items():
        if t["status"] == "skipped":
            out["skipped"].append(f"{name}: {t['summary']}")
            continue
        if t["status"] == "failed":
            out["failed"].append(f"{name}: task failed — {t['summary']}")
        for key, _ in SECTIONS:
            for line in t.get(key) or []:
                if key == "approval":
                    m = re.search(r"approve ([\w-]+)", line)
                    if m and m.group(1) in approval_ids:
                        continue
                    if m:
                        approval_ids.add(m.group(1))
                out[key].append(line)
    return out


def render(record: dict) -> str:
    night = record.get("night", "?")
    kind = record.get("kind", "scheduled")
    lines = [f"# Night report — {night}", ""]
    if record.get("status") == "missed":
        lines += [f"Night Mode did **not** run: {record.get('reason', 'unknown reason')}.", ""]
        return "\n".join(lines)
    tasks = record.get("tasks") or {}
    counts = {s: sum(1 for t in tasks.values() if t["status"] == s) for s in ("ok", "failed", "skipped")}
    lines.append(f"{kind.title()} run · {record.get('status', '?')} · "
                 f"{_t(record.get('started'))}–{_t(record.get('finished'))} · "
                 f"{len(tasks)} task(s): {counts['ok']} ok, {counts['failed']} failed, "
                 f"{counts['skipped']} skipped · {'online' if record.get('online') else 'OFFLINE'}")
    lines.append("")
    reps = ((tasks.get("run_checks") or {}).get("data") or {}).get("health")
    if reps:
        from .. import health
        lines += ["## Project health", "", health.format_report(reps, markdown=True).rstrip(), ""]
    sec = collect(record)
    for key, title in SECTIONS + (("skipped", "Skipped"),):
        lines.append(f"## {title}")
        lines += [f"- {l}" for l in sec[key]] or ["- (none)"]
        lines.append("")
    lines.append("## Log")
    lines += [f"    {l}" for l in (record.get("log") or [])[-60:]]
    return "\n".join(lines) + "\n"


def write(record: dict) -> Path:
    return safe_write(report_path(record["night"]), render(record))
