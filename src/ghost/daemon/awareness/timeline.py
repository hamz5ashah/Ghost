"""Awareness diary — what the user did on the PC during the day, in time order.

Ported from Stonic's ``awareness-timeline.js``. Pure data, no OS calls, so it
is tested in seconds without a desktop.

Every entry is a moment: ``{"t": ms, "kind": ..., ...}``

    app    → this window came to the front     {app, title, private}
    away   → the user stepped away (no input)
    back   → came back                         {mins}
    lock / unlock / sleep / wake
    start  → Ghost was opened

The model never sees the raw list: :meth:`Timeline.digest` folds it into a few
lines ("09:02–10:40 (1h 38m) Code — main.py"), because that text is billed on
every heartbeat of the brain.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

MAX_ENTRIES = 600
KEEP_MS = 18 * 60 * 60 * 1000

# A window shown for less than this is not its own line — passing through
# windows with Alt+Tab is not "work".
MIN_SEGMENT_MS = 45 * 1000
# A title that changes this quickly within one app (switching tabs, a video's
# clock) updates the last entry instead of adding a new one.
TITLE_SETTLE_MS = 15 * 1000
TITLE_MAX = 110

# Private browsing and password managers: the title never reaches the model,
# and the screen is never looked at while they are in front.
PRIVATE_TITLE = re.compile(
    r"\b(inprivate|incognito|private browsing|private window)\b", re.I
)
PRIVATE_APPS = frozenset(
    {
        "keepass",
        "keepassxc",
        "1password",
        "bitwarden",
        "lastpass",
        "dashlane",
        "nordpass",
        "enpass",
    }
)

OWN_APP = "Ghost"


def now_ms() -> float:
    return time.time() * 1000


def sanitize_focus(raw_app: Any, raw_title: Any) -> Dict[str, Any]:
    """Clean a window's app name and title, hiding the title if private."""
    app = re.sub(r"\.exe$", "", str(raw_app or ""), flags=re.I).strip()[:60]
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    private = bool(PRIVATE_TITLE.search(title)) or app.lower() in PRIVATE_APPS
    if private:
        return {"app": app or "unknown", "title": "(private)", "private": True}
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX].rstrip() + "…"
    return {"app": app or "unknown", "title": title, "private": False}


def _local(t_ms: float) -> datetime:
    return datetime.fromtimestamp(t_ms / 1000)


def clock(t_ms: float) -> str:
    d = _local(t_ms)
    return f"{d.hour:02d}:{d.minute:02d}"


