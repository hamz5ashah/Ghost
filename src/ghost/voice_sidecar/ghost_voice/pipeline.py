"""Ek voice session ki poori pipeline: sunna -> dimagh -> bolna.

    mic (app, echo cancel)
      -> SeamlessGeminiSTTService  Google transcribe-live: awaaz -> text (ears.py:
                                   Google ki ~10 min wali line chup chaap badalti hai)
      -> user aggregator         Silero VAD + Smart Turn v3 + Google ka poora jumla
                                 (turns.py): "user chup hua?"
      -> dimagh                  jawab, lafz ba lafz
      -> awaaz                   text -> Charon / Aoede ki awaaz
      -> speaker (app)

Ye Stonic ke Voice Lab ki pipeline hai: wohi services, wohi tarteeb, wohi
numbers. Har hissa Pipecat ka bana banaya service hai.

Ghost port (Phase 3, talk-only). Removed from Stonic's version:
  - the Stonic Cloud path (cloud.py, `is_cloud` branches): ears, brain and voice
    now always use the user's own keys, sent by Ghost from the OS keyring
  - the agent brain (Claude Agent SDK, PowerShell/files/screen tools, the in-app
    tool bridge, the "floor" and awareness cues): every brain here only talks
  - koi recording nahi: user ki awaaz aur baatein disk par nahi jatin (unchanged)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import BotStartedSpeakingFrame, ErrorFrame, LLMRunFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.google.gemini_live.stt import GeminiSTTService
from pipecat.services.google.tts import GeminiTTSService
from pipecat.services.llm_service import LLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.empty_user_turn import EmptyUserTurnConfig
from pipecat.turns.user_start import (
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from ghost_voice import errors
from ghost_voice.bridge import AppChannel
from ghost_voice.config import (
    AUDIO_IN_SAMPLE_RATE,
    AUDIO_OUT_SAMPLE_RATE,
    ConfigError,
    Settings,
    claude_extra,
    claude_thinking,
    conversation_prompt,
    gemini_thinking,
    hint_phrases,
    load_settings,
    missing_keys,
    opening_cue,
    openrouter_reasoning,
)
from ghost_voice.ears import GateSettings, SeamlessGeminiSTTService
from ghost_voice.tts_gemini import (
    ExpressiveSpeech,
    GeminiDirectedTTSService,
    KeySpeech,
    LiveSpeech,
    SpeechLanes,
)
from ghost_voice.turns import GhostTurnStopStrategy

FIRST_AUDIO_PATIENCE_SECS = 8.0
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def build_stt(settings: Settings) -> SeamlessGeminiSTTService:
    ears = settings.ears
    stt_settings = GeminiSTTService.Settings(
        model=ears.model,
        # Khali list = zubaan khud pehchano (code-switching ke liye).
        languages=list(ears.languages),
        adaptation_phrases=hint_phrases(settings) or None,
    )
    # Google ko sirf bolne wali awaaz (ears.py "gate").
    gate = GateSettings(
        enabled=bool(ears.gate),
        confidence=float(ears.gate_confidence),
        min_volume=float(ears.gate_min_volume),
        start_secs=float(ears.gate_start_secs),
        hangover_secs=float(ears.gate_hangover_secs),
        preroll_secs=float(ears.gate_preroll_secs),
    )
    return SeamlessGeminiSTTService(
        api_key=settings.keys.gemini, settings=stt_settings, gate=gate
    )


def build_llm(settings: Settings) -> LLMService:
    """Dimagh — sirf baat. Har dimagh ki library usi raaste ke andar import hoti
    hai, engine ke boot par nahi: jo dimagh chuna hi nahi, us ki library disk se
    parhi bhi nahi jati (thandi disk + antivirus par yehi sab se bara farq hai)."""
    brain = settings.brain
    prompt = conversation_prompt(settings)
    key = settings.keys.for_brain(brain.provider)

    if brain.provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        llm_settings = AnthropicLLMService.Settings(
            model=brain.model,
            system_instruction=prompt,
            max_tokens=settings.limits.max_tokens,
            enable_prompt_caching=settings.limits.prompt_caching,
            extra=claude_extra(brain.model, brain.thinking),
        )
        thinking = claude_thinking(brain.model, brain.thinking)
        if thinking is not None:
            llm_settings.thinking = AnthropicLLMService.ThinkingConfig(**thinking)
        return AnthropicLLMService(api_key=key, settings=llm_settings)

    if brain.provider == "gemini":
        from pipecat.services.google.llm import GoogleLLMService

        llm_settings = GoogleLLMService.Settings(
            model=brain.model,
            system_instruction=prompt,
            max_tokens=settings.limits.max_tokens,
        )
        thinking = gemini_thinking(brain.model, brain.thinking)
        if thinking is not None:
            llm_settings.thinking = GoogleLLMService.ThinkingConfig(**thinking)
        return GoogleLLMService(api_key=key, settings=llm_settings)

    if brain.provider == "openrouter":
        from pipecat.services.openrouter.llm import OpenRouterLLMService

        return OpenRouterLLMService(
            api_key=key,
            settings=OpenRouterLLMService.Settings(
                model=brain.model,
                system_instruction=prompt,
                extra={
                    "extra_body": {"reasoning": openrouter_reasoning(brain.thinking)}
                },
            ),
        )

    if brain.provider == "deepseek":
        from pipecat.services.openai.llm import OpenAILLMService

        # DeepSeek speaks the OpenAI chat API at its own address — the same
        # service Stonic used for its gateway's text path, pointed here instead.
        return OpenAILLMService(
            api_key=key,
            base_url=DEEPSEEK_BASE_URL,
            settings=OpenAILLMService.Settings(
                model=brain.model, system_instruction=prompt
            ),
        )

    raise ConfigError(
        "bad_config", f"brain.provider '{brain.provider}' is not supported"
    )


def build_speaker(
    settings: Settings, notify: Any = None
) -> LiveSpeech | ExpressiveSpeech:
    """Awaaz kahan se banegi — user ka chuna hua raasta (tts_gemini.py ke sar-e-warq par sab).

    `notify`: app ko paigham (Expressive ki hadd par "ab Standard se bol raha hoon").
    """
    voice = settings.voice

    def standard() -> LiveSpeech:
        return LiveSpeech(
            api_key=settings.keys.gemini,
            model=voice.live_model,
            voice=settings.voice_name,
        )

    if settings.voice_engine == "live":
        return standard()
    lanes = SpeechLanes(
        keys=[("key", settings.keys.gemini), ("backup", settings.keys.gemini_backup)],
        models=[voice.model],
    )
    return ExpressiveSpeech(
        key=KeySpeech(lanes=lanes, voice=settings.voice_name),
        make_standard=standard,
        notify=notify,
    )


def build_tts(settings: Settings, notify: Any = None) -> GeminiDirectedTTSService:
    # Dimagh ki likhi ada ki hidayat ("[soft, slow]", "[sigh]") yahan awaaz tak
    # pohnchti hai — tts_gemini.py.
    voice = settings.voice
    return GeminiDirectedTTSService(
        speaker=build_speaker(settings, notify),
        style=voice.style,
        chunk_min_chars=voice.chunk_min_chars,
        chunk_next_min_chars=voice.chunk_next_min_chars,
        chunk_max_hold_secs=voice.chunk_max_hold_secs,
        # Pipecat itni der awaaz ka intezar karta hai; us ke baad line ko "awaaz
        # nahi aayi" maan kar ghalti bhejta hai (default 3 s). Thande connection
        # par pehli awaaz 3 s se upar ja sakti hai, aur apni key par hadd khulne
        # ka intezar 6 s tak hai — in par jhooti ghalti na aaye.
        stop_frame_timeout_s=FIRST_AUDIO_PATIENCE_SECS,
        settings=GeminiTTSService.Settings(
            model=settings.voice_model, voice=settings.voice_name
        ),
    )


def build_user_params(settings: Settings) -> LLMUserAggregatorParams:
    """ "User chup ho gaya?" ka poora nizaam: Silero VAD + Smart Turn v3."""
    turn = settings.turn
    vad = SileroVADAnalyzer(
        params=VADParams(
            confidence=turn.vad_confidence,
            start_secs=turn.vad_start_secs,
            stop_secs=turn.vad_stop_secs,
        )
    )
    if turn.smart_turn:
        # Smart Turn + Google ka poora jumla (turns.py): chhoti Urdu baat par 3 s ka intezar nahi.
        stop_strategy = GhostTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=turn.smart_turn_max_wait_secs)
            ),
            finished_wait_secs=turn.finished_wait_secs,
        )
    else:
        stop_strategy = SpeechTimeoutUserTurnStopStrategy(
            user_speech_timeout=turn.speech_timeout_secs
        )
    # Baari shuru: VAD (awaaz), ya Google ka PAKKA jumla (VAD na pakre to). Pipecat
    # ka default Google ke kacche likhe (interim) par bhi baari shuru karta hai — aur
    # har nayi baari jawab ko kaat-ti hai. Asal log (21 Oct 2026): jumla poora hote
    # hi ek bhatka interim aaya, nayi baari bani (0 huroof), dimagh kata, aur awaaz
    # ~70 s chup rahi. Pakka jumla hamesha lafz laata hai; kaccha shor bhi ho sakta hai.
    start_strategies = [
        VADUserTurnStartStrategy(),
        TranscriptionUserTurnStartStrategy(use_interim=False),
    ]
    return LLMUserAggregatorParams(
        vad_analyzer=vad,
        user_turn_strategies=UserTurnStrategies(
            start=start_strategies, stop=[stop_strategy]
        ),
        # Khali baari (khansi / shor ne jawab kaata, koi lafz nahi): Pipecat ka apna paigham.
        empty_user_turn=EmptyUserTurnConfig(),
    )


# ── Chalti session ka haal ───────────────────────────────────────────────────


@dataclass
class SessionState:
    """Ek session ka haal — /beta/status isi ko dikhata hai. Is mein koi baat-cheet nahi."""

    id: int
    settings: Settings
    started_at: float = field(default_factory=time.time)
    turns: int = 0
    last_latency_ms: int | None = None
    last_error: dict[str, Any] | None = None
    worker: PipelineWorker | None = None
    started_mono: float = field(default_factory=time.monotonic)
    config_mono: float | None = None
    connected_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seconds": round(time.time() - self.started_at),
            "turns": self.turns,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
            **self.settings.public(),
        }


_sessions: dict[int, SessionState] = {}
_next_id = 0


def active_sessions() -> list[SessionState]:
    return list(_sessions.values())


async def end_all_sessions(reason: str) -> None:
    """Mic ek hai, baat-cheet ek: nayi session se pehle purani band."""
    for state in active_sessions():
        if state.worker is not None:
            logger.info(f"Voice session {state.id} band ki ja rahi hai — {reason}")
            await state.worker.cancel(reason=reason)


# ── Connection ka "juda" (Pipecat ka race) aur shuru ka waqt ─────────────────
#
# Pipecat 1.12 (SmallWebRTCConnection) ka ek race — 21 Oct 2026 ko Stonic mein
# pakra aur dohraya:
#
#   - App ka WebRTC pipeline shuru hone se PEHLE jud jaye to Pipecat "connected"
#     ki khabar rok leta hai (pipeline tayyar nahi), aur pipeline ke `connect()`
#     par `is_connected()` se dobara poochhta hai.
#   - `is_connected()` ka jawab: "app ka aakhri ping 3 s ke andar aaya?" Agar
#     theek us lamhe engine ka loop pehle 3 s se zyada ruka ho (thanda PC,
#     antivirus), to jawab "nahi" — aur "connected" KABHI nahi chalta: na salaam,
#     na awaaz.
#
# `_reconnect_if_missed`: connection Pipecat ke apne hisaab se jura hai magar
# "connected" ki khabar nahi aayi, to wohi sarkari `connection.connect()`
# dobara — jo "already connected" par khabar dobara chalata hai.

CONNECT_GRACE_SECS = 2.0  # "connected" ki khabar itni der mein na aaye to dobara
CONNECT_WATCH_SECS = (
    30.0  # is ke baad nigrani band (app khud bhi koshish chhor deti hai)
)

# Aakhri config kab aaya (POST /beta/config) — shuru ke waqt ka hisaab.
_config_at: float | None = None


def config_received() -> None:
    """server.py: app ka config aaya (user ne Start dabaya). Waqt ke hisaab ke liye."""
    global _config_at
    _config_at = time.monotonic()


def _since(start: float | None) -> str:
    return f"{round((time.monotonic() - start) * 1000)} ms" if start else "?"


async def _reconnect_if_missed(
    connection: SmallWebRTCConnection, state: SessionState
) -> None:
    """ "connected" chhoot gaya ho to wapas lao."""
    deadline = time.monotonic() + CONNECT_WATCH_SECS
    joined_since: float | None = None
    while state.connected_at is None and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        if state.connected_at is not None:
            return
        # `is_connected()` tab hi sach jab pipeline `connect()` kar chuki ho aur ping taaza ho.
        if not connection.is_connected():
            joined_since = None
            continue
        joined_since = joined_since or time.monotonic()
        if time.monotonic() - joined_since < CONNECT_GRACE_SECS:
            continue
        logger.warning(
            f"Voice session {state.id}: connection jura hai magar 'connected' ki khabar nahi aayi "
            f"(Pipecat race) — dobara chalayi, shuru ke {_since(state.started_mono)} baad"
        )
        await connection.connect()
        return


class _StartTimeline(BaseObserver):
    """Shuru ka hisaab log mein: pehli awaaz kab nikli (config / session ke kitni der baad)."""

    def __init__(self, state: SessionState) -> None:
        super().__init__()
        self._state = state
        self._done = False

    async def on_push_frame(self, data: FramePushed) -> None:
        if self._done or not isinstance(data.frame, BotStartedSpeakingFrame):
            return
        self._done = True
        state = self._state
        logger.info(
            f"Voice session {state.id}: pehli awaaz — session ke {_since(state.started_mono)} baad, "
            f"config ke {_since(state.config_mono)} baad"
        )


async def run_session(connection: SmallWebRTCConnection) -> None:
    """Ek app connection = ek session."""
    global _next_id
    try:
        settings = load_settings()
    except ConfigError as error:
        logger.error(f"Voice session shuru nahi ho saki: {error}")
        await connection.disconnect()
        return
    missing = missing_keys(settings)
    if missing:
        logger.error(
            f"Voice session shuru nahi ho saki — chabiyan nahi hain: {', '.join(missing)}"
        )
        await connection.disconnect()
        return

    _next_id += 1
    state = SessionState(id=_next_id, settings=settings, config_mono=_config_at)
    _sessions[state.id] = state
    logger.info(
        f"Voice session {state.id} shuru (config ke {_since(state.config_mono)} baad) — {settings.public()}"
    )
    watch: asyncio.Task[None] | None = None

    try:
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        # Engine → app ka raasta ("abhi kya ho raha hai" ki khabrein).
        channel = AppChannel()
        # VAD / Smart Turn ke models event loop ke BAHAR (~0.5 s): is waqt app ka
        # WebRTC jud raha hota hai, aur loop ka rukna Pipecat ke "connected" ko kho deta hai.
        user_params = await asyncio.to_thread(build_user_params, settings)
        llm = build_llm(settings)
        stt = build_stt(settings)
        tts = build_tts(settings, notify=channel.send)

        context = LLMContext()
        aggregators = LLMContextAggregatorPair(context, user_params=user_params)
        processors = [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            tts,
            transport.output(),
            aggregators.assistant(),
        ]

        latency = UserBotLatencyObserver()
        worker = PipelineWorker(
            Pipeline(processors),
            params=PipelineParams(
                audio_in_sample_rate=AUDIO_IN_SAMPLE_RATE,
                audio_out_sample_rate=AUDIO_OUT_SAMPLE_RATE,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[latency, _StartTimeline(state)],
            # Bhooli hui session ka mic aur connection ghanton khula na rahe.
            idle_timeout_secs=settings.session.idle_timeout_secs or None,
            # Band karne ka faisla neeche `on_idle_timeout` mein: pehle app ko wajah.
            cancel_on_idle_timeout=False,
        )
        state.worker = worker
        channel.attach(worker)

        @worker.event_handler("on_idle_timeout")
        async def on_idle_timeout(_worker):
            logger.info(f"Voice session {state.id}: lambi khamoshi — band")
            await worker.queue_frame(
                RTVIServerMessageFrame(data={"type": "ghost-idle"})
            )
            await worker.cancel(reason="idle timeout")

        @latency.event_handler("on_latency_measured")
        async def on_latency_measured(_observer, seconds: float):
            state.turns += 1
            state.last_latency_ms = round(seconds * 1000)
            logger.info(
                f"Voice session {state.id}: jawab {state.last_latency_ms} ms mein"
            )
            await worker.queue_frame(
                RTVIServerMessageFrame(
                    data={"type": "ghost-latency", "ms": state.last_latency_ms}
                )
            )

        @worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(_worker, frame: ErrorFrame):
            message = errors.describe(frame, settings)
            state.last_error = {**message, "at": round(time.time())}
            logger.warning(
                f"Voice session {state.id}: {message['code']} ({message['part']}) — {message['detail']}"
            )
            await worker.queue_frame(RTVIServerMessageFrame(data=message))

        @transport.event_handler("on_client_connected")
        async def on_client_connected(_transport, _client):
            if state.connected_at is None:
                state.connected_at = time.monotonic()
                logger.info(
                    f"Voice session {state.id}: connection jura — session ke {_since(state.started_mono)} baad"
                )
            if settings.session.greeting:
                context.add_message({"role": "user", "content": opening_cue(settings)})
                await worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport, _client):
            await worker.cancel()

        watch = asyncio.get_running_loop().create_task(
            _reconnect_if_missed(connection, state), name="ghost-connect-watch"
        )
        await PipelineRunner(handle_sigint=False).run(worker)
    except ConfigError as error:
        logger.error(f"Voice session {state.id} shuru nahi ho saki: {error}")
        await connection.disconnect()
    except Exception:
        logger.exception(f"Voice session {state.id} toot gayi")
    finally:
        if watch is not None:
            watch.cancel()
        _sessions.pop(state.id, None)
        logger.info(
            f"Voice session {state.id} band — {state.turns} jawab, "
            f"aakhri {state.last_latency_ms} ms, {round(time.time() - state.started_at)} s"
        )
