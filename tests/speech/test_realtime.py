"""Realtime voice engine supervisor and routes (Stonic port, Phase 3).

The supervisor is exercised against a small fake engine that speaks the real
engine's contract (token header, ``ready`` line on stdout, ``/beta/*`` and
``/api/offer``), so spawning, readiness, token enforcement and proxying all
run for real without the 375 MB voice runtime.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghost.core.config import GhostConfig
from ghost.speech import realtime

FAKE_ENGINE = textwrap.dedent(
    """
    import json, os, socket, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    TOKEN = os.environ.get("GHOST_VOICE_TOKEN", "")
    LEAKED = sorted(k for k in os.environ if k.upper().endswith("_API_KEY"))
    STATE = {"config": None}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("content-length") or 0)
            return json.loads(self.rfile.read(n) or b"null")

        def _handle(self):
            if self.headers.get("x-ghost-voice-token") != TOKEN:
                return self._send(401, {"error": "bad_token"})
            body = self._body() if self.command in ("POST", "PATCH") else None
            if self.path == "/beta/config":
                STATE["config"] = body
                return self._send(200, {"ok": True, "missing": []})
            if self.path == "/beta/status":
                cfg = STATE["config"] or {}
                return self._send(200, {
                    "leaked_env_keys": LEAKED,
                    "brain": cfg.get("brain"),
                    "voice": cfg.get("voice"),
                    "key_providers": sorted((cfg.get("keys") or {})),
                    "character": cfg.get("character"),
                })
            if self.path == "/api/offer":
                return self._send(200, {"type": "answer", "sdp": "v=0",
                                        "pc_id": "pc-1", "method": self.command})
            if self.path in ("/beta/forget", "/beta/hangup"):
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not_found"})

        do_GET = do_POST = do_PATCH = _handle

    if not TOKEN:
        print(json.dumps({"event": "fatal", "code": "no_token"}), flush=True)
        sys.exit(2)
    server = HTTPServer(("127.0.0.1", 0), H)
    print("not json noise", flush=True)
    print(json.dumps({"event": "ready", "port": server.server_address[1],
                      "version": "test", "pipecat": "1.12.0"}), flush=True)
    server.serve_forever()
    """
)

KEYS_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "GEMINI_API_KEY": "gem-test",
    "DEEPSEEK_API_KEY": "ds-test",
}


# -- pure helpers --------------------------------------------------------------


class TestKeysFromEnv:
    def test_maps_keyring_env_names_to_providers(self):
        keys = realtime.keys_from_env({**KEYS_ENV, "OPENROUTER_API_KEY": "or"})
        assert keys == {
            "anthropic": "sk-ant-test",
            "gemini": "gem-test",
            "deepseek": "ds-test",
            "openrouter": "or",
        }

    def test_google_key_stands_in_for_gemini(self):
        assert realtime.keys_from_env({"GOOGLE_API_KEY": "g"}) == {"gemini": "g"}

    def test_blank_values_are_ignored(self):
        assert realtime.keys_from_env({"ANTHROPIC_API_KEY": "  "}) == {}


class TestBuildEngineConfig:
    def test_explicit_brain_and_only_needed_keys_are_sent(self):
        payload = realtime.build_engine_config(brain="deepseek", env=KEYS_ENV)
        assert payload["mode"] == "custom"
        assert payload["brain"] == {"provider": "deepseek", "model": "deepseek-flash"}
        # Gemini for ears/voice + the brain's own key; Claude's key stays home.
        assert payload["keys"] == {"gemini": "gem-test", "deepseek": "ds-test"}

    def test_model_can_be_named(self):
        payload = realtime.build_engine_config(
            brain="anthropic:claude-sonnet-5-5", env=KEYS_ENV
        )
        assert payload["brain"]["model"] == "claude-sonnet-5-5"

    def test_auto_brain_prefers_claude_then_gemini(self):
        assert (
            realtime.build_engine_config(env=KEYS_ENV)["brain"]["provider"]
            == "anthropic"
        )
        only_gemini = {"GEMINI_API_KEY": "g"}
        assert (
            realtime.build_engine_config(env=only_gemini)["brain"]["provider"]
            == "gemini"
        )

    def test_no_keys_at_all_is_a_clear_error(self):
        with pytest.raises(realtime.RealtimeError) as err:
            realtime.build_engine_config(env={})
        assert err.value.code == "key_missing"

    def test_stonic_gateway_and_groq_are_not_brains(self):
        for spec in ("stonic", "cloud", "groq", "openrouter_claude"):
            with pytest.raises(realtime.RealtimeError) as err:
                realtime.build_engine_config(brain=spec, env=KEYS_ENV)
            assert err.value.code == "bad_brain"

    def test_voice_mode_is_validated(self):
        assert realtime.build_engine_config(voice_mode="live", env=KEYS_ENV)[
            "voice"
        ] == ("live")
        with pytest.raises(realtime.RealtimeError):
            realtime.build_engine_config(voice_mode="kokoro", env=KEYS_ENV)

    def test_wake_word_name_names_the_character(self):
        payload = realtime.build_engine_config(wake_word_name="Jarvis", env=KEYS_ENV)
        assert payload["character"]["name"] == "Jarvis"
        assert realtime.build_engine_config(env=KEYS_ENV)["character"]["name"] == (
            "Ghost"
        )

    def test_missing_for_reports_gemini_when_only_claude_saved(self):
        payload = realtime.build_engine_config(env={"ANTHROPIC_API_KEY": "a"})
        assert realtime.missing_for(payload) == ["gemini"]


def test_secret_env_names_are_never_inherited():
    assert realtime._is_secret_name("ANTHROPIC_API_KEY")
    assert realtime._is_secret_name("elevenlabs_api_key")
    assert realtime._is_secret_name("SOME_VENDOR_API_KEY")
    assert not realtime._is_secret_name("PATH")


def test_sidecar_script_ships_inside_src():
    # src/ is bundled by the Tauri resource map, so the engine ships with it.
    assert realtime.SERVER_SCRIPT.is_file()
    assert realtime.SERVER_SCRIPT.parent.name == "voice_sidecar"
    assert not (realtime.SERVER_SCRIPT.parent / "__init__.py").exists()


# -- supervisor against a fake engine -----------------------------------------


@pytest.fixture
def fake_script(tmp_path: Path) -> Path:
    path = tmp_path / "server.py"
    path.write_text(FAKE_ENGINE, encoding="utf-8")
    return path


@pytest.fixture
def keyring_env(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in KEYS_ENV.items():
        monkeypatch.setenv(name, value)


def test_manager_spawns_configures_proxies_and_stops(fake_script, keyring_env):
    async def scenario():
        manager = realtime.RealtimeVoiceManager(
            python=Path(sys.executable), script=fake_script
        )
        started = await manager.start()
        assert started["running"] is True
        assert started["pipecat"] == "1.12.0"
        # A second start reuses the same process.
        pid = manager._engine.process.pid
        await manager.start()
        assert manager._engine.process.pid == pid

        payload = realtime.build_engine_config(brain="gemini")
        await manager.configure(payload)
        status = await manager.engine_status()
        # Keys arrive over /beta/config only, never through the environment.
        assert status["leaked_env_keys"] == []
        assert status["key_providers"] == ["gemini"]

        code, answer = await manager.offer("POST", {"sdp": "x", "type": "offer"})
        assert code == 200 and answer["pc_id"] == "pc-1"
        code, answer = await manager.offer("PATCH", {"pc_id": "pc-1"})
        assert answer["method"] == "PATCH"

        # The engine refuses anyone without the minted token.
        engine = manager._engine
        engine_token, engine.token = engine.token, "wrong"
        code, _ = await manager._call(engine, "GET", "/beta/status")
        assert code == 401
        engine.token = engine_token

        await manager.stop()
        assert manager.running is False
        assert engine.process.returncode is not None

    asyncio.run(scenario())


def test_manager_reports_engine_that_dies_on_startup(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(
        "import json\nprint(json.dumps({'event':'fatal','code':'boom',"
        "'message':'nope'}), flush=True)\n",
        encoding="utf-8",
    )

    async def scenario():
        manager = realtime.RealtimeVoiceManager(
            python=Path(sys.executable), script=script
        )
        with pytest.raises(realtime.RealtimeError) as err:
            await manager.start()
        assert err.value.code == "boom"
        assert manager.describe()["last_error"]["error"] == "boom"

    asyncio.run(scenario())


def test_calls_before_start_say_not_running():
    manager = realtime.RealtimeVoiceManager(python=Path(sys.executable))
    with pytest.raises(realtime.RealtimeError) as err:
        asyncio.run(manager.offer("POST", {}))
    assert err.value.code == "not_running"


def test_missing_runtime_is_reported(monkeypatch):
    import ghost.runtimes as runtimes

    monkeypatch.setattr(runtimes, "is_installed", lambda name: False)
    manager = realtime.RealtimeVoiceManager()
    with pytest.raises(realtime.RealtimeError) as err:
        asyncio.run(manager.start())
    assert err.value.code == "runtime_missing"


# -- routes --------------------------------------------------------------------


def _client(enabled: bool, manager=None, monkeypatch=None) -> TestClient:
    from ghost.server.api_routes import realtime_router

    app = FastAPI()
    config = GhostConfig()
    config.stonic.realtime_voice = enabled
    config.stonic.wake_word_name = "Jarvis"
    app.state.config = config
    app.include_router(realtime_router)
    if manager is not None and monkeypatch is not None:
        monkeypatch.setattr(realtime, "get_manager", lambda: manager)
    return TestClient(app)


class TestRoutes:
    def test_flag_defaults_off(self):
        assert GhostConfig().stonic.realtime_voice is False

    def test_everything_but_status_and_stop_is_refused_when_off(self):
        client = _client(enabled=False)
        for method, path in (
            ("post", "/v1/speech/realtime/install"),
            ("post", "/v1/speech/realtime/start"),
            ("post", "/v1/speech/realtime/offer"),
            ("patch", "/v1/speech/realtime/offer"),
        ):
            response = getattr(client, method)(path, json={})
            assert response.status_code == 403, path
            assert response.json()["error"] == "disabled"
        assert client.get("/v1/speech/realtime/status").status_code == 200
        assert client.post("/v1/speech/realtime/stop").status_code == 200

    def test_status_reports_key_presence_not_values(self, keyring_env):
        body = _client(enabled=True).get("/v1/speech/realtime/status").json()
        assert body["enabled"] is True
        assert body["wake_word_name"] == "Jarvis"
        assert body["keys"] == {
            "anthropic": True,
            "gemini": True,
            "deepseek": True,
            "openrouter": False,
        }
        assert "sk-ant-test" not in json.dumps(body)

    def test_start_without_gemini_names_the_missing_key(self, monkeypatch):
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
        response = _client(enabled=True).post(
            "/v1/speech/realtime/start", json={"brain": "anthropic"}
        )
        assert response.status_code == 409
        assert "gemini" in response.json()["message"]

    def test_start_and_offer_through_the_proxy(
        self, fake_script, keyring_env, monkeypatch
    ):
        manager = realtime.RealtimeVoiceManager(
            python=Path(sys.executable), script=fake_script
        )
        client = _client(enabled=True, manager=manager, monkeypatch=monkeypatch)
        # One event loop for the whole exchange, as under uvicorn.
        with client:
            try:
                response = client.post(
                    "/v1/speech/realtime/start",
                    json={"brain": "deepseek", "voice_mode": "tts"},
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["brain"]["provider"] == "deepseek"
                assert "ds-test" not in response.text

                answer = client.post(
                    "/v1/speech/realtime/offer", json={"sdp": "x", "type": "offer"}
                )
                assert answer.status_code == 200
                assert answer.json()["type"] == "answer"
            finally:
                client.post("/v1/speech/realtime/stop")
        assert manager.running is False
