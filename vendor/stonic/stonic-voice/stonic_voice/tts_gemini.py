"""Dimagh ki likhi line ko awaaz banana — ada ki hidayat ke saath.

Dimagh apni line ke saath ada likhta hai ("[soft and slow] Sir... [sigh] afsos
hai."). Yahan wo do hisson mein batti hai: andaaz ("soft and slow") aur bolne
wala text jis mein awaazein model ki zubaan mein hain ("<sigh>"). Ye hissa
Voice Lab (voice-lab/lab/tts_gemini.py) se hoobahoo hai.

Awaaz in mein se ek "speaker" banata hai — user Settings mein raasta chunta hai:

  KeySpeech        user ki apni key par Gemini 3.8 TTS. Poori ada. Key par
                   requests ki tang hadd hai (naapi hui, 12 Oct 2026, Tier 1:
                   10 fi minute aur 100 fi din) — hadd par line boli nahi jati,
                   saaf wajah ("stonic:gemini_daily_quota: ...").
  ExpressiveSpeech Settings ka "Expressive": KeySpeech, aur us ki hadd par
                   wohi line Standard (LiveSpeech) se — BATA KAR (neeche).
  LiveSpeech       user ki apni key par Gemini Live se line bulwana. Requests
                   ki hadd nahi (muft key par bhi), ada TTS se kam. Settings ka
                   "Standard", apni keys par default.
  CloudSpeech      Stonic Cloud (cloud.py) — hamare gateway se wohi Gemini 3.8
                   TTS, user ke plan ke andar, key ke baghair.

Hadd par CHUP CHAAP doosri awaaz par girna nahi (12 Oct 2026: user ko pata hi
nahi chalta tha ke jazbaat kyun badal gaye). 14 Oct 2026, Usman: Expressive ki
hadd Gemini ke TIER ki hai, key kharab nahi — baat-cheet na ruke, Standard par
chali jaye, aur user ko ek dheemi line mile ("stonic-voice" paigham). Hadd
khulte hi agli line phir Expressive.

Lab se ek farq: jawab tukron mein jata hai (ReplyChunkAggregator) — pehla jumla
akela, taake awaaz foran shuru ho; baqi jumle chand bare tukron mein. Lab mein
har jumla ek request tha.

Baqi sab — awaaz ke frames, ruk jana, metrics — Pipecat ka apna hai.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types
from loguru import logger
from pipecat.frames.frames import AggregatedTextFrame, ErrorFrame, Frame, StartFrame, TTSAudioRawFrame
from pipecat.services.google.tts import GeminiTTSService
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.skip_tags_aggregator import SkipTagsAggregator
from pipecat.utils.types import assert_given

# Dimagh ada ki har hidayat ek hi shakal mein likhta hai: "[...]". Do qismein:
#   awaaz   "[sigh]"            usi jagah ki ek awaaz -> model ka tag "<sigh>"
#   andaaz  "[soft and slow]"   agle jumle kaise bole jayen -> `style`
_BRACKET = re.compile(r"\[([^\[\]]*)\]")
# Gemini 3.8 TTS ki awaazein (ai.google.dev, speech generation, 12 Oct 2026).
SOUNDS = frozenset(
    "argh, breath, heavy breath, exhales, cackle, cheer, chuckle, cough, cry, gasp, giggle, groan, growl, "
    "grunt, grr, hiss, laugh, moan, pant, pff, phew, scream, shout, shriek, sigh, sneeze, snicker, snort, sob, "
    "throat-clearing, tsk, whimper, whispers, yawn, short pause, long pause".split(", ")
)
# Andaaz chand saade lafz hota hai. "[1, 2, 3]" ya "[x + y]" andaaz nahi, asli text hai.
_DIRECTION = re.compile(r"[A-Za-z][A-Za-z ,'\-]{0,80}")
# Ye koi andaaz nahi, "wapas aam awaaz" ka ishara hai.
_BACK_TO_BASE = {"", "neutral", "normal"}
_PARAGRAPH_START = re.compile(r"[ \t]*\n\s*\n")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def tts_request(*, model: str, voice: str, text: str, style: str = "") -> dict[str, Any]:
    """Gemini 3.8 TTS ka request (`generate_content_stream` ke kwargs).

    Zubaan model khud pehchanta hai; lehja awaaz se aata hai, `style` se nahi.
    """
    part: dict[str, Any] = {"text": text}
    if style:
        part["speech_metadata"] = {"style": style}
    return {
        "model": model,
        "contents": [{"role": "user", "parts": [part]}],
        "config": types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(voice=voice)),
        ),
    }


def audio_from_chunk(chunk: Any) -> Iterator[bytes]:
    """Stream ke ek tukre se awaaz (bina header ka 16-bit PCM, 24 kHz)."""
    candidate = chunk.candidates[0] if chunk.candidates else None
    for part in (candidate.content.parts if candidate and candidate.content else None) or []:
        if part.inline_data and part.inline_data.data:
            yield part.inline_data.data


def starts_paragraph(text: str) -> bool:
    """Jumla naye paragraph se shuru hota hai? Andaaz apne paragraph tak rehta hai."""
    return bool(_PARAGRAPH_START.match(text))


def _sound(name: str) -> str | None:
    """ "[laughs]" / "[Sigh]" / "[pause]" / "[soft chuckle]" -> model ki apni awaaz ka naam, warna None."""
    name = name.lower()
    stem = name.removesuffix("ing")
    for candidate in (name, name.removesuffix("s"), stem, f"{stem}e", f"short {name}"):
        if candidate in SOUNDS:
            return candidate
    # "[soft chuckle]", "[deep sigh]": sifat + awaaz. Lambi hidayat ("[out of breath]") andaaz hi rehti hai.
    words = name.split()
    return _sound(words[1]) if len(words) == 2 else None


def split_directions(text: str, style: str, base: str = "") -> tuple[list[tuple[str, str]], str]:
    """Line ko (andaaz, bolne wala text) ke tukron mein baanto.

        "[soft and slow] Sir... [sigh] afsos hai. [steady] Main yahin hoon."
          -> [("soft and slow", "Sir... <sigh> afsos hai."), ("steady", "Main yahin hoon.")]

    `style` wo andaaz hai jo pichhle jumle se chala aa raha hai. Andaaz agli
    hidayat tak qaim rehta hai, is liye aakhri andaaz wapas milta hai — agla
    jumla usi mein bola jata hai. [neutral] = wapas aam awaaz (`base`).
    """
    segments: list[tuple[str, str]] = []
    spoken = ""

    def flush() -> None:
        nonlocal spoken
        # Adhoori hidayat ka akela bracket awaaz tak na pohnche.
        line = " ".join(spoken.replace("[", " ").replace("]", " ").split())
        spoken = ""
        # Sirf nishan (".", "—") ho to bolne ko kuch nahi.
        if any(char.isalnum() for char in line):
            segments.append((style, line))

    position = 0
    for match in _BRACKET.finditer(text):
        spoken += text[position : match.start()]
        position = match.end()
        inner = match.group(1).strip()
        sound = _sound(inner)
        if sound:
            spoken += f"<{sound}>"
        elif not inner or _DIRECTION.fullmatch(inner):
            flush()
            style = base if inner.lower() in _BACK_TO_BASE else inner
        else:
            spoken += f" {inner} "
    spoken += text[position:]
    flush()
    return segments, style


def split_reply(text: str, style: str, base: str = "") -> tuple[list[tuple[str, str]], str]:
    """Jawab ke ek tukre ko (andaaz, bolne wala text) mein baanto — har (andaaz) ek request.

    Tukre mein kai jumle aur kai paragraph ho sakte hain. Andaaz apne paragraph
    tak rehta hai: har naya paragraph aam awaaz (`base`) se shuru hota hai. Ek
    hi andaaz wale parosi hisse jor diye jate hain, taake requests kam hon.
    """
    segments: list[tuple[str, str]] = []
    for index, paragraph in enumerate(_PARAGRAPH_BREAK.split(text)):
        if index:
            style = base
        parts, style = split_directions(paragraph, style, base)
        for part_style, spoken in parts:
            if segments and segments[-1][0] == part_style:
                segments[-1] = (part_style, f"{segments[-1][1]} {spoken}")
            else:
                segments.append((part_style, spoken))
    return segments, style


# ── Jawab ke tukre ───────────────────────────────────────────────────────────


class ReplyChunkAggregator(SkipTagsAggregator):
    """Dimagh ke lafzon ko awaaz ke requests mein baantna.

    Pehla jumla foran aur akela jata hai — wohi der sunai deti hai. Us ke baad
    ke jumle jama hote hain aur bare tukron mein jate hain: awaaz bolne se 3-4
    guna tez banti hai, is liye agla tukra pichhle ki awaaz ke peeche ban jata
    hai aur sunne mein koi waqfa nahi aata.

    Hidayat ke andar ka nuqta jumle ka ikhtitaam nahi ("[tired... then brighter]")
    — ye SkipTagsAggregator sambhalta hai.
    """

    def __init__(self, *, min_chars: int, next_min_chars: int, max_hold_secs: float, **kwargs):
        super().__init__([("[", "]")], **kwargs)
        self._min_chars = min_chars
        self._next_min_chars = next_min_chars
        self._max_hold_secs = max_hold_secs
        self._sent = 0  # is jawab ke kitne tukre ja chuke
        self._held = ""  # poore jumle jo agle tukre ke intezar mein hain
        self._held_since = 0.0

    @property
    def text(self) -> Aggregation:
        return Aggregation(text=f"{self._held}{self._text}".strip(" "), type=AggregationType.SENTENCE)

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        if self._aggregation_type == AggregationType.TOKEN:
            async for token in super().aggregate(text):
                yield token
            return

        async for sentence in super().aggregate(text):
            if self._sent == 0:
                self._sent = 1
                yield sentence
                continue
            if not self._held:
                self._held_since = time.monotonic()
            # Naya paragraph apni khali line saath lata hai; baqi jumlon ke beech ek space.
            joiner = "" if not self._held or sentence.text.startswith("\n") else " "
            self._held += f"{joiner}{sentence.text}"
            if len(self._held) >= (self._min_chars if self._sent == 1 else self._next_min_chars):
                yield self._release()

        # Dimagh sust ho to jama jumle hamesha intezar na karein.
        if self._held and time.monotonic() - self._held_since >= self._max_hold_secs:
            yield self._release()

    def _release(self) -> Aggregation:
        held, self._held = self._held, ""
        self._sent += 1
        return Aggregation(text=held, type=AggregationType.SENTENCE)

    async def flush(self) -> Aggregation | None:
        # Pehle jama jumle utha lo: parent ka flush reset() bulata hai, aur
        # reset() jama jumle bhi saaf kar deta hai.
        held, self._held = self._held, ""
        rest = await super().flush()
        self._sent = 0  # agla jawab phir pehle jumle se
        if self._aggregation_type == AggregationType.TOKEN:
            return rest
        tail = rest.text if rest else ""
        joiner = "" if not held or not tail or tail.startswith("\n") else " "
        text = f"{held}{joiner}{tail}"
        return Aggregation(text=text, type=AggregationType.SENTENCE) if text.strip() else None

    async def handle_interruption(self):
        await super().handle_interruption()
        self._held = ""
        self._sent = 0

    async def reset(self):
        await super().reset()
        self._held = ""
        self._sent = 0


# ── Raaste: key aur model ki hadd ke andar rehna ─────────────────────────────

RATE_WINDOW_SECS = 60.0
# Hadd lagne par Google "retry in Xs" na bataye to itna intezar.
DEFAULT_RETRY_SECS = 15.0
# Din ki hadd khatam: is raste par jaldi dobara koshish ka faida nahi.
DAILY_QUOTA_BLOCK_SECS = 30 * 60.0
# Seekhi hui hadd itni der baad bhula di jati hai (hadd barh gayi ho, ya us
# waqt koi aur app bhi isi key par chal rahi thi).
LIMIT_MEMORY_SECS = 5 * 60.0
# Sab raaste hadd par hon to ek line zyada se zyada itna intezar karti hai.
MAX_WAIT_SECS = 6.0
# ExpressiveSpeech: Standard tayyar hai, is liye hadd khulne ka lamba intezar nahi.
STANDARD_INSTEAD_AFTER_SECS = 0.5
# Hadd ke ye do paigham Standard par le jate hain; baqi (ghalat key…) waise hi ghalti.
LIMIT_CODES = {"gemini_daily_quota": "daily", "gemini_rate_limit": "rate"}


class SpeechUnavailable(Exception):
    """Koi raasta is line ko bol nahi saka. `code` app ko wajah batata hai."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _status_code(error: BaseException) -> int | None:
    for name in ("code", "status_code", "status"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def classify_google_error(error: BaseException) -> str:
    """Google ki ghalti ki qism: "rate" | "daily" | "auth" | "other"."""
    text = str(error).lower()
    code = _status_code(error)
    if code == 429 or "resource_exhausted" in text or "quota" in text:
        # Google ka apna naam: "generate_requests_per_model_per_day" / "...PerDayPerProjectPerModel".
        return "daily" if "per_day" in text or "perday" in text or "per day" in text else "rate"
    if code in (401, 403) or "api key not valid" in text or "api_key_invalid" in text or "permission_denied" in text:
        return "auth"
    return "other"


# "Please retry in 34.5s." — aur din ki hadd par "Please retry in 11h51m26.18s."
_RETRY_IN = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retrydelay\W{0,8}([\d.]+)s", re.IGNORECASE)
_QUOTA_LIMIT = re.compile(r"limit:\s*(\d+)", re.IGNORECASE)


def retry_secs(error: BaseException) -> float | None:
    """Google ka bataya hua intezar, second mein — agar us ne bataya ho."""
    text = str(error)
    match = _RETRY_IN.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds) + 0.5
    match = _RETRY_DELAY.search(text)
    return float(match.group(1)) + 0.5 if match else None


