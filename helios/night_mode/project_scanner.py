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
        res.data[p["name"]] = {"git": c["git"], "commits": len(c["commits"]),
                               "uncommitted": len(c["uncommitted"]),
                               "files": c.get("files_total", 0)}
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
    """One line per project: the health verdict + tonight's changes. The detail (rows + potential
    issues) is the night report's PROJECT HEALTH section, so nothing is repeated here."""
    from .. import health
    act = projects.active()
    if not act:
        return Result.skipped("no projects configured")
    res = Result()
    reps = {r["project"]: r for r in (ctx.get("health") or health.reports())}
    for p in act:
        rep = reps.get(p["name"]) or health.project_report(p)
        parts = [rep["verdict"]]
        if rep["issues"]:
            parts.append(f"{len(rep['issues'])} potential issue(s)")
        c = ctx.get("changes", {}).get(p["name"])
        if c:
            n = len(c["commits"]) + len(c["uncommitted"]) if c["git"] else c.get("files_total", 0)
            parts.append(f"{n} change(s)")
        res.observed.append(f"{p['name']}: " + "; ".join(parts))
        res.data[p["name"]] = {"verdict": rep["verdict"], "issues": rep["issues"]}
    res.summary = f"{len(act)} project summary(ies)"
    return res
