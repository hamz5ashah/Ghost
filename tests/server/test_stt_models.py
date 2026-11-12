"""Voice commands: choosing a Whisper size per request and preparing it."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost.server import api_routes
from ghost.speech._stubs import TranscriptionResult


class FakeWhisper:
    backend_id = "faster-whisper"

    def __init__(self):
        self.loaded = set()
        self.calls = []

    def ensure_model_size(self, size):
        self.loaded.add(size)

    def has_model_size(self, size):
        return size in self.loaded

    def transcribe(self, audio, *, format="wav", language=None, model_size=None):
        self.calls.append((format, language, model_size))
        return TranscriptionResult(text="السلام علیکم", language=language or "ur", confidence=0.9, duration_seconds=1.0)


@pytest.fixture
def client():
    api_routes._stt_prepare.clear()
    app = FastAPI()
    app.include_router(api_routes.speech_router)
    app.state.speech_backend = FakeWhisper()
    return TestClient(app), app.state.speech_backend


def test_transcribe_passes_language_and_model(client):
    http, backend = client
    r = http.post("/v1/speech/transcribe", files={"file": ("a.webm", b"x", "audio/webm")},
                  data={"language": "ur", "model": "small"})
    assert r.status_code == 200 and r.json()["text"] == "السلام علیکم"
    assert backend.calls == [("webm", "ur", "small")]


def test_unknown_model_is_rejected(client):
    http, _ = client
    r = http.post("/v1/speech/transcribe", files={"file": ("a.webm", b"x")}, data={"model": "huge"})
    assert r.status_code == 400


def test_prepare_then_ready(client):
    http, _ = client
    assert http.get("/v1/speech/stt/status?model=small").json()["state"] == "missing"
    assert http.post("/v1/speech/stt/prepare?model=small").json()["state"] == "downloading"
    for _ in range(50):
        if http.get("/v1/speech/stt/status?model=small").json()["state"] == "ready":
            break
        time.sleep(0.02)
    assert http.get("/v1/speech/stt/status?model=small").json()["state"] == "ready"
