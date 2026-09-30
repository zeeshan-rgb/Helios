"""Night task: extract lessons from the recent conversations (learning.learn_from_daily — one
tool-less AI call per day, heuristic fallback when offline). New rules/skills stay pending."""

from __future__ import annotations

from .. import learning
from .common import Result


def run(ctx: dict) -> Result:
    res = Result()
    use_llm = bool(ctx.get("online"))
    if not use_llm:
        res.observed.append("offline — used the no-AI fallback for lessons")
    stored = pending = 0
    for day in ctx["days"]:
        out = learning.learn_from_daily(day, use_llm=use_llm)
        if out.get("skipped") or not out["exchanges"]:
            continue
        for l in out["lessons"]:
            if l["result"] == "saved":
                stored += 1
                if l["status"] == "active":
                    res.completed.append(f"learned ({l['category']}): {l['text']}")
                else:
                    pending += 1
                    res.approval.append(f"new {l['category'][:-1]}: {l['text']} "
                                        f"(`helios learn approve {l['id']}`)")
            elif l["result"] == "duplicate":
                res.observed.append(f"seen again ({l['category']}): {l['text']}")
        res.data[day] = {"method": out["method"], "lessons": len(out["lessons"])}
    res.summary = f"{stored} new lesson(s), {pending} waiting for approval" if stored else "nothing new learned"
    return res
