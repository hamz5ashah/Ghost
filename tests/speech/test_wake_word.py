"""Wake word (Stonic port, Phase 3): Stonic's name matching, local transcription."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost.core.config import GhostConfig
from ghost.speech import wake_word
from ghost.speech.wake_word import WakeWordListener, WakeWordMatcher, first_name


class TestNames:
    def test_first_word_only(self):
        assert first_name("Ayesha Khan") == "ayesha"

    def test_short_names_are_dropped(self):
        # Two letters turn up in ordinary speech all day.
        assert first_name("Al") == ""
        assert "al" not in WakeWordMatcher("Al").names

    def test_default_name_is_ghost(self):
        matcher = WakeWordMatcher("")
        assert matcher.name == "Ghost"
        assert "ghost" in matcher.names


class TestMatching:
    def test_whisper_punctuation_is_handled(self):
        hit = WakeWordMatcher().match("Ghost, what's the time?")
        assert hit is not None
        assert hit.trigger == "ghost"
        assert hit.command == "what's the time"

    def test_custom_name_with_stonic_slack(self):
        matcher = WakeWordMatcher("Ayesha")
        for heard in ("Ayesha?", "ayeshaa suno", "Ayeshah kya haal hai"):
            assert matcher.match(heard) is not None, heard

    def test_slack_only_for_four_letters_or_more(self):
        assert WakeWordMatcher("Max").match("maxim gorky") is None
        assert WakeWordMatcher("Max").match("hey max") is not None

    def test_no_stonic_nic_catch_all(self):
        # Stonic woke on anything ending in "nic"; Ghost must not.
        matcher = WakeWordMatcher()
        for heard in ("we had a picnic", "the clinic is open", "Stonic"):
            assert matcher.match(heard) is None, heard

    def test_ordinary_speech_does_not_wake(self):
        assert WakeWordMatcher("Jarvis").match("pass the salt please") is None

    def test_roman_urdu_command_is_kept(self):
        hit = WakeWordMatcher("Jarvis").match("Jarvis, kal ki meeting kab hai?")
        assert hit is not None
        assert hit.command == "kal ki meeting kab hai"


class _Backend:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def transcribe(self, audio, *, format="wav", language=None, model_size=None):
        self.calls.append(model_size)
        return SimpleNamespace(text=self.texts.pop(0) if self.texts else "")


AUDIO = b"\0" * 32000


class TestListener:
    def test_hear_emits_a_wake_event(self):
        hits = []
        listener = WakeWordListener(_Backend(["Ghost open notes"]), on_hit=hits.append)
        assert listener.hear(AUDIO).command == "open notes"
        events = listener.events()
        assert events[-1]["type"] == "wake"
        assert events[-1]["command"] == "open notes"
        assert hits and hits[0].trigger == "ghost"

    def test_silence_never_reaches_whisper(self):
        backend = _Backend(["Ghost"])
        listener = WakeWordListener(backend)
        assert listener.hear(b"") is None
        assert listener.hear(b"\0" * 100) is None
        assert backend.calls == []

    def test_events_are_numbered_for_polling(self):
        listener = WakeWordListener(_Backend(["Ghost one", "nothing", "Ghost two"]))
        for _ in range(3):
            listener.hear(AUDIO)
        first = listener.events()
        assert [e["command"] for e in first] == ["one", "two"]
        assert [e["command"] for e in listener.events(after=first[0]["seq"])] == ["two"]

    def test_background_loop_pause_and_stop(self):
        backend = _Backend(["Ghost hello"] + ["noise"] * 1000)
        listener = WakeWordListener(
            backend, recorder=lambda: (time.sleep(0.01), AUDIO)[1]
        )
        listener.start()
        deadline = time.time() + 5
        while not any(e["type"] == "wake" for e in listener.events()):
            assert time.time() < deadline
            time.sleep(0.01)
        listener.pause()
        time.sleep(0.1)
        calls = len(backend.calls)
        time.sleep(0.2)
        assert len(backend.calls) <= calls + 1  # at most the phrase in flight
        listener.stop()
        assert not listener.running
        assert listener.events()[-1]["type"] == "stopped"

    def test_repeated_errors_stop_the_loop(self):
        def broken():
            raise OSError("no microphone")

        listener = WakeWordListener(_Backend([]), recorder=broken)
        listener._stop.wait = lambda timeout=None: False  # no back-off in tests
        listener._loop()
        assert "no microphone" in listener.error
        assert listener.events()[-1]["type"] == "stopped"


@pytest.fixture(autouse=True)
def _no_global_listener():
    yield
    wake_word.stop_listener()


def _client(realtime: bool, wake: bool, backend=None) -> TestClient:
    from ghost.server.api_routes import realtime_router

    app = FastAPI()
    config = GhostConfig()
    config.stonic.realtime_voice = realtime
    config.stonic.wake_word = wake
    config.stonic.wake_word_name = "Jarvis"
    app.state.config = config
    app.state.speech_backend = backend
    app.include_router(realtime_router)
    return TestClient(app)


class TestRoutes:
    def test_off_by_default(self):
        assert GhostConfig().stonic.wake_word is False

    @pytest.mark.parametrize(
        "realtime,wake", [(False, False), (False, True), (True, False)]
    )
    def test_needs_both_flags(self, realtime, wake):
        response = _client(realtime, wake, _Backend([])).post(
            "/v1/speech/realtime/wake/start"
        )
        assert response.status_code == 403

    def test_start_uses_the_configured_name(self, monkeypatch):
        # Keep the real microphone out of the test.
        created = {}

        class _Listener(WakeWordListener):
            def __init__(self, backend, name=""):
                super().__init__(backend, name, recorder=lambda: b"")
                created["name"] = name

        monkeypatch.setattr(wake_word, "WakeWordListener", _Listener)
        client = _client(True, True, _Backend([]))
        body = client.post("/v1/speech/realtime/wake/start").json()
        assert body["ok"] is True
        assert body["name"] == "Jarvis"
        assert "jarvis" in body["answers_to"]
        assert client.post("/v1/speech/realtime/wake/pause").json()["paused"] is True
        assert (
            client.post("/v1/speech/realtime/wake/pause?resume=true").json()["paused"]
            is False
        )
        assert client.get("/v1/speech/realtime/wake/events").json()["running"] is True
        assert client.post("/v1/speech/realtime/wake/stop").json()["running"] is False

    def test_events_when_never_started(self):
        body = _client(True, True).get("/v1/speech/realtime/wake/events").json()
        assert body == {"running": False, "events": [], "last_seq": 0}