@dataclass
class Budget:
    """Ek (key, model) ki requests — engine ke chalne tak yaad rehti hain, taake
    nayi session pichhli ki seekhi hui hadd se shuru ho."""

    started: deque[float] = field(default_factory=deque)
    limit: int | None = None  # seekhi hui hadd fi minute; None = abhi tak lagi nahi
    limit_at: float = 0.0
    daily_limit: int | None = None  # Google ki batayi hui din ki hadd — sirf user ke paigham ke liye
    blocked_until: float = 0.0

    def wait_secs(self, now: float) -> float:
        """Kitni der baad is raste par agli request ja sakti hai. 0 = abhi."""
        while self.started and now - self.started[0] >= RATE_WINDOW_SECS:
            self.started.popleft()
        if self.limit is not None and now - self.limit_at >= LIMIT_MEMORY_SECS:
            self.limit = None
        wait = max(0.0, self.blocked_until - now)
        if self.limit is not None and len(self.started) >= self.limit:
            wait = max(wait, self.started[0] + RATE_WINDOW_SECS - now)
        return wait

    def hit_limit(self, error: BaseException, now: float, daily: bool) -> None:
        told = _QUOTA_LIMIT.search(str(error))
        if daily:
            self.daily_limit = int(told.group(1)) if told else None
            # Google batata hai din kab badlega ("retry in 11h51m"); na bataye to aadha ghanta.
            self.blocked_until = now + (retry_secs(error) or DAILY_QUOTA_BLOCK_SECS)
            return
        # Ye request hadd se bahar thi: us se pehle wali ginti hi hadd hai.
        self.limit = max(1, int(told.group(1)) if told else len(self.started) - 1)
        self.limit_at = now
        self.blocked_until = now + (retry_secs(error) or DEFAULT_RETRY_SECS)


