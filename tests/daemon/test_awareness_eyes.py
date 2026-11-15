"""Awareness eyes and diary (Stonic port, Phase 5)."""

from __future__ import annotations

import json
from datetime import datetime

from ghost.daemon.awareness import timeline as tl
from ghost.daemon.awareness.eyes import (
    AWAY_AFTER_S,
    AwarenessEyes,
    call_app_from_mic,
)

MIN = 60_000


def at(h: int, m: int = 0, day: int = 14) -> float:
    return datetime(2026, 11, day, h, m).timestamp() * 1000


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


# -- diary -----------------------------------------------------------------------


class TestSanitize:
    def test_private_windows_hide_their_title(self):
        assert (
            tl.sanitize_focus("chrome.exe", "Bank — InPrivate")["title"] == "(private)"
        )
        assert tl.sanitize_focus("KeePassXC", "vault")["private"] is True

    def test_long_titles_are_cut(self):
        out = tl.sanitize_focus("code", "x" * 300)
        assert len(out["title"]) == tl.TITLE_MAX + 1 and out["title"].endswith("…")
        assert out["app"] == "code"


class TestTimeline:
    def test_digest_folds_an_app_and_drops_quick_switches(self):
        t = tl.Timeline()
        t.add({"t": at(9), "kind": "start"})
        t.note_focus({"app": "Code", "title": "main.py", "private": False}, at(9, 2))
        t.note_focus({"app": "Explorer", "title": "", "private": False}, at(10, 40))
        # 20 seconds in Explorer: too short for its own line.
        t.note_focus(
            {"app": "Code", "title": "main.py", "private": False},
            at(10, 40) + 20_000,
        )
        lines = t.digest(at(11, 0))
        assert lines[0] == "09:00 Ghost was opened"
        assert lines[1].startswith("09:02–10:40 (1h 38m) Code")
        assert not any("Explorer" in line for line in lines)
        assert lines[-1].startswith("10:40–now (20m) Code")

    def test_title_churn_within_one_app_updates_in_place(self):
        t = tl.Timeline()
        t.note_focus({"app": "Chrome", "title": "a", "private": False}, 1000)
        t.note_focus({"app": "Chrome", "title": "b", "private": False}, 5000)
        assert len(t.entries) == 1 and t.entries[0]["title"] == "b"

    def test_old_entries_are_pruned_and_loads_are_cleaned(self):
        t = tl.Timeline()
        t.load([{"t": 1, "kind": "start"}, {"t": "x"}, None], now=tl.KEEP_MS + 10)
        assert t.entries == []

    def test_many_lines_fold_the_oldest(self):
        t = tl.Timeline()
        for i in range(20):
            t.add({"t": at(9) + i * MIN, "kind": "lock"})
        lines = t.digest(at(10), max_lines=5)
        assert len(lines) == 5 and lines[0].startswith("(earlier today: 16")


class TestLedger:
    def test_day_turns_at_five_am(self):
        assert tl.day_key(at(3, 0, day=15)) == "2026-11-14"
        assert tl.day_key(at(6, 0, day=15)) == "2026-11-15"

    def test_yesterday_line_and_top_apps(self):
        led = tl.DayLedger()
        for i in range(30):
            led.note(at(22) + i * MIN, "Code", MIN)
        now = at(12, 0, day=15)
        assert led.lines(now)[0].startswith(
            "Yesterday (Sat): at the computer 21:59–22:29"
        )
        assert "mostly Code" in led.lines(now)[0]
        assert led.today(now) is None
        led.note(now, "Code", 6 * MIN)
        assert led.top_apps(now) == [{"name": "Code", "minutes": 6}]

    def test_ledger_never_keeps_ghost_as_a_top_app(self):
        led = tl.DayLedger()
        led.note(at(12), "Ghost", 30 * MIN)
        assert led.top_apps(at(12)) == []


# -- eyes ------------------------------------------------------------------------


def eyes(tmp_path, t=None):
    clock = Clock(t if t is not None else at(9))
    e = AwarenessEyes(tmp_path / "awareness", now=clock)
    return e, clock


