"""Tests for speech configuration."""

from ghost.core.config import GhostConfig, SpeechConfig


def test_speech_config_defaults():
    cfg = SpeechConfig()
    assert cfg.backend == "auto"
    assert cfg.model == "base"
    assert cfg.language == ""
    assert cfg.device == "auto"
    assert cfg.compute_type == "float16"


def test_ghost_config_has_speech():
    cfg = GhostConfig()
    assert hasattr(cfg, "speech")
    assert isinstance(cfg.speech, SpeechConfig)
    assert cfg.speech.backend == "auto"


def test_ghost_system_has_speech_backend():
    """GhostSystem has a speech_backend attribute."""
    from ghost.system import GhostSystem

    assert "speech_backend" in GhostSystem.__dataclass_fields__
