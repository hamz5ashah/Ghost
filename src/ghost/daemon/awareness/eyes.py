"""Awareness "eyes" — knowing what is happening on the PC (Stonic port, Phase 5).

Ported from Stonic's ``awareness-manager.js``. Stonic's presence had three
parts; Ghost keeps the same split:

    eyes   → this module. Turns signals into state and writes the diary. No
             model, no network — all on this machine, free.
    brain  → :mod:`ghost.daemon.awareness.engine`. Reads the diary and asks
             the model: stay quiet, or say something?
    voice  → the desktop app speaks the line (Ghost's own TTS).

Signals and what they cost
--------------------------

Stonic read idle/lock/sleep/battery from Electron's ``powerMonitor`` and the
front window, the microphone and "is something keeping the screen awake" from
a PowerShell watcher. Ghost reads all of them natively in the desktop shell
(``src-tauri/src/awareness_sensor.rs``), which posts a sample every couple of
seconds to :meth:`AwarenessEyes.ingest`. If no sensor is running (another
OS, an older shell) awareness does not stop — it only lacks the window name.

"Watching a video" is not "walked away"
---------------------------------------

Windows counts no mouse/keyboard as idle even when the user is sitting there
watching a film. That used to read as "they left", and the moment the mouse
moved the assistant asked "back already?" when they never went anywhere. The
sensor reports when an app keeps the screen awake (video, presentation): the
user is then "watching" — neither away nor to be interrupted.

Only when the setting is on
---------------------------

Nothing runs with awareness off: no diary, no polling. It is off by default,
and turning it off also deletes a stale diary from disk.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .timeline import KEEP_MS, OWN_APP, DayLedger, Timeline, now_ms, sanitize_focus

logger = logging.getLogger(__name__)

POLL_MS = 5 * 1000
TICK_MS = 10 * 1000
# No input for this long = stepped away.
AWAY_AFTER_S = 180
# No input but something is playing: after this long, "watching".
WATCH_AFTER_S = 120
# Even a long film ends: after this long without input, assume asleep or gone
# (autoplay runs all night).
WATCH_MAX_IDLE_S = 3 * 60 * 60
# On return: if the screen was kept awake for this share of the idle time,
# they were watching.
WATCHED_SHARE = 0.6
# After an absence this long, the "working without a break" count restarts.
LONG_AWAY_MS = 5 * 60 * 1000
SAVE_DEBOUNCE_MS = 30 * 1000
LEDGER_SAVE_MS = 2 * 60 * 1000
# A sensor silent for this long is gone: its window/mic/screen signals expire.
SENSOR_STALE_MS = 15 * 1000
LOG_MAX_BYTES = 1024 * 1024

# Only these apps count as "a call" when they hold the microphone — messengers,
# meeting apps and browsers (Meet, Zoom web, WhatsApp web). Not every app with
# the mic: going quiet while the user records with OBS or dictates would be
# wrong.
CALL_APPS = re.compile(
    r"zoom|teams|webex|skype|slack|discord|whatsapp|telegram|signal|viber|"
    r"facetime|chrome|msedge|firefox|brave|opera|vivaldi|comet",
    re.I,
)
OWN_PROCESS = re.compile(r"^ghost(-desktop)?(\.exe)?$|ghost_desktop", re.I)
OWN_APP_LABEL = f"{OWN_APP} (this app)"

Emit = Callable[[Dict[str, Any]], None]


def call_app_from_mic(mic_list: Any) -> Optional[str]:
    """Pick a call app out of the sensor's ``a|b|c`` microphone list. Pure."""
    if not isinstance(mic_list, str):
        return None
    for name in (part.strip() for part in mic_list.split("|")):
        if not name or OWN_PROCESS.search(name):
            continue
        if CALL_APPS.search(name):
            name = re.sub(r"\.exe$", "", name, flags=re.I)
            return re.sub(r"_[a-z0-9]{10,}$", "", name, flags=re.I)
    return None


