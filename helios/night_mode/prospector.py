"""Night task: business prospecting in the configured areas (helios/prospecting). Research and
drafting only — outreach waits for the user's approval; nothing is ever sent from Night Mode."""

from __future__ import annotations

from .common import Result


def run(ctx: dict) -> Result:
    from ..prospecting import engine
    return engine.night_task(ctx)
