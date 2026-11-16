"""State machine for the computer-use HUD (Phase 2 of ``/STONIC_PORT_PLAN.md``).

A line-for-line port of the reducer in Stonic's
``electron/services/computer-overlay.js``. The HUD itself is drawn by
``sidecar/overlay_hud.py`` — a native Windows layered window, because it is the
only kind of window Windows reliably leaves *out* of screenshots
(``WDA_EXCLUDEFROMCAPTURE``). A webview overlay, Electron's or Tauri's, comes
out black in the agent's screenshots and blinds it; Stonic learned that the
hard way, which is why its HUD lives in Python.

This module only decides *what* the HUD shows. It is pure — no timers, no
process — so the supervisor drives it and the tests can check it directly.

Phases: ``idle`` → ``starting`` → ``running`` ⇄ ``stopping`` → ``done`` → (after
:data:`LINGER_S`) ``idle``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Keep the result on screen this long, then hide.
LINGER_S = 6.0
# Start the exit animation this long before hiding.
LEAVE_S = 0.45
# Kill an idle HUD process after this long without a task.
IDLE_S = 15 * 60.0
# Steps remembered for the HUD's details panel.
HISTORY_MAX = 40

IDLE: Dict[str, Any] = {"phase": "idle"}


def _other_task(state: Dict[str, Any], event: Dict[str, Any]) -> bool:
    """An event for a different task than the one on screen."""
    eid, sid = event.get("id"), state.get("id")
    return bool(eid and sid and eid != sid)


def reduce(
    state: Optional[Dict[str, Any]],
    event: Dict[str, Any],
    now_ms: float,
) -> Optional[Dict[str, Any]]:
    """Apply one sidecar/supervisor event to the HUD state.

    Returns the new state, or ``None`` when the event changes nothing (the
    caller then sends no update to the HUD process). ``now_ms`` is wall-clock
    milliseconds, matching the ``startedAt`` the HUD renders elapsed time from.
    """
    s = dict(state or IDLE)
    kind = (event or {}).get("event")
    phase = s.get("phase", "idle")

    if kind == "task_queued":
        return {
            "phase": "starting",
            "id": event.get("id"),
            "task": event.get("task"),
            "source": event.get("source") or "unknown",
            "maxSteps": event.get("max_steps"),
            "step": 0,
            "startedAt": now_ms,
            "history": [],
        }

    if kind == "task_starting":
        # The sidecar's heartbeat while the agent loads. Shown as a running
        # counter: without it a cold machine sat on "Waking up the agent…" for
        # minutes and looked hung.
        if phase != "starting" or _other_task(s, event):
            return None
        s["loadingFor"] = _number(event.get("elapsed"))
        return s

    if kind == "task_started":
        if phase == "idle" or _other_task(s, event):
            return None
        s["phase"] = "running"
        s["id"] = event.get("id") or s.get("id")
        if event.get("max_steps") is not None:
            s["maxSteps"] = event["max_steps"]
        return s

    if kind == "step":
        if phase == "idle" or _other_task(s, event):
            return None
        s["phase"] = "stopping" if phase == "stopping" else "running"
        s["step"] = event.get("step")
        if event.get("max_steps") is not None:
            s["maxSteps"] = event["max_steps"]
        s["thought"] = event.get("thought") or s.get("thought")
        s["action"] = event.get("action")
        started = s.get("startedAt") or now_ms
        history = list(s.get("history") or [])
        history.append(
            {
                "step": event.get("step"),
                "thought": event.get("thought") or "",
                "action": event.get("action") or "",
                "at": max(0, now_ms - started),
            }
        )
        s["history"] = history[-HISTORY_MAX:]
        return s

    if kind == "stopping":
        if phase in ("idle", "done"):
            return None
        s["phase"] = "stopping"
        return s

    if kind == "task_done":
        if phase == "idle" or _other_task(s, event):
            return None
        s["phase"] = "done"
        s["status"] = event.get("status")
        s["summary"] = event.get("summary")
        s["steps"] = (
            event.get("steps") if event.get("steps") is not None else s.get("step")
        )
        s["finishedAt"] = now_ms
        return s

    if kind == "process_exit":
        # `task_done` normally arrives first. If it did not, the process died
        # without finishing a task: just hide.
        if phase not in ("done", "idle"):
            return dict(IDLE)
        return None

    return None


def should_hide(state: Optional[Dict[str, Any]], now_ms: float) -> bool:
    """True once a finished task has lingered long enough."""
    if not state or state.get("phase") != "done":
        return False
    finished = state.get("finishedAt")
    return finished is not None and now_ms - finished >= LINGER_S * 1000


def is_leaving(state: Optional[Dict[str, Any]], now_ms: float) -> bool:
    """True during the exit animation just before hiding."""
    if not state or state.get("phase") != "done":
        return False
    finished = state.get("finishedAt")
    if finished is None:
        return False
    elapsed = now_ms - finished
    return (LINGER_S - LEAVE_S) * 1000 <= elapsed < LINGER_S * 1000


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
