"""Awareness brain: moments, prompt, parsing, pacing, routes (Stonic port, Phase 5)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost.core.config import GhostConfig
from ghost.daemon.awareness import brain, service
from ghost.daemon.awareness.engine import MIN, AwarenessEngine, ThinkError
from ghost.daemon.awareness.moments import MomentDetector, stretch_mark

from .test_awareness_eyes import Clock, at

# -- moments ---------------------------------------------------------------------


class TestMoments:
    def test_stretch_marks(self):
        assert [stretch_mark(m) for m in (10, 45, 89, 90, 149, 150, 211)] == [
            0, 45, 45, 90, 90, 150, 210,
        ]  # fmt: skip

    def tick(self, d, now, **kw):
        args = dict(
            attentive=True, stretch_minutes=0, screen_awake=False, mic_in_call_by=None
        )
        args.update(kw)
        return d.on_tick(now=now, **args)

    def test_stretch_fires_once_per_mark(self):
        d = MomentDetector()
        assert self.tick(d, at(10), stretch_minutes=30) is None
        assert self.tick(d, at(10, 15), stretch_minutes=45) == {
            "what": "stretch",
            "minutes": 45,
        }
        assert self.tick(d, at(10, 16), stretch_minutes=46) is None

    def test_call_end_is_held_until_attentive(self):
        d = MomentDetector()
        self.tick(d, at(10), mic_in_call_by="Zoom")
        self.tick(d, at(10, 30), mic_in_call_by="Zoom")
        assert self.tick(d, at(10, 31), attentive=False) is None
        m = self.tick(d, at(10, 33))
        assert m == {"what": "call_end", "minutes": 31, "app": "Zoom"}

    def test_video_end(self):
        d = MomentDetector()
        self.tick(d, at(20), screen_awake=True)
        assert self.tick(d, at(20, 40)) == {"what": "video_end", "minutes": 40}

    def test_late_hour(self):
        d = MomentDetector()
        self.tick(d, at(23, 59))
        assert self.tick(d, at(0, 0, day=15)) == {"what": "late", "hour": 0}

    def test_restless(self):
        d = MomentDetector()
        hits = [d.on_switch(1000 + i * 5000) for i in range(8)]
        assert hits[-1] == {"what": "restless", "count": 8}
        assert all(h is None for h in hits[:-1])


# -- prompt and parsing -----------------------------------------------------------


def snapshot(**over):
    s = {
        "ok": True,
        "running": True,
        "now": at(23, 30),
        "idleSeconds": 5,
        "away": False,
        "locked": False,
        "watching": False,
        "battery": None,
        "active": {"app": "Code", "title": "main.py", "private": False, "own": False},
        "micInCallBy": None,
        "focusWatcher": True,
        "activeStretchMinutes": 95,
        "digest": ['21:55–now (1h 35m) Code — "main.py"'],
        "today": {"activeMinutes": 300, "firstSeenAt": at(9)},
        "todayApps": [],
        "recentDays": [
            "Yesterday (Sat): at the computer 09:00–23:00, about 9h of it active."
        ],
    }
    s.update(over)
    return s


class TestPrompt:
    def test_messages_carry_the_moment_and_no_stonic_branding(self):
        msgs = brain.build_messages(
            {
                "trigger": {"kind": "moment", "what": "stretch", "minutes": 90},
                "snapshot": snapshot(),
                "characterName": "Jarvis",
                "userName": "Hamza",
                "thoughts": [],
                "rapport": "cool",
            }
        )
        system, user = msgs[0]["content"], msgs[1]["content"]
        assert "You are the inner life of Jarvis" in system
        assert "Hamza's computer" in system
        assert "Stonic" not in system + user
        assert "look_screen" not in system and "look_camera" not in system
        assert "1 hour 30 min without a real break" in user
        assert 'In front of them: Code — "main.py".' in user
        assert "EARLIER DAYS" in user
        assert "be much more selective" in user

    def test_private_window_is_never_described(self):
        user = brain.build_messages(
            {
                "trigger": {"kind": "routine"},
                "snapshot": snapshot(
                    active={"app": "chrome", "title": "(private)", "private": True}
                ),
            }
        )[1]["content"]
        assert "private window — you cannot see what it is" in user
        assert "(private)" not in user

    def test_cannot_speak_says_so(self):
        user = brain.build_messages(
            {"trigger": {"kind": "routine"}, "snapshot": snapshot(), "canSpeak": False}
        )[1]["content"]
        assert "RIGHT NOW YOU CANNOT SPEAK ALOUD" in user
        assert user.endswith('Decide now: "silent".')

    def test_call_hold(self):
        assert (
            brain.call_hold(snapshot(micInCallBy="Zoom")) == "microphone in use by Zoom"
        )
        meet = snapshot(
            active={"app": "chrome", "title": "Meet - abc-defg-hij", "private": False}
        )
        assert brain.call_hold(meet) == "call window: chrome"
        assert brain.call_hold(snapshot()) is None


class TestParse:
    def test_reads_json_even_with_text_around_it(self):
        d = brain.parse_decision(
            "Sure! "
            + json.dumps(
                {
                    "action": "speak",
                    "say": '"Chai break?"',
                    "kind": "Care!",
                    "mood": "fond",
                }
            )
        )
        assert d == {
            "action": "speak",
            "say": "Chai break?",
            "note": "",
            "mood": "fond",
            "kind": "care",
        }

    def test_unreadable_and_empty_say_are_silent(self):
        assert brain.parse_decision("nope")["note"] == brain.UNREADABLE
        assert brain.parse_decision('{"action":"speak","say":""}')["action"] == "silent"

    def test_looks_are_never_taken(self):
        assert brain.parse_decision('{"action":"look_camera"}')["action"] == "silent"

    def test_quiet_wins_and_numbers_are_clamped(self):
        d = brain.parse_decision(
            '{"action":"speak","say":"hi","quiet_minutes":5000,"look_again_minutes":1}'
        )
        assert (
            d["action"] == "silent"
            and d["quietMinutes"] == 720
            and "lookAgainMinutes" not in d
        )
        d = brain.parse_decision(
            '{"action":"silent","look_again_minutes":1,"watching_for":"tea"}'
        )
        assert d["lookAgainMinutes"] == 2 and d["watchingFor"] == "tea"


# -- engine ------------------------------------------------------------------------


class Fake:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        r = (
            self.replies.pop(0)
            if self.replies
            else '{"action":"silent","note":"nothing"}'
        )
        if isinstance(r, Exception):
            raise r
        return r


def make_engine(tmp_path, replies, snap=None, start=None):
    clock = Clock(start or at(11))
    events = []
    think = Fake(replies)
    state = {"snap": snap or snapshot(now=clock.t)}

    def snapshot_fn():
        return {**state["snap"], "now": clock.t}

    eng = AwarenessEngine(
        tmp_path, snapshot=snapshot_fn, think=think, now=clock, emit=events.append
    )
    eng.set_enabled(True)
    eng.tick()  # the eyes tick every 10 s; the first tick switches it on
    return eng, clock, think, events, state


SPEAK = json.dumps(
    {"action": "speak", "say": "Pani pi lo, yaar.", "kind": "care", "mood": "fond"}
)


class TestEngine:
    def test_hello_after_the_delay_then_heartbeat(self, tmp_path):
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK])
        assert think.calls == []
        clock.t += 21_000
        eng.tick()
        assert len(think.calls) == 1
        assert "only just become aware" in think.calls[0][1]["content"]
        speak = [e for e in events if e["type"] == "speak"]
        assert speak and speak[0]["say"] == "Pani pi lo, yaar."
        assert eng.status()["spokenToday"] == 1
        clock.t += 19 * MIN
        eng.tick()
        assert len(think.calls) == 1
        clock.t += 2 * MIN
        eng.tick()
        assert len(think.calls) == 2

    def test_never_thinks_during_a_call(self, tmp_path):
        eng, clock, think, _, _ = make_engine(tmp_path, [SPEAK])
        eng.on_event(
            {
                "type": "tick",
                "idleSeconds": 1,
                "micInCallBy": "Teams",
                "stretchMinutes": 0,
            }
        )
        clock.t += 5 * MIN
        eng.tick()
        assert think.calls == []
        assert eng.status()["label"]["label"] == "Quiet while you are on a call"

    def test_back_after_an_absence_is_a_trigger(self, tmp_path):
        eng, clock, think, _, _ = make_engine(tmp_path, [SPEAK, SPEAK])
        clock.t += 21_000
        eng.tick()  # hello
        clock.t += 2 * MIN
        eng.on_event({"type": "back", "awayMinutes": 25})
        clock.t += 7_000
        eng.tick()
        assert "after being away for 25 minutes" in think.calls[-1][1]["content"]

    def test_short_absences_are_ignored(self, tmp_path):
        eng, clock, think, _, _ = make_engine(tmp_path, [])
        eng.on_event({"type": "back", "awayMinutes": 4})
        assert eng.pending is None

    def test_min_gap_empty_bucket_and_daily_cap(self, tmp_path):
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK] * 10)
        clock.t += 21_000
        eng.tick()  # hello, spoken
        assert not eng._can_speak(clock.t + 30_000)  # never two lines within 90 s
        assert eng._can_speak(clock.t + 91_000)
        eng.state["tokens"] = 0.2
        assert not eng._can_speak(clock.t + 10 * MIN)
        eng.state["tokens"] = 3
        eng.state["spokenToday"] = 30
        assert not eng._can_speak(clock.t + 10 * MIN)
        assert eng.status()["capped"] == "spoken"

    def test_held_back_line_is_remembered_not_spoken(self, tmp_path):
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK, SPEAK])
        clock.t += 21_000
        eng.tick()
        eng.state["tokens"] = 0
        eng.state["gapMs"] = 60 * MIN  # 21 min refills only a third of a line
        clock.t += 21 * MIN
        eng.tick()  # heartbeat; the bucket is still empty
        assert "RIGHT NOW YOU CANNOT SPEAK ALOUD" in think.calls[-1][1]["content"]
        assert sum(e["type"] == "speak" for e in events) == 1
        assert "you had said enough for now" in eng.thoughts[-1]["note"]

    def test_outcomes_adapt_the_gap(self, tmp_path):
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK])
        clock.t += 21_000
        eng.tick()
        t = [e for e in events if e["type"] == "speak"][0]["t"]
        gap = eng.state["gapMs"]
        assert eng.report_outcome(t, "answered", "haan abhi")
        assert eng.state["gapMs"] == pytest.approx(gap * 0.7)
        said = [x for x in eng.thoughts if x.get("spoke")][0]
        assert said["outcome"] == "answered" and said["reply"] == "haan abhi"
        assert not eng.report_outcome(t, "ignored")  # settled once

    def test_dismissed_empties_the_bucket_and_rests(self, tmp_path):
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK])
        clock.t += 21_000
        eng.tick()
        t = [e for e in events if e["type"] == "speak"][0]["t"]
        eng.report_outcome(t, "dismissed")
        assert eng.state["tokens"] == 0
        assert eng.state["restUntil"] >= clock.t + 19 * MIN

    def test_no_report_counts_as_ignored(self, tmp_path):
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK])
        clock.t += 21_000
        eng.tick()
        gap = eng.state["gapMs"]
        clock.t += 3 * MIN
        eng.tick()
        assert eng.state["gapMs"] == pytest.approx(gap * 1.4)

    def test_quiet_request_rests(self, tmp_path):
        eng, clock, think, _, _ = make_engine(
            tmp_path, ['{"action":"silent","quiet_minutes":60}']
        )
        clock.t += 21_000
        eng.tick()
        assert eng.state["restUntil"] == pytest.approx(clock.t + 60 * MIN, abs=1000)
        clock.t += 30 * MIN
        eng.tick()
        assert len(think.calls) == 1

    def test_follow_up_comes_back(self, tmp_path):
        reply = json.dumps(
            {
                "action": "speak",
                "say": "Khana khaya?",
                "look_again_minutes": 20,
                "watching_for": "did they eat",
            }
        )
        eng, clock, think, _, _ = make_engine(tmp_path, [reply, SPEAK])
        clock.t += 21_000
        eng.tick()
        assert eng.state["followUp"]["why"] == "did they eat"
        clock.t += 21 * MIN
        eng.tick()
        assert 'to see: "did they eat"' in think.calls[-1][1]["content"]

    def test_talking_with_the_assistant_holds_thinking(self, tmp_path):
        eng, clock, think, _, _ = make_engine(tmp_path, [SPEAK])
        eng.set_activity(voice_open=True, last_user_at=clock.t)
        clock.t += 21_000
        eng.tick()
        assert think.calls == []

    def test_away_skips_but_retries_soon(self, tmp_path):
        eng, clock, think, _, state = make_engine(tmp_path, [SPEAK])
        state["snap"] = snapshot(away=True)
        clock.t += 21_000
        eng.tick()
        assert think.calls == []
        assert eng.hello_done is True

    def test_failure_backs_off(self, tmp_path):
        eng, clock, think, _, _ = make_engine(tmp_path, [ThinkError("network", "down")])
        clock.t += 21_000
        eng.tick()
        assert eng.backoff_reason == "network"
        assert eng.backoff_until == pytest.approx(clock.t + 3 * MIN, abs=1000)

    def test_state_survives_a_restart(self, tmp_path):
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK])
        clock.t += 21_000
        eng.tick()
        again = AwarenessEngine(
            tmp_path, snapshot=lambda: {}, think=Fake([]), now=clock
        )
        assert again.state["spokenToday"] == 1
        assert any(x.get("spoke") for x in again.thoughts)

    def test_remember_goes_into_the_journal(self, tmp_path):
        eng, clock, think, _, _ = make_engine(
            tmp_path, ['{"action":"silent","remember":"Exam on Friday."}']
        )
        clock.t += 21_000
        eng.tick()
        assert eng.journal[-1]["text"] == "Exam on Friday."


# -- service and routes -----------------------------------------------------------


@pytest.fixture
def svc(tmp_path, monkeypatch):
    service.reset_for_tests()
    replies = Fake([SPEAK])
    s = service.AwarenessService(tmp_path / "aw", replies)
    monkeypatch.setattr(service, "_service", s)
    yield s
    service.reset_for_tests()


def _client(flag: bool) -> TestClient:
    from ghost.server.api_routes import awareness_router

    app = FastAPI()
    cfg = GhostConfig()
    cfg.stonic.awareness = flag
    app.state.config = cfg
    app.include_router(awareness_router)
    return TestClient(app)


class TestRoutes:
    def test_flag_defaults_off_and_routes_refuse(self):
        assert GhostConfig().stonic.awareness is False
        c = _client(False)
        assert c.get("/v1/awareness/status").json() == {
            "available": False,
            "enabled": False,
        }
        assert (
            c.post("/v1/awareness/configure", json={"enabled": True}).status_code == 403
        )
        # The sensor's route stays silent rather than erroring.
        assert (
            c.post("/v1/awareness/signals", json={"idleSeconds": 1}).status_code == 204
        )

    def test_double_opt_in(self, svc):
        c = _client(True)
        assert c.get("/v1/awareness/status").json()["enabled"] is False
        c.post(
            "/v1/awareness/signals",
            json={"idleSeconds": 0, "app": "Code", "title": "x"},
        )
        assert svc.eyes.timeline.entries == []  # not on yet: nothing recorded
        assert (
            c.post("/v1/awareness/configure", json={"enabled": True}).json()["enabled"]
            is True
        )
        c.post(
            "/v1/awareness/signals",
            json={"idleSeconds": 0, "app": "Code", "title": "x"},
        )
        diary = c.get("/v1/awareness/diary").json()
        assert diary["running"] is True and diary["active"]["app"] == "Code"
        svc.stop()

    def test_think_now_and_events_and_outcome(self, svc):
        c = _client(True)
        c.post(
            "/v1/awareness/configure",
            json={"enabled": True, "character": {"name": "Jarvis"}},
        )
        c.post(
            "/v1/awareness/signals",
            json={"idleSeconds": 0, "app": "Code", "title": "x"},
        )
        r = c.post("/v1/awareness/think-now").json()
        assert r["ok"] and r["decision"]["action"] == "speak"
        events = c.get("/v1/awareness/events").json()["events"]
        speak = [e for e in events if e["type"] == "speak"][0]
        assert speak["character"] == "Jarvis"
        assert c.post(
            "/v1/awareness/outcome", json={"t": speak["t"], "outcome": "answered"}
        ).json()["ok"]
        svc.stop()

    def test_forget_deletes_everything(self, svc, tmp_path):
        c = _client(True)
        c.post("/v1/awareness/configure", json={"enabled": True})
        c.post(
            "/v1/awareness/signals",
            json={"idleSeconds": 0, "app": "Code", "title": "x"},
        )
        svc.eyes.flush()
        c.post("/v1/awareness/forget")
        assert svc.eyes.timeline.entries == [] or all(
            e["kind"] != "app" for e in svc.eyes.timeline.entries
        )
        svc.stop()

    def test_awareness_signals_pass_the_face_lock_gate(self):
        from ghost.security import facelock

        assert not facelock.gate_blocks(
            "/v1/awareness/signals", locked=True, loopback=True
        )
        assert facelock.gate_blocks("/v1/awareness/diary", locked=True, loopback=True)


def test_make_think_uses_ghost_engine():
    class Eng:
        def generate(self, messages, *, model, temperature, max_tokens):
            assert messages[0].role.value == "system" and model == "m"
            return {"content": '{"action":"silent"}'}

    think = service.make_think(lambda: Eng(), lambda: "m")
    assert (
        think([{"role": "system", "content": "a"}, {"role": "user", "content": "b"}])
        == '{"action":"silent"}'
    )
    with pytest.raises(ThinkError) as err:
        service.make_think(lambda: None, lambda: "")([])
    assert err.value.kind == "no_key"


_ = datetime  # keep the import used by helpers in sibling tests
