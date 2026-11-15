"""Face Lock routes, the loopback gate in a real app, and on-demand models."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost.core.config import GhostConfig
from ghost.security import facelock as fl
from ghost.security import facelock_models as fm

from .test_facelock import OWNER, PIN, TEMPLATES, MemoryStore, _probes


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    fl.reset_for_tests()
    store = MemoryStore()

    def ensure(config_dir=None):
        if fl._manager is None:
            fl._manager = fl.FaceLockManager(tmp_path, store=store)
        return fl._manager

    monkeypatch.setattr(fl, "ensure_manager", ensure)
    yield
    fl.reset_for_tests()


def _app(facelock=True, gestures=False) -> FastAPI:
    from ghost.server.api_routes import facelock_assets_router, facelock_router

    app = FastAPI()
    config = GhostConfig()
    config.stonic.facelock = facelock
    config.stonic.gestures = gestures
    app.state.config = config
    app.include_router(facelock_router)
    app.include_router(facelock_assets_router)

    @app.get("/v1/chat/ping")
    async def ping():
        return {"pong": True}

    app.add_middleware(fl.FaceLockGate, get_manager=fl.get_manager)
    return app


def _loopback(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


class TestRoutes:
    def test_flags_default_off(self):
        assert GhostConfig().stonic.facelock is False
        assert GhostConfig().stonic.gestures is False

    def test_flag_off_status_is_inert_and_actions_refused(self):
        client = _loopback(_app(facelock=False))
        status = client.get("/v1/security/facelock/status").json()
        assert status["available"] is False and status["locked"] is False
        for path in (
            "verify-face",
            "verify-pin",
            "enroll",
            "change-pin",
            "disable",
            "lock",
        ):
            assert (
                client.post(f"/v1/security/facelock/{path}", json={}).status_code == 403
            )

    def test_enroll_lock_unlock_round_trip(self):
        client = _loopback(_app())
        r = client.post(
            "/v1/security/facelock/enroll", json={"templates": TEMPLATES, "pin": PIN}
        ).json()
        assert r["ok"] is True
        assert "templates" not in r["status"]
        assert client.post("/v1/security/facelock/lock").json()["status"]["locked"]

        # Locked: the desktop UI's API calls are refused, the lock screen's are not.
        assert client.get("/v1/chat/ping").status_code == 423
        assert client.get("/v1/security/facelock/status").json()["locked"] is True
        # The phone app (not loopback) is unaffected.
        assert (
            TestClient(_app_like(client), client=("192.168.1.20", 1))
            .get("/v1/chat/ping")
            .status_code
            == 200
        )

        bad = client.post(
            "/v1/security/facelock/verify-face",
            json={"embeddings": _probes([-x for x in OWNER])},
        ).json()
        assert bad["ok"] is False
        good = client.post(
            "/v1/security/facelock/verify-face", json={"embeddings": _probes(OWNER)}
        ).json()
        assert good["ok"] is True and good["status"]["locked"] is False
        assert client.get("/v1/chat/ping").json() == {"pong": True}

    def test_pin_fallback_over_http(self):
        client = _loopback(_app())
        client.post(
            "/v1/security/facelock/enroll", json={"templates": TEMPLATES, "pin": PIN}
        )
        client.post("/v1/security/facelock/lock")
        assert not client.post(
            "/v1/security/facelock/verify-pin", json={"pin": "000001"}
        ).json()["ok"]
        assert client.post(
            "/v1/security/facelock/verify-pin", json={"pin": PIN}
        ).json()["ok"]

    def test_weak_pin_refused(self):
        client = _loopback(_app())
        r = client.post(
            "/v1/security/facelock/enroll",
            json={"templates": TEMPLATES, "pin": "123456"},
        ).json()
        assert r == {"ok": False, "reason": "weak_pin"}


def _app_like(client: TestClient) -> FastAPI:
    return client.app


# -- models --------------------------------------------------------------------


def _fake_assets(monkeypatch, payloads):
    """Swap ASSETS for small fakes with real hashes."""
    fakes = {}
    for name, data in payloads.items():
        real = fm.ASSETS[name]
        fakes[name] = fm.Asset(
            name,
            len(data),
            hashlib.sha256(data).hexdigest(),
            ("https://mirror-a.example/" + name, "https://mirror-b.example/" + name),
            real.media_type,
        )
    monkeypatch.setattr(fm, "ASSETS", {**fm.ASSETS, **fakes})
    monkeypatch.setattr(fm, "_jobs", {})
    monkeypatch.setattr(fm, "_verified", {})


GESTURE_FILES = {
    "gesture_recognizer.task": b"gesture-model" * 10,
    "vision_wasm_internal.wasm": b"\0asm" + b"x" * 50,
}


class TestModels:
    def test_pins_cover_every_bundle_file(self):
        for names in fm.BUNDLES.values():
            for name in names:
                asset = fm.ASSETS[name]
                assert len(asset.sha256) == 64 and asset.size > 0 and asset.urls

    def test_download_verifies_and_falls_back_to_the_next_mirror(
        self, monkeypatch, tmp_path
    ):
        _fake_assets(monkeypatch, GESTURE_FILES)
        seen = []

        def fetch(url, dest: Path, on_bytes):
            seen.append(url.split("/")[2])
            name = url.rsplit("/", 1)[1]
            data = GESTURE_FILES[name]
            if "mirror-a" in url and name.endswith(".wasm"):
                data = b"tampered" + data  # wrong bytes from the first mirror
            dest.write_bytes(data)
            on_bytes(len(data))

        status = fm.start_download("gestures", tmp_path, fetch=fetch, block=True)
        assert status["state"] == "ready", status
        assert "mirror-b.example" in seen
        assert fm.bundle_present("gestures", tmp_path)
        assert status["downloaded"] == sum(len(v) for v in GESTURE_FILES.values())
        assert not list(tmp_path.glob("*.part"))

    def test_all_mirrors_bad_is_an_error(self, monkeypatch, tmp_path):
        _fake_assets(monkeypatch, GESTURE_FILES)

        def fetch(url, dest, on_bytes):
            dest.write_bytes(b"nope")

        status = fm.start_download("gestures", tmp_path, fetch=fetch, block=True)
        assert status["state"] == "error"
        assert "gesture_recognizer.task" in status["error"]
        assert fm.download_status("gestures", tmp_path)["state"] == "error"

    def test_tampered_file_on_disk_is_not_served(self, monkeypatch, tmp_path):
        _fake_assets(monkeypatch, GESTURE_FILES)
        name = "gesture_recognizer.task"
        good = GESTURE_FILES[name]
        (tmp_path / name).write_bytes(b"X" * len(good))  # right size, wrong bytes
        assert fm.asset_path(name, tmp_path) is None
        assert not (tmp_path / name).exists()

    def test_unknown_names_are_never_paths(self, tmp_path):
        assert fm.asset_path("../config.toml", tmp_path) is None
        assert fm.asset_path("face_lock.dpapi", tmp_path) is None


class TestAssetRoute:
    def test_serves_verified_assets_only_while_a_flag_is_on(
        self, monkeypatch, tmp_path
    ):
        _fake_assets(monkeypatch, GESTURE_FILES)
        monkeypatch.setattr(fm, "model_dir", lambda: tmp_path)
        for name, data in GESTURE_FILES.items():
            (tmp_path / name).write_bytes(data)

        on = TestClient(_app(facelock=False, gestures=True))
        r = on.get("/facelock-assets/vision_wasm_internal.wasm")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/wasm"
        assert r.content == GESTURE_FILES["vision_wasm_internal.wasm"]
        assert on.get("/facelock-assets/face_lock.dpapi").status_code == 404
        assert (
            on.get("/facelock-assets/sface.onnx").status_code == 404
        )  # not downloaded

        off = TestClient(_app(facelock=False, gestures=False))
        assert off.get("/facelock-assets/vision_wasm_internal.wasm").status_code == 404

    def test_model_routes_respect_their_flag(self):
        client = TestClient(_app(facelock=True, gestures=False))
        assert (
            client.get("/v1/security/facelock/models?bundle=facelock").status_code
            == 200
        )
        assert (
            client.get("/v1/security/facelock/models?bundle=gestures").status_code
            == 403
        )
        assert client.get("/v1/security/facelock/models?bundle=nope").status_code == 400
