"""Supervisor for the realtime voice engine sidecar (Stonic port, Phase 3).

Ghost's default voice path — Kokoro ONNX for speech and faster-whisper for
listening — is untouched by this module. When the user turns on
``[stonic].realtime_voice`` and picks the realtime engine in Settings →
Assistant, this supervisor:

1. starts ``src/ghost/voice_sidecar/server.py`` with the interpreter of the
   on-demand ``voice`` runtime (``<config>/runtimes/voice``), never Ghost's
   own environment;
2. mints a one-off token the engine demands on every request and binds it to
   loopback only;
3. waits for the engine's ``{"event": "ready", "port": ...}`` line on stdout;
4. pushes the user's keys to ``/beta/config`` **from this process's own
   environment**, which the desktop shell fills from the OS keyring
   (``secure_store_*`` → ``cloud_keys_for_inference``). Keys never pass
   through the frontend and there is no gateway: the engine talks to the
   providers directly with the user's own keys;
5. proxies the WebRTC offer so the browser only ever talks to Ghost.

The engine exits on its own if Ghost dies (``GHOST_VOICE_PARENT_PID``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SIDECAR_DIR = Path(__file__).resolve().parent.parent / "voice_sidecar"
SERVER_SCRIPT = SIDECAR_DIR / "server.py"

READY_TIMEOUT_S = 90.0
REQUEST_TIMEOUT_S = 30.0

# Engine provider name → environment variables that may hold its key, in
# order. These are exactly the names the Rust shell injects from the keyring.
PROVIDER_ENV: Dict[str, Tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}

BRAIN_PROVIDERS = ("anthropic", "gemini", "deepseek", "openrouter")
VOICE_MODES = ("tts", "live")

# Default brain model per provider when the user named only the provider.
DEFAULT_MODELS: Dict[str, str] = {
    "anthropic": "claude-haiku-5-5",
    "gemini": "gemini-3.8-flash",
    "deepseek": "deepseek-flash",
    "openrouter": "anthropic/claude-haiku-5.5",
}

# Preference order when no brain was chosen: whichever key exists first.
_AUTO_BRAIN_ORDER = ("anthropic", "gemini", "openrouter", "deepseek")


class RealtimeError(RuntimeError):
    """A failure the UI can show. ``code`` is stable, ``message`` is human."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message}


def keys_from_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Collect provider keys from the environment. Empty values are skipped."""
    source = os.environ if env is None else env
    keys: Dict[str, str] = {}
    for provider, names in PROVIDER_ENV.items():
        for name in names:
            value = (source.get(name) or "").strip()
            if value:
                keys[provider] = value
                break
    return keys


def parse_brain(spec: str) -> Tuple[str, str]:
    """Split ``"provider"`` or ``"provider:model"``. Provider may be empty."""
    spec = (spec or "").strip()
    if not spec:
        return "", ""
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower()
    if provider not in BRAIN_PROVIDERS:
        raise RealtimeError(
            "bad_brain",
            f"Unknown brain provider {provider!r}; use one of "
            + ", ".join(BRAIN_PROVIDERS),
            status=400,
        )
    return provider, model.strip()


def resolve_brain(spec: str, keys: Mapping[str, str]) -> Tuple[str, str]:
    """Pick the brain provider/model, falling back to whichever key is saved."""
    provider, model = parse_brain(spec)
    if not provider:
        provider = next((p for p in _AUTO_BRAIN_ORDER if p in keys), "")
        if not provider:
            raise RealtimeError(
                "key_missing",
                "Save a Claude, Gemini, DeepSeek or OpenRouter key in Settings "
                "to use the realtime voice engine.",
            )
    return provider, model or DEFAULT_MODELS[provider]


def build_engine_config(
    *,
    brain: str = "",
    voice_mode: str = "tts",
    character: Optional[Mapping[str, Any]] = None,
    wake_word_name: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Build the ``/beta/config`` payload. Pure, so it is easy to test.

    Gemini always handles speech in and out (STT/TTS), so its key is required
    whichever brain thinks. Only keys the session needs are sent.
    """
    keys = keys_from_env(env)
    provider, model = resolve_brain(brain, keys)
    mode = (voice_mode or "tts").strip().lower()
    if mode not in VOICE_MODES:
        raise RealtimeError(
            "bad_voice", f"voice_mode must be one of {VOICE_MODES}", 400
        )
    sent = {p: keys[p] for p in {"gemini", provider} if p in keys}
    char = dict(character or {})
    name = (
        (wake_word_name or "").strip() or str(char.get("name") or "").strip() or "Ghost"
    )
    char.setdefault("name", name)
    return {
        "mode": "custom",
        "voice": mode,
        "brain": {"provider": provider, "model": model},
        "character": char,
        "keys": sent,
    }


