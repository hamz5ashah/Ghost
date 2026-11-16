"""User ki baari kab khatam: Smart Turn + Google ka poora jumla.

Pipecat ka Smart Turn awaaz ke lehje se tay karta hai ke user ki baat poori hui ya
wo abhi soch raha hai. "Adhoori" kahe to baari tab khatam hoti hai jab 3 s
khamoshi guzar jaye (settings.toml `smart_turn_max_wait_secs`).

Masla (naapa, 21 Oct 2026): Smart Turn zyada tar English par sikhaya gaya hai.
Chhoti Urdu / Hindi baat ("Baat suno.", "Stonic GF") ko wo aksar "adhoori" kehta
hai, aur user 3 s khamoshi + dimagh ke ~3 s ka intezar karta hai — lagta hai
Stonic atak gaya. User doosra lafz bole to baari foran khatam (sab ek saath).
Asal log: "Stonic GF" ka jawab 7.7 s mein, jis mein ~3.3 s sirf ye intezar.

Hal: ek doosri raaye — Google ki likhai. Google jumla poora sun kar hi us ke
aakhir mein full stop / sawal ka nishan lagata hai ("Baat suno.", "Acha, ek
kaam karo."); adhoori baat par nahi ("So I was thinking that", "اور", "لیکن").
Ek-lafzi jawab ("ہاں", "نہیں") par wo aksar nishan nahi lagata — wo alag pehchane
jate hain. To:

  Smart Turn "poori"                    -> baari foran khatam (pehle jaisa)
  Smart Turn "adhoori", Google ka jumla
  nishan par khatam ("." "?" "!" "۔"),
  ya ek-lafzi jawab                     -> `finished_wait_secs` (1.2 s) khamoshi kaafi
  Smart Turn "adhoori", baqi sab        -> 3 s khamoshi (pehle jaisa)

Naap (21 Oct 2026, Google TTS ki Hindustani awaaz, asli Google + Stonic Cloud):
14 chhote jumlon mein 5 par Smart Turn ne ghalat "adhoori" kaha (3.2 s intezar).

Is dauran user phir bole (VAD) ya Google naye lafz likhe to intezar wahin khatam
— baari jaari rehti hai. "..." / "…" baat ka latakna hai, poora jumla nahi.

Har baari ke khatam hone par ek line log mein: khamoshi se kitni der baad, kis
wajah se (Smart Turn / Google ka jumla / 3 s khamoshi / 5 s watchdog) aur kitne
huroof — user ke lafz kabhi nahi. Agli dafa "atak gaya" ho to log batata hai kahan.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

# Jumle ka aakhri nishan (English, Urdu, Hindi, aur full-width), peeche band hone wale quote / bracket chal sakte hain.
_CLOSERS = "'\")]»”’"
_FINISHED = re.compile(rf"[.!?؟۔।！？。][{re.escape(_CLOSERS)}]*\s*$")
_TRAILING = re.compile(rf"(?:\.\.\.|…)[{re.escape(_CLOSERS)}]*\s*$")
# Ek-lafzi jawab jo apne aap mein poora hai. Google Urdu likhai mein in par aksar
# nishan nahi lagata ("ہاں", "نہیں") aur Smart Turn inhein adhoora samajhta hai
# (naapa: "ہاں" par 3.2 s). "aur" / "lekin" jaise jorne wale lafz yahan NAHI.
_ANSWER = re.compile(
    r"(?:ha+n|ji|jee|nahi+n?|nai|theek(?:\s+hai)?|thik(?:\s+hai)?|achh?a+|ok(?:ay)?|yes|yeah|yep|no|nope"
    r"|bilkul|shukriya|thanks|thank\s+you"
    r"|हाँ|हां|जी|नहीं|ठीक(?:\s+है)?|अच्छा|बिल्कुल|शुक्रिया"
    r"|ہاں|ہا|جی|نہیں|ٹھیک(?:\s+ہے)?|اچھا|بالکل|شکریہ)",
    re.IGNORECASE,
)

# Khali baari ka nishan (pipeline.py `build_user_params`): VAD ne awaaz suni (khansi,
# kursi, "hmm") magar Google ne koi lafz nahi likha, aur is baari ne Stonic ko beech
# mein kaata (soch raha tha / bol raha tha). Pipecat aisi baari par context mein ek
# "developer" paigham daal kar dimagh chalata hai (EmptyUserTurnConfig); agent wala
# dimagh (agent_service.py) sirf user ke paigham parhta tha, is liye ye hifazat
# chup chaap bekaar thi. Ab agent ye nishan pehchanta hai — model tak nahi jata.
EMPTY_TURN_CUE = "[ghost:empty-turn]"

# Baari band hone ki wajah (log ke liye).
WHY_SMART = "Smart Turn ne poori kahi"
WHY_GOOGLE = "Smart Turn ne adhoori kahi, Google ka jumla poora"
WHY_SILENCE = "Smart Turn ne adhoori kahi, khamoshi ki hadd guzri"
WHY_TEXT_ONLY = "VAD ne bolna nahi pakra, sirf likhai"
WHY_OUTSIDE = "5 s watchdog / bahar se"


def sounds_finished(text: str) -> bool:
    """Google ne jumla poora likha? Aakhir mein full stop / sawal / hairat ka nishan ("..." nahi),
    ya poori baat ek-lafzi jawab ("haan", "ہاں") — comma ke saath nahi ("Haan," = aage kuch hai)."""
    clean = str(text or "").strip()
    if not clean or _TRAILING.search(clean):
        return False
    return bool(_FINISHED.search(clean)) or bool(_ANSWER.fullmatch(clean))


class GhostTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """Pipecat ka Smart Turn wala faisla, magar Google ka poora jumla bhi ginti mein."""

    def __init__(
        self,
        *,
        finished_wait_secs: float = 1.2,
        now: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.finished_wait_secs = finished_wait_secs
        self._now = now
        # Smart Turn ki raaye is waqfe par: "complete" | "incomplete" | None (abhi nahi).
        self._verdict: str | None = None
        # Asal khamoshi kab shuru hui (VAD ke `stop_secs` ghata kar). 0 = VAD ne chup nahi kaha.
        self._silent_since = 0.0
        self._why = ""
        self._finish_task: asyncio.Task | None = None
        # Is baari mein Google ne kitne huroof likhe (log ke liye; lafz nahi).
        self._chars = 0

    # ── Pipecat ke darwaze ───────────────────────────────────────────────────

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        result = await super().process_frame(frame)
        if isinstance(frame, InterimTranscriptionFrame):
            # Google naye lafz likh raha hai: user abhi bol raha hai.
            await self._cancel_finish()
        return result

    async def _handle_vad_user_stopped_speaking(
        self, frame: VADUserStoppedSpeakingFrame
    ) -> None:
        self._silent_since = self._now() - float(
            getattr(frame, "stop_secs", 0.0) or 0.0
        )
        await super()._handle_vad_user_stopped_speaking(frame)
        # Upar wala Smart Turn se poochh chuka (aur shayad baari khatam bhi kar chuka).
        if self._vad_stopped:
            self._verdict = "complete" if self._turn_complete else "incomplete"
            await self._maybe_finish_early()

    async def _handle_transcription(self, frame: Any) -> None:
        # Ginti pehle: upar wala isi jumle par baari khatam kar sakta hai (aur log likhta hai).
        self._chars += len(str(getattr(frame, "text", "") or "").strip())
        await super()._handle_transcription(frame)
        await self._maybe_finish_early()

    async def _discard_pending_end_of_turn(self) -> None:
        # User phir bola (ya baari ki hadd): pichla andaza bekaar.
        await self._cancel_finish()
        self._verdict = None
        self._why = ""
        await super()._discard_pending_end_of_turn()

    async def handle_user_turn_started(self) -> None:
        self._chars = 0
        self._silent_since = 0.0
        await super().handle_user_turn_started()

    async def handle_user_turn_stopped(self) -> None:
        self._log_end()
        self._chars = 0
        self._silent_since = 0.0
        await super().handle_user_turn_stopped()

    async def trigger_user_turn_stopped(self, **kwargs: Any) -> None:
        if not self._why:
            self._why = self._infer_why()
        await super().trigger_user_turn_stopped(**kwargs)

    async def cleanup(self) -> None:
        await self._cancel_finish()
        await super().cleanup()

    # ── Google ka poora jumla ────────────────────────────────────────────────

    def _finish_ready(self) -> bool:
        """Smart Turn "adhoori", magar user chup hai aur Google ka aakhri jumla poora."""
        return (
            self.finished_wait_secs > 0
            and not self._turn_complete
            and self._verdict == "incomplete"
            and self._vad_stopped
            and not self._vad_user_speaking
            and self._transcript_finalized
            and sounds_finished(self._text)
        )

    async def _maybe_finish_early(self) -> None:
        if self._finish_task is not None or not self._finish_ready():
            return
        # Khamoshi ki ginti wahin se jahan user chup hua (Google ka likha ~0.5 s baad aata hai).
        delay = max(0.0, self._silent_since + self.finished_wait_secs - self._now())
        self._finish_task = self.task_manager.create_task(
            self._finish_after(delay), f"{self}::finish_early"
        )

    async def _finish_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        finally:
            # Baari khatam karte hi `_discard_pending_end_of_turn` chalta hai: wo is
            # task ko khud na kaate (Pipecat ka `_timeout_handler` bhi yehi karta hai).
            if self._finish_task is asyncio.current_task():
                self._finish_task = None
        if not self._finish_ready():
            return
        self._why = WHY_GOOGLE
        self._turn_complete = True
        await self._maybe_trigger_user_turn_stopped()

    async def _cancel_finish(self) -> None:
        task, self._finish_task = self._finish_task, None
        if task is not None and task is not asyncio.current_task():
            await self.task_manager.cancel_task(task)

    # ── Log ──────────────────────────────────────────────────────────────────

    def _infer_why(self) -> str:
        if self._verdict == "incomplete":
            return WHY_SILENCE  # baad mein khamoshi ki hadd ne "poori" kiya
        if self._vad_stopped:
            return WHY_SMART  # VAD ke chup par hi Smart Turn ne "poori" kaha
        return WHY_TEXT_ONLY

    def _log_end(self) -> None:
        why = self._why or WHY_OUTSIDE
        if self._silent_since:
            after = f"khamoshi se {round((self._now() - self._silent_since) * 1000)} ms baad"
        else:
            after = "VAD ne khamoshi nahi pakri"
        logger.info(f"Baari khatam: {after} — {why} ({self._chars} huroof)")
        self._why = ""
