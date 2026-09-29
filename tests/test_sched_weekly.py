"""helios.sched_util — the `weekly <day> HH:MM` grammar (ported from Helios-main for the
Weekly-cleanup workflow) plus regression pins for the pre-existing formats."""
from datetime import datetime

from helios import sched_util as su

WED = datetime(2026, 7, 8, 10, 0)   # 2026-07-08 is a Wednesday


def test_weekly_valid():
    assert su.valid_schedule("weekly sun 12:00")
    assert su.valid_schedule("Weekly MON 07:30")
    assert su.valid_schedule("weekly sunday 12:00")     # full day name tolerated
    assert not su.valid_schedule("weekly zzz 12:00")
    assert not su.valid_schedule("weekly sun 25:00")
    assert not su.valid_schedule("weekly 12:00")


def test_weekly_next_run_later_this_week():
    nxt = su.next_run_after("weekly sun 12:00", WED)
    assert nxt == datetime(2026, 7, 12, 12, 0)          # the coming Sunday


def test_weekly_same_day_future_time():
    nxt = su.next_run_after("weekly wed 23:00", WED)
    assert nxt == datetime(2026, 7, 8, 23, 0)           # today, later


def test_weekly_same_day_past_time_rolls_a_week():
    nxt = su.next_run_after("weekly wed 09:00", WED)
    assert nxt == datetime(2026, 7, 15, 9, 0)           # next Wednesday


def test_existing_formats_still_work():
    assert su.valid_schedule("daily 07:30")
    assert su.valid_schedule("weekdays 09:00")
    assert su.next_run_after("daily 07:30", WED) == datetime(2026, 7, 9, 7, 30)
    assert su.next_run_after("hourly", WED) == datetime(2026, 7, 8, 11, 0)
