"""Phone-access mode: local requests skip the key, remote ones never do."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="ghost[server] not installed")

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from ghost.server.auth_middleware import AuthMiddleware, authenticate_websocket

KEY = "ghost_sk_phone123"


def _make_app(trust_loopback: bool) -> FastAPI:
    app = FastAPI()
    app.state.trust_loopback = trust_loopback
    app.add_middleware(AuthMiddleware, api_key=KEY, trust_loopback=trust_loopback)

    @app.get("/v1/models")
    async def models():
        return {"models": []}

    @app.websocket("/v1/ws")
    async def ws(websocket: WebSocket):
        ok, proto = authenticate_websocket(websocket, KEY)
        if not ok:
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=proto)
        await websocket.send_text("hi")
        await websocket.close()

    return app


def _client(app: FastAPI, host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_skips_key_when_trusted(host):
    assert _client(_make_app(True), host).get("/v1/models").status_code == 200


def test_loopback_still_needs_key_when_not_trusted():
    assert _client(_make_app(False), "127.0.0.1").get("/v1/models").status_code == 401


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "testclient"])
def test_remote_clients_always_need_key(host):
    client = _client(_make_app(True), host)
    assert client.get("/v1/models").status_code == 401
    ok = client.get("/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    assert ok.status_code == 200


@pytest.mark.parametrize(
    "header", ["X-Forwarded-For", "X-Real-IP", "Forwarded"]
)
def test_proxied_loopback_requests_are_not_trusted(header):
    client = _client(_make_app(True), "127.0.0.1")
    resp = client.get("/v1/models", headers={header: "203.0.113.9"})
    assert resp.status_code == 401


def test_websocket_follows_the_same_rule():
    local = _client(_make_app(True), "127.0.0.1")
    with local.websocket_connect("/v1/ws") as ws:
        assert ws.receive_text() == "hi"
    remote = _client(_make_app(True), "192.168.1.20")
    with pytest.raises(Exception):
        with remote.websocket_connect("/v1/ws") as ws:
            ws.receive_text()
