"""German local time without a time zone database.

EFA wants request times in local time and people read dates in local time,
while everything else in the app is UTC. Python's zoneinfo needs the tzdata
package on Windows; the EU rule is short enough to write down instead:
summer time (UTC+2) runs from the last Sunday of March, 01:00 UTC, to the
last Sunday of October, 01:00 UTC; otherwise it is UTC+1.
"""
from datetime import datetime, timedelta, timezone


def last_sunday(year: int, month: int) -> datetime:
    """01:00 UTC on the last Sunday of the month (which has 31 days)."""
    day = datetime(year, month, 31, 1, 0, tzinfo=timezone.utc)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def to_local(moment: datetime) -> datetime:
    """A UTC moment as naive German local time."""
    moment = moment.astimezone(timezone.utc)
    summer = last_sunday(moment.year, 3) <= moment < last_sunday(moment.year, 10)
    return (moment + timedelta(hours=2 if summer else 1)).replace(tzinfo=None)
