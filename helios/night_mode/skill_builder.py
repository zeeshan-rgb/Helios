"""Night task: review the rules and skills Helios has learned. Night Mode may create and update
them, but only as PENDING memory items (learning.py) — this task lists what is waiting for the
user's approval, repeated lessons first. It never activates anything and never touches code."""

from __future__ import annotations

from .. import memory_store
from .common import Result


def run(ctx: dict) -> Result:
    waiting = [i for i in memory_store.pending() if i["category"] in ("rules", "skills")]
    res = Result()
    if not waiting:
        res.summary = "no rules or skills waiting"
        return res
    waiting.sort(key=lambda i: (-int(i.get("seen", 1)), i.get("updated", "")))
    res.data["pending"] = [{"id": i["id"], "category": i["category"], "text": i["text"],
                            "seen": int(i.get("seen", 1))} for i in waiting]
    for it in waiting:
        seen = int(it.get("seen", 1))
        res.approval.append(f"{it['category'][:-1]}: {it['text']}"
                            + (f" (said {seen}x)" if seen > 1 else "")
                            + f" (`helios learn approve {it['id']}`)")
    repeated = sum(1 for i in waiting if int(i.get("seen", 1)) > 1)
    if repeated:
        res.suggested.append(f"{repeated} lesson(s) came up more than once — worth approving")
    res.summary = f"{len(waiting)} rule/skill lesson(s) waiting for approval"
    return res
