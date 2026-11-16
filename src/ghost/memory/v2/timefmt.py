"""Time in human words for the model — and back (Stonic port, phase 6).

Ported from Stonic's ``memory/timefmt.js``. Models are weak at date arithmetic
("was that 3 weeks or 3 months ago?") and a wrong count becomes a wrong answer,
so code does the arithmetic and the model gets ready-made words:
"3 weeks ago (12 Sep 2026)". The reverse direction (``parse_when``) turns the
model's short forms ("tomorrow 17:00", "+10m") into a time using the PC's
clock — the model never converts dates itself.

All timestamps are epoch milliseconds, read in local time (like JS ``Date``).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

DAY = 24 * 60 * 60 * 1000
MINUTE = 60 * 1000
HOUR = 60 * MINUTE
MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
#: With no time given ("tomorrow"), this morning hour.
DEFAULT_HOUR = 9
#: A promise further ahead than this is a mistake (wrong year).
MAX_AHEAD = 5 * 365 * DAY


def now_ms() -> int:
    return int(time.time() * 1000)


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts / 1000)


def _ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _js_day(d: datetime) -> int:
    """JS getDay(): 0 = Sunday."""
    return (d.weekday() + 1) % 7


def start_of_day(ts: float) -> int:
    d = _local(ts)
    return _ms(datetime(d.year, d.month, d.day))


def absolute_date(ts: float) -> str:
    """ "12 Sep 2026"."""
    d = _local(ts)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _days_between(ts: float, now: float) -> int:
    return round((start_of_day(now) - start_of_day(ts)) / DAY)


def relative_time(ts: Optional[float], now: Optional[float] = None) -> str:
    """ "today" | "yesterday" | "3 days ago" | "2 weeks ago" | "about a year ago" | "in 2 days"."""
    if ts is None or not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    now = now_ms() if now is None else now
    # Calendar days, not 24 h blocks: 11 PM to 9 AM is "yesterday".
    days = _days_between(ts, now)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days == -1:
        return "tomorrow"
    a = abs(days)
    if a < 14:
        span = f"{a} days"
    elif a < 60:
        span = f"{round(a / 7)} weeks"
    elif a < 330:
        span = f"{round(a / 30)} months"
    elif a < 550:
        span = "about a year"
    else:
        span = f"{round(a / 365)} years"
    return f"{span} ago" if days > 0 else f"in {span}"


def when_text(ts: Optional[float], now: Optional[float] = None) -> str:
    """ "3 weeks ago (12 Sep 2026)" — no date for the last few days (noise)."""
    now = now_ms() if now is None else now
    rel = relative_time(ts, now)
    if not rel or ts is None:
        return ""
    return rel if abs(_days_between(ts, now)) < 7 else f"{rel} ({absolute_date(ts)})"


_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def parse_clock(text: str) -> Optional[Tuple[int, int]]:
    """ "17:00" | "5pm" | "5:30 pm" -> (h, m). A bare "5" is refused (morning or evening?)."""
    m = _CLOCK.match(text)
    if not m or (not m.group(2) and not m.group(3)):
        return None
    h = int(m.group(1))
    mins = int(m.group(2)) if m.group(2) else 0
    if m.group(3):
        if h < 1 or h > 12:
            return None
        h = (h % 12) + (12 if m.group(3) == "pm" else 0)
    return None if h > 23 or mins > 59 else (h, mins)


_REL = re.compile(
    r"^(?:\+|in )\s?(\d+(?:\.\d+)?) ?(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$"
)
_NAMED = re.compile(
    r"^(today|tomorrow|sun|mon|tue|wed|thu|fri|sat)[a-z]*(?: (?:at )?(.+))?$"
)
_DATED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[t ](?:at )?(.+))?$")


def parse_when(value: object, now: Optional[float] = None) -> Optional[int]:
    """A follow-up's "when" -> epoch ms, or None.

    Forms: "+10m" / "+2h" / "+3d" · "17:00" / "5pm" (today, else tomorrow) ·
    "today 17:00" · "tomorrow" · "tomorrow 9am" · "fri" / "friday 15:00" ·
    "2026-10-12" / "2026-10-12 10:00". Not understood, or already past -> None:
    a wrong time is worse than no promise.
    """
    now = now_ms() if now is None else now
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text:
        return None

    def in_range(ts: Optional[int]) -> Optional[int]:
        return ts if ts is not None and now < ts <= now + MAX_AHEAD else None

    rel = _REL.match(text)
    if rel:
        u = rel.group(2)[0]
        unit = MINUTE if u == "m" else HOUR if u == "h" else DAY
        return in_range(int(now + round(float(rel.group(1)) * unit)))

    today = _local(now)
    base = datetime(today.year, today.month, today.day)

    def at(days: int, clock: Tuple[int, int]) -> int:
        d = base + timedelta(days=days)
        return _ms(datetime(d.year, d.month, d.day, clock[0], clock[1], 0))

    bare = parse_clock(text)
    if bare:
        return in_range(at(0, bare) if at(0, bare) > now else at(1, bare))

    named = _NAMED.match(text)
    if named:
        clock = parse_clock(named.group(2)) if named.group(2) else (DEFAULT_HOUR, 0)
        if not clock:
            return None
        word = named.group(1)
        if word == "today":
            # "today" with no time, morning already gone: an hour from now.
            return in_range(
                at(0, clock) if named.group(2) else max(at(0, clock), int(now + HOUR))
            )
        if word == "tomorrow":
            return in_range(at(1, clock))
        ahead = (_WEEKDAYS.index(word) - _js_day(today) + 7) % 7
        return in_range(
            at(ahead, clock) if at(ahead, clock) > now else at(ahead + 7, clock)
        )

    dated = _DATED.match(text)
    if dated:
        clock = parse_clock(dated.group(4)) if dated.group(4) else (DEFAULT_HOUR, 0)
        if not clock:
            return None
        try:
            d = datetime(
                int(dated.group(1)),
                int(dated.group(2)),
                int(dated.group(3)),
                clock[0],
                clock[1],
            )
        except ValueError:
            return None
        return in_range(_ms(d))
    return None


def clock_text(ts: float) -> str:
    """ "5:00 PM"."""
    d = _local(ts)
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def due_text(ts: float, now: Optional[float] = None) -> str:
    """ "in 10 minutes (3:45 PM)" | "today at 5:00 PM" | "tomorrow at 9:00 AM" | "on Fri 9 Oct 2026 at 5:00 PM"."""
    now = now_ms() if now is None else now
    clock = clock_text(ts)
    diff = ts - now
    if 0 <= diff < 90 * MINUTE:
        mins = max(1, round(diff / MINUTE))
        return f"in {mins} {'minute' if mins == 1 else 'minutes'} ({clock})"
    days = round((start_of_day(ts) - start_of_day(now)) / DAY)
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    return f"on {WEEKDAY_NAMES[_js_day(_local(ts))]} {absolute_date(ts)} at {clock}"


__all__ = [
    "DAY",
    "HOUR",
    "MINUTE",
    "absolute_date",
    "clock_text",
    "due_text",
    "now_ms",
    "parse_clock",
    "parse_when",
    "relative_time",
    "start_of_day",
    "when_text",
]
