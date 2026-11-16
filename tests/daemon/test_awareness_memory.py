"""Awareness × memory v2 (Stonic port, phase 6): the memory card, due promises,
"about_promise", the character's own record, and the 1.5 s budget."""

from __future__ import annotations

import json
import time

from ghost.daemon.awareness import brain
from ghost.daemon.awareness import engine as engine_mod
from ghost.memory.v2 import bridge
from ghost.memory.v2.service import MemoryCoreService

from .test_awareness_brain import MIN, make_engine

SPEAK_PROMISE = json.dumps(
    {
        "action": "speak",
        "say": "Dentist ka time ho gaya!",
        "note": "promise",
        "about_promise": True,
    }
)
SPEAK_OTHER = json.dumps({"action": "speak", "say": "Pani pi lo.", "note": "water"})


def wire(eng, card="", due="", ids=(1,)):
    raised, events = [], []
    eng.memory_fn = lambda _cid: {"card": card, "due": due, "due_ids": list(ids)}
    eng.promise_raised_fn = raised.extend
    eng.character_event_fn = events.append
    return raised, events


class TestBrain:
    def test_prompt_carries_memory(self):
        ctx = {
            "trigger": {"kind": "moment", "what": "promise_due"},
            "snapshot": {
                "now": time.time() * 1000,
                "digest": [],
                "recentDays": [],
                "active": None,
            },
            "memoryCard": "[YOUR MEMORY OF THIS PERSON]\nABOUT THEM\n- Lives in Lahore",
            "dueBlock": (
                "PROMISES AND FOLLOW-UPS THAT ARE DUE (from your memory of them):\n"
                "- (due now) Ask about the dentist"
            ),
        }
        user = brain.build_messages(ctx)[1]["content"]
        assert "has just come due" in user and "Lives in Lahore" in user
        assert "Ask about the dentist" in user and '"about_promise": true' in user

    def test_parse_about_promise(self):
        assert brain.parse_decision(SPEAK_PROMISE)["aboutPromise"] is True
        silent = brain.parse_decision('{"action":"silent","about_promise":true}')
        assert "aboutPromise" not in silent


class TestEngine:
    def test_promise_due_moment_marks_it_raised(self, tmp_path):
        eng, clock, think, events, _ = make_engine(
            tmp_path, [SPEAK_OTHER, SPEAK_PROMISE]
        )
        raised, recorded = wire(
            eng,
            card="ABOUT THEM\n- x",
            due="PROMISES ... DUE\n- (due now) dentist",
            ids=(7,),
        )
        clock.t += 21_000
        eng.tick()  # hello
        clock.t += 2 * MIN
        assert eng.on_promise_due(1)
        clock.t += 9_000
        eng.tick()
        assert "has just come due" in think.calls[-1][1]["content"]
        assert raised == [7]
        kinds = [e["kind"] for e in recorded]
        assert (
            kinds.count("said") == 2
            and recorded[-1]["text"] == "Dentist ka time ho gaya!"
        )

    def test_nothing_due_falls_back_to_routine(self, tmp_path):
        eng, clock, think, _, _ = make_engine(tmp_path, [SPEAK_OTHER, SPEAK_OTHER])
        raised, _ = wire(eng, card="", due="")
        clock.t += 21_000
        eng.tick()
        clock.t += 2 * MIN
        eng.on_promise_due(1)
        clock.t += 9_000
        eng.tick()
        assert "has just come due" not in think.calls[-1][1]["content"]
        assert raised == []

    def test_slow_memory_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine_mod, "MEMORY_TIMEOUT_S", 0.05)
        eng, clock, think, events, _ = make_engine(tmp_path, [SPEAK_OTHER])

        def slow(_cid):
            time.sleep(0.5)
            return {"card": "LATE", "due": "", "due_ids": []}

        eng.memory_fn = slow
        clock.t += 21_000
        eng.tick()
        assert think.calls and "LATE" not in think.calls[0][1]["content"]
        assert any(e["type"] == "speak" for e in events)

    def test_without_memory_phase5_behaviour(self, tmp_path):
        eng, clock, think, _, _ = make_engine(tmp_path, [SPEAK_OTHER])
        assert eng.memory_fn is None
        clock.t += 21_000
        eng.tick()
        assert "YOUR MEMORY OF THIS PERSON" not in think.calls[0][1]["content"]


class TestBridge:
    def test_awareness_memory_from_core(self, tmp_path, monkeypatch):
        core = MemoryCoreService(tmp_path / "m")
        core.init()
        monkeypatch.setattr(bridge, "memory_v2_enabled", lambda config=None: True)
        core.remember("Works as a backend engineer", source="user")
        core.follow_up("Ask how the interview went", when="+1m")
        mem = bridge.awareness_memory(core, None)
        assert "backend engineer" in mem["card"] and mem["due"] == ""
        core.now = lambda: int(time.time() * 1000) + 5 * 60_000
        mem = bridge.awareness_memory(core, None)
        assert "interview" in mem["due"] and mem["due_ids"]
        monkeypatch.setattr(bridge, "memory_v2_enabled", lambda config=None: False)
        assert bridge.awareness_memory(core, None) is None
        core.shutdown()