_budgets: dict[tuple[str, str], Budget] = {}


def _budget(key: str, model: str) -> Budget:
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
    return _budgets.setdefault((fingerprint, model), Budget())


@dataclass
class Lane:
    name: str  # log ke liye: "key" | "backup" (ek se zyada model hon to "key/alt")
    key_name: str  # "key" | "backup"
    client: genai.Client
    model: str
    budget: Budget


class SpeechLanes:
    """Ek line ko awaaz banane ke raaste, pasand ki tarteeb mein: key, phir backup key.

    App `models` mein ek hi naam deti hai — wohi awaaz ka model jo user sunta
    aaya hai. Hadd par kisi halke model par girna nahi (file ke upar wajah).

    Pehla raasta jis ki hadd baqi ho, wohi chalta hai. Awaaz shuru hone se
    pehle nakaam ho (hadd lag gayi) to wohi line agle raste se — sunne wale ko
    farq nahi parta. Awaaz shuru ho chuki ho to dobara nahi (line do dafa bolti).
    """

    def __init__(
        self,
        *,
        keys: list[tuple[str, str]],
        models: list[str],
        make_client: Callable[[str], Any] | None = None,
    ):
        make_client = make_client or (lambda key: genai.Client(api_key=key))
        # Backup mein wohi key dobara daal di gayi ho to wo koi naya raasta nahi.
        key_of: dict[str, str] = {}
        for name, key in keys:
            if key and key not in key_of.values():
                key_of[name] = key
        self._clients = {name: make_client(key) for name, key in key_of.items()}
        self._dead: dict[str, str] = {}  # key_name -> wajah (ghalat key)
        self._lanes: list[Lane] = []
        for key_name, client in self._clients.items():
            for index, model in enumerate(dict.fromkeys(m for m in models if m)):
                name = key_name if index == 0 else f"{key_name}/alt"
                self._lanes.append(Lane(name, key_name, client, model, _budget(key_of[key_name], model)))
        if not self._lanes:
            raise SpeechUnavailable("gemini_key_missing", "Gemini key nahi hai")

    @property
    def primary(self) -> Lane:
        return self._lanes[0]

    def blocked_secs(self) -> float:
        """Sab se jaldi khulne wale raste ka intezar. 0 = abhi bol sakte hain."""
        now = time.monotonic()
        waits = [lane.budget.wait_secs(now) for lane in self._lanes if lane.key_name not in self._dead]
        return min(waits) if waits else 0.0

    async def check_key(self) -> None:
        """Pehli key ko ek saste sawal se jaancho (aur connection garam karo).

        Model ki maloomat maangna awaaz ke request mein nahi ginta, is liye hadd
        par asar nahi. Ghalat key yahin pakri jati hai — user ke bolne se pehle.
        """
        lane = self.primary
        try:
            await lane.client.aio.models.get(model=lane.model)
        except Exception as error:
            if classify_google_error(error) == "auth":
                self._dead[lane.key_name] = str(error)[:200]
                if all(other.key_name in self._dead for other in self._lanes):
                    raise SpeechUnavailable("gemini_key_invalid", str(error)[:200]) from error
            else:
                logger.debug(f"Gemini TTS: key ki jaanch na ho saki ({str(error)[:120]}) — pehli line par pata chalega")

    async def synthesize(
        self, build: Callable[[str], dict[str, Any]], max_wait: float = MAX_WAIT_SECS
    ) -> AsyncGenerator[bytes, None]:
        """Ek line ki awaaz, tukra ba tukra (bina header ka 16-bit PCM, 24 kHz).

        `build(model)` us model ka request banata hai. Sab raaste hadd par hon
        to `max_wait` second tak kisi ke khulne ka intezar, phir SpeechUnavailable.
        """
        skipped: set[str] = set()  # is line par jo raaste aazma liye
        reasons: list[str] = []
        deadline = time.monotonic() + max_wait
        while True:
            now = time.monotonic()
            open_lanes = [lane for lane in self._lanes if lane.name not in skipped and lane.key_name not in self._dead]
            if not open_lanes:
                raise self._give_up(reasons)
            waits = [(lane.budget.wait_secs(now), lane) for lane in open_lanes]
            wait, lane = min(waits, key=lambda item: item[0])
            if wait > 0:
                if now + wait > deadline:
                    raise self._give_up(reasons or ["rate"], wait)
                await asyncio.sleep(wait + 0.05)
                continue

            lane.budget.started.append(now)
            started = False
            try:
                async for chunk in await lane.client.aio.models.generate_content_stream(**build(lane.model)):
                    for audio in audio_from_chunk(chunk):
                        started = True
                        yield audio
            except Exception as error:
                if started:
                    raise
                kind = classify_google_error(error)
                reasons.append(kind)
                if kind in ("rate", "daily"):
                    lane.budget.hit_limit(error, time.monotonic(), daily=kind == "daily")
                    which = "din ki hadd" if kind == "daily" else "fi-minute hadd"
                    logger.warning(f"Gemini TTS: raasta '{lane.name}' ({lane.model}) {which} par")
                elif kind == "auth":
                    self._dead[lane.key_name] = str(error)[:200]
                    logger.warning(f"Gemini TTS: '{lane.key_name}' key rad ho gayi — {str(error)[:140]}")
                else:
                    skipped.add(lane.name)
                    logger.warning(f"Gemini TTS: raasta '{lane.name}' nakaam — {str(error)[:140]}")
                continue
            if started:
                return
            # Na awaaz, na ghalti: is line ke liye ye raasta bekaar.
            skipped.add(lane.name)
            reasons.append("silent")

    def _give_up(self, reasons: list[str], wait: float = 0.0) -> SpeechUnavailable:
        """Line kyun nahi boli gayi. Ye matn app ki screen par user ko dikhta hai
        (paigham ke neeche chhoti line), is liye saaf English mein aur asli numbers ke saath."""
        if self._dead and all(lane.key_name in self._dead for lane in self._lanes):
            return SpeechUnavailable("gemini_key_invalid", next(iter(self._dead.values())))
        model = self.primary.model
        # Din ki hadd wala raasta aadhe ghante ke liye band hota hai — lamba intezar usi ki nishani hai.
        if "daily" in reasons or wait > RATE_WINDOW_SECS * 2:
            limit = next((lane.budget.daily_limit for lane in self._lanes if lane.budget.daily_limit), None)
            told = f" ({limit} requests per day)" if limit else ""
            return SpeechUnavailable("gemini_daily_quota", f"{model} reached this key's daily limit{told}.")
        if "rate" in reasons:
            limit = next((lane.budget.limit for lane in self._lanes if lane.budget.limit), None)
            told = f" ({limit} requests per minute)" if limit else ""
            again = f" It opens again in {math.ceil(wait)} s." if wait else ""
            return SpeechUnavailable("gemini_rate_limit", f"{model} reached this key's per-minute limit{told}.{again}")
        return SpeechUnavailable("gemini_voice_failed", f"{model} returned no audio for this line.")

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.aio.aclose()
            except Exception:
                pass


