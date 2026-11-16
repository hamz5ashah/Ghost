"""Tests for GET /v1/computer/ocr/status."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from ghost.core.config import GhostConfig
from ghost.server.app import create_app


class _Engine:
    name = "fake"

    async def complete(self, *args, **kwargs):
        return ""


def _client(config: GhostConfig) -> TestClient:
    return TestClient(create_app(_Engine(), "test-model", config=config))


def test_route_is_registered():
    app = create_app(_Engine(), "test-model", config=GhostConfig())
    assert "/v1/computer/ocr/status" in {getattr(r, "path", "") for r in app.routes}


def test_reports_missing_tesseract_with_a_reason(monkeypatch, tmp_path):
    monkeypatch.delenv("GHOST_TESSERACT", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    config = GhostConfig()
    config.stonic.tesseract_path = str(tmp_path / "not-there.exe")
    body = _client(config).get("/v1/computer/ocr/status").json()
    assert body["available"] is False
    assert body["source"] == "missing"
    assert body["error"]
    assert body["computer_use_enabled"] is False


def test_reports_the_flag_state(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    config = GhostConfig()
    config.stonic.computer_use = True
    config.stonic.tesseract_path = str(tmp_path / "absent")
    body = _client(config).get("/v1/computer/ocr/status").json()
    assert body["computer_use_enabled"] is True


def test_probe_failure_is_reported_not_raised(monkeypatch):
    from ghost.server import api_routes

    def boom(config):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(api_routes, "_ocr_status_for", boom)
    response = _client(GhostConfig()).get("/v1/computer/ocr/status")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "probe exploded" in response.json()["error"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="no tesseract here")
def test_a_real_tesseract_on_path_is_available(monkeypatch):
    monkeypatch.delenv("GHOST_TESSERACT", raising=False)
    body = _client(GhostConfig()).get("/v1/computer/ocr/status").json()
    assert body["source"] == "path"
    assert body["version"]
    if "eng" in body["languages"]:
        assert body["available"] is True