class AwarenessEyes:
    def __init__(
        self,
        directory: Path,
        *,
        now: Callable[[], float] = now_ms,
        emit: Optional[Emit] = None,
    ) -> None:
        self.dir = Path(directory)
        self.diary_path = self.dir / "diary.json"
        self.log_path = self.dir / "awareness.log"
        self.now = now
        self.emit_hook = emit
        self.timeline = Timeline()
        self.ledger = DayLedger()
        self.running = False
        self._lock = threading.RLock()
        self._boot_at = now()
        self._start_logged = False
        self._active_since = self._boot_at
        self._away = False
        self._away_since = 0.0
        self._locked = False
        self._focus: Optional[Dict[str, Any]] = None
        self._mic_in_call_by: Optional[str] = None
        self._screen_awake = False
        self._idle_ticks = 0
        self._idle_awake_ticks = 0
        self._last_poll_at = 0.0
        self._last_tick_at = 0.0
        self._ledger_saved_at = 0.0
        self._dirty = False
        self._dirty_since = 0.0
        self._sample: Optional[Dict[str, Any]] = None
        self._sample_at = 0.0
        self._battery: Optional[Dict[str, Any]] = None
        self._events: List[Dict[str, Any]] = []
        self._seq = 0

    # -- on / off --------------------------------------------------------------

    def configure(self, enabled: bool) -> Dict[str, Any]:
        if enabled:
            self.start()
        else:
            self.stop()
            self._drop_stale_diary()
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "running": self.running,
            "focusWatcher": self._sensor_alive(),
        }

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self.running = True
            now = self.now()
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                saved = json.loads(self.diary_path.read_text(encoding="utf-8"))
                self.timeline.load(saved.get("entries"), now)
                self.ledger.load(saved.get("days"), now)
            except (OSError, ValueError, AttributeError):
                pass  # first time, or a broken file — start an empty diary
            # One "opened" per process, however often the setting flips.
            if not self._start_logged:
                self._start_logged = True
                self.timeline.add({"t": self._boot_at, "kind": "start"})
            self._active_since = now
            self._away = False
            self._last_poll_at = now
            self._last_tick_at = now
            self._idle_ticks = self._idle_awake_ticks = 0
            self._mark_dirty()

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            self.running = False
            self._focus = None
            self._mic_in_call_by = None
            self._screen_awake = False
            self._dirty = True
            self._save()

    def _drop_stale_diary(self) -> None:
        """Awareness is off and the diary is old — delete it.

        Turning it off means "do not watch me". Leaving yesterday's window
        names in a file afterwards breaks that promise. The 18-hour grace is so
        that switching it off for a meeting does not lose the day.
        """
        try:
            age = self.now() / 1000 - self.diary_path.stat().st_mtime
            if age * 1000 >= KEEP_MS:
                self.diary_path.unlink(missing_ok=True)
        except OSError:
            pass

    # -- signals from the desktop shell ----------------------------------------

    def ingest(self, sample: Dict[str, Any]) -> None:
        """One sensor sample. Lock/unlock and sleep/wake are derived here."""
        with self._lock:
            if not self.running or not isinstance(sample, dict):
                return
            now = self.now()
            slept = _num(sample.get("sleptMs"))
            if slept and slept > 0:
                self._on_suspend(now - slept)
                self._on_resume(now)
            locked = bool(sample.get("locked"))
            if locked and not self._locked:
                self._on_lock(now)
            elif not locked and self._locked:
                self._on_unlock(now)
            if isinstance(sample.get("onBattery"), bool):
                level = _num(sample.get("batteryLevel"))
                self._battery = {
                    "level": None if level is None else max(0.0, min(1.0, level)),
                    "charging": not sample["onBattery"],
                }
            self._sample = sample
            self._sample_at = now
            if "app" in sample or "mic" in sample or "display" in sample:
                self._on_focus(sample, now)
            self.pump(now)

    def pump(self, now: Optional[float] = None) -> None:
        """Run the poll/tick clocks. Called by ingest and by the runner."""
        with self._lock:
            if not self.running:
                return
            now = self.now() if now is None else now
            if not self._sensor_alive(now):
                # Without the sensor these signals are stale — no false "watching".
                self._screen_awake = False
                self._mic_in_call_by = None
            if now - self._last_poll_at >= POLL_MS:
                self._poll(now)
            if now - self._last_tick_at >= TICK_MS:
                self._last_tick_at = now
                self._tick(now)
            if self._dirty and now - self._dirty_since >= SAVE_DEBOUNCE_MS:
                self._save()

    def _sensor_alive(self, now: Optional[float] = None) -> bool:
        now = self.now() if now is None else now
        return self._sample is not None and now - self._sample_at < SENSOR_STALE_MS

    def idle_seconds(self, now: Optional[float] = None) -> float:
        now = self.now() if now is None else now
        if not self._sample:
            return 0.0
        base = _num(self._sample.get("idleSeconds")) or 0.0
        # Idle keeps growing between samples; never extrapolate past staleness.
        return base + min(now - self._sample_at, SENSOR_STALE_MS) / 1000

    def _watching(self, idle: float) -> bool:
        return self._screen_awake and WATCH_AFTER_S <= idle < WATCH_MAX_IDLE_S

    def _tick(self, now: float) -> None:
        idle = self.idle_seconds(now)
        self._emit(
            {
                "type": "tick",
                "idleSeconds": round(idle),
                "away": self._away,
                "locked": self._locked,
                "watching": self._watching(idle),
                "micInCallBy": self._mic_in_call_by,
                "screenAwake": self._screen_awake,
                "stretchMinutes": max(0, round((now - self._active_since) / 60000)),
            },
            now,
        )

    def _poll(self, now: float) -> None:
        idle = self.idle_seconds(now)
        # One poll after sleep or a stuck clock must not count as hours.
        delta = max(0.0, min(now - (self._last_poll_at or now), 3 * POLL_MS))
        self._last_poll_at = now

        if idle >= AWAY_AFTER_S:
            self._idle_ticks += 1
            if self._screen_awake:
                self._idle_awake_ticks += 1
        elif not self._away:
            self._idle_ticks = self._idle_awake_ticks = 0

        # Something playing on the screen: idle is not "away" — up to a point.
        watching = self._screen_awake and idle < WATCH_MAX_IDLE_S
        if not self._away and idle >= AWAY_AFTER_S and not watching:
            self._away = True
            # A video just stopped: they were watching until now; absent from now.
            self._away_since = now if self._watched_while_idle() else now - idle * 1000
            self.timeline.add({"t": self._away_since, "kind": "away"})
            self._mark_dirty()
        elif self._away and idle < 10 and not self._locked:
            self._come_back(now, "back")

        if not self._locked and not self._away and (idle < AWAY_AFTER_S or watching):
            app = ""
            if self._focus:
                app = OWN_APP if self._focus.get("own") else self._focus["app"]
            self.ledger.note(now, app, delta)
            if now - self._ledger_saved_at >= LEDGER_SAVE_MS:
                self._ledger_saved_at = now
                self._mark_dirty()
        if self.timeline.prune(now):
            self._mark_dirty()

    def _watched_while_idle(self) -> bool:
        return (
            self._idle_ticks > 0
            and self._idle_awake_ticks / self._idle_ticks >= WATCHED_SHARE
        )

    def _come_back(self, now: float, kind: str) -> None:
        away_ms = now - self._away_since if self._away_since else 0
        mins = max(0, round(away_ms / 60000))
        watched = self._watched_while_idle()
        self._away = False
        self._away_since = 0
        self._idle_ticks = self._idle_awake_ticks = 0
        self.timeline.add({"t": now, "kind": "back", "mins": mins})
        if away_ms >= LONG_AWAY_MS:
            self._active_since = now
        self._mark_dirty()
        self._emit({"type": kind, "awayMinutes": mins, "watched": watched}, now)

    def _on_lock(self, now: float) -> None:
        self._locked = True
        self.timeline.add({"t": now, "kind": "lock"})
        self._mark_dirty()

    def _on_unlock(self, now: float) -> None:
        self._locked = False
        self.timeline.add({"t": now, "kind": "unlock"})
        # The idle clock keeps running while locked; unlocking is the return.
        if self._away:
            self._come_back(now, "unlock")
        else:
            self._emit({"type": "unlock", "awayMinutes": 0}, now)
        self._mark_dirty()

    def _on_suspend(self, at: float) -> None:
        self.timeline.add({"t": at, "kind": "sleep"})
        self._suspended_at = at

    def _on_resume(self, now: float) -> None:
        slept = now - getattr(self, "_suspended_at", now)
        mins = max(0, round(slept / 60000))
        self.timeline.add({"t": now, "kind": "wake", "mins": mins})
        if slept >= LONG_AWAY_MS:
            self._active_since = now
        # After sleep, an old "away" means nothing — waking is the return.
        self._away = False
        self._away_since = 0
        self._idle_ticks = self._idle_awake_ticks = 0
        self._last_poll_at = now
        self._mark_dirty()
        self._emit({"type": "wake", "awayMinutes": mins}, now)

    def _on_focus(self, raw: Dict[str, Any], now: float) -> None:
        if "mic" in raw:
            self._mic_in_call_by = call_app_from_mic(raw.get("mic"))
        if isinstance(raw.get("display"), bool):
            self._screen_awake = raw["display"]
        if "app" not in raw and "own" not in raw:
            return
        own = bool(raw.get("own"))
        focus = (
            {"app": OWN_APP_LABEL, "title": "", "private": False}
            if own
            else sanitize_focus(raw.get("app"), raw.get("title"))
        )
        same_app = bool(self._focus) and self._focus["app"] == focus["app"]
        changed = not same_app or self._focus["title"] != focus["title"]
        since = self._focus["since"] if same_app else now
        self._focus = {**focus, "own": own, "since": since}
        if self.timeline.note_focus(focus, now):
            self._mark_dirty()
        # The engine thinks on this (it gives its own settle time). The title
        # is not sent here — the engine reads it from the snapshot.
        if changed:
            self._emit({"type": "focus", "own": own, "appChanged": not same_app}, now)

    # -- for the brain and the UI ----------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self.now()
            idle = self.idle_seconds(now)
            running = self.running
            return {
                "ok": True,
                "running": running,
                "now": now,
                "idleSeconds": round(idle),
                "away": self._away,
                "locked": self._locked,
                "watching": self._watching(idle),
                "onBattery": bool(self._battery and not self._battery["charging"]),
                "battery": dict(self._battery) if self._battery else None,
                "active": dict(self._focus) if self._focus else None,
                "micInCallBy": self._mic_in_call_by,
                "focusWatcher": self._sensor_alive(now),
                "activeStretchMinutes": max(
                    0, round((now - self._active_since) / 60000)
                ),
                "appOpenMinutes": max(0, round((now - self._boot_at) / 60000)),
                "digest": self.timeline.digest(now) if running else [],
                "today": self.ledger.today(now) if running else None,
                "todayApps": self.ledger.top_apps(now) if running else [],
                "recentDays": self.ledger.lines(now) if running else [],
            }

    def events(self, after: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["seq"] > after]

    def write_log(self, kind: str, message: str, details: Any = None) -> bool:
        """The brain's decisions, one line each, in ``awareness/awareness.log``.

        Awareness runs in the background; without this file "why did it not
        speak?" has no answer. At 1 MB the file rolls to ``.old``.
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            try:
                if self.log_path.stat().st_size > LOG_MAX_BYTES:
                    os.replace(self.log_path, self.log_path.with_suffix(".log.old"))
            except OSError:
                pass
            extra = ""
            if details is not None:
                extra = (
                    details
                    if isinstance(details, str)
                    else json.dumps(details, default=str)
                )
                extra = " | " + re.sub(r"\s+", " ", extra)[:600]
            from datetime import datetime, timezone  # noqa: PLC0415

            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            line = (
                f"{stamp} [{str(kind or 'info')}] {str(message or '')[:300]}{extra}\n"
            )
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except OSError:
            return False

    # -- internals -------------------------------------------------------------

    def _emit(self, payload: Dict[str, Any], now: float) -> None:
        self._seq += 1
        event = {**payload, "at": now, "seq": self._seq}
        self._events.append(event)
        del self._events[:-100]
        if self.emit_hook is not None:
            try:
                self.emit_hook(event)
            except Exception as exc:  # noqa: BLE001
                logger.debug("awareness emit hook failed: %s", exc)

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty_since = self.now()
        self._dirty = True

    def _save(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            data = {
                "savedAt": self.now(),
                "entries": self.timeline.to_json(),
                "days": self.ledger.to_json(),
            }
            tmp = self.diary_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.diary_path)
        except OSError as exc:
            logger.warning("Awareness diary not saved: %s", exc)

    def flush(self) -> None:
        with self._lock:
            self._save()


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
