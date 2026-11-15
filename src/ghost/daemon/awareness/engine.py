"""When to think, and when a thought may be spoken (Stonic's ``TFe``).

Ported from Stonic's awareness engine, recovered from its built renderer, with
its tunables unchanged. The eyes feed it events; it decides whether a thought
is worth the model's time, asks the model (:mod:`.brain`), and paces speech.

Pacing — so a presence does not become a nag:

* **Gates before thinking.** No thought during a call, while away (unless they
  just came back), while they watch something, while resting after "be
  quiet", while they are talking with the assistant, or when the daily thought
  cap is reached. A routine heartbeat on an unchanged screen is skipped.
* **A small token bucket for speech.** Up to 3 lines in a burst (2 at
  start); one refills every ``gap`` minutes. The gap starts at 10 min and
  adapts: an answered line shrinks it (×0.7), an ignored one grows it (×1.4),
  a line cut off doubles it and empties the bucket for 20 minutes. Never two
  lines within 90 s; at most 30 a day.
* **Follow-ups.** The model can say "look again in N minutes for X"; the
  engine comes back to it then (if they are there).

Speech in Ghost: Stonic handed the line to a live voice session. Ghost emits a
``speak`` event; the desktop app says it with Ghost's own voice and reports
back whether the user answered, ignored it, or cut it off
(:meth:`AwarenessEngine.report_outcome`). With no report within two minutes
the line counts as ignored.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import brain
from .moments import MomentDetector

logger = logging.getLogger(__name__)

MIN = 60_000

TUNING: Dict[str, Any] = {
    "heartbeatMs": 20 * MIN,
    "minBeatGapMs": 45_000,
    "momentSettleMs": 8_000,
    "switchSettleMs": 20_000,
    "switchGapMs": 2 * MIN,
    "unchangedHoldMs": 12 * MIN,
    "talkQuietMs": 90_000,
    "minAwayMin": 10,
    "burstMax": 3,
    "burstStart": 2,
    "minSpeakGapMs": 90_000,
    "gapBaseMs": 10 * MIN,
    "gapMinMs": 4 * MIN,
    "gapMaxMs": 60 * MIN,
    "gapOnAnswer": 0.7,
    "gapOnIgnore": 1.4,
    "gapOnDismiss": 2.0,
    "maxThoughtsPerDay": 300,
    "maxSpokenPerDay": 30,
}

ATTENTIVE_IDLE_S = 120
TALK_RECENT_MS = 5 * MIN
HEARTBEAT_RETRY_MS = MIN
HELLO_DELAY_MS = 90_000
HELLO_DELAY_FRESH_MS = 20_000
BACK_SETTLE_MS = 6_000
SWITCH_STALE_MS = 90_000
TALK_END_STALE_MS = 10 * MIN
OTHER_STALE_MS = 3 * MIN
FOLLOWUP_STALE_MS = 120 * MIN
GAP_DECAY_MS = 720 * MIN
THOUGHTS_KEEP_MS = 840 * MIN
JOURNAL_KEEP_MS = 1080 * 60 * MIN
JOURNAL_MAX = 40
DISMISS_REST_MS = 20 * MIN
DAY_CUT_MS = 300 * MIN
# How long a spoken line waits for the app's report before it counts as ignored.
OUTCOME_WAIT_MS = 2 * MIN
REPLY_MAX = 140

BACKOFF_MS = {
    "blocked": 30 * MIN,
    "no_key": 10 * MIN,
    "auth": 10 * MIN,
    "network": 3 * MIN,
    "upstream": 5 * MIN,
    "voice": 5 * MIN,
}

Think = Callable[[List[Dict[str, str]]], str]


def day_of(t_ms: float) -> str:
    d = datetime.fromtimestamp(t_ms / 1000) - timedelta(milliseconds=DAY_CUT_MS)
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def priority(trigger: Dict[str, Any]) -> int:
    kind = trigger.get("kind")
    return 1 if kind == "switch" else 2 if kind == "moment" else 3


class ThinkError(RuntimeError):
    """A failed thought. ``kind`` picks the back-off (see ``BACKOFF_MS``)."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind if kind in BACKOFF_MS else "upstream"


