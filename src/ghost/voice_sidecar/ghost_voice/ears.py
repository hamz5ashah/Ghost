"""Kaan jo beech mein nahi kat-te: Google ki ~10 minute wali line ke aar-paar sunna.

Google ki Live transcription (gemini-3.5-transcribe-live) ki ek line — ek
WebSocket — zyada se zyada ~10 minute chalti hai (ai.google.dev live-transcribe:
"up to 10 minutes"; band hone se ~60 s pehle `go_away` aata hai), aur ~60 s tak
koi awaaz na pohnche to bhi Google band kar deta hai (1008 "session idle timeout").
Ye hadd barhti nahi. Pipecat ka `GeminiSTTService` ek hi line rakhta hai: wo
kat-ti to ghalti, aur kaan ki ghalti par baat-cheet band (errors.py `fatal`).
20 Oct 2026 tak har lambi baat-cheet ~9.5 min par khud band hoti thi (gateway
logs, 14 din: ek bhi listen session 573 s se lambi nahi).

Yahan line kaati nahi jati, BADLI jati hai — munh (tts_gemini.LiveSpeech) ki tarah:

  1. Line ~7 min purani ho (ya Google `go_away` bheje): nayi line peeche khulti
     hai aur tayyar khadi rehti hai (har 10 s halki khamoshi — warna Google use
     60 s par band kar deta).
  2. Khamoshi ka lamha (VAD chup, aur purani line ka har jumla likha ja chuka):
     pichli 0.5 s ki awaaz nayi line ko — user bolna shuru hi kar raha ho to
     pehla lafz na kate — phir mic nayi line par, purani band. Purani line ka
     baad ka koi lafz aage nahi jata (wo awaaz nayi line ke paas hai), is liye
     koi lafz do dafa nahi aata.
  3. ~9 min ho gaye aur user lagatar bol raha hai: agle jumle ke waqfe par (VAD
     "chup" — wahi lamha jab Google ko "jumla likh do" jata hai) line badalti
     hai: purani line apna jumla poora likhti hai, agla jumla nayi line shuru se
     sunti hai. Koi lafz beech se nahi kat-ta (asli Google par naapa: lafzon ke
     beech badalne se lafz kat-te the — "lemon" -> "Stonic").
     Phir bhi koi waqfa na aaye (Google ke kaatne se ~15 s pehle tak): chalta
     jumla SHURU se nayi line ko (pichli 45 s ki awaaz yaad-dasht mein rehti hai)
     aur purani ka adhoora jumla phenka; jumla bohat lamba ho to aakhri raasta —
     lafzon ke beech ki chhoti khamoshi.
  4. Chalti line achanak toot jaye (internet, Google): baat-cheet band NAHI —
     nayi line khulti hai aur jo awaaz is dauran aayi wo dobara bheji jati hai.
     Ghalti sirf tab jab bar bar jurna nakaam ho, ya wajah aisi ho jo dobara
     koshish se theek nahi hoti (plan ki hadd, ghalat key — errors.is_permanent).
  5. Key do lines ek saath na kholne de (apni key ki hadd): khamoshi ke lamhe
     mein pehle purani band, phir nayi — beech ki awaaz yaad-dasht se dobara.
  6. Google ne jumle ka kacha hissa likha magar poora nahi kiya, aur user chup
     hai: 1 s baad dobara "likh do" (audio_stream_end). Phir bhi 2.5 s kuch na
     aaye to line atki hui maani jati hai — 4 wala raasta: nayi line, jumla shuru
     se dobara. Pakka jumla na aaye to baari kabhi band nahi hoti (turns.py) aur
     Stonic chup khara rehta hai. Asli Google par naapa (21 Oct 2026): "likh do"
     har haalat mein ~0.4 s mein pakka jumla laata hai — ye sirf atki line /
     internet ki rukawat ke liye hai.

── KHAMOSHI GOOGLE KO NAHI JATI (gate) ─────────────────────────────────────

Google is model par har us second ka paisa leta hai jo hum bhejte hain — khamoshi
aur shor samet (28 Oct 2026 naapa: Google ke apne token meter par 62 s khamoshi =
1,550 tokens, jab ke us ne `usageMetadata` mein 0 bataya; socket khula magar awaaz
na bheji = 0). Asal users ki bheji awaaz mein bolna ~7-10% tha.

Is liye awaaz sirf tab jati hai jab koi bol raha ho. Faisla PC par hota hai (Silero
VAD ka apna, turn wale se zyada hassaas instance — shak ho to bhejo, lafz chhootna
khamoshi bhejne se bura hai):
  - bolna shuru: pichli `gate_preroll_secs` ki awaaz (yaad-dasht se) + phir sab live
  - bolna ruka: `gate_hangover_secs` tak aur bhejo, phir "likh do" (audio_stream_end)
    aur bhejna band
  - chup: kuch nahi; line zinda rakhne ko har 10 s 0.1 s (neeche `_ping_idle`)
  - mute: browser khamoshi bhejta hai = kuch nahi jata
Gate ka model na chale to sab kuch bhejo (pehle jaisa) — kabhi lafz kho kar nahi.
Line badalte / toot-ne par sirf wohi awaaz dobara jati hai jo gate ne bheji thi.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from google.genai.types import Blob
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.stt import GeminiSTTService

from ghost_voice import errors

# Line itni purani ho to nayi line ki tayyari (munh bhi 7 min par badalta hai).
ROTATE_AFTER_SECS = 7 * 60
# Is ke baad khamoshi ka intezar nahi: Google ~570 s par kaat-ta hai (logs).
FORCE_AFTER_SECS = 9 * 60
# Khamoshi ka intezar bhi khatam (pending jumla ho to bhi): Google ke kaatne se pehle.
HARD_AFTER_SECS = 9 * 60 + 15
# `go_away` ke baad itna waqt bache to khamoshi ka intezar nahi.
GO_AWAY_MARGIN_SECS = 10.0
# `go_away` mein waqt na likha ho to itna maano.
GO_AWAY_DEFAULT_SECS = 30.0
# VAD itni der chup = khamoshi ka lamha (VAD khud 0.2 s baad "chup" kehta hai).
QUIET_SECS = 0.6
# Nayi line ko pichli itni awaaz: VAD bolna 0.2 s baad pehchanta hai.
PREROLL_SECS = 0.5
# Jumlon ke beech ke waqfe par badalte waqt (VAD abhi "chup" bola = pichli 0.2 s
# khamoshi): nayi line ko sirf itni — pichle jumle ka koi hissa nahi.
PAUSE_PREROLL_SECS = 0.1
# Purani line par adhoora jumla: us ke likhe jane ka itna intezar.
FINAL_WAIT_SECS = 4.0
# Zabardasti badalte waqt jumla itna lamba ho tak shuru se dobara bheja jata hai.
REPLAY_MAX_SECS = 30.0
# Awaaz ki yaad-dasht (dobara bhejne ke liye).
RING_SECS = 45.0
# Line par itni der koi awaaz na gayi ho to halki khamoshi (Google 60 s par band karta hai).
IDLE_PING_SECS = 10.0
PING_AUDIO_SECS = 0.1
# Retire hoti line (jis ke jumle abhi aage jate hain): aakhri likhai ke baad itna intezar.
RETIRE_GRACE_SECS = 2.0
RETIRE_MAX_SECS = 8.0
# Nayi line na khule to itni der baad dobara koshish.
STANDBY_RETRY_SECS = 5.0
# Tayyar khadi line itni purani ho jaye (khamoshi ka moqa na mila) to us ki jagah nayi.
STANDBY_MAX_AGE_SECS = 4 * 60
CONNECT_TIMEOUT_SECS = 15.0
# Chalti line toot jaye: dobara jurne ki koshishein (pehli foran).
RECOVER_DELAYS_SECS = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0)
# Dobara bheji awaaz in tukron mein.
REPLAY_CHUNK_SECS = 0.1
# Lambe jumle par lafzon ke beech ki khamoshi: is se dheemi awaaz (int16 RMS) ...
GAP_FLOOR_RMS = 200.0
# ... ya bolne ki aam awaaz ka itna hissa.
GAP_SPEECH_RATIO = 0.2
# Lambe jumle mein beech ki khamoshi ka zyada se zyada intezar.
SPLIT_WAIT_SECS = 10.0
# Kacha jumla, user chup, pakka nahi aaya: aakhri likhai (ya VAD ke chup) se itni der baad dobara "likh do".
NUDGE_AFTER_SECS = 1.0
# Dobara kehne ke baad bhi itni der kuch na aaye: line atki — nayi line, jumla shuru se.
STALL_AFTER_SECS = 2.5
# Atki line ki wajah se nayi line itni der mein ek hi dafa (shor par bar bar line na khule).
STALL_COOLDOWN_SECS = 30.0
TICK_SECS = 0.2


def _seconds(value: Any) -> float | None:
    """Google ka waqt ("59s", "1.5s", 59) -> second."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*s?\s*", str(value))
    return float(match.group(1)) if match else None