# ── Speakers: awaaz kahan se banti hai ───────────────────────────────────────
#
# Teeno ki ek hi shakl:
#   check()            session ke shuru mein — ghalat key / band raasta yahin pakra jaye
#   new_reply()        naya jawab shuru hua
#   speak(text, style) ek tukre ki awaaz (bina header ka 16-bit PCM, 24 kHz).
#                      Awaaz shuru hone se pehle nakaami = SpeechUnavailable.
#   close()


class KeySpeech:
    """User ki apni key par Gemini 3.8 TTS (SpeechLanes: key, phir backup key)."""

    def __init__(self, *, lanes: SpeechLanes, voice: str):
        self._lanes = lanes
        self._voice = voice

    async def check(self) -> None:
        await self._lanes.check_key()

    async def new_reply(self) -> None:
        return None

    def blocked_secs(self) -> float:
        return self._lanes.blocked_secs()

    def speak(self, text: str, style: str, max_wait: float = MAX_WAIT_SECS) -> AsyncGenerator[bytes, None]:
        def build(model: str) -> dict[str, Any]:
            return tts_request(model=model, voice=self._voice, text=text, style=style)

        return self._lanes.synthesize(build, max_wait)

    async def close(self) -> None:
        await self._lanes.close()


# Live ka socket ~10 minute se zyada nahi chalta; us se pehle nayi session.
LIVE_SESSION_MAX_AGE_SECS = 7 * 60
LIVE_CONNECT_TIMEOUT_SECS = 15.0
# Jawab ki aakhri line ke itni der baad nayi session khul jati hai (neeche wajah).
LIVE_ROTATE_AFTER_SECS = 1.5