@dataclass
class Activity:
    """What the desktop app reports about the conversation with the assistant."""

    voice_open: bool = False
    speaking: bool = False
    last_user_at: float = 0.0
    recent_talk: Optional[List[Dict[str, str]]] = None


class AwarenessEngine:
    def __init__(
        self,
        directory: Path,
        *,
        snapshot: Callable[[], Dict[str, Any]],
        think: Think,
        now: Callable[[], float],
        log: Callable[[str, str, Any], Any] = lambda *a: None,
        emit: Callable[[Dict[str, Any]], None] = lambda e: None,
        character: Optional[Dict[str, Any]] = None,
        tuning: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.dir = Path(directory)
        self.snapshot_fn = snapshot
        self.think_fn = think
        self.now = now
        self._log = log
        self._emit = emit
        self.t = {**TUNING, **(tuning or {})}
        self.character = {"id": "ghost", "name": "Ghost", "prompt": "", "awareness": ""}
        self.character.update(character or {})
        self.activity = Activity()
        self.moments = MomentDetector()
        self.enabled = False
        self.was_on = False
        self.on_since = 0.0
        self.hello_done = False
        self.hello_delay = HELLO_DELAY_MS
        self.last_beat_at = 0.0
        self.last_spoke_at = 0.0
        self.last_thought_at = 0.0
        self.beating = False
        self.pending: Optional[Dict[str, Any]] = None
        self.backoff_until = 0.0
        self.backoff_reason: Optional[str] = None
        self.failures = 0
        self.call_hold: Optional[str] = None
        self.last_skip = ""
        self.mic_in_call_by: Optional[str] = None
        self.attentive = True
        self.talk_handled_at = 0.0
        self.previous: Optional[Dict[str, Any]] = None
        self.awaiting: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        start = now()
        self.thoughts: List[Dict[str, Any]] = self._load("thoughts.json", [])
        self.journal: List[Dict[str, Any]] = [
            n
            for n in self._load("journal.json", [])
            if isinstance(n, dict)
            and isinstance(n.get("text"), str)
            and n["text"].strip()
            and start - n.get("t", 0) < JOURNAL_KEEP_MS
        ][-JOURNAL_MAX:]
        self.state = self._restore_state(start)
        spoke = [x for x in self.thoughts if x.get("spoke")]
        self.last_spoke_at = spoke[-1]["t"] if spoke else 0.0

    # -- outward ---------------------------------------------------------------

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.enabled = bool(on)
            if not self.enabled and self.was_on:
                self._switch_off()

    def set_character(self, character: Dict[str, Any]) -> None:
        with self._lock:
            new = {
                **self.character,
                **{k: v for k, v in character.items() if v is not None},
            }
            changed = new.get("id") != self.character.get("id")
            self.character = new
            if changed and self.was_on:
                # A new character is chosen: it gets a first word as itself.
                self.pending = {
                    "trigger": {"kind": "hello", "chosen": True},
                    "at": self.now(),
                }
                self.last_spoke_at = 0
                self.state["tokens"] = max(self.state["tokens"], 1)
                self._persist()

    def set_activity(self, **fields: Any) -> None:
        with self._lock:
            for k, v in fields.items():
                if hasattr(self.activity, k) and v is not None:
                    setattr(self.activity, k, v)

    def quiet_for(self, minutes: float, why: str = "you asked") -> None:
        with self._lock:
            self._rest(max(1, minutes) * MIN, why)

    def on_event(self, ev: Dict[str, Any]) -> None:
        with self._lock:
            self._on_event(ev)

    def tick(self) -> None:
        with self._lock:
            self._tick()

    def report_outcome(self, t: float, outcome: str, reply: str = "") -> bool:
        """The app tells how a spoken line landed."""
        with self._lock:
            if outcome not in ("answered", "ignored", "dismissed"):
                return False
            if not self.awaiting or self.awaiting["t"] != t:
                return False
            self._settle(outcome, reply)
            return True

    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = self.now()
            capped = (
                "thoughts"
                if self.state["thoughtsToday"] >= self.t["maxThoughtsPerDay"]
                else "spoken"
                if self.state["spokenToday"] >= self.t["maxSpokenPerDay"]
                else None
            )
            mood = next(
                (x["mood"] for x in reversed(self.thoughts) if x.get("mood")), ""
            )
            return {
                "on": self._is_on(),
                "beating": self.beating,
                "lastBeatAt": self.last_beat_at,
                "lastSpokeAt": self.last_spoke_at,
                "backoffUntil": self.backoff_until,
                "backoffReason": self.backoff_reason,
                "callHold": self.call_hold,
                "restUntil": self.state["restUntil"],
                "restWhy": self.state["restWhy"],
                "gapMs": self.state["gapMs"],
                "tokens": round(self.state["tokens"] * 100) / 100,
                "rapport": self._rapport(),
                "followUp": dict(self.state["followUp"])
                if self.state["followUp"]
                else None,
                "thoughtsToday": self.state["thoughtsToday"],
                "spokenToday": self.state["spokenToday"],
                "capped": capped,
                "mood": mood,
                "label": status_label(self, now, capped),
                "thoughts": [
                    x for x in self.thoughts if x["t"] >= now - THOUGHTS_KEEP_MS
                ],
                "characterId": self.character.get("id"),
            }

    # -- events ----------------------------------------------------------------

    def _on_event(self, ev: Dict[str, Any]) -> None:
        if not ev:
            return
        now = self.now()
        kind = ev.get("type")
        if kind == "tick":
            if "micInCallBy" in ev:
                self.mic_in_call_by = ev.get("micInCallBy") or None
            if isinstance(ev.get("idleSeconds"), (int, float)):
                self.attentive = (
                    not ev.get("away")
                    and not ev.get("locked")
                    and ev["idleSeconds"] <= ATTENTIVE_IDLE_S
                )
            if (
                self.was_on
                and self._is_on()
                and isinstance(ev.get("stretchMinutes"), int)
            ):
                m = self.moments.on_tick(
                    now=now,
                    attentive=self.attentive,
                    stretch_minutes=ev["stretchMinutes"],
                    screen_awake=bool(ev.get("screenAwake")),
                    mic_in_call_by=self.mic_in_call_by,
                )
                if m:
                    self._offer({"kind": "moment", **m})
            self._tick()
            return
        if not self._is_on():
            return
        if kind == "focus":
            if ev.get("own"):
                if self.pending and self.pending["trigger"]["kind"] == "switch":
                    self.pending = None
                return
            m = self.moments.on_switch(now)
            self._offer({"kind": "moment", **m} if m else {"kind": "switch"})
            return
        if kind not in ("back", "unlock", "wake"):
            return
        away = max(0, round(float(ev.get("awayMinutes") or 0)))
        if away < self.t["minAwayMin"]:
            return
        self.attentive = True
        held = self.pending["trigger"] if self.pending else None
        held_away = held.get("awayMinutes", -1) if held else -1
        if away >= held_away:
            trig = {"kind": kind, "awayMinutes": away}
            if ev.get("watched"):
                trig["watched"] = True
            self.pending = {"trigger": trig, "at": now}
        self._tick()

    def _offer(self, trigger: Dict[str, Any]) -> None:
        if self.pending and priority(self.pending["trigger"]) > priority(trigger):
            return
        self.pending = {"trigger": trigger, "at": self.now()}

    # -- the clock -------------------------------------------------------------

    def _is_on(self) -> bool:
        return self.enabled

    def _tick(self) -> None:
        now = self.now()
        if not self._is_on():
            if self.was_on:
                self._switch_off()
            return
        self._roll_day(now)
        if not self.was_on:
            self.was_on = True
            self.on_since = now
            fresh = self.last_beat_at == 0
            self.hello_delay = HELLO_DELAY_FRESH_MS if fresh else HELLO_DELAY_MS
            self.hello_done = False
            self.last_beat_at = now
            self.talk_handled_at = now
            self.moments.reset()
            self._log("info", "Awareness on.", None)
        self._settle_overdue(now)
        self._refill(now)
        if self.beating or now < self.backoff_until:
            return
        if self.mic_in_call_by:
            self._hold_for_call(f"microphone in use by {self.mic_in_call_by}")
            return
        if self.call_hold and self.call_hold.startswith("microphone in use by"):
            self.call_hold = None
        if now < self.state["restUntil"]:
            self._note_skip(
                "resting — " + (self.state["restWhy"] or "asked to be quiet")
            )
            return
        if self.state["thoughtsToday"] >= self.t["maxThoughtsPerDay"]:
            self._note_skip("enough thinking for today")
            return
        if self._in_conversation(now):
            self._note_skip("they are talking with the assistant")
            return
        self._notice_talk_end()

        if self.pending:
            k = self.pending["trigger"]["kind"]
            stale = (
                SWITCH_STALE_MS
                if k == "switch"
                else TALK_END_STALE_MS
                if k == "moment" and self.pending["trigger"].get("what") == "talk_end"
                else OTHER_STALE_MS
            )
            if now - self.pending["at"] > stale:
                self.pending = None

        chosen: Optional[Dict[str, Any]] = None
        if self.pending:
            trig = self.pending["trigger"]
            is_switch = trig["kind"] == "switch"
            settle = (
                self.t["switchSettleMs"]
                if is_switch
                else HELLO_DELAY_FRESH_MS
                if trig["kind"] == "hello"
                else self.t["momentSettleMs"]
                if trig["kind"] == "moment"
                else BACK_SETTLE_MS
            )
            gap = self.t["switchGapMs"] if is_switch else self.t["minBeatGapMs"]
            if (
                now - self.pending["at"] >= settle
                and now - self.last_beat_at >= gap
                and (not is_switch or self._can_speak(now))
            ):
                chosen = trig
                self.pending = None
            elif not is_switch:
                return
        if not chosen:
            chosen = self._due_follow_up(now)
        if not chosen:
            if self.hello_done:
                if now - self.last_beat_at >= self.t["heartbeatMs"]:
                    chosen = {"kind": "routine"}
            elif now - self.on_since >= self.hello_delay:
                chosen = {"kind": "hello"}
        if chosen:
            self._beat(chosen)

    # -- one thought -----------------------------------------------------------

    def _beat(
        self, trigger: Dict[str, Any], force: bool = False
    ) -> Optional[Dict[str, Any]]:
        now = self.now()
        prev_beat = self.last_beat_at
        self.beating = True
        self.last_beat_at = now
        if trigger["kind"] != "switch":
            self.hello_done = True
        calls = 0
        try:
            s = self.snapshot_fn()
            if not s or not s.get("ok") or not s.get("running"):
                return None
            active = s.get("active")
            if not force:
                if s.get("locked"):
                    self._note_skip("the computer is locked")
                    return None
                came_back = trigger["kind"] in ("back", "unlock", "wake")
                talked = now - self.activity.last_user_at < TALK_RECENT_MS
                idle_long = (s.get("idleSeconds") or 0) > ATTENTIVE_IDLE_S
                if (
                    not came_back
                    and not talked
                    and (s.get("away") or (idle_long and not s.get("watching")))
                ):
                    self._retry_soon(now)
                    self._note_skip("nobody at the computer")
                    return None
                if not came_back and s.get("watching"):
                    self._retry_soon(now)
                    self._note_skip("they are watching something")
                    return None
                hold = brain.call_hold(s)
                if hold:
                    self._hold_for_call(hold)
                    return None
                self.call_hold = None
                if trigger["kind"] == "switch" and active and active.get("own"):
                    self.last_beat_at = prev_beat
                    return None
                same_screen = bool(self.previous and active) and (
                    self.previous["app"],
                    self.previous["title"],
                ) == (active.get("app"), active.get("title"))
                talked_since = self.activity.last_user_at > self.last_thought_at
                if (
                    trigger["kind"] in ("routine", "switch")
                    and same_screen
                    and not talked_since
                    and now - self.last_thought_at < self.t["unchangedHoldMs"]
                ):
                    self._note_skip("nothing new on the screen since the last thought")
                    return None

            first_today = self.state["lastThoughtDay"] != day_of(now)
            can_speak = force or self._can_speak(now, trigger)
            calls = 1
            self._mind({"phase": "thinking"})
            ctx = {
                "trigger": trigger,
                "snapshot": s,
                "thoughts": self._recent_thoughts(now),
                "previous": self.previous,
                "canSpeak": can_speak,
                "journal": self.journal,
                "rapport": self._rapport(),
                "firstToday": first_today,
                "characterName": self.character.get("name") or "Ghost",
                "characterPrompt": self.character.get("prompt") or "",
                "characterAwareness": self.character.get("awareness") or "",
                "userName": self.character.get("userName") or "",
                "voiceOpen": self.activity.voice_open,
                "recentTalk": self.activity.recent_talk or [],
                "lastTalkAt": self.activity.last_user_at,
            }
            # The model is slow; never hold the lock while waiting for it.
            self._lock.release()
            try:
                raw = self.think_fn(brain.build_messages(ctx))
            finally:
                self._lock.acquire()
            decision = brain.parse_decision(raw)
            self.failures = 0
            self.backoff_reason = None
            self.last_skip = ""
            where = (
                "a private window"
                if active and active.get("private")
                else f"{active['app']}"
                + (f" — {active['title']}" if active.get("title") else "")
                if active
                else "unknown"
            )
            trig_label = (
                f"moment:{trigger.get('what')}"
                if trigger["kind"] == "moment"
                else trigger["kind"]
            )
            info = {"trigger": trig_label, "window": where, "tookMs": self.now() - now}
            self.last_thought_at = self.now()
            self.previous = {
                "at": self.last_thought_at,
                "app": (active or {}).get("app", ""),
                "title": (active or {}).get("title", ""),
            }
            self.state["lastThoughtDay"] = day_of(now)
            if not self._is_on():
                return None
            if decision.get("note") and decision["note"] != brain.UNREADABLE:
                self._mind(
                    {
                        "phase": "thought",
                        "text": decision["note"],
                        "mood": decision.get("mood", ""),
                        "at": self.now(),
                    }
                )
            if decision.get("remember"):
                self._keep(decision["remember"])
            if decision.get("quietMinutes"):
                self.state["followUp"] = None
                self._rest(decision["quietMinutes"] * MIN, "they asked for quiet")
                self._remember(
                    {
                        "t": self.now(),
                        "spoke": False,
                        "note": decision.get("note") or "they asked me to stay quiet",
                        "mood": decision.get("mood"),
                    }
                )
                self._log(
                    "info",
                    f"Awareness: quiet for {decision['quietMinutes']} min.",
                    info,
                )
                return decision
            if decision.get("lookAgainMinutes"):
                why = (
                    decision.get("watchingFor")
                    or decision.get("note")
                    or "check on them"
                )
                at = self.now() + decision["lookAgainMinutes"] * MIN
                self.state["followUp"] = {
                    "at": at,
                    "why": why,
                    "setAt": self.now(),
                    "by": self.character.get("id"),
                }
                self._persist()
                self._log(
                    "info",
                    f"Awareness: will look again in {decision['lookAgainMinutes']} min.",
                    {"why": why},
                )
            if decision["action"] == "speak" and decision.get("say"):
                if not force and self._in_conversation(self.now()):
                    self._remember(
                        {
                            "t": self.now(),
                            "spoke": False,
                            "note": f'held back "{decision["say"]}" — they were already talking',
                            "mood": decision.get("mood"),
                        }
                    )
                    self._log(
                        "info",
                        "Awareness: wanted to speak but held back — they started talking.",
                        info,
                    )
                elif can_speak:
                    self._log(
                        "success",
                        "Awareness: speaking.",
                        {"say": decision["say"], **info},
                    )
                    self._speak(decision, trig_label)
                else:
                    self._remember(
                        {
                            "t": self.now(),
                            "spoke": False,
                            "note": f'held back "{decision["say"]}" — you had said enough for now',
                            "mood": decision.get("mood"),
                        }
                    )
                    self._log(
                        "info",
                        "Awareness: wanted to speak but held back — has said enough for now.",
                        info,
                    )
            else:
                self._remember(
                    {
                        "t": self.now(),
                        "spoke": False,
                        "note": decision.get("note") or "nothing worth saying",
                        "mood": decision.get("mood"),
                    }
                )
                self._log(
                    "info",
                    "Awareness: stayed quiet.",
                    {"note": decision.get("note"), **info},
                )
            return decision
        except ThinkError as exc:
            self._fail(exc.kind, str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - a thought must never crash the loop
            self._fail("upstream", str(exc))
            return None
        finally:
            self.beating = False
            if calls:
                self._mind({"phase": "idle"})
                self.state["thoughtsToday"] += calls
                self._persist()

    def beat_now(
        self, trigger: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Think right away, past the gates (Settings → "think now")."""
        with self._lock:
            if not self._is_on() or self.beating:
                return None
            return self._beat(trigger or {"kind": "routine"}, force=True)

    def _fail(self, kind: str, message: str) -> None:
        self.failures += 1
        base = BACKOFF_MS.get(kind, BACKOFF_MS["upstream"])
        wait = (
            min(30 * MIN, base * 2 ** min(4, self.failures - 1))
            if kind in ("network", "upstream")
            else base
        )
        self.backoff_until = self.now() + wait
        repeated = kind == "no_key" and self.backoff_reason == "no_key"
        self.backoff_reason = kind
        if not repeated:
            self._log(
                "error" if kind != "no_key" else "info",
                f"Awareness: thought failed ({kind}) — pausing {round(wait / MIN)} min.",
                message[:300],
            )

    # -- speech ----------------------------------------------------------------

    def _speak(self, decision: Dict[str, Any], trigger: str) -> None:
        now = self.now()
        self.last_spoke_at = now
        self._refill(now)
        self.state["tokens"] = max(0.0, self.state["tokens"] - 1)
        self.state["spokenToday"] += 1
        thought = {
            "t": now,
            "spoke": True,
            "say": decision["say"],
            "note": decision.get("note", ""),
            "mood": decision.get("mood"),
            "kind": decision.get("kind"),
        }
        self._remember(thought)
        self.awaiting = {"t": now, "owner": self.character.get("id")}
        self._persist()
        self._emit(
            {
                "type": "speak",
                "t": now,
                "say": decision["say"],
                "mood": decision.get("mood", ""),
                "kind": decision.get("kind", ""),
                "trigger": trigger,
                "character": self.character.get("name"),
            }
        )

    def _settle_overdue(self, now: float) -> None:
        if self.awaiting and now - self.awaiting["t"] >= OUTCOME_WAIT_MS:
            self._settle("ignored", "")

    def _settle(self, outcome: str, reply: str) -> None:
        a, self.awaiting = self.awaiting, None
        if not a:
            return
        now = self.now()
        for th in self.thoughts:
            if th.get("spoke") and th["t"] == a["t"]:
                th["outcome"] = outcome
                if outcome == "answered" and reply:
                    th["reply"] = brain.clip(reply, REPLY_MAX)
        self._save_thoughts()
        factor = (
            self.t["gapOnAnswer"]
            if outcome == "answered"
            else self.t["gapOnDismiss"]
            if outcome == "dismissed"
            else self.t["gapOnIgnore"]
        )
        self._refill(now)
        self.state["gapMs"] = self._clamp_gap(self.state["gapMs"] * factor)
        self.state["gapAt"] = now
        if outcome == "dismissed":
            self.state["tokens"] = 0
            self._rest(DISMISS_REST_MS, "cut off — giving them space")
        self._persist()
        self._log(
            "info",
            f"Awareness: the line was {outcome}.",
            {"nextRefillMin": round(self.state["gapMs"] / MIN)},
        )

    # -- pacing ----------------------------------------------------------------

    def _in_conversation(self, now: float) -> bool:
        a = self.activity
        if a.speaking:
            return True
        return a.voice_open and now - a.last_user_at < self.t["talkQuietMs"]

    def _notice_talk_end(self) -> None:
        last = self.activity.last_user_at
        if last and last > self.talk_handled_at:
            self.talk_handled_at = last
            self._offer({"kind": "moment", "what": "talk_end"})

    def _can_speak(self, now: float, trigger: Optional[Dict[str, Any]] = None) -> bool:
        if (
            trigger
            and trigger.get("kind") == "moment"
            and trigger.get("what") == "talk_end"
        ):
            return False
        if self.state["tokens"] < 1:
            return False
        if self.state["spokenToday"] >= self.t["maxSpokenPerDay"]:
            return False
        return (
            not self.last_spoke_at
            or now - self.last_spoke_at >= self.t["minSpeakGapMs"]
        )

    def _refill(self, now: float) -> None:
        s = self.state
        if now <= s["tokensAt"]:
            return
        s["tokens"] = min(
            self.t["burstMax"], s["tokens"] + (now - s["tokensAt"]) / s["gapMs"]
        )
        s["tokensAt"] = now

    def _clamp_gap(self, gap: float) -> float:
        return min(self.t["gapMaxMs"], max(self.t["gapMinMs"], gap))

    def _rapport(self) -> str:
        base = self.t["gapBaseMs"]
        if self.state["gapMs"] <= base * 0.8:
            return "warm"
        if self.state["gapMs"] >= base * 1.6:
            return "cool"
        return "neutral"

    def _rest(self, ms: float, why: str) -> None:
        self.state["restUntil"] = max(self.state["restUntil"], self.now() + ms)
        self.state["restWhy"] = why
        self._persist()

    def _due_follow_up(self, now: float) -> Optional[Dict[str, Any]]:
        f = self.state["followUp"]
        if not f or now < f["at"]:
            return None
        if now - f["at"] > FOLLOWUP_STALE_MS:
            self.state["followUp"] = None
            self._persist()
            return None
        if f.get("by") and f["by"] != self.character.get("id"):
            return None
        if not self.attentive or now - self.last_beat_at < self.t["minBeatGapMs"]:
            return None
        self.state["followUp"] = None
        self._persist()
        return {
            "kind": "followup",
            "why": f["why"],
            "minutes": max(1, round((now - f["setAt"]) / MIN)),
        }

    def _retry_soon(self, now: float) -> None:
        if self.t["heartbeatMs"] > HEARTBEAT_RETRY_MS:
            self.last_beat_at = now - self.t["heartbeatMs"] + HEARTBEAT_RETRY_MS

    def _roll_day(self, now: float) -> None:
        d = day_of(now)
        if self.state["day"] != d:
            self.state["day"] = d
            self.state["thoughtsToday"] = 0
            self.state["spokenToday"] = 0
            self._persist()

    def _hold_for_call(self, why: str) -> None:
        if not self.call_hold:
            self._log("info", f"Awareness: holding back — {why}.", None)
        self.call_hold = why
        self.last_skip = ""

    def _note_skip(self, why: str) -> None:
        if why != self.last_skip:
            self.last_skip = why
            self._log("info", f"Awareness: not thinking right now — {why}.", None)

    def _switch_off(self) -> None:
        self.was_on = False
        self.pending = None
        self.backoff_until = 0
        self.backoff_reason = None
        self.failures = 0
        self.call_hold = None
        self.last_skip = ""
        self._mind({"phase": "off"})

    def _mind(self, payload: Dict[str, Any]) -> None:
        self._emit({"type": "mind", **payload})

    # -- memory ----------------------------------------------------------------

    def _recent_thoughts(self, now: float) -> List[Dict[str, Any]]:
        return [x for x in self.thoughts if x["t"] >= now - THOUGHTS_KEEP_MS]

    def _remember(self, thought: Dict[str, Any]) -> None:
        both = [*self.thoughts, {k: v for k, v in thought.items() if v is not None}]
        said = [x for x in both if x.get("spoke")][-20:]
        quiet = [x for x in both if not x.get("spoke")][-15:]
        self.thoughts = sorted(said + quiet, key=lambda x: x["t"])
        self._save_thoughts()

    def _keep(self, text: str) -> None:
        line = " ".join(text.split())
        if not line or any(
            n["text"].lower().split() == line.lower().split() for n in self.journal
        ):
            return
        now = self.now()
        self.journal = [
            n
            for n in [*self.journal, {"t": now, "text": line}]
            if now - n["t"] < JOURNAL_KEEP_MS
        ][-JOURNAL_MAX:]
        self._store("journal.json", self.journal)
        self._log(
            "info", "Awareness: noted something to carry forward.", {"note": line}
        )

    def _save_thoughts(self) -> None:
        self._store("thoughts.json", self.thoughts)

    def _restore_state(self, now: float) -> Dict[str, Any]:
        raw = self._load("state.json", {})
        raw = raw if isinstance(raw, dict) else {}

        def num(key: str) -> float:
            try:
                return float(raw.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        gap = self._clamp_gap(num("gapMs") or self.t["gapBaseMs"])
        gap_at = num("gapAt")
        if 0 < gap_at <= now:
            # A cool day forgives itself: the gap drifts back toward the base.
            for _ in range(min(4, int((now - gap_at) // GAP_DECAY_MS))):
                gap = (gap + self.t["gapBaseMs"]) / 2
        tokens_at = num("tokensAt")
        has_tokens = "tokens" in raw and 0 < tokens_at <= now
        same_day = raw.get("day") == day_of(now)
        f = raw.get("followUp")
        follow = (
            {k: f[k] for k in ("at", "why", "setAt", "by") if k in f}
            if isinstance(f, dict)
            and isinstance(f.get("at"), (int, float))
            and isinstance(f.get("setAt"), (int, float))
            and isinstance(f.get("why"), str)
            and now - f["at"] < FOLLOWUP_STALE_MS
            else None
        )
        return {
            "gapMs": gap,
            "gapAt": gap_at or now,
            "tokens": min(self.t["burstMax"], max(0.0, num("tokens")))
            if has_tokens
            else float(self.t["burstStart"]),
            "tokensAt": tokens_at if has_tokens else now,
            "restUntil": num("restUntil"),
            "restWhy": raw.get("restWhy")
            if isinstance(raw.get("restWhy"), str)
            else "",
            "day": day_of(now),
            "thoughtsToday": max(0, int(num("thoughtsToday"))) if same_day else 0,
            "spokenToday": max(0, int(num("spokenToday"))) if same_day else 0,
            "lastThoughtDay": raw.get("lastThoughtDay")
            if isinstance(raw.get("lastThoughtDay"), str)
            else "",
            "followUp": follow,
        }

    def _persist(self) -> None:
        self._store("state.json", self.state)

    def _load(self, name: str, default: Any) -> Any:
        try:
            data = json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if isinstance(default, list):
            return (
                [
                    x
                    for x in data
                    if isinstance(x, dict) and isinstance(x.get("t"), (int, float))
                ]
                if isinstance(data, list)
                else default
            )
        return data

    def _store(self, name: str, data: Any) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / name
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.debug("awareness store %s failed: %s", name, exc)


def status_label(
    engine: AwarenessEngine, now: float, capped: Optional[str]
) -> Dict[str, str]:
    """Settings line: ``{tone, label}`` like Stonic's status badge."""
    if not engine._is_on():
        return {"tone": "off", "label": "Off"}
    if engine.call_hold:
        return {"tone": "hold", "label": "Quiet while you are on a call"}
    if engine.backoff_reason == "no_key" and now < engine.backoff_until:
        return {"tone": "hold", "label": "Quiet — no model is set up"}
    if now < engine.backoff_until:
        mins = max(1, round((engine.backoff_until - now) / MIN))
        return {"tone": "hold", "label": f"Paused — trying again in {mins} min"}
    if now < engine.state["restUntil"]:
        mins = max(1, round((engine.state["restUntil"] - now) / MIN))
        return {"tone": "rest", "label": f"Resting — quiet for {mins} min"}
    if capped == "spoken":
        return {
            "tone": "rest",
            "label": "Has said enough for today — quiet until tomorrow",
        }
    if capped == "thoughts":
        return {"tone": "rest", "label": "Done for today — back tomorrow"}
    if engine.beating:
        return {"tone": "live", "label": "Thinking…"}
    return {"tone": "live", "label": "Watching quietly"}
