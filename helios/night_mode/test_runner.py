"""Night task: run each active project's configured health checks (projects.run_checks — the
manifest's commands only, no shell, refused if they push/publish/deploy/delete)."""

from __future__ import annotations

from .. import projects
from .common import Result


def run(ctx: dict) -> Result:
    if not any(p["health_checks"] for p in projects.active()):
        return Result.skipped("no project has health checks configured")
    try:
        report = projects.run_checks()
    except projects.Busy as e:
        return Result.skipped(str(e))
    ctx["checks"] = report
    res = Result()
    passed = failed = skipped = 0
    for rep in report:
        for r in rep["results"]:
            label = f"{rep['project']} {r['name']}"
            if r.get("skipped"):
                skipped += 1
                res.failed.append(f"{label}: not run — {r['tail'][:160]}")
            elif r["ok"]:
                passed += 1
                res.completed.append(f"{label}: PASS ({r['seconds']}s)")
            else:
                failed += 1
                last = [l for l in r["tail"].strip().splitlines() if l.strip()][-1:]
                res.failed.append(f"{label}: FAIL (exit {r['code']}, {r['seconds']}s)"
                                  + (f" — {last[0][:160]}" if last else ""))
    res.summary = f"{passed} passed, {failed} failed" + (f", {skipped} not run" if skipped else "")
    res.data = {"passed": passed, "failed": failed, "skipped": skipped}
    return res