# Gemini Live ko di jane wali hidayat. Naapa hua (12 Oct 2026, gemini-3.8-live,
# Aoede): 16 lines apni apni ada ke saath — sab hoobahoo boli gayin (alag model
# se wapas sun kar milaya), hidayat ka koi lafz nahi bola gaya, aur ada ka asar
# awaaz mein aaya (wohi line "soft and slow" par 3.4 s, "urgent, quick" par 2.6 s).
LIVE_MOUTH_INSTRUCTION = (
    "You are the speaking voice of Stonic. Each message you receive is one line of a script, in two parts. "
    "DELIVERY says how the line is performed: the mood, pace and volume. LINE holds the words. "
    "Speak the LINE out loud exactly as written, word for word, in the language it is written in, "
    "performed the way DELIVERY describes. A word in angle brackets inside the LINE, such as <sigh> or "
    "<laugh>, is a sound you make at that spot. Hindi and Urdu lines are written in Roman letters; "
    "pronounce them the way a native Urdu and Hindi speaker does. Your whole reply is the spoken LINE."
)


def live_line(text: str, style: str) -> str:
    """Live ko jane wala message: ada aur line, do alag hisson mein."""
    return f"DELIVERY: {style}\nLINE: {text}"


class LiveSpeech:
    """Gemini Live ko sirf "munh" banana: line dimagh ki, awaaz Live ki.

    Live par requests ki hadd nahi (wo ek khuli session hai), is liye ye muft
    key par bhi poori baat-cheet chalata hai. Hadd sirf input tokens fi minute
    ki hai, aur wahan ek jaal hai (naapa hua, 12 Oct 2026): Live apni session
    ki har purani line — us ki boli hui awaaz samet — har nayi line par dobara
    input mein ginta hai (8 lines mein 586 -> 1,794 tokens fi line). Is liye
    har jawab apni taaza session mein bola jata hai: jawab ki aakhri line ke
    foran baad purani session band aur nayi peeche (background mein) khul jati
    hai, taake agla jawab us ka intezar na kare.
    """

    def __init__(self, *, api_key: str, model: str, voice: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
            ),
            system_instruction=LIVE_MOUTH_INSTRUCTION,
        )
        self._session: Any = None
        self._session_ctx: Any = None
        self._opened_at = 0.0
        self._used = False  # is session par koi line boli ja chuki?
        self._connecting: asyncio.Task | None = None
        self._rotating: asyncio.Task | None = None

    async def check(self) -> None:
        """Session abhi khol lo: ghalat key ya band raasta user ke bolne se pehle pakra jaye."""
        try:
            await self._ready()
        except Exception as error:
            raise self._unavailable(error) from error

    async def new_reply(self) -> None:
        # Pichhla jawab abhi abhi khatam hua ho aur nayi session na khuli ho: ab kholo.
        if self._used:
            self._reconnect()

    async def _open(self) -> None:
        await self._close_session()
        self._session_ctx = self._client.aio.live.connect(model=self._model, config=self._config)
        self._session = await asyncio.wait_for(self._session_ctx.__aenter__(), LIVE_CONNECT_TIMEOUT_SECS)
        self._opened_at = time.monotonic()
        self._used = False
        logger.debug(f"Gemini Live munh tayyar ({self._model})")

    async def _close_session(self) -> None:
        context, self._session_ctx, self._session = self._session_ctx, None, None
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except Exception as error:
                logger.debug(f"Purani Live session band karte hue: {error}")

    def _reconnect(self) -> None:
        """Nayi session peeche kholna (purani isi dauran band hoti hai)."""
        self._cancel_rotation()
        if self._connecting is not None and not self._connecting.done():
            return
        self._session = None
        self._connecting = asyncio.create_task(self._open())

    def _rotate_soon(self) -> None:
        self._cancel_rotation()
        self._rotating = asyncio.create_task(self._rotate_later())

    async def _rotate_later(self) -> None:
        await asyncio.sleep(LIVE_ROTATE_AFTER_SECS)
        self._rotating = None
        self._reconnect()

    def _cancel_rotation(self) -> None:
        task, self._rotating = self._rotating, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _ready(self) -> Any:
        expired = self._session is not None and time.monotonic() - self._opened_at > LIVE_SESSION_MAX_AGE_SECS
        if self._session is None or expired:
            if self._connecting is None or self._connecting.done():
                self._reconnect()
            await self._connecting
        return self._session

    @staticmethod
    def _unavailable(error: BaseException) -> SpeechUnavailable:
        if isinstance(error, SpeechUnavailable):
            return error
        detail = " ".join(str(error).split())[:200] or type(error).__name__
        kind = classify_google_error(error)
        if kind == "auth":
            return SpeechUnavailable("gemini_key_invalid", detail)
        if kind == "daily":
            return SpeechUnavailable("gemini_daily_quota", detail)
        if kind == "rate":
            return SpeechUnavailable("gemini_rate_limit", detail)
        return SpeechUnavailable("gemini_voice_failed", detail)

    async def speak(self, text: str, style: str) -> AsyncGenerator[bytes, None]:
        self._cancel_rotation()
        finished = False
        try:
            try:
                session = await self._ready()
                self._used = True
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=live_line(text, style))]),
                    turn_complete=True,
                )
            except Exception as error:
                raise self._unavailable(error) from error
            got_audio = False
            async for message in session.receive():
                if message.data:
                    got_audio = True
                    yield message.data
                if message.server_content and message.server_content.turn_complete:
                    finished = True
                    break
            if not got_audio:
                raise SpeechUnavailable("gemini_voice_failed", f"{self._model} returned no audio for this line.")
        finally:
            # Yahan sirf foran hone wale kaam: kaatne par ye generator cancel
            # hota hai aur cancel ke dauran intezar (await) bharose-mand nahi.
            if finished and time.monotonic() - self._opened_at <= LIVE_SESSION_MAX_AGE_SECS:
                # Jawab ka agla tukra isi session mein; koi na aaye to nayi session.
                self._rotate_soon()
            else:
                # Adhoori line ke baad session mein purani awaaz bachi hoti hai
                # jo agli line mein ghul jati — nayi session saaf hai.
                self._reconnect()

    async def close(self) -> None:
        self._cancel_rotation()
        task, self._connecting = self._connecting, None
        if task is not None and not task.done():
            task.cancel()
        await self._close_session()
        try:
            await self._client.aio.aclose()
        except Exception:
            pass


