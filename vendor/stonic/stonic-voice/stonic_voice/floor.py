"""Baat-cheet ka ehsaas — "ye baat mujh se hai ya kisi aur se?"

Awareness (asal app) Stonic ka pehla sutoon hai: duniya ka ehsaas — waqt, screen,
thakan — aur us se KHUD bolna. Ye doosra hai: baat-cheet ka ehsaas. Mic khula
rehta hai, is liye Stonic sab sunta hai — user ki apni baat, kamre mein dost se
baat, phone call, khud se baat, YouTube ki awaaz. Insaan ki tarah wo sirf us
baat mein hissa leta hai jo us se ho, aur jab us ki baat ka jawab na aaye ya wo
beech mein kat jaye to khud tay karta hai: dobara pukare ya chhor de.

Faisla dimagh (Claude) ka hai — qaide nahi. Yahan sirf teen kaam hain:

  1. Har sune hue jumle ke saath ek chhoti "moment" line (`heard`): abhi kya
     waqt hai, Stonic aakhri dafa kab bola aur kya kuch poocha tha, kya user
     us ke bolte hue bol para, is se pehle ki baatein kisi aur se thin, screen
     par kya hai, kaun si doosri app mic istemal kar rahi hai (call, dictation).
  2. Dimagh ke jawab ka shuru parhna (`open_reply`): har jawab pehle apna
     faisla likhta hai — `<for-me>` (baat meri hai, aage bolo) ya `<quiet:aside>`
     (meri nahi, kuch bola nahi jata). Tag kabhi awaaz tak nahi jata (agent.py ka darwaza).
  3. Khamoshi par khud ka moqa (`due`): Stonic ne kuch poocha aur jawab nahi
     aaya, ya us ki baat aisi baat ne kaati jo us se thi hi nahi — to dimagh se
     ek dafa poochha jata hai "ab kya karoge?". Wo halka sa pukarta hai ya chup
     rehta hai. Haddein `settings.toml` ke `[floor]` mein.

Kharcha: har jumle par ~50 tokens ka note; khamoshi ke moqe zyada se zyada
teen fi line (do pukaar + ek kati baat), har ek ek chhota dimagh ka turn.

Signals sab isi PC se, muft: saamne wali window (`GetForegroundWindow`), mic
istemal karne wali apps (Windows ka CapabilityAccessManager), aur "koi app screen
ko jagaye hue hai" = video chal rahi hai (`CallNtPowerInformation`) — teeno wohi
jo awareness-focus-watch.js parhta hai. Koi camera nahi, koi network nahi.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# ── Dimagh ka faisla, har jawab ke shuru mein ────────────────────────────────
#
# Suni hui baat ke jawab ka pehla hissa dimagh ka faisla hai — awaaz tak kabhi
# nahi jata:
#
#     <for-me> Ji Sir, …     baat Stonic se thi: `<for-me>` ke baad wala matn bola jata hai
#     <quiet:aside>          baat us se nahi thi: kuch nahi bola jata (self / media / wait / rest)
#
# Faisla har dafa LIKHWANA kyun (naapa, 14 Oct 2026, Haiku 5.5, soch band): jab
# sirf itna kaha tha ke "baat tumhari na ho to <quiet>", model aksar seedha jawab
# dene lagta tha — do daur mein se ek mein "Ali, wo folder delete kar do" ko
# apna hukm samjha aur TV ki khabar par web search chala di. Pehle faisla, phir
# baat: model ko har jumle par ruk kar sochna parta hai ke ye kis se hai.
# Tag na ho to jawab baat hai (purana rawaiya) — koi lafz zaya nahi.

QUIET_TAG = "<quiet"
SPEAK_TAG = "<for-me>"
_QUIET = re.compile(r"^<\s*quiet\b([^>]*)>", re.IGNORECASE)
_SPEAK = re.compile(r"^<\s*for[\s_-]*me\s*/?\s*>", re.IGNORECASE)
WHYS = ("aside", "self", "media", "wait", "rest")
# `<` se shuru matn itne huroof tak roka jata hai jab tak tag band (`>`) na ho.
TAG_LIMIT = 32


def _why(inside: str) -> str:
    return next((name for name in WHYS if name in inside.lower()), "other")


def open_reply(start: str) -> tuple[str, str, str]:
    """Jawab ka shuru → (faisla, wajah, bolne wala matn).

        "wait"   abhi pata nahi — shuru `<` hai aur tag abhi band nahi hua
        "quiet"  chup; wajah aside / self / media / wait / rest (na mile to "other")
        "speak"  bolo; matn `<for-me>` hata kar — ya jaisa tha (koi tag nahi, ya "<b>" jaisa koi aur)
    """
    text = start.lstrip()
    if not text:
        return "wait", "", ""
    if not text.startswith("<"):
        return "speak", "", start
    close = text.find(">")
    if close < 0:
        if len(text) < TAG_LIMIT:
            return "wait", "", ""
        if text.lower().startswith(QUIET_TAG):
            return "quiet", _why(text), ""
        return "speak", "", start
    tag = text[: close + 1]
    quiet = _QUIET.match(tag)
    if quiet:
        return "quiet", _why(quiet.group(1)), ""
    if _SPEAK.match(tag):
        return "speak", "", text[close + 1:].lstrip()
    return "speak", "", start


# Jawab ke beech / aakhir mein `<for-me>` ya `</for-me>`: model kabhi tag ko HTML ki
# tarah band kar deta hai ("… Bolo, kya chahiye?</for-me>"). Shuru wala tag
# `open_reply` hatata hai; ye baqi sab — taake na chat mein dikhe na awaaz mein bole.
_STRAY_SPEAK = re.compile(r"<\s*/?\s*for[\s_-]*me\s*/?\s*>", re.IGNORECASE)
_STRAY_TARGET = "<forme"


class SpeakTagScrubber:
    """Bolne wale matn se bhatka hua `<for-me>` / `</for-me>` nikalta hai.

    Matn tukron mein aata hai aur tag do tukron mein toot sakta hai ("</for" +
    "-me>"): aakhir ka adhoora tag agle tukre tak roka jata hai. `flush()`
    paigham ke aakhir mein — bacha hua adhoora tag phenk diya jata hai.
    """

    def __init__(self) -> None:
        self._held = ""

    def feed(self, text: str) -> str:
        joined = _STRAY_SPEAK.sub("", self._held + text)
        self._held = ""
        cut = joined.rfind("<")
        if cut >= 0 and ">" not in joined[cut:] and len(joined) - cut < TAG_LIMIT:
            tail = re.sub(r"[\s_\-/]", "", joined[cut:].lower())
            if _STRAY_TARGET.startswith(tail):
                joined, self._held = joined[:cut], joined[cut:]
        return joined

    def flush(self) -> None:
        self._held = ""


def settle_reply(start: str, *, at_end: bool) -> tuple[str, str, str]:
    """Paigham ka text khatam (tool shuru hua ya paigham band) aur faisla abhi baqi tha."""
    verdict, why, text = open_reply(start)
    if verdict != "wait":
        return verdict, why, text
    lower = start.strip().lower()
    if not lower:
        # Na koi lafz: paigham band = chup; tool shuru = kaam (baat Stonic se thi).
        return ("quiet", "other", "") if at_end else ("speak", "", "")
    if lower.startswith("<q"):
        return "quiet", _why(lower), ""  # `<quiet` beech mein kata
    return "speak", "", ""  # adhoora tag (`<for-m`) kabhi bola nahi jata


# ── Haddein (settings.toml [floor]) ──────────────────────────────────────────


@dataclass
class FloorLimits:
    enabled: bool = True
    # Stonic ne kuch poocha, user chup: itni der baad dimagh se poochho "pukaroge?".
    nudge_after_secs: float = 8.0
    # Pehli pukaar (ya us ka chup rehne ka faisla) ke baad doosra aur aakhri moqa.
    nudge_again_secs: float = 20.0
    max_nudges: int = 2
    # Stonic ki baat aisi baat ne kaati jo us se nahi thi: wo khatam ho kar itni
    # khamoshi ho to dimagh se poochho "baat dobara uthaoge?".
    resume_after_secs: float = 5.0
    # Aakhri "kisi aur se" wali baat ke itni der baad tak khud ka koi moqa nahi —
    # user shayad abhi bhi kisi se baat kar raha hai (bas saans le raha hai).
    aside_hold_secs: float = 15.0
    # "Is se pehle ki baatein kisi aur se thin" note mein itni purani tak.
    aside_memory_secs: float = 180.0


# ── Is PC ke signals ─────────────────────────────────────────────────────────


@dataclass
class Signals:
    """Is lamhe PC par kya hai. Khali = pata nahi (ya Windows nahi)."""

    app: str = ""        # saamne wali window ki app ("chrome")
    title: str = ""      # us window ka unwan, chhota kiya hua
    own_window: bool = False  # saamne Stonic ki apni window hai
    mic_apps: list[str] = field(default_factory=list)  # doosri apps jo is waqt mic istemal kar rahi hain
    # Koi app screen ko jagaye hue hai (video, presentation) — TV / YouTube ki awaaz
    # ka sab se pakka ishara: lafzon se khabar aur user ki baat mein farq mushkil
    # hai (naapa: 6 mein se 1 daur mein Haiku ne khabar ko user ki baat samjha).
    video: bool = False


TITLE_LIMIT = 70


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _ago(secs: float) -> str:
    secs = max(0.0, secs)
    if secs < 60:
        return f"{int(secs)} s"
    if secs < 3600:
        return f"{int(secs // 60)} min"
    return f"{int(secs // 3600)} h"


def asks(line: str) -> bool:
    """Stonic ki line ke aakhri do jumlon mein sawal hai? (Jawab ki umeed — pukaarne ka moqa.)

    Do, kyunke sawal ke baad aksar ek hidayat aati hai: "Kis waqt lagaun? Bas time
    bata dijiye." (naapa: aakhri ek jumla dekhne par ye sawal gina nahi jata tha).
    """
    text = re.sub(r"\[[^\]]*\]", " ", str(line or "")).strip()
    if not text:
        return False
    tail = " ".join(re.split(r"(?<=[.!?।؟])\s+", text)[-2:])
    return "?" in tail or "؟" in tail


if os.name == "nt":
    import ctypes
    import winreg
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _QUERY_LIMITED = 0x1000

    def _image_path(pid: int) -> str:
        handle = _kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            _kernel32.CloseHandle(handle)

    def _foreground(parent_pid: int) -> tuple[str, str, bool]:
        window = _user32.GetForegroundWindow()
        if not window:
            return "", "", False
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if parent_pid and pid.value == parent_pid:
            return "", "", True
        length = _user32.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        _user32.GetWindowTextW(window, buffer, length + 1)
        app = os.path.splitext(os.path.basename(_image_path(pid.value)))[0]
        return app, buffer.value, False

    _powrprof = ctypes.WinDLL("powrprof")
    _powrprof.CallNtPowerInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    _powrprof.CallNtPowerInformation.restype = wintypes.LONG
    _SYSTEM_EXECUTION_STATE = 16
    _DISPLAY_REQUIRED = 0x2

    def _screen_kept_awake() -> bool:
        """Koi app screen ko jagaye hue hai (video, call, presentation) — awareness ka `display`."""
        state = wintypes.ULONG(0)
        if _powrprof.CallNtPowerInformation(_SYSTEM_EXECUTION_STATE, None, 0, ctypes.byref(state), ctypes.sizeof(state)) != 0:
            return False
        return bool(state.value & _DISPLAY_REQUIRED)

    _MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

    def _busy(root: winreg.HKEYType, non_packaged: bool, skip: str, found: list[str]) -> None:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                return
            index += 1
            if name == "NonPackaged" or (non_packaged and name.lower() == skip):
                continue
            try:
                with winreg.OpenKey(root, name) as key:
                    stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
                    start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
            except OSError:
                continue
            # Windows har app ke liye likhta hai; Stop = 0 (aur Start mojood) = abhi istemal ho raha hai.
            if stop == 0 and start:
                if non_packaged:
                    found.append(os.path.splitext(name.rsplit("#", 1)[-1])[0])
                else:
                    found.append(name.split("_", 1)[0].rsplit(".", 1)[-1])

    def _mic_apps(parent_pid: int) -> list[str]:
        own = _image_path(parent_pid).replace("\\", "#").lower() if parent_pid else ""
        found: list[str] = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_KEY) as root:
                _busy(root, False, own, found)
                try:
                    with winreg.OpenKey(root, "NonPackaged") as non_packaged:
                        _busy(non_packaged, True, own, found)
                except OSError:
                    pass
        except OSError:
            return []
        seen: list[str] = []
        for name in found:
            if name and name.lower() not in (item.lower() for item in seen):
                seen.append(name)
        return seen[:4]

    def read_signals(parent_pid: int) -> Signals:
        """Saamne wali window + mic wali apps. Kuch bhi gire to khali — baat-cheet kabhi is par nahi rukti."""
        try:
            app, title, own = _foreground(parent_pid)
        except Exception:  # noqa: BLE001
            app, title, own = "", "", False
        try:
            mic = _mic_apps(parent_pid)
        except Exception:  # noqa: BLE001
            mic = []
        try:
            video = _screen_kept_awake()
        except Exception:  # noqa: BLE001
            video = False
        return Signals(app=app, title=_clip(title, TITLE_LIMIT), own_window=own, mic_apps=mic, video=video)

else:

    def read_signals(parent_pid: int) -> Signals:  # noqa: ARG001 — macOS / Linux: abhi koi signal nahi
        return Signals()


# ── Moqe ki hidayat (dimagh ko user ki jagah jati hain, qaus mein) ───────────

# Pehle moqe par jhukaav pukaar ki taraf (naapa, 14 Oct 2026: dono raaste barabar likhe
# the to Haiku 3 mein se 2 dafa seedha <quiet:rest> chunta tha — jab ke insaan ek
# dafa "Sir?" zaroor kehta hai). Chup sirf masroofi ki nishani par.
NUDGE_FIRST = (
    '(moment: it has been quiet for {silence} since you asked them something — "{line}" — and they have not answered.{between} '
    "A person sitting with them would call them once, gently — a few words, the way you would say their name to get their attention. "
    "If something shows they are busy right now (on a call, talking with someone), let it rest with <quiet:rest> instead.)"
)
NUDGE_AGAIN = (
    "(moment: still no answer — {silence} of quiet since your last line.{between} They are probably busy or away from the desk. "
    'A few easy words that leave it with them ("no rush, tell me when you are free") or simply <quiet:rest>. After this you wait for them.)'
)
RESUME = (
    "(moment: your last line was cut off by talk that was not meant for you, and now it has been quiet for {silence}. "
    'They did not hear the end of: "{line}". If it still matters, pick it back up in a sentence; otherwise let it rest with <quiet:rest>.)'
)
BETWEEN = " In between they were talking with someone else."
NOW = " Right now — {parts}."

LINE_LIMIT = 160


Clock = Callable[[], float]


def signal_parts(signals: Signals | None) -> list[str]:
    """Is PC ka haal, dimagh ke liye chhote tukron mein."""
    if signals is None:
        return []
    parts: list[str] = []
    if signals.own_window:
        parts.append("on screen: your own window")
    elif signals.title or signals.app:
        shown = f'"{signals.title}"' if signals.title else "a window"
        parts.append(f"on screen: {shown}" + (f" in {signals.app}" if signals.app else ""))
    if signals.mic_apps:
        parts.append("also using the microphone: " + ", ".join(signals.mic_apps))
    if signals.video:
        parts.append("a video or call is keeping the screen awake, so its sound may be what you heard")
    return parts


class Floor:
    """Ek baat-cheet ka "floor" — kaun bol raha hai, kis se, aur Stonic ka moqa kab hai.

    Pipeline isay batati hai (Stonic bola / chup hua, user bola / chup hua,
    dimagh ka har faisla); ye har sune hue jumle ka note banata hai aur
    khamoshi par bataata hai ke dimagh se poochhne ka moqa hai ya nahi.
    """

    def __init__(self, limits: FloorLimits, *, clock: Clock = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.limits = limits
        self._clock = clock
        self._wall = wall
        # Stonic.
        self._bot_speaking = False
        self._line = ""                 # chalte / aakhri jawab ka matn
        self._spoke_at: float | None = None
        self._asked = False             # aakhri poori suni gayi line ne kuch poocha
        self._nudges = 0
        self._cue_line = False          # aakhri line ek moqe ka jawab thi (pukaar), user ke jumle ka nahi
        self._initiative = False        # Stonic ki apni pehal (Awareness): us par Floor ki pukaar nahi
        # User.
        self._user_speaking = False
        self._user_at: float | None = None
        self._cut_pending = False       # user Stonic ke bolte hue bola (faisla baqi: kis se?)
        self._resume = False            # kati hui line, aur kaatne wali baat Stonic se nahi thi
        self._resume_line = ""
        # Faisle.
        self._pending: str | None = None  # "user" | "cue": jawab ka intezar
        self._aside_at: float | None = None
        self._asides = 0                # lagatar "kisi aur se" wali baatein
        self._waiting = False           # user ne "ek minute" kaha

    # ── Pipeline se ─────────────────────────────────────────────────────────

    def reply_started(self) -> None:
        self._line = ""

    def stonic_text(self, text: str) -> None:
        self._line += text

    def bot_speaking(self, speaking: bool) -> None:
        now = self._clock()
        if speaking:
            if not self._bot_speaking and not self._pending:
                # Nayi line shuru: pichhli "kati" ka koi faisla baqi nahi (shor tha, jumla bana hi nahi).
                self._cut_pending = False
            self._bot_speaking = True
            return
        if not self._bot_speaking:
            return
        self._bot_speaking = False
        self._spoke_at = now
        if self._cut_pending:
            # Line beech mein kati — user ne poori suni nahi: na sawal gina jata hai, na pukaar.
            self._asked = False
            self._resume_line = self._line
            return
        # Awareness ki baat ("paani piya?") ka anjaam Awareness khud dekhti hai
        # (jawab / nazar-andaz / kaat diya) — us par yahan se doosri pukaar nahi.
        self._asked = asks(self._line) and not self._initiative
        if not self._cue_line:
            self._nudges = 0

    def user_speaking(self, speaking: bool) -> None:
        now = self._clock()
        if speaking and self._bot_speaking:
            self._cut_pending = True
        self._user_speaking = speaking
        self._user_at = now

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def heard(self, signals: Signals | None = None) -> str:
        """User ka mukammal jumla aaya: dimagh ke liye moment ka note. Ab faisle ka intezar."""
        now = self._clock()
        self._pending = "user"
        self._cue_line = False
        self._initiative = False
        parts = [time.strftime("%I:%M %p", time.localtime(self._wall())).lstrip("0")]
        if self._cut_pending:
            parts.append("they started while you were still speaking")
        elif self._spoke_at is None:
            parts.append("you have not spoken yet")
        else:
            parts.append(f"you last spoke {_ago(now - self._spoke_at)} ago" + (" and asked them something" if self._asked else ""))
        if self._asides and self._aside_at is not None and now - self._aside_at <= self.limits.aside_memory_secs:
            count = "1 line" if self._asides == 1 else f"{self._asides} lines"
            parts.append(
                f"just before this, {count} not meant for you (last {_ago(now - self._aside_at)} ago)"
                " — they may still be talking with that person"
            )
        if self._waiting:
            parts.append("they had asked you to wait")
        parts.extend(signal_parts(signals))
        # Har jumle par yaad-dihani: pehle faisla (upar "Dimagh ka faisla" — kyun).
        parts.append("begin with <for-me> or <quiet:…>")
        return "[moment: " + " · ".join(parts) + "]"

    def initiative(self) -> None:
        """Agli line Stonic ki apni pehal hai (Awareness ka ishara), kisi sawal ka jawab nahi."""
        self._initiative = True

    @staticmethod
    def with_signals(cue: str, signals: Signals | None) -> str:
        """Moqe ki hidayat mein is lamhe ka haal (call par hai? kya chal raha hai?) — faisle ke liye."""
        parts = signal_parts(signals)
        if not parts or not cue.endswith(")"):
            return cue
        return cue[:-1] + NOW.format(parts="; ".join(parts)) + ")"

    def decided(self, kind: str, why: str = "") -> None:
        """Dimagh ka faisla — `kind` "speak" ya "quiet". Sirf intezar wale jumle / moqe par lagta hai."""
        source, self._pending = self._pending, None
        if source is None:
            return
        now = self._clock()
        if source == "cue":
            if kind == "speak":
                self._cue_line = True
            return
        cut, self._cut_pending = self._cut_pending, False
        if kind == "speak":
            self._asides = 0
            self._waiting = False
            self._resume = False
            return
        if why == "wait":
            self._waiting = True
            self._asked = False
            self._resume = False
            return
        self._aside_at = now
        self._asides += 1
        if cut and self._resume_line.strip():
            self._resume = True

    def due(self, *, busy: bool) -> str | None:
        """Khamoshi mein Stonic ka moqa? Hidayat (dimagh ko jati hai) ya None.

        `busy` = dimagh kaam kar raha hai / koi jawab chal raha hai. Moqa
        lene ke baad us ka faisla `decided()` se aata hai.
        """
        if busy or self._pending or self._waiting or self._bot_speaking or self._user_speaking:
            return None
        now = self._clock()
        marks = [at for at in (self._spoke_at, self._user_at) if at is not None]
        if not marks:
            return None
        silence = now - max(marks)
        if self._aside_at is not None and now - self._aside_at < self.limits.aside_hold_secs:
            return None
        between = BETWEEN if self._asides else ""
        if self._resume and silence >= self.limits.resume_after_secs:
            self._resume = False
            self._pending = "cue"
            return RESUME.format(silence=_ago(silence), line=_clip(self._resume_line, LINE_LIMIT))
        if self._asked and self._nudges < self.limits.max_nudges:
            wait = self.limits.nudge_after_secs if self._nudges == 0 else self.limits.nudge_again_secs
            if silence >= wait:
                self._nudges += 1
                self._pending = "cue"
                template = NUDGE_FIRST if self._nudges == 1 else NUDGE_AGAIN
                return template.format(silence=_ago(silence), line=_clip(self._line, LINE_LIMIT), between=between)
        return None
