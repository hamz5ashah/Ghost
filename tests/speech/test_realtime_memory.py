"""Realtime voice (Phase 3) × Phase 6: the female default voice and the memory card."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghost.speech import realtime

SIDECAR = Path(realtime.SERVER_SCRIPT).parent


@pytest.fixture
def ghost_voice(monkeypatch):
    monkeypatch.syspath_prepend(str(SIDECAR))
    import ghost_voice.config as cfg

    return cfg


BASE = {
    "mode": "custom",
    "voice": "live",
    "brain": {"provider": "gemini", "model": "gemini-x"},
}


def test_default_voice_is_female(ghost_voice):
    assert ghost_voice.set_app_config(BASE).voice_name == "Aoede"
    male = ghost_voice.set_app_config(
        {**BASE, "character": {"id": "male_jarvis", "gender": "male"}}
    )
    assert male.voice_name == "Charon"


def test_memory_card_reaches_the_prompt(ghost_voice):
    s = ghost_voice.set_app_config({**BASE, "memory": "ABOUT THEM\n- Lives in Lahore"})
    prompt = ghost_voice.conversation_prompt(s)
    assert "Lives in Lahore" in prompt
    assert (
        ghost_voice.set_app_config({**BASE, "memory": "x" * 9000}).memory
        == "x" * ghost_voice.MAX_MEMORY_CHARS
    )
    assert "Lives in Lahore" not in ghost_voice.conversation_prompt(
        ghost_voice.set_app_config(BASE)
    )


def test_engine_config_carries_memory():
    env = {"GEMINI_API_KEY": "k"}
    with_card = realtime.build_engine_config(
        brain="gemini", env=env, memory="  ABOUT THEM  "
    )
    assert with_card["memory"] == "ABOUT THEM"
    assert "memory" not in realtime.build_engine_config(brain="gemini", env=env)
