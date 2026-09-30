"""Night tasks over the configured projects: sync state (what changed) and per-project summaries.
Read-only: git is only queried, files are only listed."""

from __future__ import annotations

from .. import projects
from .common import Result


def sync_projects(ctx: dict) -> Result:
    ps, errors = projects.load_all()
    act = [p for p in ps if p["active"]]
    if not act and not errors:
        return Result.skipped("no projects configured (`helios projects add <folder>`)")
    res = Result()
    for e in errors:
        res.failed.append(f"manifest {e}")
    for p in act:
        c = projects.changes(p, ctx["since"])
        ctx.setdefault("changes", {})[p["name"]] = c
        if c["error"] and not (c["commits"] or c["files"]):
            res.failed.append(f"{p['name']}: {c['error']}")
            continue
        if c["git"]:
            bits = []
            if c["commits"]:
                bits.append(f"{len(c['commits'])} commit(s)")
            if c["uncommitted"]:
                bits.append(f"{len(c['uncommitted'])} uncommitted file(s)")
            res.observed.append(f"{p['name']}: " + (", ".join(bits) if bits else "no changes"))
        else:
            n = c.get("files_total", 0)
            res.observed.append(f"{p['name']}: {n} file(s) modified" if n else f"{p['name']}: no changes")
        for prob in p["problems"]:
            res.suggested.append(f"{p['name']}: fix the manifest — {prob}")
    res.completed.append(f"scanned {len(act)} project(s) for changes since "
                         f"{ctx['since']:%Y-%m-%d %H:%M}")
    res.summary = f"{len(act)} project(s) scanned"
    return res


def project_summaries(ctx: dict) -> Result:
    act = projects.active()
    if not act:
        return Result.skipped("no projects configured")
    res = Result()
    checks = {r["project"]: r for r in ctx.get("checks", [])}
    for p in act:
        parts = []
        rep = checks.get(p["name"])
        if rep and rep["results"]:
            parts.append(", ".join(
                f"{r['name']} {'skipped' if r.get('skipped') else ('PASS' if r['ok'] else 'FAIL')}"
                for r in rep["results"]))
        elif p["health_checks"]:
            parts.append("checks not run tonight")
        else:
            parts.append("no health checks configured")
        c = ctx.get("changes", {}).get(p["name"])
        if c:
            n = len(c["commits"]) + len(c["uncommitted"]) if c["git"] else c.get("files_total", 0)
            parts.append(f"{n} change(s)")
        res.observed.append(f"{p['name']}: " + "; ".join(parts))
        for reason in projects.attention(p):
            # manifest problems were suggested by sync_projects; failing checks are in Failed
            if reason not in p["problems"] and " failing (exit " not in reason:
                res.suggested.append(f"{p['name']}: {reason}")
        res.data[p["name"]] = parts
    res.summary = f"{len(act)} project summary(ies)"
    return res
