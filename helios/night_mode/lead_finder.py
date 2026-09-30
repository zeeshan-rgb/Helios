"""Night task: search worldwide for paid-work leads matching the user's services (helios/leads.py).
Leads are stored with a suggested price range and a pitch draft — Helios never contacts anyone."""

from __future__ import annotations

from .common import Result


def run(ctx: dict) -> Result:
    if not ctx.get("online"):
        return Result.skipped("offline — the lead finder needs the internet")
    from .. import leads
    return leads.night_task(ctx)
