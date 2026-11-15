"""Ek voice session ki poori pipeline: sunna -> dimagh -> bolna.

    mic (app, echo cancel)
      -> SeamlessGeminiSTTService  Google transcribe-live: awaaz -> text (ears.py:
                                   Google ki ~10 min wali line chup chaap badalti hai)
      -> user aggregator         Silero VAD + Smart Turn + Google ka poora jumla
                                 (turns.py): "user chup hua?"
      -> dimagh                  jawab, lafz ba lafz
      -> awaaz                   text -> Charon / Aoede ki awaaz
      -> speaker (app)

Ye Voice Lab (voice-lab/lab/pipeline.py) ki pipeline hai, hoobahoo: wohi
services, wohi tarteeb, wohi numbers. Har hissa Pipecat ka bana banaya service
hai. Yahan jo alag hai:
  - settings app se aati hain (raasta, chabiyan, dimagh, kirdar), .env se nahi
  - do raaste (config.py ke sar-e-warq par tafseel):
      Stonic Cloud   teeno hisse hamare gateway se, user ke plan ke andar
      apni keys      dimagh Claude / Gemini / Groq; awaaz Gemini Live ya 3.8 TTS
  - koi recording nahi: user ki awaaz aur baatein disk par nahi jatin
  - dimagh Claude ho to wo kaam bhi karta hai (agent.py — Claude Agent SDK):
    PowerShell, files, Stonic ke apne tools (yaadash, web search…) jo app ke
    andar chalte hain (bridge.py), aur screen ke haath — mouse / keyboard
    (screen.py). Baqi dimagh sirf bolte hain, jaise pehle.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import BotStartedSpeakingFrame, ErrorFrame, LLMMessagesAppendFrame, LLMRunFrame
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
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from stonic_voice import errors
from stonic_voice.bridge import AppChannel, ToolBridge
from stonic_voice.cloud import CloudSpeech, allow_plain_gateway, listen_http_options
from stonic_voice.config import (
    AUDIO_IN_SAMPLE_RATE,
    AUDIO_OUT_SAMPLE_RATE,
    ConfigError,
    Settings,
    agent_home,
    agent_system_prompt,
    agent_thinking,
    agent_tools,
    agent_workspace,
    claude_extra,
    claude_thinking,
    conversation_prompt,
    gemini_thinking,
    groq_reasoning,
    openrouter_reasoning,
    hint_phrases,
    load_settings,
    missing_keys,
    opening_cue,
)
from stonic_voice.ears import GateSettings, SeamlessGeminiSTTService
from stonic_voice.floor import Floor
from stonic_voice.tts_gemini import ExpressiveSpeech, GeminiDirectedTTSService, KeySpeech, LiveSpeech, SpeechLanes
from stonic_voice.turns import EMPTY_TURN_CUE, StonicTurnStopStrategy


FIRST_AUDIO_PATIENCE_SECS = 8.0


def build_stt(settings: Settings) -> SeamlessGeminiSTTService:
    ears = settings.ears
    stt_settings = GeminiSTTService.Settings(
        model=ears.model,
        # Khali list = zubaan khud pehchano (code-switching ke liye).
        languages=list(ears.languages),
        adaptation_phrases=hint_phrases(settings) or None,
    )
    # Google ko sirf bolne wali awaaz (ears.py "gate") — Cloud par bhi, apni key par bhi.
    gate = GateSettings(
        enabled=bool(ears.gate),
        confidence=float(ears.gate_confidence),
        min_volume=float(ears.gate_min_volume),
        start_secs=float(ears.gate_start_secs),
        hangover_secs=float(ears.gate_hangover_secs),
        preroll_secs=float(ears.gate_preroll_secs),
    )
    if not settings.is_cloud:
        return SeamlessGeminiSTTService(api_key=settings.keys.gemini, settings=stt_settings, cloud=False, gate=gate)

    # Stonic Cloud: wohi service, sirf pata gateway ka aur "key" Stonic ka token.
    # Model ka naam gateway khud lagata hai — yahan wala sirf ishara hai.
    cloud = settings.cloud
    stt = SeamlessGeminiSTTService(
        api_key=cloud.token, http_options=listen_http_options(cloud.url), settings=stt_settings, cloud=True, gate=gate
    )
    allow_plain_gateway(stt, cloud.url)
    return stt


def agent_env(settings: Settings, home: str) -> dict[str, str]:
    """Claude Code (SDK ka process) ka mahaul: apna ghar + raasta (Cloud gateway / apni key / OpenRouter).

    New ki baat-cheet aur Classic ka kaam (work.py) — dono isi se: ek hi raasta, ek hi qaida.
    """
    from stonic_voice.agent import base_env, cloud_env, gemini_env, key_env, openrouter_env

    brain = settings.brain
    if settings.is_cloud:
        cloud = settings.cloud
        auth = cloud_env(cloud.url, cloud.token, cloud.purpose)
    elif brain.provider == "openrouter":
        auth = openrouter_env(settings.keys.openrouter, brain.model)
    elif brain.provider == "gemini":
        # Muft Gemini key: wohi Claude Code dimagh, model Gemini — engine ke pul (gemini_relay.py)
        # se. Key engine mein rehti hai; CLI ko sirf pul ka ticket.
        from stonic_voice import gemini_relay

        ticket = gemini_relay.ticket_for(settings.keys.gemini, brain.model, brain.thinking)
        auth = gemini_env(gemini_relay.base_url(), ticket, brain.model)
    else:
        auth = key_env(settings.keys.anthropic)
    return {**base_env(home), **auth}


def build_agent_llm(settings: Settings, channel: AppChannel, bridge: ToolBridge | None) -> LLMService:
    """Dimagh jo kaam bhi karta hai: Claude Agent SDK (agent.py)."""
    # Yahan import: SDK sirf isi raaste par chahiye, aur purane runtime (jis mein
    # SDK nahi) par baqi dimagh phir bhi chalte hain.
    try:
        from stonic_voice.agent import AgentConfig, AppTool, saved_session
        from stonic_voice.agent_service import ClaudeAgentLLMService
    except ImportError as error:
        raise ConfigError("runtime_missing", f"Agent ka engine is build mein nahi hai: {error}") from error

    brain = settings.brain
    # Is chat ki notebook (Claude Code jaisi): disk par ho to wahin se — poori
    # pichli baat, tools samet; prompt mein dobara nahi likhi jati. Na ho to isi
    # naam se nayi, aur chat ki pichli baat (agar ho) prompt mein. Resume phir bhi
    # nakaam ho to agent.py nayi notebook isi "fresh" prompt se kholta hai.
    home = agent_home()
    session = settings.agent.session
    resume = bool(session) and saved_session(home, session)
    config = AgentConfig(
        model=brain.model,
        system_prompt=agent_system_prompt(settings, with_history=not resume),
        env=agent_env(settings, home),
        cwd=agent_workspace(),
        effort=brain.thinking,
        thinking=agent_thinking(brain.model, brain.thinking),
        builtin_tools=list(settings.agent_limits.tools),
        app_tools=[AppTool(name=tool.name, description=tool.description, schema=tool.schema) for tool in agent_tools(settings)],
        max_turns=settings.agent_limits.max_steps,
        floor=settings.uses_floor,
        session_id=session,
        resume=resume,
        fresh_system_prompt=agent_system_prompt(settings) if resume else None,
        # Nayi yaadash: Claude Agent SDK ki apni auto memory isi folder par.
        memory_dir=settings.agent.memory_dir,
        # Saanjha waqt (life.py): baat-cheet ka hisaab (har kirdar) + naam ka tool (Soulmate).
        life=settings.life,
        # User ki skills (app ka Skills card): Claude Code ka plugin + Skill tool.
        skills_plugin=settings.skills.plugin if settings.skills.on else "",
        skills=settings.skills.qualified,
    )
    # Baat-cheet ka ehsaas (floor.py): "ye baat mujh se hai?" + khamoshi mein apna moqa.
    floor = Floor(settings.floor_limits) if settings.uses_floor else None
    return ClaudeAgentLLMService(
        config=config, channel=channel, bridge=bridge, screen=build_screen(settings), floor=floor, parent_pid=parent_pid(),
    )


def preload_agent() -> bool:
    """Session ke bhaari imports abhi kar lo — event loop ke BAHAR.

      - Pipecat ka OpenAI adapter (`openai` library samet, ~1,500 modules): Pipecat
        har dimagh (LLMService, hamara Claude agent bhi) banate waqt pehli dafa ise
        import karta hai. Naapa (21 Oct 2026): pehli session ka `build_llm` is ki
        wajah se 2.6 s (garam disk) loop par — theek us khidki mein jahan Pipecat
        ka "connected" kho jata hai (neeche "Pipecat ka race").
      - Agent ke modules (Claude Agent SDK, screen ke haath): garam ~1 s, thandi disk 16 s.

    Engine ke boot ke foran baad background thread mein (server.py `_warm`), aur
    session se pehle `asyncio.to_thread` se. Har jagah yehi function aur yehi
    tarteeb: do thread ek saath bulayen to Python ke module-lock unhein ek ke baad
    ek chalate hain. False = is runtime mein agent ka engine nahi (purana build) —
    wohi baat `build_agent_llm` user ko saaf wajah ke saath batata hai.
    """
    try:
        import pipecat.adapters.services.open_ai_adapter  # noqa: F401
    except Exception as error:  # noqa: BLE001 — na mile to pehle jaisa (LLMService khud import karega)
        logger.warning(f"Pipecat ka adapter pehle se load nahi hua: {type(error).__name__}: {error}")
    try:
        import stonic_voice.agent  # noqa: F401
        import stonic_voice.agent_service  # noqa: F401
        import stonic_voice.aim  # noqa: F401
        import stonic_voice.screen  # noqa: F401
    except ImportError as error:
        logger.debug(f"Agent ka engine pehle se load nahi hua: {error}")
        return False
    except Exception as error:  # noqa: BLE001 — ye sirf raftaar hai: kabhi session na giraye
        # (Misal: do thread ek saath import karein aur Python "deadlock detected" kahe.)
        # Session phir `build_agent_llm` mein khud import karti hai, pehle jaisa.
        logger.warning(f"Agent ka engine pehle se load nahi hua: {type(error).__name__}: {error}")
        return False
    return True


def parent_pid() -> int:
    """App ka process (engine ko wahi chalata hai): us ki window Stonic ki apni, us ka mic hamara."""
    try:
        return int(os.environ.get("STONIC_VOICE_PARENT_PID", "0") or 0)
    except ValueError:
        return 0


def build_screen(settings: Settings) -> Any:
    """Dimagh ke screen wale haath (screen.py), ya None agar user ne band rakhe hon."""
    if not settings.uses_screen:
        return None
    from stonic_voice.aim import Aimer
    from stonic_voice.hands import shared_hands
    from stonic_voice.screen import ScreenTools

    limits = settings.screen_limits
    aimer = None
    if settings.screen_aim == "uitars":
        # Key na ho to bhi banta hai: pehle click par dimagh ko saaf wajah milti
        # hai ("OpenRouter key nahi") aur wo user ko batata hai. Chup chaap
        # Claude ke nishane par girna nahi — user ne UI-TARS chuna hai.
        aimer = Aimer(url=limits.aim_url, key=settings.keys.openrouter, model=limits.aim_model, timeout_secs=limits.aim_timeout_secs)
    return ScreenTools(
        aim=settings.screen_pointing,
        hands=shared_hands(),
        limits=limits,
        aimer=aimer,
        max_steps=settings.agent_limits.max_steps,
    )


def build_llm(settings: Settings, channel: AppChannel | None = None, bridge: ToolBridge | None = None) -> LLMService:
    """Dimagh. Claude = agent (kaam bhi karta hai); baqi Pipecat ke apne services, sirf baat.

    Har dimagh ki library (anthropic, openai, groq…) usi raaste ke andar import
    hoti hai — engine ke boot par nahi. Naapa (20 Oct 2026): boot 4,442 → 1,683
    modules, garam disk par import 6.0 s → ~2.8 s; Beta aaj sirf Claude agent
    chalata hai. Jo dimagh chuna hi nahi, us ki library disk se parhi bhi nahi
    jati (thandi disk + antivirus par yehi sab se bara farq hai).
    """
    if settings.uses_agent:
        return build_agent_llm(settings, channel or AppChannel(), bridge)
    brain = settings.brain
    prompt = conversation_prompt(settings)

    if settings.is_cloud:
        from pipecat.services.openai.llm import OpenAILLMService

        # Stonic Cloud: gateway ka OpenAI-jaisa text raasta (/v1/chat/completions).
        # Model aur soch ki satah gateway ne batayi hai (app ne pooch kar di);
        # `stonic_purpose` se hisaab mein ye kharcha Beta ke dimagh ke naam likha jata hai.
        cloud = settings.cloud
        extra: dict[str, Any] = {"extra_body": {"stonic_purpose": cloud.purpose}}
        if cloud.reasoning:
            extra["reasoning_effort"] = cloud.reasoning
        return OpenAILLMService(
            api_key=cloud.token,
            base_url=f"{cloud.url}/v1",
            settings=OpenAILLMService.Settings(model=cloud.brain_model, system_instruction=prompt, extra=extra),
        )

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

        # Claude ke ilawa koi bhi model (GPT, Gemini, DeepSeek…): OpenAI-jaisa raasta, sirf baat.
        return OpenRouterLLMService(
            api_key=key,
            settings=OpenRouterLLMService.Settings(
                model=brain.model,
                system_instruction=prompt,
                extra={"extra_body": {"reasoning": openrouter_reasoning(brain.thinking)}},
            ),
        )

    from pipecat.services.groq.llm import GroqLLMService

    llm_settings = GroqLLMService.Settings(model=brain.model, system_instruction=prompt)
    reasoning = groq_reasoning(brain.model, brain.thinking)
    if reasoning is not None:
        llm_settings.reasoning_effort = reasoning
    return GroqLLMService(api_key=key, settings=llm_settings)


def build_speaker(
    settings: Settings, notify: Callable[[dict[str, Any]], Awaitable[Any]] | None = None
) -> CloudSpeech | LiveSpeech | ExpressiveSpeech:
    """Awaaz kahan se banegi — user ka chuna hua raasta (tts_gemini.py ke sar-e-warq par sab).

    `notify`: app ko paigham (Expressive ki hadd par "ab Standard se bol raha hoon").
    """
    voice = settings.voice

    def standard() -> LiveSpeech:
        return LiveSpeech(api_key=settings.keys.gemini, model=voice.live_model, voice=settings.voice_name)

    if settings.is_cloud:
        return CloudSpeech(url=settings.cloud.url, token=settings.cloud.token, voice=settings.voice_name)
    if settings.voice_engine == "live":
        return standard()
    lanes = SpeechLanes(
        keys=[("key", settings.keys.gemini), ("backup", settings.keys.gemini_backup)],
        models=[voice.model],
    )
    return ExpressiveSpeech(key=KeySpeech(lanes=lanes, voice=settings.voice_name), make_standard=standard, notify=notify)


def build_tts(settings: Settings, notify: Callable[[dict[str, Any]], Awaitable[Any]] | None = None) -> GeminiDirectedTTSService:
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
        # par pehli awaaz 3 s se upar ja sakti hai (naapa hua: Cloud par 2.4 s +
        # raasta), aur apni key par hadd khulne ka intezar 6 s tak hai — in par
        # jhooti ghalti na aaye, awaaz bas zara der se aaye.
        stop_frame_timeout_s=FIRST_AUDIO_PATIENCE_SECS,
        settings=GeminiTTSService.Settings(model=settings.voice_model, voice=settings.voice_name),
    )


def build_user_params(settings: Settings) -> LLMUserAggregatorParams:
    """"User chup ho gaya?" ka poora nizaam."""
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
        stop_strategy = StonicTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(
                params=SmartTurnParams(stop_secs=turn.smart_turn_max_wait_secs)
            ),
            finished_wait_secs=turn.finished_wait_secs,
        )
    else:
        stop_strategy = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=turn.speech_timeout_secs)
    # Baari shuru: VAD (awaaz), ya Google ka PAKKA jumla (VAD na pakre to). Pipecat
    # ka default Google ke kacche likhe (interim) par bhi baari shuru karta hai — aur
    # har nayi baari Stonic ko kaat-ti hai. Asal log (21 Oct 2026): jumla poora hote
    # hi ek bhatka interim aaya, nayi baari bani (0 huroof), dimagh kata, aur Stonic
    # ~70 s chup raha. Pakka jumla hamesha lafz laata hai; kaccha shor bhi ho sakta hai.
    start_strategies = [VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy(use_interim=False)]
    # Khali baari (khansi / shor ne Stonic ko kaata, koi lafz nahi): agent wala dimagh
    # turns.EMPTY_TURN_CUE pehchan kar user ki aakhri baat ka jawab dobara deta hai
    # (agent_service.py). Baqi dimagh Pipecat ka apna paigham parhte hain (default).
    empty_turn = EmptyUserTurnConfig(interrupted_prompt=EMPTY_TURN_CUE, max_consecutive_recoveries=2) if settings.uses_agent else EmptyUserTurnConfig()
    return LLMUserAggregatorParams(
        vad_analyzer=vad,
        user_turn_strategies=UserTurnStrategies(start=start_strategies, stop=[stop_strategy]),
        empty_user_turn=empty_turn,
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
    bridge: ToolBridge | None = None
    llm: LLMService | None = None
    # Shuru ka hisaab (monotonic): session kab bani, config kab aaya, connection kab jura.
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


def resolve_tool(call_id: str, payload: dict[str, Any]) -> bool:
    """App ke tool ka natija us session ko do jo us ka intezar kar rahi hai."""
    return any(state.bridge is not None and state.bridge.resolve(call_id, payload) for state in active_sessions())


async def interrupt_work() -> bool:
    """Stop button: jo kaam chal raha hai roko (baat-cheet chalti rehti hai)."""
    stopped = False
    for state in active_sessions():
        brain = getattr(state.llm, "brain", None)
        if brain is not None and brain.working:
            await brain.interrupt()
            stopped = True
    return stopped


async def awareness_cue(text: str) -> bool:
    """Awareness ka "pehle khud bolo" ishara chalti baat-cheet ke dimagh ko.

    Agent (Claude): `ClaudeAgentLLMService.awareness_cue` — Floor ke moqe ki tarah,
    koi tool nahi. Baqi dimagh (sirf baat): ishara user ki jagah context mein jata
    hai aur jawab shuru hota hai — wohi tareeqa jo Standard voice ka hai.
    False = koi baat-cheet nahi, ya dimagh is waqt masroof.
    """
    for state in active_sessions():
        llm = state.llm
        if isinstance(llm, LLMService) and hasattr(llm, "awareness_cue"):
            return await llm.awareness_cue(text)
        if state.worker is not None:
            await state.worker.queue_frames([LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=True)])
            return True
    return False


async def end_all_sessions(reason: str) -> None:
    """Mic ek hai, baat-cheet ek: nayi session se pehle purani band."""
    for state in active_sessions():
        if state.worker is not None:
            logger.info(f"Voice session {state.id} band ki ja rahi hai — {reason}")
            await state.worker.cancel(reason=reason)


# ── Connection ka "juda" (Pipecat ka race) aur shuru ka waqt ─────────────────
#
# Pipecat 1.12 (SmallWebRTCConnection) ka ek race — 21 Oct 2026 ko pakra aur
# dohraya (Usman ke PC par app launch ke foran baad pehli baat-cheet "Waking…"
# par hamesha atki rahi):
#
#   - App ka WebRTC pipeline shuru hone se PEHLE jud jaye to Pipecat "connected"
#     ki khabar rok leta hai (pipeline tayyar nahi), aur pipeline ke `connect()`
#     par `is_connected()` se dobara poochhta hai.
#   - `is_connected()` ka jawab: "app ka aakhri ping 3 s ke andar aaya?" Agar
#     theek us lamhe engine ka loop pehle 3 s se zyada ruka ho (thanda PC,
#     antivirus, bhaari process ka uthna), to jawab "nahi" — aur "connected"
#     KABHI nahi chalta: na salaam, na awaaz, app ka "client-ready" bhi qatar mein.
#
# Do hifazatein:
#   1. Claude ka process (bhaari, Windows par CreateProcess) sirf "connected" ke
#      BAAD uthta hai (`start_brain` — on_client_connected / dimagh ki pipeline
#      jagah, jo pehle aaye). Is race ki khidki mein kuch bhaari nahi.
#   2. `_reconnect_if_missed`: connection Pipecat ke apne hisaab se jura hai magar
#      "connected" ki khabar nahi aayi, to wohi sarkari `connection.connect()`
#      dobara — jo "already connected" par khabar dobara chalata hai. Wajah
#      kuch bhi ho, baat-cheet atak kar nahi rehti.

CONNECT_GRACE_SECS = 2.0     # "connected" ki khabar itni der mein na aaye to dobara
CONNECT_WATCH_SECS = 30.0    # is ke baad nigrani band (app khud bhi koshish chhor deti hai)

# Aakhri config kab aaya (POST /beta/config) — shuru ke waqt ka hisaab.
_config_at: float | None = None


def config_received() -> None:
    """server.py: app ka config aaya (user ne Start dabaya). Waqt ke hisaab ke liye."""
    global _config_at
    _config_at = time.monotonic()


def _since(start: float | None) -> str:
    return f"{round((time.monotonic() - start) * 1000)} ms" if start else "?"


async def _reconnect_if_missed(connection: SmallWebRTCConnection, state: "SessionState") -> None:
    """Hifazat 2 (upar): "connected" chhoot gaya ho to wapas lao."""
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

    def __init__(self, state: "SessionState") -> None:
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
        logger.error(f"Voice session shuru nahi ho saki — chabiyan nahi hain: {', '.join(missing)}")
        await connection.disconnect()
        return

    _next_id += 1
    state = SessionState(id=_next_id, settings=settings, config_mono=_config_at)
    _sessions[state.id] = state
    logger.info(f"Voice session {state.id} shuru (config ke {_since(state.config_mono)} baad) — {settings.public()}")
    watch: asyncio.Task[None] | None = None

    try:
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        # Engine → app ka raasta (tool ke hukm aur "abhi kya ho raha hai" ki khabrein).
        channel = AppChannel()
        if settings.uses_agent and settings.agent.tools:
            state.bridge = ToolBridge(channel, timeout_secs=settings.agent_limits.tool_timeout_secs)
        # Bhaari imports aur models event loop ke BAHAR: is waqt app ka WebRTC jud
        # raha hota hai, aur loop ka rukna Pipecat ke "connected" ko kho deta hai
        # (upar "Pipecat ka race"). Imports aam taur par boot ke baad ka warm thread
        # pehle hi kar chuka hota hai; VAD / Smart Turn ke models har session naye (~0.5 s).
        await asyncio.to_thread(preload_agent)
        user_params = await asyncio.to_thread(build_user_params, settings)
        llm = build_llm(settings, channel, state.bridge)
        state.llm = llm

        stt = build_stt(settings)
        tts = build_tts(settings, notify=channel.send)

        context = LLMContext()
        aggregators = LLMContextAggregatorPair(context, user_params=user_params)
        # Agent ka dimagh user ki baatein isi context se uthata hai (agent_service.py
        # `watch_context`): Pipecat ka "context frame" nayi baari ki rukawat mein mit
        # bhi jaye, to bhi koi jumla dimagh tak pohnche baghair nahi rehta.
        watch_context = getattr(llm, "watch_context", None)
        if watch_context is not None:
            watch_context(context)

        processors = [transport.input(), stt, aggregators.user(), llm, tts, transport.output(), aggregators.assistant()]

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
            # Lab mein khamoshi par session band nahi hoti thi (test ke dauran
            # banda minton chup rehta hai). Yahan hoti hai: bhooli hui session
            # ka mic aur connection ghanton khula na rahe (settings.toml mein
            # kharche wali baat bhi — wo docs se hai, naapi nahi gayi).
            idle_timeout_secs=settings.session.idle_timeout_secs or None,
            # Band karne ka faisla neeche `on_idle_timeout` mein: dimagh ka koi
            # kaam peeche chal raha ho (lamba install, download) to khamoshi par
            # baat-cheet band nahi hoti — warna us ki "ho gaya" sunne wala koi na hota.
            cancel_on_idle_timeout=False,
        )
        state.worker = worker
        channel.attach(worker)

        @latency.event_handler("on_latency_measured")
        async def on_latency_measured(_observer, seconds: float):
            state.turns += 1
            state.last_latency_ms = round(seconds * 1000)
            logger.info(f"Voice session {state.id}: jawab {state.last_latency_ms} ms mein")
            await worker.queue_frame(RTVIServerMessageFrame(data={"type": "stonic-latency", "ms": state.last_latency_ms}))

        @worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(_worker, frame: ErrorFrame):
            message = errors.describe(frame, settings)
            state.last_error = {**message, "at": round(time.time())}
            logger.warning(f"Voice session {state.id}: {message['code']} ({message['part']}) — {message['detail']}")
            await worker.queue_frame(RTVIServerMessageFrame(data=message))

        @worker.event_handler("on_idle_timeout")
        async def on_idle_timeout(_worker):
            if getattr(getattr(llm, "brain", None), "jobs", None):
                logger.info(f"Voice session {state.id}: khamoshi, magar peeche kaam chal raha hai — khuli rehti hai")
                return
            logger.info(f"Voice session {state.id}: lambi khamoshi — band")
            await worker.queue_frame(RTVIServerMessageFrame(data={"type": "stonic-idle"}))
            await worker.cancel(reason="idle timeout")

        @transport.event_handler("on_client_connected")
        async def on_client_connected(_transport, _client):
            if state.connected_at is None:
                state.connected_at = time.monotonic()
                logger.info(f"Voice session {state.id}: connection jura — session ke {_since(state.started_mono)} baad")
            # Claude ka process ab (connection jur chuka): is ke baad engine ka thora
            # rukna kuch nahi torta. Pehle nahi — upar "Pipecat ka race" dekho.
            start_brain = getattr(llm, "start_brain", None)
            if start_brain is not None:
                start_brain()
            # Awareness ne baat-cheet kholi ho (`agent.greet` false) to salaam nahi —
            # Stonic ki pehli baat wohi jo wo kehna chahta tha.
            if settings.session.greeting and settings.agent.greet:
                # Notebook khuli (resume) = pichli baat dimagh ke paas: salaam nahi, baat wahin se.
                resumed = bool(getattr(getattr(llm, "brain", None), "resuming", False))
                context.add_message({"role": "user", "content": opening_cue(settings, resumed=resumed)})
                await worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport, _client):
            await worker.cancel()

        watch = asyncio.get_running_loop().create_task(_reconnect_if_missed(connection, state), name="stonic-connect-watch")
        await PipelineRunner(handle_sigint=False).run(worker)
    except ConfigError as error:
        # Session banne se pehle ki rukawat (misal: is build mein agent ka engine nahi).
        logger.error(f"Voice session {state.id} shuru nahi ho saki: {error}")
        await connection.disconnect()
    except Exception:
        logger.exception(f"Voice session {state.id} toot gayi")
    finally:
        if watch is not None:
            watch.cancel()
        if state.bridge is not None:
            state.bridge.close()
        # Dimagh ka process kabhi bina maalik na rahe: pipeline ka band hona dimagh
        # tak na pohnche (misal: connection judte hi pipeline giri) to yahan band.
        # Aam baat-cheet mein wo pehle hi band ho chuka hota hai — ye no-op hai.
        release = getattr(state.llm, "discard", None)
        if release is not None:
            try:
                await release()
            except Exception as error:  # noqa: BLE001
                logger.debug(f"Dimagh band karte hue: {error}")
        _sessions.pop(state.id, None)
        logger.info(
            f"Voice session {state.id} band — {state.turns} jawab, "
            f"aakhri {state.last_latency_ms} ms, {round(time.time() - state.started_at)} s"
        )