def human_minutes(ms: float) -> str:
    """95 min → "1h 35m", 7 → "7m", 0 → "<1m"."""
    mins = int(math.floor(ms / 60000 + 0.5))
    if mins < 1:
        return "<1m"
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class Timeline:
    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []

    def load(self, items: Any, now: Optional[float] = None) -> None:
        """Restore from disk. Broken or stale entries are dropped quietly."""
        now = now_ms() if now is None else now
        good = [
            e
            for e in (items if isinstance(items, list) else [])
            if isinstance(e, dict)
            and _finite(e.get("t"))
            and isinstance(e.get("kind"), str)
            and now - e["t"] < KEEP_MS
            and e["t"] <= now
        ]
        good.sort(key=lambda e: e["t"])
        self.entries = good[-MAX_ENTRIES:]

    def to_json(self) -> List[Dict[str, Any]]:
        return self.entries

    def add(self, entry: Dict[str, Any]) -> None:
        """Insert in time order — ``away`` is written at its real (earlier) time."""
        if not entry or not _finite(entry.get("t")):
            return
        i = len(self.entries)
        while i > 0 and self.entries[i - 1]["t"] > entry["t"]:
            i -= 1
        self.entries.insert(i, entry)
        if len(self.entries) > MAX_ENTRIES:
            del self.entries[: len(self.entries) - MAX_ENTRIES]

    def note_focus(self, focus: Dict[str, Any], t: Optional[float] = None) -> bool:
        """The front window changed. ``True`` = something new was written."""
        t = now_ms() if t is None else t
        last = self.entries[-1] if self.entries else None
        if last and last.get("kind") == "app" and last.get("app") == focus["app"]:
            if last.get("title") == focus["title"]:
                return False
            if t - last["t"] < TITLE_SETTLE_MS:
                last["title"] = focus["title"]
                last["private"] = focus["private"]
                return True
        self.add(
            {
                "t": t,
                "kind": "app",
                "app": focus["app"],
                "title": focus["title"],
                "private": focus["private"],
            }
        )
        return True

    def prune(self, now: Optional[float] = None) -> bool:
        now = now_ms() if now is None else now
        before = len(self.entries)
        self.entries = [e for e in self.entries if now - e["t"] < KEEP_MS]
        return before != len(self.entries)

    def digest(
        self,
        now: Optional[float] = None,
        *,
        max_lines: int = 14,
        since_ms: float = 12 * 60 * 60 * 1000,
    ) -> List[str]:
        """Fold the diary into a few lines for the model.

        Consecutive entries of one app become one line (with its two longest
        titles). Windows shown for a few seconds drop out. If there are too
        many lines, the oldest fold into one.
        """
        now = now_ms() if now is None else now
        items = [e for e in self.entries if e["t"] >= now - since_ms]
        rows: List[Dict[str, Any]] = []
        seg: Optional[Dict[str, Any]] = None

        def close(end_t: float) -> None:
            nonlocal seg
            if seg and max(0, end_t - seg["start"]) >= MIN_SEGMENT_MS:
                rows.append({"type": "app", **seg, "end": end_t})
            seg = None

        for i, e in enumerate(items):
            nxt = items[i + 1]["t"] if i + 1 < len(items) else now
            if e["kind"] == "app":
                if seg and seg["app"] == e["app"]:
                    if e.get("title"):
                        seg["titles"][e["title"]] = (
                            seg["titles"].get(e["title"], 0) + nxt - e["t"]
                        )
                    continue
                close(e["t"])
                seg = {"start": e["t"], "app": e["app"], "titles": {}}
                if e.get("title"):
                    seg["titles"][e["title"]] = nxt - e["t"]
                continue
            close(e["t"])
            rows.append({"type": e["kind"], "t": e["t"], "mins": e.get("mins")})
        # The current window always has its line, even if it just opened.
        if seg:
            rows.append({"type": "app", **seg, "end": now, "current": True})

        lines: List[str] = []
        for r in rows:
            line = _row_line(r)
            if line:
                lines.append(line)
        if len(lines) <= max_lines:
            return lines
        dropped = len(lines) - (max_lines - 1)
        return [f"(earlier today: {dropped} more entries, not shown)", *lines[dropped:]]


def _row_line(r: Dict[str, Any]) -> Optional[str]:
    kind = r["type"]
    if kind == "app":
        titles = sorted(r["titles"].items(), key=lambda kv: -kv[1])[:2]
        named = "; ".join(f'"{title}"' for title, _ in titles)
        span = (
            f"{clock(r['start'])}–now"
            if r.get("current")
            else f"{clock(r['start'])}–{clock(r['end'])}"
        )
        dur = human_minutes(r["end"] - r["start"])
        return f"{span} ({dur}) {r['app']}" + (f" — {named}" if named else "")
    t = clock(r["t"])
    mins = r.get("mins") or 0
    after = f" after {human_minutes(mins * 60000)}" if mins else ""
    return {
        "away": f"{t} stepped away from the computer",
        "back": f"{t} came back{after}",
        "lock": f"{t} locked the computer",
        "unlock": f"{t} unlocked the computer",
        "sleep": f"{t} computer went to sleep",
        "wake": f"{t} computer woke up{after}",
        "start": f"{t} Ghost was opened",
    }.get(kind)


# ---------------------------------------------------------------------------
# Day ledger — "you were up until 3 last night"
# ---------------------------------------------------------------------------
#
# The diary only holds 18 hours, so yesterday is not in it. What makes the
# assistant "someone" is that it remembers yesterday. The ledger keeps four
# facts per day: when it started, when it ended, how much was active, and in
# which apps. App names only — never titles.
#
# A day turns over at 5 AM, not midnight: for someone working until 3 AM,
# that is "last night", not "this morning".

DAY_CUT_HOURS = 5
LEDGER_KEEP_DAYS = 8
LEDGER_MAX_APPS = 16
# A day with less than this much activity is not worth mentioning.
LEDGER_MIN_ACTIVE_MS = 5 * 60 * 1000

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip
_DAY_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_key(t_ms: float) -> str:
    """Which day a moment belongs to: ``YYYY-MM-DD``, 5 AM to 5 AM."""
    d = _local(t_ms) - timedelta(hours=DAY_CUT_HOURS)
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