def feed(e, clock, seconds, **sample):
    """Advance in 2 s sensor steps, feeding the same sample."""
    for _ in range(int(seconds // 2)):
        clock.t += 2000
        e.ingest(dict(sample))


class TestEyes:
    def test_off_records_nothing(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.ingest({"idleSeconds": 0, "app": "Code", "title": "x"})
        assert e.snapshot()["running"] is False
        assert not (tmp_path / "awareness" / "diary.json").exists()

    def test_away_and_back(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.start()
        feed(e, clock, 10, idleSeconds=1, app="Code", title="main.py")
        idle = 0
        for _ in range(110):  # ~220 s with no input
            clock.t += 2000
            idle += 2
            e.ingest({"idleSeconds": idle, "app": "Code", "title": "main.py"})
        assert e.snapshot()["away"] is True
        # The return is noticed on the next 5 s poll.
        feed(e, clock, 6, idleSeconds=1, app="Code", title="main.py")
        snap = e.snapshot()
        assert snap["away"] is False
        back = [ev for ev in e.events() if ev["type"] == "back"]
        assert back and back[-1]["awayMinutes"] >= 3
        assert any("stepped away" in line for line in snap["digest"])

    def test_watching_a_video_is_not_away(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.start()
        idle = 0
        for _ in range(300):  # ten minutes, no input, video playing
            clock.t += 2000
            idle += 2
            e.ingest(
                {"idleSeconds": idle, "app": "vlc", "title": "film", "display": True}
            )
        snap = e.snapshot()
        assert snap["away"] is False
        assert snap["watching"] is True
        assert idle > AWAY_AFTER_S

    def test_lock_unlock_and_sleep(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.start()
        feed(e, clock, 4, idleSeconds=0, locked=True)
        assert e.snapshot()["locked"] is True
        feed(e, clock, 4, idleSeconds=0, locked=False)
        assert [ev["type"] for ev in e.events()][-1] == "unlock"
        clock.t += 40 * MIN
        e.ingest({"idleSeconds": 0, "sleptMs": 40 * MIN})
        wake = [ev for ev in e.events() if ev["type"] == "wake"][-1]
        assert wake["awayMinutes"] == 40
        assert e.snapshot()["activeStretchMinutes"] == 0

    def test_private_and_own_windows(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.start()
        e.ingest({"idleSeconds": 0, "app": "firefox", "title": "x — Private Browsing"})
        assert e.snapshot()["active"]["private"] is True
        e.ingest(
            {"idleSeconds": 0, "own": True, "app": "ghost-desktop", "title": "Ghost"}
        )
        active = e.snapshot()["active"]
        assert active["own"] is True and active["app"] == "Ghost (this app)"

    def test_call_detection_from_mic(self, tmp_path):
        assert call_app_from_mic("obs64.exe|Zoom.exe") == "Zoom"
        assert call_app_from_mic("ghost-desktop.exe") is None
        assert call_app_from_mic("obs64.exe") is None
        e, clock = eyes(tmp_path)
        e.start()
        e.ingest({"idleSeconds": 0, "mic": "Teams.exe"})
        assert e.snapshot()["micInCallBy"] == "Teams"

    def test_stale_sensor_expires_its_signals(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.start()
        e.ingest({"idleSeconds": 0, "mic": "Zoom.exe", "display": True})
        clock.t += 60_000
        e.pump()
        snap = e.snapshot()
        assert snap["micInCallBy"] is None and snap["focusWatcher"] is False

    def test_diary_persists_and_off_drops_a_stale_one(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.start()
        e.ingest({"idleSeconds": 0, "app": "Code", "title": "a"})
        e.stop()
        path = tmp_path / "awareness" / "diary.json"
        saved = json.loads(path.read_text())
        assert any(x["kind"] == "app" for x in saved["entries"])

        e2, clock2 = eyes(tmp_path, clock.t)
        e2.start()
        assert any(x["kind"] == "app" for x in e2.timeline.entries)
        e2.stop()
        import os

        old = (clock.t - tl.KEEP_MS - 1000) / 1000
        os.utime(path, (old, old))
        e2.configure(False)
        assert not path.exists()

    def test_tick_events_carry_the_stretch(self, tmp_path):
        e, clock = eyes(tmp_path)
        e.start()
        feed(e, clock, 30, idleSeconds=0)
        ticks = [ev for ev in e.events() if ev["type"] == "tick"]
        assert ticks and "stretchMinutes" in ticks[-1]

    def test_log_rolls_over(self, tmp_path):
        e, _ = eyes(tmp_path)
        assert e.write_log("info", "stayed quiet", {"why": "nothing"})
        text = (tmp_path / "awareness" / "awareness.log").read_text()
        assert "[info] stayed quiet | " in text
