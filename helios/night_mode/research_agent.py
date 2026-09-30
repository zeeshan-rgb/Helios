"""Night task: research the configured topics. The research system itself is blueprint phase 8;
until it exists this task reports itself as skipped (never silently absent)."""

from __future__ import annotations

from .common import Result


def run(ctx: dict) -> Result:
    if not ctx.get("online"):
        return Result.skipped("offline — research needs the internet")
    try:
        from .. import research          # arrives in phase 8
    except ImportError:
        return Result.skipped("the research system isn't built yet (blueprint phase 8)")
    return research.night_task(ctx)
