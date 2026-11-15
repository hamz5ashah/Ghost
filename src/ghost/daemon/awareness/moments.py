"""Moments worth a thought, found in the eyes' ticks (Stonic's ``NFe``). Pure.

    call_end   a call of 2+ minutes just ended
    video_end  something kept the screen awake 5+ minutes (no call) and stopped
    stretch    45 min, 90 min, then every hour without a real break
    late       the clock passed midnight, 2 AM or 4 AM and they are still at it
    restless   8+ window switches within two minutes (at most every 15 min)

A moment found while the user is not attentive is held for up to ten minutes
and offered when they are back.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

STRETCH_FIRST, STRETCH_SECOND, STRETCH_EVERY = 45, 90, 60
LATE_HOURS = (0, 2, 4)
VIDEO_MIN_MS = 5 * 60_000
CALL_MIN_MS = 2 * 60_000
RESTLESS_SWITCHES = 8
RESTLESS_WINDOW_MS = 2 * 60_000
RESTLESS_GAP_MS = 15 * 60_000
HOLD_MS = 10 * 60_000


def stretch_mark(minutes: int) -> int:
    if minutes < STRETCH_FIRST:
        return 0
    if minutes < STRETCH_SECOND:
        return STRETCH_FIRST
    return (
        STRETCH_SECOND + ((minutes - STRETCH_SECOND) // STRETCH_EVERY) * STRETCH_EVERY
    )


class MomentDetector:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.mark = -1
        self.last_stretch = 0
        self.hour = -1
        self.awake_since = 0.0
        self.awake_had_call = False
        self.call_since = 0.0
        self.call_app = ""
        self.switches: List[float] = []
        self.restless_at = -RESTLESS_GAP_MS
        self.held: Optional[Dict[str, Any]] = None

    def on_tick(
        self,
        *,
        now: float,
        attentive: bool,
        stretch_minutes: int,
        screen_awake: bool,
        mic_in_call_by: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        found: Optional[Dict[str, Any]] = None
        if mic_in_call_by:
            if not self.call_since:
                self.call_since = now
            self.call_app = mic_in_call_by
        elif self.call_since:
            took = now - self.call_since
            if took >= CALL_MIN_MS:
                self.held = {
                    "moment": {
                        "what": "call_end",
                        "minutes": round(took / 60_000),
                        "app": self.call_app,
                    },
                    "at": now,
                }
            self.call_since = 0
            self.call_app = ""

        if screen_awake:
            if not self.awake_since:
                self.awake_since = now
                self.awake_had_call = False
            if mic_in_call_by:
                self.awake_had_call = True
        elif self.awake_since:
            took = now - self.awake_since
            if not self.awake_had_call and took >= VIDEO_MIN_MS and not self.held:
                self.held = {
                    "moment": {"what": "video_end", "minutes": round(took / 60_000)},
                    "at": now,
                }
            self.awake_since = 0

        if self.held:
            if now - self.held["at"] > HOLD_MS:
                self.held = None
            elif attentive and not mic_in_call_by:
                found = self.held["moment"]
                self.held = None

        mark = stretch_mark(stretch_minutes)
        if self.mark < 0 or stretch_minutes < self.last_stretch - 5:
            self.mark = mark
        elif mark > self.mark and attentive:
            self.mark = mark
            if not found:
                found = {"what": "stretch", "minutes": mark}
        self.last_stretch = stretch_minutes

        hour = datetime.fromtimestamp(now / 1000).hour
        if (
            self.hour >= 0
            and hour != self.hour
            and hour in LATE_HOURS
            and attentive
            and not found
        ):
            found = {"what": "late", "hour": hour}
        self.hour = hour
        return found

    def on_switch(self, now: float) -> Optional[Dict[str, Any]]:
        self.switches = [t for t in self.switches if now - t < RESTLESS_WINDOW_MS] + [
            now
        ]
        if (
            len(self.switches) < RESTLESS_SWITCHES
            or now - self.restless_at < RESTLESS_GAP_MS
        ):
            return None
        count = len(self.switches)
        self.restless_at = now
        self.switches = []
        return {"what": "restless", "count": count}
