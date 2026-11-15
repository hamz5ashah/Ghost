"""Ghost realtime voice engine — talk-only (Phase 3 of /STONIC_PORT_PLAN.md).

Ported from Stonic's stonic-voice/server.py. A WebRTC voice conversation
(/api/offer) through a Pipecat pipeline: Gemini Live transcription, Silero VAD +
Smart Turn v3, a talk-only brain, Gemini voice.

Removed from Stonic's server: the Classic-voice work routes (/work/*), the
in-app tool bridge (/beta/tool-result), the computer-agent mouse hand-off
(/beta/mouse), the awareness cue and interrupt routes, and the Gemini relay that
let the Claude Code CLI run on a Gemini key. None of them belong to a talk-only
engine.

Ghost's backend (ghost/speech/realtime.py) runs it:

    python server.py

Mahaul (environment) se:
    GHOST_VOICE_TOKEN       lazmi. Har request ke saath yehi token aana chahiye.
    GHOST_VOICE_PORT        ikhtiyari. Na ho to khali port khud chuna jata hai.
    GHOST_VOICE_PARENT_PID  ikhtiyari. Wo process mar jaye to engine khud band.
    GHOST_VOICE_LOG         ikhtiyari. Log file ka raasta.
    GHOST_VOICE_LOG_LEVEL   ikhtiyari. INFO (default) ya DEBUG.

Keys are not in the environment: Ghost sends them with POST /beta/config, from
the OS keyring, and they live only in this process's memory.

App ko khabar stdout par, har line ek JSON: {"event": "ready", "port": 51234, ...}

Hifazat: sirf isi machine par sunta hai (127.0.0.1), aur token ke baghair koi
request nahi chalti — user ke browser mein khuli koi website is engine se
user ki chabiyon par baat-cheet shuru nahi kar sakti.
"""

from __future__ import annotations

import sys

# Windows ka console aksar cp1252 hota hai; Pipecat ka banner aur Urdu/Hindi
# text us mein chhap nahi sakte aur engine shuru hote hi gir jata hai.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import asyncio  # noqa: E402
import hmac  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from typing import Any  # noqa: E402

# App ko khabar (stdout par ek JSON line). Halka module: bhaari imports neeche hain.
from ghost_voice.host import emit  # noqa: E402
from loguru import logger  # noqa: E402

TOKEN = os.environ.get("GHOST_VOICE_TOKEN", "").strip()
LOG_LEVEL = os.environ.get("GHOST_VOICE_LOG_LEVEL", "INFO").upper()
TOKEN_HEADER = "x-ghost-voice-token"
PORT = 0  # asli port neeche, socket khulne par

# Log: INFO par sirf session ka haal (shuru, band, der, ghalti). User ki baatein
# aur jawab sirf DEBUG par chhapte hain — wo developer ke liye hai.
logger.remove()
logger.add(sys.stderr, level=LOG_LEVEL)
if os.environ.get("GHOST_VOICE_LOG"):
    logger.add(
        os.environ["GHOST_VOICE_LOG"],
        level=LOG_LEVEL,
        rotation="5 MB",
        retention=3,
        encoding="utf-8",
    )

emit("starting")

# Bhaari imports (Pipecat, onnxruntime) yahan — "starting" ke baad, taake app
# ko pata ho ke engine utha hai aur load ho raha hai.
import uvicorn  # noqa: E402
from fastapi import BackgroundTasks, FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from ghost_voice import __version__, pipeline, providers  # noqa: E402
from ghost_voice.config import (  # noqa: E402
    ConfigError,
    Settings,
    clear_app_config,
    has_app_config,
    load_settings,
    missing_keys,
    set_app_config,
)
from pipecat import __version__ as pipecat_version  # noqa: E402
from pipecat.transports.smallwebrtc.connection import (  # noqa: E402
    SmallWebRTCConnection,
)
from pipecat.transports.smallwebrtc.request_handler import (  # noqa: E402
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

handler = SmallWebRTCRequestHandler()


def _open_parent() -> Any:
    """Parent process ka hawala, jab tak zinda hai ye dekhne ke liye."""
    pid = int(os.environ.get("GHOST_VOICE_PARENT_PID", "0") or 0)
    if not pid:
        return None
    if os.name == "nt":
        import ctypes

        # SYNCHRONIZE: sirf "khatam hua ya nahi" poochhne ka haq. Hawala ek hi
        # dafa khulta hai, is liye pid kisi aur process ko mil jaye to dhoka nahi hota.
        return (
            "handle",
            ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid) or None,
        )
    return ("pid", pid)


def _parent_alive(parent: Any) -> bool:
    if parent is None:
        return True
    kind, value = parent
    if value is None:
        return False
    if kind == "handle":
        import ctypes

        return (
            ctypes.windll.kernel32.WaitForSingleObject(value, 0) == 0x102
        )  # WAIT_TIMEOUT = abhi chal raha hai
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


async def _watch_parent() -> None:
    """Ghost crash ho ya Task Manager se maara jaye to engine peeche na rahe."""
    parent = _open_parent()
    if parent is None:
        return
    while True:
        await asyncio.sleep(3)
        if not _parent_alive(parent):
            logger.warning("Ghost band ho gaya — engine bhi band.")
            os._exit(0)


