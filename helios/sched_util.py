"""Lightweight schedule/time parsing shared by the tools server and the scheduler.

Supported schedule strings:
  "daily HH:MM"          -> every day at that local time
  "every <N>m"           -> every N minutes
  "every <N>h"           -> every N hours
  "hourly"               -> every hour
  "weekdays HH:MM"       -> Mon-Fri at that time
  "weekly <day> HH:MM"   -> once a week (day = mon/tue/wed/thu/fri/sat/sun; ported from
                            Helios-main sched_util 2026-07-08)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_due(in_minutes: float | None, due_iso: str | None, now: datetime) -> datetime:
    """Resolve a reminder due-time from a relative minutes value or an absolute ISO string.

    All times are normalized to naive-local so the DB's lexical `due_at <= now` ordering
    holds (a stored offset like +05:00 would otherwise sort wrong against naive now).
    """
    if in_minutes is not None:
        try:
            mins = float(in_minutes)
        except (TypeError, ValueError):
            mins = 5.0
        if mins != mins or mins in (float("inf"), float("-inf")):  # NaN / inf
            mins = 5.0
        return now + timedelta(minutes=max(0.0, mins))  # never schedule in the past
    if due_iso:
        try:
            dt = datetime.fromisoformat(due_iso)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)  # -> local naive
            return dt
        except Exception:
            pass
    return now + timedelta(minutes=5)


def next_run_after(schedule: str, after: datetime) -> datetime:
    """Compute the next fire time strictly after `after` for a routine `schedule` string.

    Inputs are clamped/sanitised so a malformed schedule can never raise or busy-loop:
    intervals are forced >= 1 unit, and daily/weekdays hours/minutes are clamped into range.
    Unrecognised strings fall back to "once a day from now". See module docstring for the
    accepted formats; valid_schedule() validates them up front."""
    s = (schedule or "").strip().lower()
    m = re.match(r"every\s+(\d+)\s*m(in)?", s)
    if m:
        return after + timedelta(minutes=max(1, int(m.group(1))))  # never 0 -> busy-loop
    m = re.match(r"every\s+(\d+)\s*h", s)
    if m:
        return after + timedelta(hours=max(1, int(m.group(1))))
    if s == "hourly":
        return after + timedelta(hours=1)
    m = re.match(r"(daily|weekdays)\s+(\d{1,2}):(\d{2})", s)
    if m:
        kind = m.group(1)
        hh = min(23, max(0, int(m.group(2))))   # clamp so a bad time can't raise ValueError
        mm = min(59, max(0, int(m.group(3))))
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= after:
            cand += timedelta(days=1)
        if kind == "weekdays":
            while cand.weekday() >= 5:  # Sat/Sun
                cand += timedelta(days=1)
        return cand
    m = re.match(r"weekly\s+([a-z]{3})[a-z]*\s+(\d{1,2}):(\d{2})", s)
    if m and m.group(1) in _DAYS:
        hh = min(23, max(0, int(m.group(2))))
        mm = min(59, max(0, int(m.group(3))))
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = _DAYS[m.group(1)]
        while cand.weekday() != target or cand <= after:
            cand += timedelta(days=1)
        return cand
    # default: once a day from now
    return after + timedelta(days=1)


def valid_schedule(schedule: str) -> bool:
    """True if `schedule` is a well-formed, runnable schedule string."""
    s = (schedule or "").strip().lower()
    if s == "hourly":
        return True
    m = re.fullmatch(r"every\s+(\d+)\s*m(in)?", s)
    if m:
        return int(m.group(1)) >= 1
    m = re.fullmatch(r"every\s+(\d+)\s*h", s)
    if m:
        return int(m.group(1)) >= 1
    m = re.fullmatch(r"(daily|weekdays)\s+(\d{1,2}):(\d{2})", s)
    if m:
        return 0 <= int(m.group(2)) <= 23 and 0 <= int(m.group(3)) <= 59
    m = re.fullmatch(r"weekly\s+([a-z]{3})[a-z]*\s+(\d{1,2}):(\d{2})", s)
    if m:
        return (m.group(1) in _DAYS
                and 0 <= int(m.group(2)) <= 23 and 0 <= int(m.group(3)) <= 59)
    return False