def _rms(audio: bytes) -> float:
    if len(audio) < 2:
        return 0.0
    samples = np.frombuffer(audio[: len(audio) // 2 * 2], dtype=np.int16).astype(
        np.float32
    )
    return float(np.sqrt(np.mean(samples * samples)))


def _short(error: BaseException | str) -> str:
    return " ".join(str(error).split())[:300] or type(error).__name__


@dataclass
class GateSettings:
    """Kab awaaz Google ko jaye (settings.toml [ears] gate_*)."""

    enabled: bool = True
    confidence: float = 0.5
    min_volume: float = 0.35
    start_secs: float = 0.06
    hangover_secs: float = 0.8
    preroll_secs: float = 1.0


class SpeechGate:
    """Silero VAD — sirf ye batata hai ke is waqt koi bol raha hai (ya abhi abhi ruka)."""

    def __init__(self, settings: GateSettings):
        self.settings = settings
        self._vad: Any = None

    def prepare(self, sample_rate: int) -> None:
        """Model load (~0.2 s) — event loop ke bahar bulao."""
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams

        vad = SileroVADAnalyzer(
            params=VADParams(
                confidence=self.settings.confidence,
                start_secs=self.settings.start_secs,
                stop_secs=self.settings.hangover_secs,
                min_volume=self.settings.min_volume,
            )
        )
        vad.set_sample_rate(sample_rate)
        self._vad = vad

    @property
    def ready(self) -> bool:
        return self._vad is not None

    def voice(self, audio: bytes) -> bool:
        """True = bolna chal raha hai (ya hangover) — ye tukra bhejo. (~0.5 ms fi 32 ms)"""
        from pipecat.audio.vad.vad_analyzer import VADState

        state = self._vad._run_analyzer(audio)
        return state in (VADState.SPEAKING, VADState.STOPPING)


@dataclass(eq=False)
class _Line:
    """Google se ek WebSocket (ek "line")."""

    number: int
    session: Any
    context: Any
    opened_at: float
    last_sent: float
    # Is waqt se pehle ki awaaz pichhli line ke zimme thi (dobara bhejte hue is se peeche nahi).
    responsible_from: float = 0.0
    task: asyncio.Task | None = None
    # Google ka `go_away`: is waqt line band hogi.
    deadline: float | None = None
    # Jumla likha ja raha hai (interim aaya), abhi poora nahi hua.
    open_text: bool = False
    open_since: float = 0.0
    last_output_at: float = 0.0
    last_final_at: float = 0.0
    # Kacha jumla atka to Google ko dobara "likh do" kab kaha (0 = kabhi nahi).
    nudged_at: float = 0.0
    retired_at: float = 0.0
    # Retire hone ke baad bhi is ke poore jumle aage jate hain.
    keep_finals: bool = False
    dead: bool = False
    closed: bool = False


class SeamlessGeminiSTTService(GeminiSTTService):
    """Pipecat ka GeminiSTTService, magar Google ki line badalti rehti hai aur baat-cheet nahi kat-ti."""

    def __init__(self, *, gate: GateSettings | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Khamoshi ka darwaza. `None` / band = har tukra Google ko (purana tareeqa).
        self.gate_settings = gate or GateSettings(enabled=False)
        self._gate: SpeechGate | None = None
        self._gate_open = False
        self._now: Callable[[], float] = time.monotonic
        # Waqt ki haddein: test inhein chhota karte hain.
        self.rotate_after_secs = ROTATE_AFTER_SECS
        self.force_after_secs = FORCE_AFTER_SECS
        self.hard_after_secs = HARD_AFTER_SECS
        self.go_away_margin_secs = GO_AWAY_MARGIN_SECS
        self.quiet_secs = QUIET_SECS
        self.preroll_secs = PREROLL_SECS
        self.final_wait_secs = FINAL_WAIT_SECS
        self.replay_max_secs = REPLAY_MAX_SECS
        self.ring_secs = RING_SECS
        self.idle_ping_secs = IDLE_PING_SECS
        self.retire_grace_secs = RETIRE_GRACE_SECS
        self.retire_max_secs = RETIRE_MAX_SECS
        self.standby_retry_secs = STANDBY_RETRY_SECS
        self.connect_timeout_secs = CONNECT_TIMEOUT_SECS
        self.recover_delays_secs: tuple[float, ...] = RECOVER_DELAYS_SECS
        self.split_wait_secs = SPLIT_WAIT_SECS
        self.nudge_after_secs = NUDGE_AFTER_SECS
        self.stall_after_secs = STALL_AFTER_SECS
        self.stall_cooldown_secs = STALL_COOLDOWN_SECS
        self.tick_secs = TICK_SECS

        self._line: _Line | None = None
        self._standby: _Line | None = None
        self._retiring: list[_Line] = []
        self._line_count = 0
        self._lock = asyncio.Lock()
        self._supervisor: asyncio.Task | None = None
        self._opening: asyncio.Task | None = None
        self._recovering: asyncio.Task | None = None
        self._helpers: set[asyncio.Task] = set()
        self._recover_from = 0.0
        self._standby_retry_at = 0.0
        self._rotate_requested = False
        # Key do lines ek saath nahi kholne deti: pehle purani band, phir nayi.
        self._break_first = False
        # Waqt khatam ho raha hai: agle jumle ke waqfe (VAD chup) par line badlo.
        self._pause_due = False
        # Lamba jumla: is waqt tak lafzon ke beech ki khamoshi par line badalni hai.
        self._split_due: float | None = None
        self._closing = False
        # Atki line ki wajah se aakhri nayi line kab kholi.
        self._stall_recovered_at = float("-inf")

        # Awaaz ki yaad-dasht: [pohnchne ka waqt, PCM, Google ke liye thi?]. Dobara
        # sirf wohi jati hai jo gate ne Google ke liye chuni thi (khamoshi nahi).
        self._ring: deque[list] = deque()
        # VAD ka haal.
        self._speaking = False
        self._utt_start = 0.0
        self._utt_starts: deque[float] = deque(maxlen=20)
        self._quiet_since = 0.0
        self._speech_level = 0.0

        self.stats: dict[str, float] = {
            "lines": 0,
            "switches": 0,
            "recoveries": 0,
            "replayed_secs": 0.0,
            "nudges": 0,
            "stalls": 0,
            # Kharche ki ginti: mic se kitni awaaz aayi, Google ko kitni gayi (dobara bheji + ping samet).
            "heard_secs": 0.0,
            "sent_secs": 0.0,
            "gate_opens": 0,
        }

    # ── Pipecat ke darwaze ───────────────────────────────────────────────────

    async def _connect(self) -> None:
        """Pehli line. Ye na khule to session shuru hi nahi hoti (pehle jaisa)."""
        if self._line is not None or self._supervisor is not None:
            return
        self._closing = False
        self._quiet_since = self._now()
        await self._prepare_gate()
        line = await self._open_line()
        self._activate(line)
        await self._call_event_handler("on_connected")
        self._supervisor = asyncio.create_task(self._supervise(), name="ghost-ears")

    async def _disconnect(self) -> None:
        self._closing = True
        tasks = [self._supervisor, self._opening, self._recovering]
        self._supervisor = self._opening = self._recovering = None
        for task in tasks:
            await _stop(task)
        # Band hoti lines ko poora band hone do (beech mein kaata to socket latka rehta).
        helpers = [task for task in self._helpers if not task.done()]
        if helpers:
            await asyncio.wait(helpers, timeout=2.0)
        lines = [
            line
            for line in (self._line, self._standby, *self._retiring)
            if line is not None
        ]
        had_line = self._line is not None
        self._line = self._standby = None
        self._session = None
        self._retiring.clear()
        for line in lines:
            await self._close_line(line)
        self._ring.clear()
        self._gate_open = False
        heard, sent = self.stats["heard_secs"], self.stats["sent_secs"]
        if heard and had_line:
            logger.info(
                f"Kaan: {heard:.0f} s suna, {sent:.0f} s Google ko gaya "
                f"({max(0.0, 100.0 * (1 - sent / heard)):.0f}% bachat; gate {'on' if self.gate_active else 'off'}, "
                f"{self.stats['gate_opens']:.0f} dafa khula)"
            )
        if had_line:
            await self._call_event_handler("on_disconnected")

    async def _do_reconnect(self) -> None:
        """Settings badlin (zubaan, khaas lafz): nayi line nayi settings se, khamoshi mein."""
        if self._closing:
            return
        if self._line is None and (self._recovering is None or self._recovering.done()):
            await self._connect()
            return
        self._rotate_requested = True
        standby, self._standby = self._standby, None
        if standby is not None:
            await self._close_line(standby)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_started(float(getattr(frame, "start_secs", 0.0) or 0.0))
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_stopped()
        await super().process_frame(frame, direction)

    def _vad_started(self, start_secs: float) -> None:
        self._speaking = True
        self._utt_start = self._now() - start_secs
        self._utt_starts.append(self._utt_start)

    def _vad_stopped(self) -> None:
        self._speaking = False
        self._quiet_since = self._now()

    async def _send_audio(self, audio: bytes) -> None:
        now = self._now()
        self.stats["heard_secs"] += self._secs(audio)
        wanted = self._wanted(audio)
        entry = self._remember(now, audio, wanted)
        gap = self._is_gap(_rms(audio))
        if self._split_due is not None and (gap or now >= self._split_due):
            standby = self._standby
            if standby is not None and not standby.dead:
                await self._switch(standby, "split")
            else:
                self._split_due = None
        if not wanted:
            if self._gate_open:
                # Bolna (aur us ke baad ka thehrao) khatam: jo suna wo Google abhi
                # likh de, phir bhejna band — line ko sirf `_ping_idle` zinda rakhta hai.
                self._gate_open = False
                await self._send_finalization_signal()
            return
        preroll: list[bytes] = []
        if not self._gate_open:
            self._gate_open = True
            if self.gate_active:
                self.stats["gate_opens"] += 1
                preroll = self._take_preroll(now, entry)
        failed: tuple[_Line, BaseException] | None = None
        async with self._lock:
            line = self._line
            if line is None or line.dead:
                return  # line jur rahi hai: ye awaaz yaad-dasht se dobara jayegi
            try:
                for piece in (*preroll, audio):
                    await line.session.send_realtime_input(audio=self._blob(piece))
                    self.stats["sent_secs"] += self._secs(piece)
                line.last_sent = now
            except Exception as error:  # noqa: BLE001 — line ka toot-na, neeche sambhala
                failed = (line, error)
        if failed:
            await self._line_lost(*failed)

    # ── Khamoshi ka darwaza (gate) ───────────────────────────────────────────

    @property
    def gate_active(self) -> bool:
        """Gate chal raha hai? (band ho ya model na ho = har awaaz Google ko)"""
        return (
            self.gate_settings.enabled and self._gate is not None and self._gate.ready
        )

    @property
    def gate_open(self) -> bool:
        """Is lamhe awaaz Google ko ja rahi hai?"""
        return self._gate_open

    async def _prepare_gate(self) -> None:
        if not self.gate_settings.enabled or (
            self._gate is not None and self._gate.ready
        ):
            return
        gate = SpeechGate(self.gate_settings)
        try:
            await asyncio.to_thread(gate.prepare, self.sample_rate or 16000)
        except Exception as error:  # noqa: BLE001 — gate ke baghair bhi sunna chalta hai
            logger.warning(
                f"Kaan: gate ka VAD load nahi hua ({_short(error)}) — har awaaz Google ko (pehle jaisa)"
            )
            return
        self._gate = gate

    def _wanted(self, audio: bytes) -> bool:
        """Ye tukra Google ko jaye? Shak ho to haan."""
        if not self.gate_active:
            return True
        try:
            voice = self._gate.voice(audio)  # type: ignore[union-attr]
        except Exception as error:  # noqa: BLE001
            logger.warning(
                f"Kaan: gate ka VAD nakaam ({_short(error)}) — ab har awaaz Google ko (pehle jaisa)"
            )
            self._gate = None
            return True
        # Turn wala VAD (kam hassaas, magar alag nazar) bole ke banda bol raha hai to bhi bhejo.
        return voice or self._speaking

    def _take_preroll(self, now: float, current: list) -> list[bytes]:
        """Bolna abhi pakra gaya: us se pehle ki thori awaaz (pehla lafz na kate)."""
        start = now - self.gate_settings.preroll_secs
        pieces: list[bytes] = []
        for entry in self._ring:
            if entry is current or entry[0] < start or entry[2]:
                continue
            entry[2] = True  # ab ye Google ki hai — line badle to dobara bhi jaye
            pieces.append(entry[1])
        return pieces

    def _secs(self, audio: bytes) -> float:
        return len(audio) / (2 * (self.sample_rate or 16000))

    async def _send_finalization_signal(self) -> None:
        """User chup hua: jumla abhi likh do (Google ke apne intezar ke baghair).

        Line badalne ka waqt aa chuka ho to yahi sab se saaf lamha hai: jumla
        purani line par poora, agla jumla nayi line par shuru se.
        """
        failed: tuple[_Line, BaseException] | None = None
        async with self._lock:
            line = self._line
            if line is None or line.dead:
                return
            try:
                await line.session.send_realtime_input(audio_stream_end=True)
            except Exception as error:  # noqa: BLE001
                failed = (line, error)
        if failed:
            await self._line_lost(*failed)
            return
        if not self._pause_due or self._closing:
            return
        if self._break_first:
            await self._swap_in_place(line, self._now(), "pause")
            return
        standby = self._standby
        if standby is not None and not standby.dead:
            await self._switch(standby, "pause")

    def _is_keepalive_ready(self) -> bool:
        return False  # khamoshi ka apna intezam (_ping_idle)

    # ── Lines ────────────────────────────────────────────────────────────────

    def _blob(self, audio: bytes) -> Blob:
        return Blob(data=audio, mime_type=f"audio/pcm;rate={self.sample_rate or 16000}")

    async def _open_line(self) -> _Line:
        model = self._settings.model
        context = self._client.aio.live.connect(
            model=model, config=self._build_live_config()
        )
        session = await asyncio.wait_for(
            context.__aenter__(), self.connect_timeout_secs
        )
        now = self._now()
        self._line_count += 1
        self.stats["lines"] += 1
        line = _Line(
            number=self._line_count,
            session=session,
            context=context,
            opened_at=now,
            last_sent=now,
            responsible_from=now,
        )
        line.task = asyncio.create_task(
            self._listen(line), name=f"ghost-ears-line-{line.number}"
        )
        return line

    def _activate(self, line: _Line) -> None:
        self._line = line
        self._session = line.session
        self._session_ctx = line.context
        self._connection_start_time = time.time()
        # Nayi line taaza hai: pichli line ke "badlo" wale ishare us par nahi.
        self._pause_due = False
        self._split_due = None
        self._rotate_requested = False

    async def _close_line(self, line: _Line) -> None:
        if line.closed:
            return
        line.closed = True
        try:
            await asyncio.wait_for(line.context.__aexit__(None, None, None), 5.0)
        except Exception as error:  # noqa: BLE001 — band hoti line ki ghalti kisi kaam ki nahi
            logger.debug(f"Kaan: line #{line.number} band karte hue: {_short(error)}")
        # Socket band = sunne wala task khud khatam (shayad abhi ghalti aage bhej raha
        # ho — use beech mein na kaato). Na ruke tab hi cancel.
        task = line.task
        if task is not None and not task.done() and task is not asyncio.current_task():
            await asyncio.wait({task}, timeout=1.0)
            await _stop(task)

    def _close_later(self, line: _Line) -> None:
        """Line ke apne task ke andar se bhi band karna mehfooz rahe."""
        task = asyncio.create_task(self._close_line(line))
        self._helpers.add(task)
        task.add_done_callback(self._helpers.discard)

    async def _listen(self, line: _Line) -> None:
        try:
            while not line.closed:
                got = False
                async for message in line.session.receive():
                    got = True
                    await self._on_message(line, message)
                if not got:
                    raise RuntimeError("Live receive khali lauta")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — har toot-na _line_lost tay karta hai
            if not line.closed:
                await self._line_lost(line, error)

    async def _on_message(self, line: _Line, message: Any) -> None:
        now = self._now()
        go_away = getattr(message, "go_away", None)
        if go_away is not None:
            left = _seconds(getattr(go_away, "time_left", None))
            line.deadline = now + (left if left is not None else GO_AWAY_DEFAULT_SECS)
            logger.info(
                f"Kaan: Google ka go_away line #{line.number} par ({left} s baqi, line {now - line.opened_at:.0f} s purani)"
            )
        content = getattr(message, "server_content", None)
        if not content:
            return
        interim = content.interim_input_transcription
        if interim is not None and interim.text:
            if not line.open_text:
                line.open_since = now
            line.open_text = True
            line.last_output_at = now
            if line is self._line:
                await self._handle_interim_transcription(interim, message)
        final = content.input_transcription
        if final is not None and final.text:
            line.open_text = False
            line.open_since = 0.0
            line.last_output_at = now
            line.last_final_at = now
            if line is self._line or (line.keep_finals and line in self._retiring):
                await self._handle_input_transcription(final, message)
            else:
                logger.debug(
                    f"Kaan: line #{line.number} ka jumla chhora — wo awaaz nayi line ke paas hai ({len(final.text)} huroof)"
                )

    async def _line_lost(self, line: _Line, error: BaseException | str) -> None:
        if line.dead or line.closed or self._closing:
            return
        line.dead = True
        text = _short(error)
        if line is not self._line:
            if line is self._standby:
                self._standby = None
                self._standby_retry_at = self._now() + self.standby_retry_secs
                logger.info(
                    f"Kaan: tayyar khadi line #{line.number} band hui ({text}) — baad mein nayi"
                )
            else:
                logger.debug(f"Kaan: purani line #{line.number} band: {text}")
            if line in self._retiring:
                self._retiring.remove(line)
            self._close_later(line)
            return

        now = self._now()
        replay_from = self._replay_point(line, now)
        self._line = None
        self._session = None
        self._close_later(line)
        if errors.is_permanent(text):
            logger.warning(
                f"Kaan: line #{line.number} band — dobara jurna bekaar ({text})"
            )
            await self._give_up(text, error)
            return
        logger.warning(
            f"Kaan: chalti line #{line.number} achanak band ({text}) — nayi line, beech ki awaaz dobara"
        )
        self._start_recovery(replay_from)

    def _replay_point(self, line: _Line, now: float) -> float:
        """Toot-ti line ke baad nayi line ko kahan se awaaz dobara jaye: jo likha nahi gaya wo sab."""
        start = now - self.preroll_secs
        if self._speaking:
            start = min(start, self._utt_start - self.preroll_secs)
        if line.open_text and line.open_since:
            start = min(start, max(line.open_since - 1.5, line.last_final_at))
        # Jo jumle bole gaye magar is line ne un par kuch nahi likha (line chup chaap mari ho).
        answered = max(line.last_output_at, line.responsible_from)
        for began in self._utt_starts:
            if began > answered:
                start = min(start, began - self.preroll_secs)
                break
        # Is line se pehle ki awaaz pichhli line likh chuki — wo dobara nahi.
        return max(start, line.responsible_from, now - self.ring_secs)

    def _start_recovery(self, replay_from: float) -> None:
        if self._recovering is not None and not self._recovering.done():
            self._recover_from = min(self._recover_from, replay_from)
            return
        self._recover_from = replay_from
        self._recovering = asyncio.create_task(
            self._recover(), name="ghost-ears-recover"
        )

    async def _recover(self) -> None:
        last_error = "line nahi jur saki"
        last_exception: BaseException | None = None
        for delay in self.recover_delays_secs:
            if delay:
                await asyncio.sleep(delay)
            if self._closing:
                return
            line = (
                self._standby
                if self._standby is not None and not self._standby.dead
                else None
            )
            self._standby = None
            if line is None:
                try:
                    line = await self._open_line()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001
                    last_error, last_exception = _short(error), error
                    if errors.is_permanent(last_error):
                        await self._give_up(last_error, error)
                        return
                    logger.warning(
                        f"Kaan: nayi line nahi khuli ({last_error}) — dobara koshish"
                    )
                    continue
            if self._closing:
                await self._close_line(line)
                return
            replayed = 0.0
            failure: BaseException | None = None
            async with self._lock:
                self._activate(line)
                line.responsible_from = self._recover_from
                try:
                    replayed = await self._replay(line, self._recover_from)
                    if not self._speaking and replayed:
                        # Jumla jur-ne ke dauran poora ho chuka: abhi likh do.
                        await line.session.send_realtime_input(audio_stream_end=True)
                except Exception as error:  # noqa: BLE001
                    failure = error
                    line.dead = True
                    self._line = None
                    self._session = None
            if failure is not None:
                last_error, last_exception = _short(failure), failure
                self._close_later(line)
                logger.warning(
                    f"Kaan: nayi line #{line.number} par awaaz nahi gayi ({last_error}) — dobara koshish"
                )
                continue
            self.stats["recoveries"] += 1
            self._recovering = None
            logger.info(
                f"Kaan: line #{line.number} chalu — {replayed:.1f} s awaaz dobara bheji, baat-cheet jaari"
            )
            return
        await self._give_up(last_error, last_exception)

    async def _give_up(self, text: str, error: BaseException | str | None) -> None:
        """Ab baat-cheet nahi chal sakti: wohi ghalti jo pehle foran aati thi (errors.py: kaan = fatal)."""
        self._line = None
        self._session = None
        exception = error if isinstance(error, Exception) else None
        await self.push_error(
            error_msg=f"connection error: {text}",
            exception=exception,
            force_treat_as_permanent=True,
        )

    async def _replay(self, line: _Line, start: float | None) -> float:
        """Yaad-dasht ki awaaz (`start` ke baad ki) nayi line ko. Second lautata hai."""
        if start is None:
            return 0.0
        # Sirf wo awaaz jo Google ke liye thi — gate ne jo khamoshi roki wo dobara bhi nahi.
        audio = b"".join(
            chunk for at, chunk, wanted in self._ring if at >= start and wanted
        )
        if not audio:
            return 0.0
        rate = self.sample_rate or 16000
        step = max(2, int(rate * REPLAY_CHUNK_SECS) * 2)
        for offset in range(0, len(audio), step):
            piece = audio[offset : offset + step]
            await line.session.send_realtime_input(audio=self._blob(piece))
            self.stats["sent_secs"] += self._secs(piece)
        line.last_sent = self._now()
        seconds = len(audio) / (rate * 2)
        self.stats["replayed_secs"] += seconds
        return seconds

    def _remember(self, now: float, audio: bytes, wanted: bool = True) -> list:
        entry = [now, audio, wanted]
        self._ring.append(entry)
        oldest = now - self.ring_secs
        while self._ring and self._ring[0][0] < oldest:
            self._ring.popleft()
        return entry

    def _is_gap(self, level: float) -> bool:
        """Lafzon ke beech ki chhoti khamoshi? (lambe jumle mein line badalne ki jagah)"""
        if self._speaking and level > GAP_FLOOR_RMS:
            self._speech_level = (
                level
                if not self._speech_level
                else 0.9 * self._speech_level + 0.1 * level
            )
        return level < max(GAP_FLOOR_RMS, GAP_SPEECH_RATIO * self._speech_level)

    # ── Nigrani: line kab aur kaise badle ────────────────────────────────────

    async def _supervise(self) -> None:
        while True:
            await asyncio.sleep(self.tick_secs)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — nigrani kabhi na ruke
                logger.exception(f"Kaan ki nigrani mein ghalti: {error}")

    async def _tick(self) -> None:
        now = self._now()
        await self._tidy_retiring(now)
        await self._ping_idle(now)
        line = self._line
        if line is None or line.dead or self._closing:
            return
        if self._recovering is not None and not self._recovering.done():
            return
        if await self._watch_unfinished(line, now):
            return  # line atki thi: nayi line jur rahi hai
        age = now - line.opened_at
        if not (
            self._rotate_requested
            or age >= self.rotate_after_secs
            or line.deadline is not None
        ):
            return
        left = (line.deadline - now) if line.deadline is not None else None
        force = age >= self.force_after_secs or (
            left is not None and left <= self.go_away_margin_secs
        )
        hard = age >= self.hard_after_secs or (left is not None and left <= 4.0)
        quiet = self._quiet(line, now)
        if force and not self._pause_due:
            # Ab khamoshi ka intezar nahi: agle jumle ke waqfe par (_send_finalization_signal).
            self._pause_due = True
            logger.info(
                f"Kaan: line #{line.number} {age:.0f} s purani — agle waqfe par badlegi"
            )

        if self._break_first:
            if quiet:
                await self._swap_in_place(line, now, "quiet")
            elif hard:
                await self._swap_in_place(line, now, "hard")
            return

        standby = self._standby
        if standby is None or standby.dead:
            self._standby = None
            self._ensure_standby(now)
            if hard:
                # Nayi line waqt par tayyar nahi: Google ke kaatne se behtar khud badalna.
                await self._swap_in_place(line, now, "hard")
            return

        if quiet:
            await self._switch(standby, "quiet")
        elif not hard or self._split_due is not None:
            return
        elif self._speaking:
            # Google ke kaatne se pehle koi waqfa nahi aaya.
            if self._replay_ok(line, now):
                await self._switch(standby, "replay")
            else:
                deadline = now + self.split_wait_secs
                if left is not None:
                    deadline = min(deadline, now + max(0.0, left - 3.0))
                self._split_due = deadline
                logger.info(
                    f"Kaan: line #{line.number} lamba jumla, koi waqfa nahi — lafzon ke beech ki khamoshi par badlegi"
                )
        else:
            # Jumla poora hua magar Google ne abhi likha nahi: purani line likh kar band ho.
            await self._switch(standby, "quiet", keep_old_finals=True)

    async def _watch_unfinished(self, line: _Line, now: float) -> bool:
        """Kacha jumla jo Google ne poora nahi likha, aur user chup hai (upar 6).

        Pehle dobara "likh do"; us ke baad bhi kuch na aaye to line atki hui —
        nayi line, jumla shuru se. True = line nayi line ke hawale (recovery).
        """
        if not line.open_text or self._speaking:
            return False
        if line.nudged_at <= line.last_output_at:
            # Is likhai ke baad abhi dobara nahi kaha. VAD ne bolna na pakra ho to sirf likhai se ginti.
            if (
                now - max(line.last_output_at, self._quiet_since)
                < self.nudge_after_secs
            ):
                return False
            failed: BaseException | None = None
            async with self._lock:
                if self._line is not line or line.dead:
                    return False
                try:
                    await line.session.send_realtime_input(audio_stream_end=True)
                    line.nudged_at = now
                except Exception as error:  # noqa: BLE001
                    failed = error
            if failed is not None:
                await self._line_lost(line, failed)
                return True
            self.stats["nudges"] += 1
            logger.info(
                f"Kaan: line #{line.number} ka jumla adhoora reh gaya (user chup) — Google ko dobara 'likh do'"
            )
            return False
        if now - line.nudged_at < self.stall_after_secs:
            return False
        if now - self._stall_recovered_at < self.stall_cooldown_secs:
            return False  # abhi abhi nayi line khuli thi: shor par bar bar nahi
        self._stall_recovered_at = now
        self.stats["stalls"] += 1
        logger.warning(
            f"Kaan: line #{line.number} ne {now - line.last_output_at:.1f} s se jumla poora nahi likha — "
            "line atki, nayi line par jumla shuru se"
        )
        await self._line_lost(line, "jumla poora nahi likha gaya (line atki)")
        return True

    def _quiet(self, line: _Line, now: float) -> bool:
        if self._speaking or now - self._quiet_since < self.quiet_secs:
            return False
        return not (line.open_text and now - line.last_output_at < self.final_wait_secs)

    def _replay_ok(self, line: _Line, now: float) -> bool:
        """Chalta jumla shuru se nayi line ko bheja ja sakta hai, bina kisi lafz ko dohraye?"""
        start = self._utt_start - self.preroll_secs
        if now - start > min(self.replay_max_secs, self.ring_secs - 1.0):
            return False
        if start < line.responsible_from:
            return False  # jumle ka shuru pichhli line ke paas tha
        if line.last_final_at >= start:
            return False  # Google is jumle ka kuch hissa likh chuka
        return not (
            line.open_text and line.open_since < start
        )  # pichla jumla abhi likha ja raha

    def _ensure_standby(self, now: float) -> None:
        if self._opening is not None and not self._opening.done():
            return
        if now < self._standby_retry_at:
            return
        self._opening = asyncio.create_task(
            self._prepare_standby(), name="ghost-ears-standby"
        )

    async def _prepare_standby(self) -> None:
        try:
            line = await self._open_line()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            text = _short(error)
            self._standby_retry_at = self._now() + self.standby_retry_secs
            if errors.is_rate_limited(text):
                self._break_first = True
                logger.info(
                    f"Kaan: key do lines ek saath nahi kholne deti ({text}) — khamoshi mein pehle purani band, phir nayi"
                )
            else:
                logger.warning(
                    f"Kaan: nayi line nahi khuli ({text}) — {self.standby_retry_secs:.0f} s baad dobara"
                )
            return
        if self._closing:
            await self._close_line(line)
            return
        self._standby = line
        current = self._line
        if current is not None:
            logger.info(
                f"Kaan: nayi line #{line.number} tayyar (chalti line #{current.number} {self._now() - current.opened_at:.0f} s purani)"
            )

    async def _switch(
        self, new: _Line, mode: str, *, keep_old_finals: bool = False
    ) -> None:
        """Mic nayi line par.

        `mode`:  quiet   khamoshi ka lamha — pichli 0.5 s saath, purani ka baad ka likha nahi
                 pause   jumlon ke beech ka waqfa — purani apna jumla likhti hai, nayi agla
                 replay  koi waqfa nahi aaya — chalta jumla shuru se nayi line ko
                 split   aakhri raasta — lafzon ke beech
        """
        failure: BaseException | None = None
        async with self._lock:
            old = self._line
            if old is None or old is new or new.dead or old.dead:
                return
            now = self._now()
            if mode == "quiet":
                start: float | None = now - self.preroll_secs
                keep = keep_old_finals or old.open_text
            elif mode == "pause":
                start, keep = now - PAUSE_PREROLL_SECS, True
            elif mode == "replay":
                start, keep = self._utt_start - self.preroll_secs, False
            else:
                start, keep = None, True
            self._activate(new)
            new.responsible_from = start if start is not None else now
            if self._standby is new:
                self._standby = None
            old.retired_at = now
            old.keep_finals = keep
            self._retiring.append(old)
            replayed = 0.0
            try:
                replayed = await self._replay(new, start)
            except Exception as error:  # noqa: BLE001
                failure = error
            try:
                # Purani line: jo jumla adhoora hai wo abhi likh de (keep_finals par aage jata hai).
                await old.session.send_realtime_input(audio_stream_end=True)
            except Exception:  # noqa: BLE001 — purani line ab kisi kaam ki nahi
                pass
        if failure is not None:
            await self._line_lost(new, failure)
            return
        self.stats["switches"] += 1
        reasons = {
            "quiet": "khamoshi mein",
            "pause": "jumlon ke beech ke waqfe par",
            "replay": "jumla shuru se dobara",
            "split": "lafzon ke beech",
        }
        logger.info(
            f"Kaan: line #{old.number} -> #{new.number} ({reasons[mode]}; purani {now - old.opened_at:.0f} s, "
            f"{replayed:.1f} s awaaz saath{', purane jumle aage' if keep else ''})"
        )

    async def _swap_in_place(self, line: _Line, now: float, mode: str) -> None:
        """Nayi line saath nahi khul sakti: purani band, phir nayi — beech ki awaaz yaad-dasht se.

        `mode`: quiet (khamoshi) | pause (jumlon ke beech ka waqfa) | hard (Google ke kaatne se pehle).
        """
        async with self._lock:
            if self._line is not line or line.dead:
                return
            if mode == "pause":
                replay_from, keep = now - PAUSE_PREROLL_SECS, True
            elif mode == "hard" and self._speaking and self._replay_ok(line, now):
                replay_from, keep = self._utt_start - self.preroll_secs, False
            elif mode == "hard" and self._speaking:
                replay_from, keep = now, True
            else:
                replay_from, keep = now - self.preroll_secs, line.open_text
            self._line = None
            self._session = None
            self._pause_due = False
            self._split_due = None
            self._rotate_requested = False
            line.retired_at = now
            line.keep_finals = keep
            self._retiring.append(line)
            try:
                await line.session.send_realtime_input(audio_stream_end=True)
            except Exception:  # noqa: BLE001
                pass
        reasons = {
            "quiet": "khamoshi mein",
            "pause": "jumlon ke beech ke waqfe par",
            "hard": "waqt khatam",
        }
        logger.info(
            f"Kaan: line #{line.number} badal rahe hain — pehle purani band, phir nayi ({reasons.get(mode, mode)})"
        )
        self._start_recovery(max(replay_from, now - self.ring_secs))

    async def _tidy_retiring(self, now: float) -> None:
        for line in list(self._retiring):
            settled = (
                not line.open_text
                and now - max(line.retired_at, line.last_output_at)
                >= self.retire_grace_secs
            )
            if (
                not line.keep_finals
                or line.dead
                or settled
                or now - line.retired_at >= self.retire_max_secs
            ):
                self._retiring.remove(line)
                self._close_later(line)
        # Tayyar khadi line bhi Google ke 10 minute mein gin-ti hai: bohat purani ho to nayi.
        standby = self._standby
        if (
            standby is not None
            and not standby.dead
            and now - standby.opened_at >= STANDBY_MAX_AGE_SECS
        ):
            self._standby = None
            logger.info(
                f"Kaan: tayyar khadi line #{standby.number} purani ho gayi — nayi khulegi"
            )
            self._close_later(standby)

    async def _ping_idle(self, now: float) -> None:
        """Jis line par der se awaaz nahi gayi (tayyar khadi line, ya mic chup) usay halki khamoshi."""
        rate = self.sample_rate
        if not rate:
            return
        silence = b"\x00" * (int(rate * PING_AUDIO_SECS) * 2)
        for line in (self._line, self._standby):
            if line is None or line.dead or now - line.last_sent < self.idle_ping_secs:
                continue
            failed: BaseException | None = None
            async with self._lock:
                if line.dead:
                    continue
                try:
                    await line.session.send_realtime_input(audio=self._blob(silence))
                    line.last_sent = now
                    self.stats["sent_secs"] += PING_AUDIO_SECS
                except Exception as error:  # noqa: BLE001
                    failed = error
            if failed is not None:
                await self._line_lost(line, failed)


async def _stop(task: asyncio.Task | None) -> None:
    if task is None or task.done() or task is asyncio.current_task():
        return
    task.cancel()
    await asyncio.wait({task}, timeout=1.0)