class ExpressiveSpeech:
    """Settings ka "Expressive": Gemini 3.8 TTS (KeySpeech), aur us ki hadd par Standard.

    TTS ki hadd key ke Gemini TIER ki hai (muft key par chand jawab roz) — key
    theek hai, is liye baat-cheet nahi rukti: jo line TTS se nahi bol sakti wo
    Standard (Gemini Live, wohi key, wohi awaaz ka naam) se. Pehli dafa app ko
    ek paigham (`notify`) — chat mein dheemi line. Hadd khulte hi (agla minute /
    agla din) agli line phir TTS se: har line pehle TTS ko aazmati hai, aur band
    raasta Google ko chhuye baghair foran pata chal jata hai (Budget).

    Ghalat key, khamosh jawab waghera hadd nahi — wo pehle jaisi saaf ghalti.
    """

    def __init__(
        self,
        *,
        key: KeySpeech,
        make_standard: Callable[[], Any],
        notify: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ):
        self._key = key
        self._make_standard = make_standard
        self._standard: Any = None
        self._notify = notify
        # User ko kya bataya ja chuka: "" | "rate" | "daily" (din wali bari baat, ek dafa).
        self._told = ""

    def _standard_speaker(self) -> Any:
        if self._standard is None:
            self._standard = self._make_standard()
        return self._standard

    async def check(self) -> None:
        await self._key.check()
        # Pichhli session mein din ki hadd lag chuki (engine yaad rakhta hai): Standard abhi garam karo.
        if self._key.blocked_secs() > RATE_WINDOW_SECS * 2:
            await self._standard_speaker().check()

    async def new_reply(self) -> None:
        await self._key.new_reply()
        if self._standard is not None:
            await self._standard.new_reply()

    async def _tell(self, why: str) -> None:
        # Din ki hadd fi-minute se bari khabar hai: wo pehle bata di ho to dobara kuch nahi.
        if self._told == why or self._told == "daily":
            return
        self._told = why
        logger.info(f"Expressive awaaz {'din' if why == 'daily' else 'fi-minute'} ki hadd par — Standard se bol raha hai")
        if self._notify is not None:
            try:
                await self._notify({"type": "stonic-voice", "voice": "standard", "why": why})
            except Exception as error:  # noqa: BLE001 — paigham na jaye to bhi awaaz na ruke
                logger.debug(f"Standard awaaz ka paigham app ko nahi gaya: {error}")

    async def speak(self, text: str, style: str) -> AsyncGenerator[bytes, None]:
        started = False
        try:
            async for audio in self._key.speak(text, style, max_wait=STANDARD_INSTEAD_AFTER_SECS):
                started = True
                yield audio
            return
        except SpeechUnavailable as error:
            if started or error.code not in LIMIT_CODES:
                raise
            why = LIMIT_CODES[error.code]
        await self._tell(why)
        async for audio in self._standard_speaker().speak(text, style):
            yield audio

    async def close(self) -> None:
        await self._key.close()
        if self._standard is not None:
            await self._standard.close()


