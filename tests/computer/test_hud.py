"""Tests for the computer-use HUD state machine (port of computer-overlay.js)."""

from __future__ import annotations

from ghost.computer import hud


def queued(task_id="t1", now=1000.0):
    return hud.reduce(
        None,
        {
            "event": "task_queued",
            "id": task_id,
            "task": "open notepad",
            "max_steps": 50,
        },
        now,
    )


class TestLifecycle:
    def test_queued_starts_the_hud(self):
        s = queued()
        assert s["phase"] == "starting"
        assert s["task"] == "open notepad"
        assert s["step"] == 0
        assert s["history"] == []
        assert s["startedAt"] == 1000.0
        assert s["source"] == "unknown"

    def test_loading_heartbeat_shows_elapsed_seconds(self):
        s = hud.reduce(
            queued(), {"event": "task_starting", "id": "t1", "elapsed": 12.5}, 2000
        )
        assert s["loadingFor"] == 12.5

    def test_started_moves_to_running(self):
        s = hud.reduce(queued(), {"event": "task_started", "id": "t1"}, 2000)
        assert s["phase"] == "running"

    def test_steps_accumulate_history(self):
        s = hud.reduce(queued(), {"event": "task_started", "id": "t1"}, 1500)
        s = hud.reduce(
            s,
            {
                "event": "step",
                "id": "t1",
                "step": 1,
                "thought": "find app",
                "action": "click",
            },
            3000,
        )
        assert s["step"] == 1
        assert s["thought"] == "find app"
        assert s["history"] == [
            {"step": 1, "thought": "find app", "action": "click", "at": 2000.0}
        ]

    def test_a_step_without_a_thought_keeps_the_last_one(self):
        s = hud.reduce(
            queued(), {"event": "step", "id": "t1", "step": 1, "thought": "a"}, 2000
        )
        s = hud.reduce(s, {"event": "step", "id": "t1", "step": 2}, 3000)
        assert s["thought"] == "a"

    def test_history_is_capped(self):
        s = queued()
        for i in range(hud.HISTORY_MAX + 10):
            s = hud.reduce(s, {"event": "step", "id": "t1", "step": i}, 2000 + i)
        assert len(s["history"]) == hud.HISTORY_MAX
        assert s["history"][-1]["step"] == hud.HISTORY_MAX + 9

    def test_done_records_the_outcome(self):
        s = hud.reduce(queued(), {"event": "step", "id": "t1", "step": 4}, 2000)
        s = hud.reduce(
            s,
            {"event": "task_done", "id": "t1", "status": "done", "summary": "Typed it"},
            9000,
        )
        assert s["phase"] == "done"
        assert s["status"] == "done"
        assert s["summary"] == "Typed it"
        assert s["steps"] == 4  # falls back to the last step seen
        assert s["finishedAt"] == 9000


class TestStopping:
    def test_stopping_marks_the_hud(self):
        s = hud.reduce(queued(), {"event": "stopping", "id": "t1"}, 2000)
        assert s["phase"] == "stopping"

    def test_a_step_during_stopping_stays_stopping(self):
        s = hud.reduce(queued(), {"event": "stopping", "id": "t1"}, 2000)
        s = hud.reduce(s, {"event": "step", "id": "t1", "step": 2}, 2100)
        assert s["phase"] == "stopping"

    def test_stopping_after_done_changes_nothing(self):
        s = hud.reduce(
            queued(), {"event": "task_done", "id": "t1", "status": "done"}, 2000
        )
        assert hud.reduce(s, {"event": "stopping", "id": "t1"}, 2100) is None


class TestIgnoredEvents:
    def test_events_for_another_task_are_ignored(self):
        s = queued("t1")
        for kind in ("task_starting", "task_started", "step", "task_done"):
            assert hud.reduce(s, {"event": kind, "id": "t2"}, 2000) is None

    def test_events_while_idle_are_ignored(self):
        for kind in ("task_started", "step", "stopping", "task_done"):
            assert hud.reduce(None, {"event": kind, "id": "t1"}, 2000) is None

    def test_loading_heartbeat_after_start_is_ignored(self):
        s = hud.reduce(queued(), {"event": "task_started", "id": "t1"}, 2000)
        assert (
            hud.reduce(s, {"event": "task_starting", "id": "t1", "elapsed": 3}, 2100)
            is None
        )

    def test_unknown_events_are_ignored(self):
        assert hud.reduce(queued(), {"event": "pong"}, 2000) is None
        assert hud.reduce(queued(), {}, 2000) is None

    def test_reduce_never_mutates_its_input(self):
        s = queued()
        before = dict(s)
        hud.reduce(s, {"event": "step", "id": "t1", "step": 1}, 2000)
        assert s == before


class TestProcessExit:
    def test_exit_mid_task_hides(self):
        s = hud.reduce(queued(), {"event": "process_exit"}, 2000)
        assert s == {"phase": "idle"}

    def test_exit_after_done_leaves_the_result_up(self):
        s = hud.reduce(
            queued(), {"event": "task_done", "id": "t1", "status": "done"}, 2000
        )
        assert hud.reduce(s, {"event": "process_exit"}, 2100) is None


class TestLinger:
    def done(self, at=10_000.0):
        return hud.reduce(
            queued(), {"event": "task_done", "id": "t1", "status": "done"}, at
        )

    def test_result_lingers_for_six_seconds(self):
        s = self.done()
        assert not hud.should_hide(s, 10_000 + 5_999)
        assert hud.should_hide(s, 10_000 + 6_000)

    def test_exit_animation_starts_just_before_hiding(self):
        s = self.done()
        assert not hud.is_leaving(s, 10_000 + 5_000)
        assert hud.is_leaving(s, 10_000 + 5_600)
        assert not hud.is_leaving(s, 10_000 + 6_000)

    def test_running_tasks_never_hide(self):
        assert not hud.should_hide(queued(), 10**12)
        assert not hud.is_leaving(queued(), 10**12)
