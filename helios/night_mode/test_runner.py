"""Night task: project health — each active project's configured health checks (projects.run_checks:
the manifest's commands only, no shell, refused if they push/publish/deploy/delete), its dependency
check and the built-in probes it opts into (health.run_all). The full PROJECT HEALTH reports go into
the task's data for the night report."""

from __future__ import annotations

from .. import health, projects
from .common import Result


def run(ctx: dict) -> Result:
    act = projects.active()
    if not act:
        return Result.skipped("no projects configured")
    try:
        reports = health.run_all()
    except projects.Busy as e:
        return Result.skipped(str(e))
    ctx["health"] = reports
    res = Result()
    passed = failed = skipped = 0
    for p in act:
        st = projects.state(p)
        results = st.get("checks") or {}
        for c in p["health_checks"]:
            r = results.get(c["name"])
            if not r:
                continue
            label = f"{p['name']} {c['name']}"
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
        deps = st.get("deps")
        if deps and deps.get("status") not in ("off", "skip"):
            res.completed.append(f"{p['name']} dependency check: {deps.get('summary', '')}")
    verdicts = [r["verdict"] for r in reports]
    res.summary = (f"{passed} passed, {failed} failed" + (f", {skipped} not run" if skipped else "")
                   + f"; {verdicts.count('FAILING')} failing, {verdicts.count('ATTENTION')} need attention")
    res.data = {"passed": passed, "failed": failed, "skipped": skipped, "health": reports}
    return res
