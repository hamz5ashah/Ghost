"""Always-on wake word for the realtime voice engine (Stonic port, Phase 3).

Ported from Stonic's ``wake_word_bg.py``. The matching logic is kept:

* the assistant answers to the **first word** of its name, because people call
  each other by first name;
* names under three letters are dropped, since a two-letter word turns up in
  ordinary speech so often that the detector would fire all day;
* names of four letters or more get a little slack: speech-to-text drops or
  adds a letter at the end of a name ("ayesha" → "ayeshaa", "aisha") often
  enough that it pays, and on a name that long a near miss is unlikely to be
  some other word.

Two things change on the way into Ghost:

1. **Listening stays on the device.** Stonic sent every phrase it heard to
   Google's free speech API (``recognize_google``). Here each phrase goes to
   Ghost's own faster-whisper backend, the same one Ghost already uses for
   dictation, so nothing leaves the machine until the user is actually talking
   to the assistant.
2. **No Stonic catch-all.** Stonic also fired on any word ending in "nic"/"nick"
   (its own name is often misheard), which would make Ghost wake on "picnic"
   and "clinic". The fixed default name is "Ghost" plus a few common
   mishearings of it, and the user's ``[stonic].wake_word_name``.

Off unless both ``[stonic].realtime_voice`` and ``[stonic].wake_word`` are on.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_NAME = "Ghost"

# Common ways Whisper writes "Ghost" when said quickly or with an accent.
GHOST_VARIANTS: FrozenSet[str] = frozenset(
    {"ghost", "ghosts", "gost", "goast", "ghosty"}
)

# Whisper punctuates ("Ghost, what's the time?"), Google's API did not, so
# words are cut on anything that is not a letter, digit or apostrophe.
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)

# A phrase this long is a monologue, not someone calling the assistant.
MAX_PHRASE_SECONDS = 8.0


def first_name(name: str) -> str:
    """The first word of a name, lower-cased; empty if under three letters."""
    parts = (name or "").strip().split()
    first = parts[0].lower() if parts else ""
    return first if len(first) >= 3 else ""


def wake_names(name: str = "") -> FrozenSet[str]:
    """Every name the assistant answers to."""
    names = set(GHOST_VARIANTS)
    custom = first_name(name)
    if custom:
        names.add(custom)
    return frozenset(names)


def _near(word: str, name: str) -> bool:
    if word == name:
        return True
    return (
        len(name) >= 4
        and abs(len(word) - len(name)) <= 2
        and (word.startswith(name[:4]) or name.startswith(word[:4]))
    )


def words_of(text: str) -> List[str]:
    return [m.group(0).lower() for m in _WORD.finditer(text or "")]


@dataclass(frozen=True)
class WakeHit:
    trigger: str
    text: str
    # Whatever followed the name: "Ghost, open my calendar" → "open my calendar".
    command: str

    def to_dict(self) -> Dict[str, str]:
        return {"trigger": self.trigger, "text": self.text, "command": self.command}


class WakeWordMatcher:
    def __init__(self, name: str = "") -> None:
        self.name = (name or "").strip() or DEFAULT_NAME
        self.names = wake_names(self.name)

    def find_trigger(self, words: Iterable[str]) -> Optional[str]:
        for word in words:
            if any(_near(word, name) for name in self.names):
                return word
        return None

    def match(self, text: str) -> Optional[WakeHit]:
        words = words_of(text)
        trigger = self.find_trigger(words)
        if trigger is None:
            return None
        index = words.index(trigger)
        return WakeHit(
            trigger=trigger,
            text=(text or "").strip(),
            command=" ".join(words[index + 1 :]),
        )


# A callable that records one phrase as WAV bytes, or b"" for silence.
Recorder = Callable[[], bytes]


def default_recorder() -> bytes:
    from ghost.speech.voice_io import record_until_silence  # noqa: PLC0415

    return record_until_silence(
        silence_seconds=0.7,
        startup_silence_seconds=2.0,
        max_seconds=MAX_PHRASE_SECONDS,
    )


class WakeWordListener:
    """Background thread: record a phrase, transcribe locally, match the name.

    Events are kept in a short numbered log that the UI polls with ``after``,
    which survives a missed poll without needing a socket.
    """

    def __init__(
        self,
        backend: Any,
        name: str = "",
        *,
        recorder: Recorder = default_recorder,
        model_size: str = "base",
        on_hit: Optional[Callable[[WakeHit], None]] = None,
    ) -> None:
        self.matcher = WakeWordMatcher(name)
        self._backend = backend
        self._recorder = recorder
        self._model_size = model_size
        self._on_hit = on_hit
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._events: List[Dict[str, Any]] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.error: str = ""

    # -- control ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.error = ""
        self._thread = threading.Thread(
            target=self._loop, name="ghost-wake-word", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def pause(self) -> None:
        """Stop matching while a realtime session owns the conversation."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # -- events ----------------------------------------------------------------

    def _emit(self, kind: str, **data: Any) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(
                {"seq": self._seq, "type": kind, "at": time.time(), **data}
            )
            del self._events[:-50]

    def events(self, after: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["seq"] > after]

    def describe(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "paused": self.paused,
            "name": self.matcher.name,
            "answers_to": sorted(self.matcher.names),
            "error": self.error,
            "last_seq": self._seq,
        }

    # -- work ------------------------------------------------------------------

    def hear(self, audio: bytes) -> Optional[WakeHit]:
        """Transcribe one phrase and match it. Public so tests can drive it."""
        if not audio or len(audio) < 4000:  # under ~0.1 s of 16 kHz audio
            return None
        result = self._backend.transcribe(
            audio, format="wav", model_size=self._model_size
        )
        hit = self.matcher.match(getattr(result, "text", "") or "")
        if hit is not None:
            self._emit("wake", **hit.to_dict())
            if self._on_hit is not None:
                try:
                    self._on_hit(hit)
                except Exception as exc:  # noqa: BLE001 - a bad callback
                    logger.warning("Wake word callback failed: %s", exc)
        return hit

    def _loop(self) -> None:
        self._emit("listening", name=self.matcher.name)
        failures = 0
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.25)
                continue
            try:
                audio = self._recorder()
                if self._stop.is_set() or self._paused.is_set():
                    continue
                self.hear(audio)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - keep listening
                failures += 1
                self.error = str(exc)[:300]
                self._emit("error", message=self.error)
                if failures >= 5:
                    logger.warning("Wake word stopped after repeated errors: %s", exc)
                    break
                self._stop.wait(min(2.0 * failures, 10.0))
        self._emit("stopped")


_listener: Optional[WakeWordListener] = None
_listener_lock = threading.Lock()


def get_listener() -> Optional[WakeWordListener]:
    return _listener


def ensure_listener(backend: Any, name: str) -> WakeWordListener:
    """Return the running listener, replacing it if the name changed."""
    global _listener
    with _listener_lock:
        current = _listener
        wanted = (name or "").strip() or DEFAULT_NAME
        if current is not None and current.matcher.name == wanted:
            current.start()
            return current
        if current is not None:
            current.stop()
        _listener = WakeWordListener(backend, wanted)
        _listener.start()
        return _listener


def stop_listener() -> None:
    global _listener
    with _listener_lock:
        if _listener is not None:
            _listener.stop()
        _listener = None
