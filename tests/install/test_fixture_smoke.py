"""Smoke test that the tmp_ghost_home fixture works."""

from __future__ import annotations

from pathlib import Path

from ghost.core import config as config_mod


def test_fixture_redirects_default_config_dir(tmp_ghost_home: Path) -> None:
    assert config_mod.DEFAULT_CONFIG_DIR == tmp_ghost_home
    assert tmp_ghost_home.exists()
    assert (tmp_ghost_home / ".state").exists()
    assert (tmp_ghost_home / ".state" / "models").exists()


def test_fixture_redirects_config_path(tmp_ghost_home: Path) -> None:
    assert config_mod.DEFAULT_CONFIG_PATH == tmp_ghost_home / "config.toml"