class DayLedger:
    def __init__(self) -> None:
        self.days: Dict[str, Dict[str, Any]] = {}

    def load(self, raw: Any, now: Optional[float] = None) -> None:
        now = now_ms() if now is None else now
        self.days = {}
        if not isinstance(raw, dict):
            return
        for key, d in raw.items():
            if (
                not isinstance(key, str)
                or not _DAY_KEY.match(key)
                or not isinstance(d, dict)
            ):
                continue
            first, last, active = d.get("first"), d.get("last"), d.get("activeMs")
            if (
                not (_finite(first) and _finite(last) and _finite(active))
                or first > now
            ):
                continue
            apps = (
                {
                    str(app)[:60]: ms
                    for app, ms in (d.get("apps") or {}).items()
                    if _finite(ms) and ms > 0
                }
                if isinstance(d.get("apps"), dict)
                else {}
            )
            self.days[key] = {
                "first": first,
                "last": min(last, now),
                "activeMs": max(0, active),
                "apps": apps,
            }
        self.prune()

    def to_json(self) -> Dict[str, Dict[str, Any]]:
        return self.days

    def note(self, t: float, app: str, delta_ms: float) -> None:
        """The user was present for ``delta_ms`` (ending at ``t``), in this app."""
        if not _finite(t) or not (delta_ms > 0):
            return
        key = day_key(t)
        d = self.days.get(key)
        if d is None:
            d = self.days[key] = {
                "first": t - delta_ms,
                "last": t,
                "activeMs": 0,
                "apps": {},
            }
            self.prune()
        d["last"] = max(d["last"], t)
        d["activeMs"] += delta_ms
        if app:
            d["apps"][app] = d["apps"].get(app, 0) + delta_ms
            if len(d["apps"]) > LEDGER_MAX_APPS:
                smallest = min(d["apps"], key=lambda a: d["apps"][a])
                del d["apps"][smallest]

    def prune(self) -> None:
        keys = sorted(self.days)
        for key in keys[: max(0, len(keys) - LEDGER_KEEP_DAYS)]:
            del self.days[key]

    def today(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        now = now_ms() if now is None else now
        d = self.days.get(day_key(now))
        if not d:
            return None
        return {
            "activeMinutes": round(d["activeMs"] / 60000),
            "firstSeenAt": d["first"],
        }

    def top_apps(
        self, now: Optional[float] = None, limit: int = 3
    ) -> List[Dict[str, Any]]:
        """Today's biggest apps (not Ghost itself), 5 minutes or more each."""
        now = now_ms() if now is None else now
        d = self.days.get(day_key(now))
        if not d:
            return []
        names = [
            a for a in d["apps"] if a and a != OWN_APP and d["apps"][a] >= 5 * 60 * 1000
        ]
        names.sort(key=lambda a: -d["apps"][a])
        return [
            {"name": a, "minutes": round(d["apps"][a] / 60000)} for a in names[:limit]
        ]

    def lines(self, now: Optional[float] = None, *, limit: int = 3) -> List[str]:
        """Earlier days, newest first, for the model. Today is not included."""
        now = now_ms() if now is None else now
        today_key = day_key(now)
        yesterday_key = day_key(now - 24 * 60 * 60 * 1000)
        keys = sorted(
            (
                k
                for k in self.days
                if k < today_key and self.days[k]["activeMs"] >= LEDGER_MIN_ACTIVE_MS
            ),
            reverse=True,
        )[:limit]
        out = []
        for key in keys:
            d = self.days[key]
            y, m, day = (int(p) for p in key.split("-"))
            weekday = WEEKDAYS[datetime(y, m, day, 12).weekday()]
            label = (
                f"Yesterday ({weekday})"
                if key == yesterday_key
                else f"{weekday} {day} {MONTHS[m - 1]}"
            )
            apps = sorted(d["apps"], key=lambda a: -d["apps"][a])[:3]
            mostly = f" — mostly {', '.join(apps)}" if apps else ""
            out.append(
                f"{label}: at the computer {clock(d['first'])}–{clock(d['last'])}, "
                f"about {human_minutes(d['activeMs'])} of it active{mostly}."
            )
        return out
