"""/v1/memory/v2 routes (Stonic port, phase 6): refused while the flag is off."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost.core.config import GhostConfig
from ghost.memory.v2 import bridge
from ghost.memory.v2.service import MemoryCoreService


@pytest.fixture
def core(tmp_path, monkeypatch):
    svc = MemoryCoreService(tmp_path / "memory_v2")
    svc.init()
    monkeypatch.setattr(bridge, "get_core", lambda base_dir=None: svc)
    monkeypatch.setattr(bridge, "v2_dir", lambda: tmp_path / "memory_v2")
    monkeypatch.setattr(bridge, "_notes", None)
    monkeypatch.setattr(bridge, "memory_v2_enabled", lambda config=None: True)
    yield svc
    svc.shutdown()


def client(flag: bool) -> TestClient:
    from ghost.server.api_routes import memory_v2_router

    app = FastAPI()
    cfg = GhostConfig()
    cfg.stonic.memory_v2 = flag
    app.state.config = cfg
    app.include_router(memory_v2_router)
    return TestClient(app)


def test_off_by_default_and_refused():
    assert GhostConfig().stonic.memory_v2 is False
    c = client(False)
    assert c.get("/v1/memory/v2/status").json() == {
        "available": False,
        "enabled": False,
    }
    for method, path in [
        ("post", "/v1/memory/v2/recall"),
        ("get", "/v1/memory/v2/memories"),
        ("post", "/v1/memory/v2/notes/command"),
    ]:
        res = (
            getattr(c, method)(path, json={"query": "x", "command": "view"})
            if method == "post"
            else c.get(path)
        )
        assert res.status_code == 403


def test_round_trip(core):
    c = client(True)
    assert c.get("/v1/memory/v2/status").json()["ready"] is True
    r = c.post(
        "/v1/memory/v2/remember", json={"text": "Favourite colour is teal", "pin": True}
    ).json()
    assert r["ok"] and r["pinned"]
    assert (
        c.post("/v1/memory/v2/recall", json={"query": "favourite colour"}).json()[
            "status"
        ]
        == "FOUND"
    )
    assert (
        "teal"
        in c.get("/v1/memory/v2/context", params={"mind": "voice"}).json()["text"]
    )
    f = c.post(
        "/v1/memory/v2/follow-up", json={"what": "Send the invoice", "when": "+2h"}
    ).json()
    assert f["ok"]
    intents = c.get("/v1/memory/v2/intentions").json()["items"]
    assert c.post(
        f"/v1/memory/v2/intentions/{intents[0]['id']}/resolve", json={"status": "done"}
    ).json()["ok"]
    mid = r["id"]
    assert c.patch(
        f"/v1/memory/v2/memories/{mid}", json={"text": "Favourite colour is navy"}
    ).json()["ok"]
    assert (
        c.get(f"/v1/memory/v2/memories/{mid}").json()["memory"]["text_original"]
        == "Favourite colour is navy"
    )
    assert c.delete(f"/v1/memory/v2/memories/{mid}").json()["ok"]
    assert c.post("/v1/memory/v2/pause", json={"paused": True}).json()["paused"] is True
    assert c.get("/v1/memory/v2/export").json()["stats"]["paused"] is True


def test_notes(core):
    c = client(True)
    made = c.post(
        "/v1/memory/v2/notes/command",
        json={
            "command": "create",
            "path": "/memories/x.md",
            "file_text": "---\ndescription: A note\n---\nbody",
        },
    ).json()
    assert made["ok"]
    assert c.get("/v1/memory/v2/notes").json()["items"][0]["path"] == "/memories/x.md"
    assert (
        c.post("/v1/memory/v2/notes/grep", json={"pattern": "body"})
        .json()["text"]
        .startswith("Found 1 file")
    )
    assert not c.post(
        "/v1/memory/v2/notes/command", json={"command": "view", "path": "/etc/passwd"}
    ).json()["ok"]
