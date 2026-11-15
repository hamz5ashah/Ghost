"""Agent ke dimagh ki jagah Pipecat ki pipeline mein.

    … sunna ──> user aggregator ──> [ClaudeAgentLLMService] ──> awaaz ──> speaker

Pipecat ke baqi dimagh (Anthropic, Gemini…) har jumle par ek request bhejte hain
aur jawab ke saath khatam ho jate hain. Ye dimagh (agent.py ka AgentBrain) poori
baat-cheet mein zinda rehta hai aur beech mein kaam bhi karta hai — is liye
yahan sirf teen kaam hain:

  1. User ka mukammal jumla dimagh ko dena — har jumla, ek hi dafa, tarteeb se
     (neeche "User ki baat dimagh tak"). Shor ne jawab kaata ho to dobara.
  2. Dimagh ke lafz awaaz tak pohnchana (wohi frames jo har Pipecat dimagh
     bhejta hai — captions aur awaaz ka baqi nizaam jaisa tha waisa chalta hai).
  3. User bol pare to dimagh ko batana, aur "ruk jao" par kaam rokna. Screen ke
     kaam ke beech user ka har jumla kaam ko wahin rokta hai aur foran usi
     dimagh ko jata hai (koi doosra agent jawab nahi deta).
  4. Baat-cheet ka ehsaas (floor.py): har jumle ke saath "moment" ka note,
     dimagh ke "chup" (`<quiet:…>`) ki khabar app ko, aur khamoshi mein Stonic
     ka apna moqa — har second ek nazar (`_watch_floor`).
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    StartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

from stonic_voice.agent import CUT_IN, JOBS_RUNNING, STOPPED_MID, AgentBrain, AgentConfig
from stonic_voice.bridge import AppChannel, ToolBridge
from stonic_voice.config import is_opening_cue
from stonic_voice.floor import Floor, Signals, read_signals
from stonic_voice.screen import ScreenTools
from stonic_voice.turns import EMPTY_TURN_CUE

# ── User ki baat dimagh tak ─────────────────────────────────────────────────
#
# Asal log (21 Oct 2026, ek 12 minute ki baat-cheet): teen dafa user ka jumla
# app ki screen par likha gaya magar dimagh (Claude) tak pohncha hi nahi — Claude
# ki notebook mein us ka nishan tak nahi. Har dafa foran baad ek nayi baari shuru
# hui thi: jumle ka doosra tukra (Google ne ek jumla do hisson mein likha), ya
# Google ka bhatka kaccha likha. Nayi baari par Pipecat dimagh wale hisse ka chalta
# kaam aur us ki qatar mita deta hai (InterruptionFrame) — aur jumla pehle hi
# "dekha hua" gina ja chuka tha. Ab:
#   - jumle pipeline.py ke usi context se uthaye jate hain (`watch_context`),
#     bina kisi `await` ke — Pipecat ka mitana beech mein nahi aa sakta;
#   - dimagh ko dene ka kaam alag task mein (`_drain`), jise wo nahi kaat-ta;
#   - user phir bolne lage to jumla ruk jata hai aur us baari ke lafz saath jur
#     kar EK jumla jata hai (dimagh adhoori baat par jawab shuru nahi karta).
#
# Shor wali baari (khansi, kursi — VAD ne awaaz suni, Google ne koi lafz nahi
# likha) Stonic ka jawab kaat deti thi aur phir koi use dobara nahi chalata tha
# (asal log: pehla "Hello" ka jawab kata, Stonic tab tak chup jab tak user ne phir
# nahi poochha). Pipecat aisi baari ka nishan deta hai (turns.EMPTY_TURN_CUE);
# jawab user tak nahi pohncha tha to dimagh wohi jawab dobara deta hai.

# User bol raha ho to ruka hua jumla zyada se zyada itni der rukta hai.
HOLD_MAX_SECS = 10.0
# Baari khatam hone ke baad us ka apna jumla context mein aane ka waqt.
TURN_SETTLE_SECS = 0.05
# Kati hui baari: dimagh ka "ruk" (interrupt) natije tak itni der.
SETTLE_WAIT_SECS = 1.5
# Ek jumle ka jawab shor ki wajah se itni dafa dobara.
MAX_RETRIES = 2
# Raasta khulne ke ishare (user ki baat nahi) kabhi kisi jumle ke saath nahi jurte:
# config.is_opening_cue (salaam, wapsi, aur saanjhe waqt ke ishare — life.py).

# Poora jumla sirf "ruk jao" ho (aage peeche chand narm lafz chal sakte hain) to
# kaam foran rukta hai. Lambi baat jis mein "stop" aa jaye ("stop sign ki
# tasveer dhoondo") rokne ka hukm nahi — wo model ko jati hai aur wohi samajhta hai.
_SOFT = r"(?:please|plz|bas|ok|okay|arey|are|yaar|abhi|now|stonic|जरा|ज़रा|बस|अभी|प्लीज़|प्लीज)"
_STOP = (
    r"stop(?:\s+(?:it|that|now|everything))?|cancel(?:\s+(?:it|that))?|ruk(?:o|iye|iyega)?(?:\s+ja(?:o|iye)?)?|ruk\s+ja"
    r"|bas\s+kar(?:o|do)?|rehne\s+d(?:o|e|ijiye)|chhor(?:o|\s+do)|band\s+kar(?:o|do)?"
    r"|रुको|रुकिए|रुक\s*जाओ|रुक\s*जा|बस\s*करो|रहने\s*दो|छोड़ो|छोड़\s*दो|बंद\s*करो|स्टॉप|कैंसल"
)
_STOP_PHRASE = re.compile(rf"^\W*(?:{_SOFT}\W+)*(?:{_STOP})(?:\W+{_SOFT})*\W*$", re.IGNORECASE)


def is_stop_request(text: str) -> bool:
    return bool(_STOP_PHRASE.fullmatch(str(text or "").strip()))


# Kaam ke dauran chhoti si "haan / theek / achha": ye hukm nahi, sirf sunne ka
# ishara hai. Is par screen ka kaam nahi rukta.
_NOD = r"(?:ok(?:ay)?|hmm+|hm+|a+c+hh?a+|theek(?:\s+hai)?|thik(?:\s+hai)?|ha+n|ji|yes|yeah|yep|right|good|nice|great|wah|shabash|अच्छा|ठीक(?:\s+है)?|हाँ|हां|जी)"
_NOD_PHRASE = re.compile(rf"^\W*{_NOD}(?:\W+{_NOD}){{0,2}}\W*$", re.IGNORECASE)


def is_backchannel(text: str) -> bool:
    return bool(_NOD_PHRASE.fullmatch(str(text or "").strip()))


def _user_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return " ".join(part for part in parts if part).strip()
    return ""


class ClaudeAgentLLMService(LLMService):
    """Claude Agent SDK, Pipecat ke dimagh ki shakal mein."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        channel: AppChannel,
        bridge: ToolBridge | None = None,
        screen: ScreenTools | None = None,
        floor: Floor | None = None,
        parent_pid: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(settings=LLMSettings(model=config.model), **kwargs)
        self._channel = channel
        self._bridge = bridge
        self._brain = AgentBrain(config, listener=self._on_brain, call_app=bridge.call if bridge else None, screen=screen)
        # User ki baatein (upar "User ki baat dimagh tak"): pipeline ka context, us
        # ke kitne paigham dekhe ja chuke, aur dimagh tak jane wali qatar —
        # ("line", jumla) ya ("empty", "") = shor wali baari.
        self._context: Any = None
        self._seen = 0
        self._inbox: deque[tuple[str, str]] = deque()
        self._handing: asyncio.Task[None] | None = None
        # User ki baari chal rahi hai (UserStartedSpeaking → UserStoppedSpeaking).
        self._user_turn = False
        self._turn_over = asyncio.Event()
        self._turn_over.set()
        # User ki aakhri baat jis ka jawab shor ne kaata ho to dobara diya jata hai.
        self._owed: str | None = None
        self._retries = 0
        self._boot: Any = None
        # Dimagh ka process uthane wala kaam — EK hi dafa (`start_brain`).
        self._brain_boot: asyncio.Task[None] | None = None
        # Is processor ke pipeline mein chalne (`start`) se pehle dimagh ke paigham
        # (notebook ka naam, uthne ki ghalti) yahan rukte hain aur `start` ke baad
        # usi tarteeb se app tak jate hain — dimagh connection judte hi uth sakta
        # hai, jab ke pipeline ka ye hissa abhi start na hua ho (paigham khoye to
        # notebook ka naam kho jata aur agli dafa chat resume nahi hoti).
        # None = chal raha hai, paigham seedhe.
        self._held: list[tuple[str, dict[str, Any]]] | None = []
        # Baat-cheet ka ehsaas (floor.py). None = band (settings.toml [floor]).
        self._floor = floor
        self._parent_pid = parent_pid  # app ka process: us ki window "Stonic ki apni", us ka mic "hamara"
        self._watch: Any = None

    @property
    def brain(self) -> AgentBrain:
        return self._brain

    @property
    def floor(self) -> Floor | None:
        return self._floor

    def watch_context(self, context: Any) -> None:
        """pipeline.py: user aggregator ka context — user ki har baat yahin se uthti hai."""
        self._context = context

    def start_brain(self) -> None:
        """Dimagh ka process uthao — EK dafa, jo pehle bulaye: app ka connection
        jurna (pipeline.py `on_client_connected`) ya pipeline mein is ki baari (`start`).

        Connection judne se PEHLE kabhi nahi: Windows par process ka uthna
        (CreateProcess + antivirus) engine ko lamha bhar rok sakta hai, aur us
        khidki mein Pipecat "connected" ki khabar kho deta hai (pipeline.py
        "Pipecat ka race"). Is ke paigham `_held` mein rukte hain jab tak ye
        processor chal na pare.
        """
        if self._brain_boot is None:
            self._brain_boot = asyncio.get_running_loop().create_task(self._brain.start(), name="stonic-agent-boot")

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        # Dimagh ke ruke paigham app tak; dimagh abhi na utha ho to ab (pehle jaisa).
        self._boot = self.create_task(self._go_live(), name="stonic-agent-live")
        if self._floor is not None:
            self._watch = self.create_task(self._watch_floor(), name="stonic-floor")

    async def _go_live(self) -> None:
        # Pehle wo paigham jo is se pehle aaye, tarteeb se. Is dauran naye aayen
        # to wo bhi isi qatar mein peeche lagte hain; qatar khali hote hi seedha.
        held = self._held
        while held:
            event, data = held.pop(0)
            await self._deliver(event, data)
        self._held = None
        self.start_brain()

    async def discard(self) -> None:
        """Session khatam ho rahi hai aur pipeline ka band hona is tak nahi pohncha
        (misal: connection judte hi pipeline giri): process band. Screen ke haath
        shared hain — kisi aur baat-cheet ka chalta kaam na ruke, is liye unhein
        nahi chhota. Aam baat-cheet ke baad (dimagh pehle hi band) kuch nahi karta."""
        if self._bridge is not None:
            self._bridge.close()
        if not self._brain.closed:
            await self._brain.stop(touch_hands=False)

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        await self._shutdown()

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._shutdown()

    async def _shutdown(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            await self.cancel_task(watch)
        handing, self._handing = self._handing, None
        self._inbox.clear()
        if handing is not None and not handing.done():
            handing.cancel()
        if self._bridge is not None:
            self._bridge.close()
        await self._brain.stop()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            # Pipecat abhi dimagh wale hisse ki qatar mitayega: jo jumla context mein
            # aa chuka (us ka frame shayad qatar mein tha) wo pehle apni qatar mein.
            self._kick()
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self._on_context(frame)
            return
        if isinstance(frame, InterruptionFrame):
            await self._brain.barge_in()
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._user_turn = True
            self._turn_over.clear()
            if self._floor is not None:
                self._floor.user_speaking(True)
            self._brain.user_speaking(True)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_turn = False
            self._turn_over.set()
            if self._floor is not None:
                self._floor.user_speaking(False)
            self._brain.user_speaking(False)
            self._kick()
        elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)) and self._floor is not None:
            # Awaaz speaker se nikalna shuru / band (transport upar ko bhi bhejta hai).
            # Khamoshi ki ghari Stonic ki awaaz rukne se chalti hai, lafz likhne se nahi.
            self._floor.bot_speaking(isinstance(frame, BotStartedSpeakingFrame))
        await self.push_frame(frame, direction)

    async def _on_context(self, frame: LLMContextFrame) -> None:
        if frame.speculation:
            return  # adhoore jumle par andaze ka jawab: ye dimagh kaam karta hai, andaze par nahi chalta
        self._collect(frame.context)
        self._kick()
        handing = self._handing
        if handing is not None:
            # Pipecat ye intezar beech mein kaat sakta hai (nayi baari) — dimagh tak
            # pohnchane wala task phir bhi poora chalta hai.
            await asyncio.shield(handing)

    # ── User ki baat dimagh tak (upar "User ki baat dimagh tak") ────────────

    def _collect(self, context: Any = None) -> None:
        """Context ke naye paigham qatar mein — koi `await` nahi, is liye Pipecat beech mein nahi kaat sakta."""
        context = self._context if self._context is not None else context
        if context is None:
            return
        messages = context.get_messages()
        if len(messages) < self._seen:
            self._seen = max(0, len(messages) - 1)
        for message in messages[self._seen:]:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "user":
                text = _user_text(message)
                if text:
                    self._inbox.append(("line", text))
            elif role == "developer" and message.get("content") == EMPTY_TURN_CUE:
                self._inbox.append(("empty", ""))
        self._seen = len(messages)

    def _kick(self) -> None:
        """Naye paigham uthao; qatar mein kuch ho to use khali karne wala task chalao (ek hi)."""
        self._collect()
        if self._inbox and (self._handing is None or self._handing.done()):
            self._handing = asyncio.get_running_loop().create_task(self._drain(), name="stonic-agent-inbox")

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        held_since: float | None = None  # qatar ka pehla jumla kab se user ke bolne ki wajah se ruka hai
        while self._inbox:
            patient = held_since is None or loop.time() - held_since < HOLD_MAX_SECS
            if self._user_turn and patient:
                # User phir bol raha hai: ye jumla us ki baat ke saath jayega (ya akela, agar wo sirf shor tha).
                held_since = held_since if held_since is not None else loop.time()
                await self._wait_turn_end(HOLD_MAX_SECS - (loop.time() - held_since))
                continue
            kind, text = self._inbox.popleft()
            try:
                if kind == "empty":
                    await self._answer_unheard()
                    continue
                # Lagataar jumle (ek hi baat ke tukre, user ke bolte hue ruke) = ek jumla.
                while not is_opening_cue(text) and self._inbox and self._inbox[0][0] == "line" and not is_opening_cue(self._inbox[0][1]):
                    text = f"{text} {self._inbox.popleft()[1]}"
                if await self._give(text, patient=patient):
                    held_since = None
                else:
                    held_since = held_since if held_since is not None else loop.time()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — ek jumle ki ghalti agle ko na roke
                logger.warning(f"Agent: jumla dimagh ko dete hue ghalti — {type(error).__name__}: {error}")

    async def _wait_turn_end(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._turn_over.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            logger.warning(f"Agent: user ki baari {HOLD_MAX_SECS:.0f} s mein khatam na hui — ruka hua jumla ab dimagh ko")
            return
        # Baari ka apna jumla (agar koi) context mein aa jaye, phir sab ek saath.
        await asyncio.sleep(TURN_SETTLE_SECS)
        self._collect()

    async def _give(self, text: str, *, patient: bool = True) -> bool:
        """Ek mukammal jumla dimagh ko (zarurat ho to chalta kaam rok kar).

        False = nahi diya: note bante hue user phir bolne laga — jumla wapas qatar
        ke shuru mein, us ki baat ke saath jayega (`patient` = abhi rok sakte hain).
        """
        self._brain.user_speaking(False)
        note = ""
        # Jumla kaam ke beech aaya: dimagh ise "kisi aur se" samjhe to kaam jari rehta hai.
        mid_job = self._brain.doing_work
        moment = await self._moment(text)
        if self._user_turn and patient:
            self._inbox.appendleft(("line", text))
            return False
        if self._brain.working and is_stop_request(text):
            logger.info("Agent: user ne rokne ko kaha — chalta kaam roka ja raha hai")
            if self._brain.on_screen:
                note = STOPPED_MID
            await self._brain.interrupt()
        elif is_stop_request(text) and self._brain.jobs:
            pass  # saamne kuch nahi chal raha: neeche wala note dimagh ko batata hai ke peeche kya chal raha hai
        elif self._brain.on_screen and not is_backchannel(text):
            # Screen ka kaam chal raha hai aur user ne kuch kaha: haath wahin
            # rukte hain aur jumla ABHI usi dimagh ko milta hai — taake "nahi,
            # aise karo" agle click se pehle sun liya jaye, kaam khatam hone ke
            # baad nahi (wajah aur naap: agent.py CUT_IN). Click aur tehreer
            # chhote kaam hain, in ka rukna kuch nahi torta. Lamba command
            # (PowerShell) beech mein nahi kaata jata: wahan jumla pehle ki
            # tarah agle qadam par pohnchta hai.
            logger.info("Agent: screen ke kaam ke beech user bola — kaam roka, jumla foran dimagh ko")
            await self._brain.interrupt()
            note = CUT_IN
        if is_stop_request(text) and self._brain.jobs:
            note = f"{note}\n\n{JOBS_RUNNING.format(jobs='; '.join(self._brain.jobs))}".strip()
        note = "\n\n".join(part for part in (moment, note) if part)
        await self._brain.say(text, note=note, mid_job=mid_job)
        self._owed, self._retries = text, 0
        return True

    async def _answer_unheard(self) -> None:
        """Shor wali baari ne Stonic ko kaata: user ki aakhri baat ka jawab us tak nahi pohncha to dobara.

        Pipecat ye nishan tab hi deta hai jab baari ne Stonic ko beech mein kaata
        (soch raha tha ya bol raha tha). Dimagh ne us baat par chup rehna chuna
        tha, ya ab Stonic khud kuch keh raha tha (moqa / Awareness) — to kuch nahi.
        """
        line = self._owed
        if line is None or self._retries >= MAX_RETRIES:
            return
        # Dimagh ko "ruk" gaya tha: us ka natija (~0.1–0.3 s) aane tak.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_WAIT_SECS
        while self._brain.working and loop.time() < deadline:
            await asyncio.sleep(0.1)
        if self._brain.working or self._user_turn:
            return  # jawab abhi aa raha hai, ya user phir bol raha hai (us ki baat aayegi)
        self._retries += 1
        logger.info("Agent: shor ne jawab kaata (koi lafz nahi) — user ki aakhri baat ka jawab dobara")
        await self._brain.retry(line)

    async def _moment(self, text: str) -> str:
        """Is jumle ka "moment" note (floor.py). Khali = baat-cheet ka ehsaas band, ya raasta khulne ka ishara."""
        if self._floor is None or is_opening_cue(text):
            return ""
        return self._floor.heard(await self._signals())

    async def _signals(self) -> Signals | None:
        """Saamne wali window + mic wali apps: chand millisecond, magar registry aur Windows API — event loop ko na roke."""
        try:
            return await asyncio.to_thread(read_signals, self._parent_pid)
        except Exception as error:  # noqa: BLE001 — signal na mile to bhi note / moqa banta hai
            logger.debug(f"Floor: PC ke signals nahi mile — {error}")
            return None

    # ── Khamoshi mein Stonic ka apna moqa ───────────────────────────────────

    async def _watch_floor(self) -> None:
        """Har second: Stonic ne kuch poocha aur jawab nahi aaya, ya us ki baat kat gayi?

        Moqa ho to dimagh se poochha jata hai (`AgentBrain.cue`) — bole ya chup
        rahe, faisla us ka. Ye loop kabhi khud kuch nahi bolta.
        """
        floor = self._floor
        assert floor is not None
        while True:
            await asyncio.sleep(1.0)
            try:
                self._kick()  # hifazat: koi jumla context mein reh gaya ho to dimagh tak
                cue = floor.due(busy=self._brain.working or bool(self._inbox))
                if cue is None:
                    continue
                cue = floor.with_signals(cue, await self._signals())
                logger.info("Floor: khamoshi mein moqa — dimagh se poochha ja raha hai (bole ya chup rahe)")
                if await self._brain.cue(cue):
                    self._owed = None  # ab Stonic khud bol raha hai — pichli baat ka hisaab band
                else:
                    floor.decided("quiet", "other")  # dimagh is lamhe masroof: moqa band, baad mein phir
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — ye nigrani baat-cheet ko kabhi na giraye
                logger.warning(f"Floor: nigrani mein ghalti — {type(error).__name__}: {error}")

    async def awareness_cue(self, text: str) -> bool:
        """Awareness (app) ne tay kiya ke Stonic ko khud pehle bolna hai: wohi
        `[STONIC_AWARENESS]` ishara jo Standard voice ko jata hai.

        Floor ke moqe jaisa: dimagh apne lafzon mein kehta hai, koi tool nahi
        (CUE_TOOLS). False = dimagh is waqt kaam mein hai — Awareness thori der
        baad dobara koshish karti hai (ya baat chhor deti hai).
        """
        if self._inbox:
            logger.info("Awareness: Stonic ki apni baat abhi nahi — user ka jumla dimagh tak ja raha hai")
            return False
        if self._floor is not None:
            self._floor.initiative()
        sent = await self._brain.cue(text)
        if sent:
            self._owed = None
        logger.info(f"Awareness: Stonic ki apni baat dimagh ko {'di gayi' if sent else 'nahi di ja saki (dimagh masroof)'}")
        return sent

    async def _on_floor(self, data: dict[str, Any]) -> None:
        """Dimagh ka faisla: suni hui baat (ya khamoshi ka moqa) par bola, ya chup raha."""
        kind = str(data.get("decision") or "")
        why = str(data.get("why") or "")
        if self._floor is not None:
            self._floor.decided(kind, why)
        if kind == "quiet" and not data.get("cue"):
            self._owed = None  # dimagh ne soch kar chup rehna chuna — shor ke baad dobara jawab nahi
            # App ki screen "soch raha hai" par na atke, aur ye jumla user ki baat
            # ke taur par yaadash / chat mein na likha jaye (doosron ki baatein).
            await self._channel.send({"type": "stonic-floor", "state": "quiet", "why": why})

    # ── Dimagh ke paigham ───────────────────────────────────────────────────

    async def _on_brain(self, event: str, data: dict[str, Any]) -> None:
        held = self._held
        if held is not None:
            held.append((event, data))  # pipeline abhi nahi chali (ya ruke paigham ja rahe hain): qatar mein
            return
        await self._deliver(event, data)

    async def _deliver(self, event: str, data: dict[str, Any]) -> None:
        if event == "text":
            if self._floor is not None:
                self._floor.stonic_text(data["text"])
            await self._push_llm_text(data["text"])
        elif event == "reply_start":
            if self._floor is not None:
                self._floor.reply_started()
            await self.push_frame(LLMFullResponseStartFrame())
        elif event == "floor":
            await self._on_floor(data)
        elif event == "reply_end":
            await self.push_frame(LLMFullResponseEndFrame())
        elif event == "tool_start":
            # `args`: app ki chat mein tool ka "Input" (agent.py tool_args — chhota kiya hua).
            await self._channel.send({
                "type": "stonic-activity", "state": "working", "id": data["id"], "tool": data["tool"], "label": data["label"],
                "args": data.get("args"),
            })
        elif event == "tool_end":
            # `output` / `ms`: chat mein tool ka "Output" aur kitni der laga.
            await self._channel.send({
                "type": "stonic-activity", "state": "step_done", "id": data["id"], "ok": data["ok"],
                "output": data.get("output", ""), "ms": data.get("ms"),
            })
        elif event == "thinking":
            # Model ki soch — boli nahi jati, sirf app ki chat mein dikhti hai.
            await self._channel.send({"type": "stonic-thinking", "text": data["text"]})
        elif event == "context":
            # Chat ki lambai (app ka ring, 70% par nazar aata hai) aur compact ka haal — agent.py `_tell_context`.
            await self._channel.send({
                "type": "stonic-context", "used": data.get("used", 0), "limit": data.get("limit", 0),
                "window": data.get("window", 0), "compacting": bool(data.get("compacting")), "compacted": bool(data.get("compacted")),
                "summarized": bool(data.get("summarized")),
            })
        elif event == "session":
            # Is chat ki notebook (SDK session): app chat ke saath yaad rakhti hai, agli dafa resume.
            await self._channel.send({"type": "stonic-agent-session", "id": data["id"], "resumed": bool(data.get("resumed"))})
        elif event == "life":
            # Naam khud chunne wale kirdar (Soulmate) ne apna naam chuna (life.py): app ise kirdar ka
            # naam bana leti hai (soul-config savePersona) — wake word, card, sab jagah.
            await self._channel.send({"type": "stonic-life", "character": data.get("character", ""), "name": data.get("name", "")})
        elif event in ("job_start", "job_end"):
            # Chat mein ek line: kaunsa kaam peeche gaya / khatam hua.
            await self._channel.send({
                "type": "stonic-job", "event": "start" if event == "job_start" else "end",
                "id": data.get("id", ""), "label": data.get("label", ""), "status": data.get("status", "running"),
            })
            # Peeche chalte kaam: screen par ek chhoti line, taake pata rahe ke kuch chal raha hai.
            jobs = self._brain.jobs
            await self._channel.send({"type": "stonic-activity", "state": "background", "count": len(jobs), "label": jobs[-1] if jobs else ""})
        elif event == "turn_end":
            await self._channel.send({"type": "stonic-activity", "state": "idle", "aborted": data["aborted"], "steps": data["steps"]})
        elif event == "error":
            detail = str(data.get("detail") or "agent error")
            if data.get("fatal"):
                # Dimagh ka process hi nahi raha: baat-cheet aage nahi chal sakti.
                detail = f"stonic:engine_agent_failed: {detail}"
            await self.push_error(error_msg=detail)
