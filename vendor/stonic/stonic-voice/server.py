"""Stonic Voice engine ka darwaza — dono voice assistants ka kaam karne wala dimagh yahin hai.

New voice ki poori baat-cheet (WebRTC, /api/offer) aur Classic voice ka kaam
(/work/*, Gemini Live ke `agent_task` se) — dono ek hi Claude Agent SDK dimagh
(stonic_voice/agent.py) par.

App (electron/services/voice-beta-manager.js) ise chalati hai jab user Beta
kholta hai, aur band kar deti hai jab wo wapas jata hai:

    python server.py

Mahaul (environment) se:
    STONIC_VOICE_TOKEN       lazmi. Har request ke saath yehi token aana chahiye.
    STONIC_VOICE_PORT        ikhtiyari. Na ho to khali port khud chuna jata hai.
    STONIC_VOICE_PARENT_PID  ikhtiyari. Wo process mar jaye to engine khud band.
    STONIC_VOICE_LOG         ikhtiyari. Log file ka raasta.
    STONIC_VOICE_LOG_LEVEL   ikhtiyari. INFO (default) ya DEBUG.

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

from loguru import logger  # noqa: E402

# App ko khabar (stdout par ek JSON line). Halka module: bhaari imports neeche hain.
from stonic_voice.host import emit  # noqa: E402


TOKEN = os.environ.get("STONIC_VOICE_TOKEN", "").strip()
LOG_LEVEL = os.environ.get("STONIC_VOICE_LOG_LEVEL", "INFO").upper()
PORT = 0  # asli port neeche, socket khulne par
# Awareness ka ishara (/beta/cue) — app ka banaya, chand jumle.
MAX_CUE_CHARS = 4000

# Log: INFO par sirf session ka haal (shuru, band, der, ghalti). User ki baatein
# aur Stonic ke jawab sirf DEBUG par chhapte hain — wo developer ke liye hai.
logger.remove()
logger.add(sys.stderr, level=LOG_LEVEL)
if os.environ.get("STONIC_VOICE_LOG"):
    logger.add(os.environ["STONIC_VOICE_LOG"], level=LOG_LEVEL, rotation="5 MB", retention=3, encoding="utf-8")

emit("starting")

# Bhaari imports (Pipecat, onnxruntime) yahan — "starting" ke baad, taake app
# ko pata ho ke engine utha hai aur load ho raha hai.
import uvicorn  # noqa: E402
from fastapi import BackgroundTasks, FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pipecat import __version__ as pipecat_version  # noqa: E402
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection  # noqa: E402
from pipecat.transports.smallwebrtc.request_handler import (  # noqa: E402
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from stonic_voice import __version__, gemini_relay, hands, pipeline, providers, work  # noqa: E402
from stonic_voice.config import (  # noqa: E402
    ConfigError,
    Settings,
    clear_app_config,
    has_app_config,
    load_settings,
    missing_keys,
    set_app_config,
)

handler = SmallWebRTCRequestHandler()


def _open_parent() -> Any:
    """Parent process ka hawala, jab tak zinda hai ye dekhne ke liye."""
    pid = int(os.environ.get("STONIC_VOICE_PARENT_PID", "0") or 0)
    if not pid:
        return None
    if os.name == "nt":
        import ctypes

        # SYNCHRONIZE: sirf "khatam hua ya nahi" poochhne ka haq. Hawala ek hi
        # dafa khulta hai, is liye pid kisi aur process ko mil jaye to dhoka nahi hota.
        return ("handle", ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid) or None)
    return ("pid", pid)


def _parent_alive(parent: Any) -> bool:
    if parent is None:
        return True
    kind, value = parent
    if value is None:
        return False
    if kind == "handle":
        import ctypes

        return ctypes.windll.kernel32.WaitForSingleObject(value, 0) == 0x102  # WAIT_TIMEOUT = abhi chal raha hai
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


async def _watch_parent() -> None:
    """App crash ho ya Task Manager se maari jaye to engine peeche na rahe."""
    parent = _open_parent()
    if parent is None:
        return
    while True:
        await asyncio.sleep(3)
        if not _parent_alive(parent):
            logger.warning("App band ho gayi — engine bhi band.")
            os._exit(0)


def _warm() -> None:
    """Pehli baat-cheet ka bhaari kaam abhi, peeche (app ko "ready" pehle hi mil chuka):

      - VAD aur Smart Turn ke models ek dafa load
      - agent ka engine (Claude Agent SDK) import — garam disk par ~1 s, thandi par 16 s (naapa)

    Thread mein, taake HTTP raaste (config, offer) is ke dauran bhi chalte rahein.
    """
    try:
        pipeline.build_user_params(Settings())
    except Exception as error:  # noqa: BLE001
        logger.debug(f"Models pehle se load na ho sake: {error}")
    pipeline.preload_agent()
    logger.info(f"Engine garam (models + agent ka engine) — app ke chalane ke {_since_spawn()} baad")


def _since_spawn() -> str:
    """App ne engine kab chalaya (STONIC_VOICE_SPAWNED_AT, epoch ms — voice-beta-manager) se ab tak."""
    try:
        spawned = int(os.environ.get("STONIC_VOICE_SPAWNED_AT", "0") or 0)
    except ValueError:
        spawned = 0
    return f"{round(time.time() * 1000) - spawned} ms" if spawned else "?"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    watcher = asyncio.create_task(_watch_parent())
    warm = asyncio.create_task(asyncio.to_thread(_warm))
    emit("ready", port=PORT, version=__version__, pipecat=pipecat_version)
    logger.info(f"Engine tayyar (ready) — app ke chalane ke {_since_spawn()} baad")
    try:
        yield
    finally:
        watcher.cancel()
        warm.cancel()
        await pipeline.end_all_sessions("engine band ho raha hai")
        await work.close_all()
        await gemini_relay.close()
        await handler.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def refuse(status: int, code: str, message: str = "", **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message, **extra}, status_code=status)


@app.middleware("http")
async def require_token(request: Request, call_next):
    # OPTIONS (browser ka CORS sawal) CORS middleware khud jawab deta hai; wo yahan tak nahi aata.
    if request.url.path.startswith(gemini_relay.PATH + "/"):
        # Gemini ka pul (muft key wala dimagh): Claude Code CLI apna ticket bhejta hai,
        # engine ka token nahi. Ticket ki jaanch pul khud karta hai (gemini_relay.route_for).
        return await call_next(request)
    given = request.headers.get("x-stonic-voice-token", "")
    if not TOKEN or not hmac.compare_digest(given.encode(), TOKEN.encode()):
        return refuse(401, "unauthorized")
    return await call_next(request)


# App ka page http://127.0.0.1:<port> (ya dev mein http://localhost:5173) se aata
# hai aur engine doosre port par hai, is liye browser CORS ki ijazat maangta hai.
# Asal pehra token hai; origin ki shart sirf ek aur taala hai.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)


# Gemini ka pul: Classic / New ka Claude Code dimagh muft Gemini key par (gemini_relay.py).
gemini_relay.install(app)


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
    report: dict[str, Any] = {
        "ok": True,
        "configured": has_app_config(),
        "sessions": [state.public() for state in pipeline.active_sessions()],
    }
    if os.environ.get("STONIC_VOICE_AUDIT"):
        # Runtime banane wale ke liye (scripts/setup-voice-runtime.js): asli
        # baat-cheet ke dauran kaun se packages waqai load hue.
        report["modules"] = sorted({name.split(".")[0] for name in sys.modules})
    return report


@app.post("/beta/config")
async def configure(request: Request):
    """App ki settings (chabiyan, dimagh, kirdar). Sirf yaad-dasht mein; agli Connect se lagti hain."""
    try:
        payload = await request.json()
        settings = set_app_config(payload)
    except ConfigError as error:
        return refuse(400, error.code, str(error))
    except ValueError:
        return refuse(400, "bad_config", "JSON parha nahi gaya")
    pipeline.config_received()
    _log_start_timing(payload.get("timing") if isinstance(payload, dict) else None)
    return {"ok": True, "missing": missing_keys(settings), "config": settings.public()}


def _log_start_timing(timing: Any) -> None:
    """App ka bheja hua "Start" ka hisaab (voice-beta-manager `start`) — sirf log ke liye, settings mein nahi."""
    if not isinstance(timing, dict):
        return

    def ms(name: str) -> str:
        value = timing.get(name)
        return f"{int(value)} ms" if isinstance(value, (int, float)) and 0 <= value < 3_600_000 else "?"

    logger.info(
        f"Start: app ne {ms('since_click_ms')} pehle dabaya — engine ka intezar {ms('engine_wait_ms')}, "
        f"gateway {ms('gateway_ms')}"
    )


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
    """Chabiyan yaad-dasht se hata do (user ne Beta chhor diya)."""
    await pipeline.end_all_sessions("app ne config hata diya")
    clear_app_config()
    return {"ok": True}


@app.post("/beta/hangup")
async def hangup():
    await pipeline.end_all_sessions("app ne session band ki")
    return {"ok": True}


@app.post("/beta/tool-result")
async def tool_result(request: Request):
    """App ne Stonic ka koi tool chala liya: us ka natija (bridge.py)."""
    try:
        body = await request.json()
    except ValueError:
        return refuse(400, "bad_request", "JSON parha nahi gaya")
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        return refuse(400, "bad_request", "id chahiye")
    # False = us call ka intezar khatam ho chuka (waqt guzar gaya ya session band).
    return {"ok": True, "delivered": pipeline.resolve_tool(body["id"], body)}


@app.post("/beta/mouse")
async def mouse(request: Request):
    """App ki khabar: Stonic ka doosra computer agent mouse chala raha hai / chhor chuka (hands.py)."""
    try:
        body = await request.json()
    except ValueError:
        return refuse(400, "bad_request", "JSON parha nahi gaya")
    busy = bool(body.get("busy")) if isinstance(body, dict) else False
    hands.set_mouse_taken(busy)
    return {"ok": True, "busy": busy}


@app.post("/beta/cue")
async def cue(request: Request):
    """Awareness (app) ka ishara: Stonic khud pehle bole — wohi jo Standard voice ko jata hai."""
    try:
        body = await request.json()
    except ValueError:
        return refuse(400, "bad_request", "JSON parha nahi gaya")
    text = str(body.get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        return refuse(400, "bad_request", "text chahiye")
    # False = koi baat-cheet nahi, ya dimagh is waqt kaam mein (app dobara koshish karti hai).
    return {"ok": True, "delivered": await pipeline.awareness_cue(text[:MAX_CUE_CHARS])}


@app.post("/beta/interrupt")
async def interrupt():
    """Stop: jo kaam chal raha hai wo ruke, baat-cheet chalti rahe."""
    return {"ok": True, "stopped": await pipeline.interrupt_work()}


# ── Classic voice ka kaam (work.py): wohi Claude Code dimagh, text mein ──────
#
# Gemini Live (Classic) `agent_task` se kaam deta hai; app (main process) yahan
# bhejti hai. Natija stdout ki "work" khabron mein aata hai (host.emit) — jawab
# mein sirf "shuru hua / chalte kaam mein gaya".


@app.post("/work/task")
async def work_task(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return refuse(400, "bad_request", "JSON parha nahi gaya")
    try:
        result = await work.submit(body)
    except ConfigError as error:
        return refuse(400, error.code, str(error))
    if not result.get("ok"):
        return refuse(409, str(result.get("code") or "work_unavailable"))
    return result


async def _desk_of(request: Request) -> str:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return str(body.get("desk") or "") if isinstance(body, dict) else ""


@app.post("/work/stop")
async def work_stop(request: Request):
    try:
        return {"ok": True, "stopped": await work.stop(await _desk_of(request))}
    except ConfigError as error:
        return refuse(400, error.code, str(error))


@app.post("/work/close")
async def work_close(request: Request):
    try:
        return {"ok": True, "closed": await work.close(await _desk_of(request))}
    except ConfigError as error:
        return refuse(400, error.code, str(error))


@app.post("/work/status")
async def work_status(request: Request):
    try:
        return {"ok": True, **work.status(await _desk_of(request))}
    except ConfigError as error:
        return refuse(400, error.code, str(error))


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

    return await handler.handle_web_request(request=webrtc_request, webrtc_connection_callback=on_connection)


@app.patch("/api/offer")
async def ice_candidates(request: Request):
    try:
        body = await request.json()
        patch = SmallWebRTCPatchRequest(
            pc_id=body["pc_id"],
            candidates=[IceCandidate(**candidate) for candidate in body.get("candidates", [])],
        )
    except (KeyError, TypeError, ValueError):
        return refuse(400, "bad_candidates", "ICE candidates parhe nahi gaye")
    await handler.handle_patch_request(patch)
    return {"status": "success"}


if __name__ == "__main__":
    if not TOKEN:
        emit("fatal", code="no_token", message="STONIC_VOICE_TOKEN nahi mila")
        raise SystemExit(2)

    # Socket khud kholte hain: port 0 = system khali port deta hai, aur wohi
    # socket uvicorn ko jata hai — "port dekha, phir kisi aur ne le liya" ka mauqa nahi.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", int(os.environ.get("STONIC_VOICE_PORT", "0") or 0)))
    except OSError as error:
        emit("fatal", code="port_busy", message=str(error))
        raise SystemExit(3) from error
    PORT = listener.getsockname()[1]
    gemini_relay.set_port(PORT)

    uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(sockets=[listener])