# ── Service ──────────────────────────────────────────────────────────────────


class GeminiDirectedTTSService(GeminiTTSService):
    """Dimagh ki likhi ada ki hidayat ke saath bolna — awaaz `speaker` banata hai."""

    def __init__(
        self,
        *,
        speaker: Any,
        style: str = "",
        chunk_min_chars: int = 220,
        chunk_next_min_chars: int = 400,
        chunk_max_hold_secs: float = 1.0,
        **kwargs,
    ):
        self._speaker = speaker
        # use_genai: parent ka `run_tts` isi par `_run_genai_tts` bulata hai (neeche).
        super().__init__(use_genai=True, **kwargs)
        self._base_style = style.strip()
        self._style = self._base_style
        self._check_task: asyncio.Task | None = None
        self._text_aggregator = ReplyChunkAggregator(
            min_chars=chunk_min_chars,
            next_min_chars=chunk_next_min_chars,
            max_hold_secs=chunk_max_hold_secs,
            aggregation_type=self._text_aggregation_mode,
        )

    def _create_client(self, credentials: str | None, credentials_path: str | None) -> Any:
        # Parent apna Google client yahan banata hai. Us ki zaroorat nahi: awaaz
        # speaker banata hai, aur Stonic Cloud par Google ki koi key hoti hi nahi.
        return None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._check_task = asyncio.create_task(self._check())

    async def _check(self) -> None:
        try:
            await self._speaker.check()
        except SpeechUnavailable as error:
            await self.push_error(error_msg=f"stonic:{error.code}: {error}")

    async def _close_client(self):
        task, self._check_task = self._check_task, None
        if task is not None and not task.done():
            task.cancel()
        await self._speaker.close()

    async def on_turn_context_created(self, context_id: str):
        await super().on_turn_context_created(context_id)
        # Har naya jawab aam awaaz se shuru hota hai.
        self._style = self._base_style
        await self._speaker.new_reply()

    async def _push_tts_frames(self, src_frame: AggregatedTextFrame, *args, **kwargs):
        # ... aur har naya paragraph bhi. Parent aage ja kar shuru ki khali
        # lines kaat deta hai, is liye tukre ke shuru ka paragraph yahin pata
        # chalta hai (beech wale split_reply dekhta hai).
        if starts_paragraph(src_frame.text):
            self._style = self._base_style
        await super()._push_tts_frames(src_frame, *args, **kwargs)

    async def _run_genai_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating voice [{text}]")
        segments, self._style = split_reply(text, self._style, self._base_style)
        # Pipeline shuru hone se pehle size 0 hota hai; 0 par neeche ka loop kabhi khatam na hota.
        chunk_size = self.chunk_size or self.sample_rate or 24000
        first_chunk = True
        try:
            for style, spoken in segments:
                await self.start_tts_usage_metrics(spoken)

                buffer = b""
                async for audio in self._speaker.speak(spoken, style):
                    if first_chunk:
                        await self.stop_ttfb_metrics()
                        first_chunk = False
                    buffer += audio
                    while len(buffer) >= chunk_size:
                        piece, buffer = buffer[:chunk_size], buffer[chunk_size:]
                        yield TTSAudioRawFrame(piece, self.sample_rate, 1, context_id=context_id)

                # 16-bit awaaz mein adha sample nahi bheja jata.
                buffer = buffer[: len(buffer) - (len(buffer) % 2)]
                if buffer:
                    yield TTSAudioRawFrame(buffer, self.sample_rate, 1, context_id=context_id)
        except SpeechUnavailable as error:
            # Hadd, ghalat key ya plan: line boli nahi jati, app user ko yehi wajah dikhati hai.
            yield ErrorFrame(error=f"stonic:{error.code}: {error}")
        except Exception as error:
            yield ErrorFrame(error=f"Voice error: {error}")