def _warm() -> None:
    """VAD aur Smart Turn ke models ek dafa load — pehli baat-cheet ka bhaari kaam
    abhi, peeche (app ko "ready" pehle hi mil chuka). Thread mein, taake HTTP
    raaste (config, offer) is ke dauran bhi chalte rahein."""
    started = time.monotonic()
    try:
        pipeline.build_user_params(Settings())
    except Exception as error:  # noqa: BLE001
        logger.debug(f"Models pehle se load na ho sake: {error}")
        return
    logger.info(
        f"Engine garam (VAD + Smart Turn) — {round((time.monotonic() - started) * 1000)} ms"
    )
    emit("warm")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    watcher = asyncio.create_task(_watch_parent())
    warm = asyncio.create_task(asyncio.to_thread(_warm))
    emit("ready", port=PORT, version=__version__, pipecat=pipecat_version)
    logger.info("Engine tayyar (ready)")
    try:
        yield
    finally:
        watcher.cancel()
        warm.cancel()
        await pipeline.end_all_sessions("engine band ho raha hai")
        await handler.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def refuse(status: int, code: str, message: str = "", **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": code, "message": message, **extra}, status_code=status
    )


@app.middleware("http")
async def require_token(request: Request, call_next):
    # OPTIONS (browser ka CORS sawal) CORS middleware khud jawab deta hai; wo yahan tak nahi aata.
    given = request.headers.get(TOKEN_HEADER, "")
    if not TOKEN or not hmac.compare_digest(given.encode(), TOKEN.encode()):
        return refuse(401, "unauthorized")
    return await call_next(request)


# Ghost talks to the engine from its own backend (the offer is proxied), so
# the browser rarely reaches this port directly. CORS stays loopback-only as a
# second lock; the token is the real one.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)


@app.get("/beta/health")
async def health():
    return {
        "ok": True,
        "version": __version__,
        "pipecat": pipecat_version,
        "configured": has_app_config(),
        "sessions": len(pipeline.active_sessions()),
    }


@app.get("/beta/status")
async def status():
    return {
        "ok": True,
        "configured": has_app_config(),
        "sessions": [state.public() for state in pipeline.active_sessions()],
    }


@app.post("/beta/config")
async def configure(request: Request):
    """Ghost ki settings (chabiyan, dimagh, kirdar). Sirf yaad-dasht mein; agli Connect se lagti hain."""
    try:
        payload = await request.json()
        settings = set_app_config(payload)
    except ConfigError as error:
        return refuse(400, error.code, str(error))
    except ValueError:
        return refuse(400, "bad_config", "JSON parha nahi gaya")
    pipeline.config_received()
    return {"ok": True, "missing": missing_keys(settings), "config": settings.public()}


@app.post("/beta/models")
async def models(request: Request):
    """Provider par is chabi ke models — Settings ka "Check" aur model ki list."""
    try:
        body = await request.json()
    except ValueError:
        return refuse(400, "bad_request", "JSON parha nahi gaya")
    provider = str(body.get("provider") or "") if isinstance(body, dict) else ""
    key = str(body.get("key") or "").strip() if isinstance(body, dict) else ""
    return await providers.list_models(provider, key)


@app.post("/beta/forget")
async def forget():
    """Chabiyan yaad-dasht se hata do (user ne realtime voice band kiya)."""
    await pipeline.end_all_sessions("app ne config hata diya")
    clear_app_config()
    return {"ok": True}


@app.post("/beta/hangup")
async def hangup():
    await pipeline.end_all_sessions("app ne session band ki")
    return {"ok": True}


@app.post("/api/offer")
async def offer(request: Request, background_tasks: BackgroundTasks):
    """WebRTC ka offer (Pipecat SmallWebRTC ka apna tareeqa). Naya connection = nayi session."""
    try:
        webrtc_request = SmallWebRTCRequest.from_dict(await request.json())
    except (TypeError, ValueError):
        return refuse(400, "bad_offer", "offer parha nahi gaya")

    if not webrtc_request.pc_id:
        # Naya connection: session shuru hone se pehle hi bata do ke kya kam hai,
        # taake app saaf paigham dikha sake (khamosh connection nahi).
        try:
            settings = load_settings()
        except ConfigError as error:
            return refuse(409, error.code, str(error))
        missing = missing_keys(settings)
        if missing:
            return refuse(409, "key_missing", "chabi nahi hai", missing=missing)
        await pipeline.end_all_sessions("nayi session shuru ho rahi hai")

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        background_tasks.add_task(pipeline.run_session, connection)

    return await handler.handle_web_request(
        request=webrtc_request, webrtc_connection_callback=on_connection
    )


@app.patch("/api/offer")
async def ice_candidates(request: Request):
    try:
        body = await request.json()
        patch = SmallWebRTCPatchRequest(
            pc_id=body["pc_id"],
            candidates=[
                IceCandidate(**candidate) for candidate in body.get("candidates", [])
            ],
        )
    except (KeyError, TypeError, ValueError):
        return refuse(400, "bad_candidates", "ICE candidates parhe nahi gaye")
    await handler.handle_patch_request(patch)
    return {"status": "success"}


if __name__ == "__main__":
    if not TOKEN:
        emit("fatal", code="no_token", message="GHOST_VOICE_TOKEN nahi mila")
        raise SystemExit(2)

    # Socket khud kholte hain: port 0 = system khali port deta hai, aur wohi
    # socket uvicorn ko jata hai — "port dekha, phir kisi aur ne le liya" ka mauqa nahi.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", int(os.environ.get("GHOST_VOICE_PORT", "0") or 0)))
    except OSError as error:
        emit("fatal", code="port_busy", message=str(error))
        raise SystemExit(3) from error
    PORT = listener.getsockname()[1]

    uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(
        sockets=[listener]
    )
