"""Seed the consolidation workflows (2026-07-08, Helios-main parity). Idempotent: skips
any workflow whose name already exists. Run with the venv python from the repo root:

    .venv\\Scripts\\python.exe tools\\seed_workflows.py

Seeds:
  - Morning brief   (daily 07:30, ENABLED)  weather + headlines -> the user's Telegram
  - Weekly cleanup  (weekly sun 12:00, ENABLED)  propose-only report -> Telegram
  - Game updates    (daily 03:00, DISABLED)  launches Steam to self-update; Helios-main's
                    game_updater logic was NOT ported - arming this is the user's call
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helios import db, sched_util  # noqa: E402

WORKFLOWS = [
    {
        "enabled": True,
        "spec": {
            "name": "Morning brief",
            "trigger": {"type": "schedule", "schedule": "daily 07:30"},
            "steps": [
                {"id": "brief", "type": "brain",
                 "prompt": ("Compose the user's morning brief: call mcp__helios__weather_report "
                            "for Larnaca, then get the top 3 tech/AI headlines with "
                            "WebSearch. 6-10 crisp plain-text lines, no markdown, under "
                            "900 characters. Output ONLY the brief text.")},
                {"id": "push", "type": "brain",
                 "prompt": ("Send exactly this to the user with mcp__helios__notify_tim, then "
                            "reply with just 'sent':\n\n{{brief.output}}")},
                {"id": "toast", "type": "notify", "title": "Morning brief",
                 "message": "Sent to your Telegram, sir."},
            ],
        },
    },
    {
        "enabled": True,
        "spec": {
            "name": "Weekly cleanup report",
            "trigger": {"type": "schedule", "schedule": "weekly sun 12:00"},
            "steps": [
                {"id": "report", "type": "agent", "agent": "organizer",
                 "prompt": ("Survey C:/Users/the user/Downloads (top level only): group the "
                            "clutter (installers, old zips, duplicates, screenshots), "
                            "estimate reclaimable space, and PROPOSE a cleanup as a short "
                            "plain-text report. Do NOT delete, move, or rename anything - "
                            "propose only.")},
                {"id": "push", "type": "brain",
                 "prompt": ("Send exactly this to the user with mcp__helios__notify_tim, then "
                            "reply with just 'sent':\n\nWeekly cleanup proposal:\n"
                            "{{report.output}}")},
            ],
        },
    },
    {
        # Seeded DISABLED: Helios-main's game_updater app-specific logic was NOT ported.
        # This lean version just launches Steam so it self-updates overnight. the user arms it
        # in the Workflows drawer if he wants it.
        "enabled": False,
        "spec": {
            "name": "Game updates (3am)",
            "trigger": {"type": "schedule", "schedule": "daily 03:00"},
            "steps": [
                {"id": "steam", "type": "agent", "agent": "operator",
                 "prompt": ("Launch Steam with mcp__computer__launch_app('steam'), wait "
                            "about two minutes for it to check for game updates, then "
                            "report in one line whether it is up and downloading anything.")},
            ],
        },
    },
]


def main() -> None:
    db.init()
    existing = {w["name"] for w in db.list_workflows()}
    now = datetime.now()
    for item in WORKFLOWS:
        spec = item["spec"]
        if spec["name"] in existing:
            print(f"skip (exists): {spec['name']}")
            continue
        sched = spec["trigger"].get("schedule", "")
        nxt = sched_util.next_run_after(sched, now).isoformat(timespec="seconds")
        wid = db.add_workflow(spec["name"], json.dumps(spec), schedule=sched,
                              next_run=nxt, enabled=item["enabled"])
        state = "ENABLED" if item["enabled"] else "disabled"
        print(f"seeded #{wid}: {spec['name']} ({sched}, {state}, next {nxt})")


if __name__ == "__main__":
    main()
