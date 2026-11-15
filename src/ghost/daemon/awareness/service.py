"""Runs awareness in the backend: eyes + engine + an outbox for the app.

Two switches, both off by default:

* ``[stonic].awareness`` (config.toml) makes the feature *available*;
* the user turns it *on* in Settings (saved in ``<config>/awareness/settings.json``).

Signals arrive from the desktop shell's sensor (``POST /v1/awareness/signals``)
— the backend never reads the screen, the camera or the microphone itself.
The model is Ghost's own configured engine: thoughts go wherever the user's
chats go (a local model stays local).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .engine import AwarenessEngine, ThinkError
from .eyes import AwarenessEyes
from .timeline import now_ms

logger = logging.getLogger(__name__)

LOOP_S = 1.0


class AwarenessService:
    def __init__(
        self,
        directory: Path,
        think: Callable[[List[Dict[str, str]]], str],
        *,
        now: Callable[[], float] = now_ms,
    ) -> None:
        self.dir = Path(directory)
        self.now = now
        self._outbox: List[Dict[str, Any]] = []
        self._seq = 0
        self._out_lock = threading.Lock()
        self.eyes = AwarenessEyes(self.dir, now=now)
        self.engine = AwarenessEngine(
            self.dir,
            snapshot=self.eyes.snapshot,
            think=think,
            now=now,
            log=self.eyes.write_log,
            emit=self._post,
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._eye_seq = 0
        self.enabled = bool(self._settings().get("enabled"))
        if self.enabled:
            self._switch(True)

    # -- settings --------------------------------------------------------------

    def _settings(self) -> Dict[str, Any]:
        try:
            data = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def configure(
        self, enabled: Optional[bool] = None, character: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if character:
            self.engine.set_character(
                {
                    k: str(character[k])[:3000]
                    for k in ("id", "name", "prompt", "awareness", "userName")
                    if character.get(k)
                }
            )
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                (self.dir / "settings.json").write_text(
                    json.dumps({"enabled": self.enabled}), encoding="utf-8"
                )
            except OSError as exc:
                logger.warning("awareness settings not saved: %s", exc)
            self._switch(self.enabled)
        return self.status()

    def _switch(self, on: bool) -> None:
        self.eyes.configure(on)
        self.engine.set_enabled(on)
        if on:
            self.start()
        else:
            self.stop()

    # -- loop ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ghost-awareness", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        self.stop()
        self.eyes.flush()

    def _run(self) -> None:
        while not self._stop.wait(LOOP_S):
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.warning("awareness step failed: %s", exc)

    def step(self) -> None:
        """One loop pass. Public so tests drive it without a thread."""
        self.eyes.pump()
        for ev in self.eyes.events(self._eye_seq):
            self._eye_seq = ev["seq"]
            self.engine.on_event(ev)

    # -- inputs from the app ---------------------------------------------------

    def ingest(self, sample: Dict[str, Any]) -> None:
        if self.enabled:
            self.eyes.ingest(sample)

    def activity(self, body: Dict[str, Any]) -> None:
        talk = body.get("recentTalk")
        self.engine.set_activity(
            voice_open=_bool(body.get("voiceOpen")),
            speaking=_bool(body.get("speaking")),
            last_user_at=_num(body.get("lastUserAt")),
            recent_talk=[
                {
                    "role": "user" if m.get("role") == "user" else "assistant",
                    "text": str(m.get("text", ""))[:400],
                }
                for m in talk[-8:]
                if isinstance(m, dict)
            ]
            if isinstance(talk, list)
            else None,
        )

    def forget(self) -> Dict[str, Any]:
        """Delete the diary, thoughts and notes. The on/off setting stays."""
        was = self.enabled
        if was:
            self._switch(False)
        for name in (
            "diary.json",
            "thoughts.json",
            "journal.json",
            "state.json",
            "awareness.log",
        ):
            try:
                (self.dir / name).unlink(missing_ok=True)
            except OSError:
                pass
        self.eyes.timeline.entries = []
        self.eyes.ledger.days = {}
        self.engine.thoughts = []
        self.engine.journal = []
        if was:
            self._switch(True)
        return self.status()

    # -- outputs ---------------------------------------------------------------

    def _post(self, event: Dict[str, Any]) -> None:
        with self._out_lock:
            self._seq += 1
            self._outbox.append({**event, "seq": self._seq})
            del self._outbox[:-50]

    def events(self, after: int = 0) -> Dict[str, Any]:
        with self._out_lock:
            return {
                "events": [e for e in self._outbox if e["seq"] > after],
                "last": self._seq,
            }

    def status(self) -> Dict[str, Any]:
        return {
            "available": True,
            "enabled": self.enabled,
            "eyes": self.eyes.status(),
            "engine": self.engine.status(),
        }


def make_think(get_engine: Callable[[], Any], get_model: Callable[[], str]):
    """A ``think`` function over Ghost's own inference engine."""

    def think(messages: List[Dict[str, str]]) -> str:
        from ghost.core.types import Message, Role  # noqa: PLC0415

        engine = get_engine()
        model = get_model()
        if engine is None or not model:
            raise ThinkError("no_key", "no model is set up")
        started = time.monotonic()
        try:
            result = engine.generate(
                [Message(role=Role(m["role"]), content=m["content"]) for m in messages],
                model=model,
                temperature=0.8,
                max_tokens=400,
            )
        except Exception as exc:  # noqa: BLE001 - classify for the back-off
            text = str(exc).lower()
            if (
                "401" in text
                or "403" in text
                or "api key" in text
                or "unauthor" in text
            ):
                raise ThinkError("auth", str(exc)) from exc
            if "connect" in text or "timeout" in text or "network" in text:
                raise ThinkError("network", str(exc)) from exc
            raise ThinkError("upstream", str(exc)) from exc
        logger.debug("awareness thought took %.1fs", time.monotonic() - started)
        content = result.get("content") if isinstance(result, dict) else result
        return str(content or "")

    return think


_service: Optional[AwarenessService] = None
_lock = threading.Lock()


def get_service() -> Optional[AwarenessService]:
    return _service


def ensure_service(app_state: Any) -> AwarenessService:
    global _service
    with _lock:
        if _service is None:
            from ghost.core.paths import get_config_dir  # noqa: PLC0415

            _service = AwarenessService(
                get_config_dir() / "awareness",
                make_think(
                    lambda: getattr(app_state, "engine", None),
                    lambda: str(getattr(app_state, "model", "") or ""),
                ),
            )
        return _service


def reset_for_tests() -> None:
    global _service
    with _lock:
        if _service is not None:
            _service.shutdown()
        _service = None


def wipe(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
