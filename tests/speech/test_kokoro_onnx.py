"""Offline natural voice (Kokoro on ONNX Runtime)."""

from __future__ import annotations

import wave
from io import BytesIO

import pytest

from ghost.speech import kokoro_onnx_tts as k


def test_not_healthy_without_model_files(tmp_path):
    assert not k.KokoroOnnxTTSBackend(model_path=str(tmp_path)).health()
    assert not k.models_present(tmp_path)


def test_download_status_reports_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "model_dir", lambda: tmp_path)
    status = k.download_status()
    assert status["state"] == "missing"
    assert status["total"] == sum(k.MODEL_FILES.values())


def test_wav_encoding_is_16bit_mono():
    np = pytest.importorskip("numpy")
    audio = k._wav_bytes(np.zeros(2400, dtype="float32"), 24000)
    with wave.open(BytesIO(audio)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        assert w.getnframes() == 2400


def test_registered_and_preferred_for_fallback():
    import importlib

    from ghost.core.registry import TTSRegistry
    from ghost.speech._tts_discovery import TTS_BACKEND_ORDER, default_voice_for

    # Registries are reset between tests; re-import to re-register.
    importlib.reload(k)
    assert TTSRegistry.contains("kokoro_onnx")
    assert TTS_BACKEND_ORDER[0] == "kokoro_onnx"
    assert default_voice_for("kokoro_onnx") == "bm_george"


def test_synthesize_uses_british_english_for_b_voices(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    calls = {}

    class FakeEngine:
        def create(self, text, voice, speed, lang):
            calls.update(voice=voice, lang=lang, speed=speed)
            return np.zeros(2400, dtype="float32"), 24000

    backend = k.KokoroOnnxTTSBackend(model_path=str(tmp_path))
    monkeypatch.setattr(backend, "_load", lambda: FakeEngine())
    result = backend.synthesize("Good evening, sir.", voice_id="bm_lewis", speed=1.0)
    assert calls == {"voice": "bm_lewis", "lang": "en-gb", "speed": 1.0}
    assert result.format == "wav" and result.voice_id == "bm_lewis"
    # Unknown voices fall back to the default British male voice.
    backend.synthesize("Hi", voice_id="nope")
    assert calls["voice"] == "bm_george"
