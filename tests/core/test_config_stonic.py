"""Tests for the Stonic port feature flags (see /STONIC_PORT_PLAN.md).

The contract these lock down: every ported subsystem is off unless the user
turns it on, and a malformed or partial config degrades to off rather than
switching something on by accident. That is what makes each phase of the port
independently revertable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghost.core.config import (
    GhostConfig,
    StonicConfig,
    StonicRuntimeConfig,
    load_config,
)

# Flags the UI and the Tauri shell are allowed to read. Kept in step with
# _STONIC_FLAG_NAMES in src/ghost/server/api_routes.py and STONIC_FLAG_NAMES
# in frontend/src-tauri/src/lib.rs.
FLAG_NAMES = (
    "ui_sounds",
    "characters",
    "computer_use",
    "computer_overlay",
    "realtime_voice",
    "wake_word",
    "facelock",
    "gestures",
    "awareness",
    "desktop_presence",
    "memory_v2",
)

# computer_overlay is cosmetic and only applies while computer_use is on, so it
# is the one flag that ships on.
FLAGS_OFF_BY_DEFAULT = tuple(n for n in FLAG_NAMES if n != "computer_overlay")


class TestDefaults:
    def test_stonic_section_exists_on_ghost_config(self):
        assert isinstance(GhostConfig().stonic, StonicConfig)

    @pytest.mark.parametrize("flag", FLAGS_OFF_BY_DEFAULT)
    def test_every_ported_feature_is_off_by_default(self, flag):
        assert getattr(StonicConfig(), flag) is False

    def test_overlay_defaults_on_but_is_inert_while_computer_use_is_off(self):
        cfg = StonicConfig()
        assert cfg.computer_overlay is True
        assert cfg.computer_use is False

    def test_runtimes_default_to_managed_venvs(self):
        cfg = StonicConfig()
        for runtime in (cfg.voice_runtime, cfg.computer_runtime):
            assert isinstance(runtime, StonicRuntimeConfig)
            # Empty means "use the managed venv under <config>/runtimes/".
            assert runtime.venv_path == ""
            assert runtime.python == ""
            assert runtime.auto_install is True

    def test_tesseract_paths_default_to_discovery(self):
        cfg = StonicConfig()
        assert cfg.tesseract_path == ""
        assert cfg.tessdata_path == ""

    def test_flag_names_cover_the_public_surface(self):
        for name in FLAG_NAMES:
            assert isinstance(getattr(StonicConfig(), name), bool)


class TestTomlLoading:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(body)
        return path

    def test_absent_section_leaves_defaults(self, tmp_path):
        cfg = load_config(self._write(tmp_path, '[engine]\ndefault = "ollama"\n'))
        for flag in FLAGS_OFF_BY_DEFAULT:
            assert getattr(cfg.stonic, flag) is False

    def test_enabling_one_flag_does_not_enable_others(self, tmp_path):
        cfg = load_config(self._write(tmp_path, "[stonic]\ncomputer_use = true\n"))
        assert cfg.stonic.computer_use is True
        for flag in FLAGS_OFF_BY_DEFAULT:
            if flag != "computer_use":
                assert getattr(cfg.stonic, flag) is False

    def test_string_values_are_read(self, tmp_path):
        cfg = load_config(
            self._write(
                tmp_path,
                '[stonic]\nwake_word_name = "Ghost"\n'
                'tesseract_path = "C:/tess/tesseract.exe"\n'
                'tessdata_path = "C:/tess/tessdata"\n',
            )
        )
        assert cfg.stonic.wake_word_name == "Ghost"
        assert cfg.stonic.tesseract_path == "C:/tess/tesseract.exe"
        assert cfg.stonic.tessdata_path == "C:/tess/tessdata"

    def test_nested_runtime_table_is_applied(self, tmp_path):
        cfg = load_config(
            self._write(
                tmp_path,
                "[stonic]\nrealtime_voice = true\n\n"
                "[stonic.voice_runtime]\n"
                'python = "python3.11"\n'
                "auto_install = false\n",
            )
        )
        assert cfg.stonic.realtime_voice is True
        assert cfg.stonic.voice_runtime.python == "python3.11"
        assert cfg.stonic.voice_runtime.auto_install is False
        # The other runtime is untouched.
        assert cfg.stonic.computer_runtime.auto_install is True

    def test_stonic_config_does_not_disturb_other_sections(self, tmp_path):
        cfg = load_config(
            self._write(
                tmp_path,
                '[engine]\ndefault = "ollama"\n\n[stonic]\nawareness = true\n',
            )
        )
        assert cfg.stonic.awareness is True
        assert cfg.engine.default == "ollama"


class TestFlagsEndpoint:
    """`GET /v1/stonic/flags` is advisory: it must never raise."""

    def _flags(self, config):
        from ghost.server.api_routes import _STONIC_FLAG_NAMES, stonic_flags

        class _State:
            pass

        class _App:
            pass

        class _Request:
            pass

        state, app, request = _State(), _App(), _Request()
        state.config = config
        app.state = state
        request.app = app

        import asyncio

        return asyncio.run(stonic_flags(request)), _STONIC_FLAG_NAMES

    def test_reports_every_flag_off_for_a_default_config(self):
        result, names = self._flags(GhostConfig())
        assert result["available"] is True
        assert set(result["flags"]) == set(names)
        for flag in FLAGS_OFF_BY_DEFAULT:
            assert result["flags"][flag] is False

    def test_reports_an_enabled_flag(self):
        config = GhostConfig()
        config.stonic.facelock = True
        result, _ = self._flags(config)
        assert result["flags"]["facelock"] is True
        assert result["flags"]["memory_v2"] is False

    def test_endpoint_name_surface_matches_this_test(self):
        from ghost.server.api_routes import _STONIC_FLAG_NAMES

        assert tuple(sorted(_STONIC_FLAG_NAMES)) == tuple(sorted(FLAG_NAMES))

    def test_unreadable_config_reports_all_off_rather_than_raising(self):
        class _Exploding:
            @property
            def stonic(self):
                raise RuntimeError("config is corrupt")

        result, names = self._flags(_Exploding())
        assert result["available"] is False
        assert set(result["flags"]) == set(names)
        assert not any(result["flags"].values())
