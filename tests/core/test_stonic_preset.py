"""Always-Active Stonic Mode: one switch over the individual [stonic] flags."""

from __future__ import annotations

import json
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost.core import stonic_preset
from ghost.core.config import GhostConfig, StonicConfig

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(stonic_preset, "_config_dir", lambda: tmp_path)
    return tmp_path


def test_individual_flags_still_default_off():
    cfg = StonicConfig()
    assert not any(getattr(cfg, name) for name in stonic_preset.ALWAYS_ACTIVE_FLAGS)


def test_on_then_off_restores_exactly(home):
    path = home / "config.toml"
    path.write_text(
        "# my config\n[server]\nport = 9000\n\n"
        "[stonic]\nui_sounds = true  # I like them\nfacelock = true\n"
    )
    on = stonic_preset.apply(True)
    assert on["active"] and all(on["flags"].values())
    data = tomllib.loads(path.read_text())
    assert data["server"]["port"] == 9000 and data["stonic"]["facelock"] is True
    assert "# my config" in path.read_text()
    # Re-applying keeps the ORIGINAL snapshot.
    stonic_preset.apply(True)
    off = stonic_preset.apply(False)
    assert not off["active"]
    data = tomllib.loads(path.read_text())
    assert data["stonic"]["ui_sounds"] is True and data["stonic"]["memory_v2"] is False
    assert data["stonic"]["awareness"] is False and data["stonic"]["facelock"] is True
    assert not (home / "stonic-preset.json").exists()


def test_works_without_a_config_file(home):
    assert stonic_preset.status()["active"] is False
    stonic_preset.apply(True)
    assert (
        tomllib.loads((home / "config.toml").read_text())["stonic"]["wake_word"] is True
    )


def test_updates_the_running_config(home):
    cfg = GhostConfig()
    stonic_preset.apply(True, config=cfg)
    assert (
        cfg.stonic.memory_v2
        and cfg.stonic.realtime_voice
        and cfg.stonic.desktop_presence
    )
    assert cfg.stonic.facelock is False  # not part of the preset


def test_routes(home, monkeypatch):
    from ghost.memory.v2 import bridge
    from ghost.server.api_routes import stonic_router

    monkeypatch.setattr(bridge, "attach", lambda state: False)
    app = FastAPI()
    app.state.config = GhostConfig()
    app.include_router(stonic_router)
    client = TestClient(app)
    assert client.get("/v1/stonic/preset").json()["active"] is False
    res = client.post("/v1/stonic/preset", json={"enabled": True}).json()
    assert res["ok"] and res["active"]
    assert client.get("/v1/stonic/flags").json()["flags"]["memory_v2"] is True
    saved = json.loads((home / "stonic-preset.json").read_text())
    assert saved["previous"]["memory_v2"] is False
    assert (
        client.post("/v1/stonic/preset", json={"enabled": False}).json()["active"]
        is False
    )
    assert client.get("/v1/stonic/flags").json()["flags"]["memory_v2"] is False


def test_set_flag_changes_only_that_flag(home):
    path = home / "config.toml"
    path.write_text("# keep me\n[stonic]\nui_sounds = true\n")
    cfg = GhostConfig()
    stonic_preset.set_flag("realtime_voice", True, config=cfg)
    data = tomllib.loads(path.read_text())
    assert data["stonic"] == {"ui_sounds": True, "realtime_voice": True}
    assert "# keep me" in path.read_text()
    assert cfg.stonic.realtime_voice is True
    stonic_preset.set_flag("realtime_voice", False, config=cfg)
    assert tomllib.loads(path.read_text())["stonic"]["realtime_voice"] is False


def test_flag_endpoint_rejects_unknown_names(home):
    from ghost.server.api_routes import stonic_router

    app = FastAPI()
    app.state.config = GhostConfig()
    app.include_router(stonic_router)
    client = TestClient(app)
    ok = client.post("/v1/stonic/flags", json={"name": "realtime_voice", "enabled": True})
    assert ok.status_code == 200 and ok.json()["flags"] == {"realtime_voice": True}
    assert client.get("/v1/stonic/flags").json()["flags"]["realtime_voice"] is True
    assert client.post("/v1/stonic/flags", json={"name": "rm_rf", "enabled": True}).status_code == 400
