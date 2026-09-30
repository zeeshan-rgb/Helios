"""Night task: look over the conversations since the last night run (Daily/<date>.md) —
how much was said and how often the user had to correct Helios. Observation only; lessons are
extracted by memory_extractor."""

from __future__ import annotations

from .. import learning
from .common import Result


def run(ctx: dict) -> Result:
    res = Result()
    total = corrections = 0
    for day in ctx["days"]:
        ex = [e for e in learning.parse_daily(day)]
        n = len(ex)
        c = sum(1 for e in ex if learning.is_correction(e["user"]))
        total += n
        corrections += c
        res.data[day] = {"exchanges": n, "corrections": c}
        if n:
            res.observed.append(f"{day}: {n} exchange(s)" + (f", {c} looked like corrections" if c else ""))
    if not total:
        res.summary = "no conversations to analyze"
        res.observed.append("no conversations since the last night run")
    else:
        res.summary = f"{total} exchange(s), {corrections} correction(s)"
        if corrections >= 3:
            res.suggested.append(f"you corrected me {corrections} times — review what I learned "
                                 "(`helios learn review`)")
    return res