def missing_for(payload: Mapping[str, Any]) -> List[str]:
    """Providers whose key a session needs but the payload lacks."""
    keys = payload.get("keys") or {}
    need = {"gemini", str((payload.get("brain") or {}).get("provider") or "")}
    return sorted(p for p in need if p and p not in keys)


@dataclass
class _Engine:
    process: asyncio.subprocess.Process
    port: int
    token: str
    version: str = ""
    pipecat: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)
    drain: Optional[asyncio.Task] = None


class RealtimeVoiceManager:
    """Owns at most one engine process. All methods are coroutine-safe."""

    def __init__(
        self, python: Optional[Path] = None, script: Path = SERVER_SCRIPT
    ) -> None:
        self._python = python
        self._script = script
        self._engine: Optional[_Engine] = None
        self._lock = asyncio.Lock()
        self._last_error: Optional[Dict[str, Any]] = None

    # -- lifecycle ----------------------------------------------------------

    def _interpreter(self) -> Path:
        if self._python is not None:
            return self._python
        from ghost.runtimes import is_installed, venv_python  # noqa: PLC0415

        if not is_installed("voice"):
            raise RealtimeError(
                "runtime_missing",
                "The realtime voice runtime is not installed yet.",
            )
        return venv_python("voice")

    @property
    def running(self) -> bool:
        engine = self._engine
        return engine is not None and engine.process.returncode is None

    async def start(self) -> Dict[str, Any]:
        async with self._lock:
            if self.running:
                return self._describe()
            await self._spawn()
            return self._describe()

    async def _spawn(self) -> None:
        python = self._interpreter()
        token = secrets.token_urlsafe(32)
        env = {k: v for k, v in os.environ.items() if not _is_secret_name(k)}
        env.update(
            GHOST_VOICE_TOKEN=token,
            GHOST_VOICE_PARENT_PID=str(os.getpid()),
            PYTHONUTF8="1",
            PYTHONUNBUFFERED="1",
        )
        log_path = _log_path()
        if log_path is not None:
            env["GHOST_VOICE_LOG"] = str(log_path)
        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            str(python),
            "-X",
            "utf8",
            str(self._script),
            cwd=str(self._script.parent),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **kwargs,
        )
        try:
            ready = await asyncio.wait_for(_read_ready(process), READY_TIMEOUT_S)
        except (asyncio.TimeoutError, RealtimeError) as exc:
            await _terminate(process)
            err = (
                exc
                if isinstance(exc, RealtimeError)
                else RealtimeError(
                    "engine_timeout", "The voice engine did not start in time."
                )
            )
            self._last_error = err.to_dict()
            raise err from None
        engine = _Engine(
            process=process,
            port=int(ready["port"]),
            token=token,
            version=str(ready.get("version") or ""),
            pipecat=str(ready.get("pipecat") or ""),
        )
        engine.drain = asyncio.create_task(_drain(process, engine.events))
        self._engine = engine
        self._last_error = None
        logger.info("Realtime voice engine ready on 127.0.0.1:%s", engine.port)

    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            engine, self._engine = self._engine, None
            if engine is None:
                return {"ok": True, "running": False}
            if engine.process.returncode is None:
                try:
                    await self._call(engine, "POST", "/beta/forget", timeout=5.0)
                except Exception:  # noqa: BLE001 - shutting down anyway
                    pass
            await _terminate(engine.process)
            if engine.drain is not None:
                engine.drain.cancel()
            return {"ok": True, "running": False}

    # -- engine calls -------------------------------------------------------

    async def configure(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        engine = self._require()
        status, body = await self._call(
            engine, "POST", "/beta/config", json_body=payload
        )
        if status >= 400:
            raise RealtimeError(
                str(body.get("error") or "bad_config"),
                str(body.get("message") or "The voice engine refused the settings."),
                status,
            )
        return body

    async def offer(self, method: str, body: Any) -> Tuple[int, Any]:
        engine = self._require()
        return await self._call(engine, method, "/api/offer", json_body=body)

    async def hangup(self) -> Dict[str, Any]:
        if not self.running:
            return {"ok": True}
        _, body = await self._call(self._require(), "POST", "/beta/hangup")
        return body if isinstance(body, dict) else {"ok": True}

    async def engine_status(self) -> Optional[Dict[str, Any]]:
        if not self.running:
            return None
        try:
            _, body = await self._call(
                self._require(), "GET", "/beta/status", timeout=5.0
            )
            return body if isinstance(body, dict) else None
        except Exception:  # noqa: BLE001 - status is advisory
            return None

    def _require(self) -> _Engine:
        if not self.running or self._engine is None:
            raise RealtimeError(
                "not_running", "The realtime voice engine is not running."
            )
        return self._engine

    async def _call(
        self,
        engine: _Engine,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> Tuple[int, Any]:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.request(
                method,
                f"http://127.0.0.1:{engine.port}{path}",
                headers={"x-ghost-voice-token": engine.token},
                json=json_body,
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body

    def _describe(self) -> Dict[str, Any]:
        engine = self._engine
        if engine is None or not self.running:
            return {"running": False, "last_error": self._last_error}
        return {
            "running": True,
            "version": engine.version,
            "pipecat": engine.pipecat,
            "recent_events": engine.events[-10:],
            "last_error": self._last_error,
        }

    def describe(self) -> Dict[str, Any]:
        return self._describe()


# -- helpers ------------------------------------------------------------------

_SECRET_NAMES = {name for names in PROVIDER_ENV.values() for name in names} | {
    "ELEVENLABS_API_KEY",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "CARTESIA_API_KEY",
    "GROQ_API_KEY",
}


def _is_secret_name(name: str) -> bool:
    """Keys go to the engine over /beta/config only, never via its environment."""
    upper = name.upper()
    return upper in _SECRET_NAMES or upper.endswith("_API_KEY")


def _log_path() -> Optional[Path]:
    try:
        from ghost.runtimes import runtime_dir  # noqa: PLC0415

        path = runtime_dir("voice") / "engine.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:  # noqa: BLE001 - logging is optional
        return None


async def _read_ready(process: asyncio.subprocess.Process) -> Dict[str, Any]:
    assert process.stdout is not None
    while True:
        line = await process.stdout.readline()
        if not line:
            raise RealtimeError(
                "engine_exited", "The voice engine exited during startup."
            )
        event = _parse_event(line)
        if event is None:
            continue
        if event.get("event") == "ready" and event.get("port"):
            return event
        if event.get("event") == "fatal":
            raise RealtimeError(
                str(event.get("code") or "engine_fatal"),
                str(event.get("message") or "The voice engine failed to start."),
            )


async def _drain(
    process: asyncio.subprocess.Process, events: List[Dict[str, Any]]
) -> None:
    """Keep stdout flowing so the engine never blocks on a full pipe."""
    assert process.stdout is not None
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            event = _parse_event(line)
            if event is not None:
                events.append(event)
                del events[:-50]
    except asyncio.CancelledError:
        return


def _parse_event(line: bytes) -> Optional[Dict[str, Any]]:
    try:
        event = json.loads(line.decode("utf-8", "replace").strip() or "null")
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), 5.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


_manager: Optional[RealtimeVoiceManager] = None


def get_manager() -> RealtimeVoiceManager:
    global _manager
    if _manager is None:
        _manager = RealtimeVoiceManager()
    return _manager
