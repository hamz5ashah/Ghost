"""Screen ke haath: dekhna, mouse aur keyboard.

Beta ka dimagh (agent.py) screen par kaam kare to ye us ke haath hain. Is file
mein koi model nahi aur koi faisla nahi — sirf "tasveer lo", "yahan click
karo", "ye likho". Kahan click karna hai ye dimagh batata hai (ya UI-TARS,
aim.py); yahan sirf wo kaam hota hai. Jor screen.py mein hai.

Teen baatein jo is file ko seedha rakhti hain:

  1. Jagah hamesha screen ka HISSA hai (0..1), pixel nahi. Tasveer kis naap ki
     bheji gayi, ye upar wale ka masla hai; yahan (0.5, 0.5) hamesha beech hai.
  2. Mouse Windows ke "absolute" raaste se chalta hai (0..65535 = poori primary
     screen). Us mein display scaling (125%, 150%) ka koi dakhal nahi — wo
     ghalti jo pixel wale raaste par har click ko khiska deti hai, yahan ho hi
     nahi sakti.
  3. Tasveer Pillow ka ImageGrab leta hai, jo khud asli pixels deta hai.

Likhna Unicode ke raaste se hai (har harf seedha), clipboard ke zariye nahi:
Urdu, Hindi aur emoji waise hi likhe jate hain aur user ka clipboard nahi badalta.

Sirf Windows, sirf primary screen (asal computer agent ki tarah). Sab kaam ek
hi thread par, ek ke baad ek: mouse ek hai.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import functools
import io
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

IS_WINDOWS = os.name == "nt"


class HandsError(Exception):
    """Haath ye kaam na kar sake. Matn angrezi mein: dimagh ko jata hai."""


class HandsStopped(HandsError):
    """Kaam beech mein roka gaya (Stop, "ruk jao", Esc)."""


class UserTookMouse(HandsError):
    """User ne khud mouse hilaya: machine wapas us ki."""


# ── Naap (sirf hisaab — koi Windows call nahi, is liye akele test hota hai) ───


def fit_size(width: int, height: int, max_dim: int, multiple: int = 1) -> tuple[int, int]:
    """Tasveer ka naap: lamba rukh `max_dim` tak, bari kabhi nahi hoti.

    `multiple` > 1 ho to dono rukh us ke qareeb tareen mazrab par aate hain.
    UI-TARS tasveer ko 28 ke mazrab par khud le aata hai aur jawab usi naap mein
    deta hai; hum pehle hi wahan hon to us ka jawab seedha hamari tasveer ka hai.
    """
    scale = min(1.0, max_dim / max(width, height, 1))
    out_w, out_h = max(1, round(width * scale)), max(1, round(height * scale))
    if multiple > 1:
        out_w = max(multiple, round(out_w / multiple) * multiple)
        out_h = max(multiple, round(out_h / multiple) * multiple)
    return out_w, out_h


def to_fraction(x: float, y: float, width: int, height: int) -> tuple[float, float]:
    """Tasveer ka pixel -> screen ka hissa (pixel ka beech)."""
    return (
        min(max((float(x) + 0.5) / max(width, 1), 0.0), 1.0),
        min(max((float(y) + 0.5) / max(height, 1), 0.0), 1.0),
    )


@dataclass(frozen=True)
class Shot:
    """Ek tasveer, model ko bhejne ke liye tayyar."""

    data: bytes
    width: int
    height: int
    mime: str = "image/jpeg"

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


# ── Keyboard ke naam ─────────────────────────────────────────────────────────
#
# Dimagh keys ke wohi naam likhta hai jo Anthropic ke computer tool mein hain
# ("Return", "ctrl+s", "Page_Down", "super"); aam naam ("enter", "win") bhi.

_VK: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "control_l": 0xA2, "control_r": 0xA3, "ctrl_l": 0xA2, "ctrl_r": 0xA3,
    "shift": 0x10, "shift_l": 0xA0, "shift_r": 0xA1,
    "alt": 0x12, "alt_l": 0xA4, "alt_r": 0xA5, "option": 0x12,
    "super": 0x5B, "super_l": 0x5B, "super_r": 0x5C, "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "cmd": 0x5B, "command": 0x5B,
    "return": 0x0D, "enter": 0x0D, "kp_enter": 0x0D,
    "escape": 0x1B, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23,
    "page_up": 0x21, "pageup": 0x21, "pgup": 0x21, "prior": 0x21,
    "page_down": 0x22, "pagedown": 0x22, "pgdn": 0x22, "next": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "caps_lock": 0x14, "capslock": 0x14, "num_lock": 0x90, "numlock": 0x90, "scroll_lock": 0x91,
    "print": 0x2C, "printscreen": 0x2C, "print_screen": 0x2C, "prtsc": 0x2C, "pause": 0x13,
    "menu": 0x5D, "apps": 0x5D,
    "kp_add": 0x6B, "kp_subtract": 0x6D, "kp_multiply": 0x6A, "kp_divide": 0x6F, "kp_decimal": 0x6E,
    "volumeup": 0xAF, "volume_up": 0xAF, "volumedown": 0xAE, "volume_down": 0xAE, "volumemute": 0xAD, "mute": 0xAD,
    "playpause": 0xB3, "play_pause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1,
    # Alamaton ke naam (US keyboard ki jagah — shortcuts isi par likhe jate hain).
    "plus": 0xBB, "equal": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
    "semicolon": 0xBA, "apostrophe": 0xDE, "grave": 0xC0, "backslash": 0xDC, "bracketleft": 0xDB, "bracketright": 0xDD,
    "+": 0xBB, "=": 0xBB, "-": 0xBD, ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE, "`": 0xC0, "\\": 0xDC, "[": 0xDB, "]": 0xDD,
}
_VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})
_VK.update({f"kp_{n}": 0x60 + n for n in range(10)})
_VK.update({chr(code).lower(): code for code in range(0x41, 0x5B)})  # a-z
_VK.update({chr(code): code for code in range(0x30, 0x3A)})  # 0-9

MODIFIERS = frozenset({0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})
# Wo keys jin ke saath "extended" ka nishan lazmi hai — warna Shift ke saath
# teer (arrow) number pad ke ginti ban jate hain.
_EXTENDED = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0xA3, 0xA5, 0x6F, 0x90, 0x2C,
                       0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB3})

MAX_CHORD_KEYS = 6
MAX_CHORDS = 40


def parse_keys(text: str) -> list[list[int]]:
    """"ctrl+s" / "Return" / "Down Down Return" -> har dabao ki keys (virtual key codes).

    `+` keys ko ek saath dabata hai; khali jagah alag alag dabao hain.
    """
    cleaned = re.sub(r"\s*\+\s*(?=\S)", "+", str(text or "").strip())
    chords: list[list[int]] = []
    for word in cleaned.split():
        keys: list[int] = []
        for name in re.split(r"\+(?=.)", word):
            code = _VK.get(name.lower()) or _VK.get(name)
            if code is None:
                raise HandsError(f'"{name}" is not a key I know. Use names like ctrl, shift, alt, win, Return, Escape, Tab, Up, F5, or a single letter.')
            keys.append(code)
        if not keys or len(keys) > MAX_CHORD_KEYS:
            raise HandsError(f'"{word}" is not a key press.')
        chords.append(keys)
    if not chords:
        raise HandsError("No key was given.")
    if len(chords) > MAX_CHORDS:
        raise HandsError(f"That is {len(chords)} key presses in one call; send at most {MAX_CHORDS}.")
    return chords


def modifier_keys(text: str) -> list[int]:
    """Click ya scroll ke saath dabi hui keys ("shift", "ctrl+shift"). Khali = koi nahi."""
    if not str(text or "").strip():
        return []
    keys = [code for chord in parse_keys(text) for code in chord]
    if any(code not in MODIFIERS for code in keys):
        raise HandsError("Only ctrl, shift, alt or win can be held during a click.")
    return keys


# ── Windows ka SendInput ─────────────────────────────────────────────────────

_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_MOVE, _ABSOLUTE = 0x0001, 0x8000
_WHEEL, _HWHEEL, _WHEEL_NOTCH = 0x0800, 0x1000, 120
_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
_KEY_EXTENDED, _KEY_UP, _KEY_UNICODE = 0x0001, 0x0002, 0x0004
_VK_RETURN, _VK_TAB = 0x0D, 0x09

if IS_WINDOWS:
    from ctypes import wintypes

    _ULONG_PTR = ctypes.c_size_t

    class _MouseInput(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _KeyInput(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _InputUnion(ctypes.Union):
        _fields_ = [("mi", _MouseInput), ("ki", _KeyInput)]

    class _Input(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    _user32.MapVirtualKeyW.restype = wintypes.UINT


def _send(*inputs: Any) -> None:
    batch = (_Input * len(inputs))(*inputs)
    if _user32.SendInput(len(inputs), batch, ctypes.sizeof(_Input)) != len(inputs):
        raise HandsError("Windows did not accept the input (a protected or administrator window may be in front).")


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> Any:
    return _Input(type=_INPUT_MOUSE, mi=_MouseInput(dx, dy, data & 0xFFFFFFFF, flags, 0, 0))


def _key(code: int, up: bool = False) -> Any:
    flags = (_KEY_EXTENDED if code in _EXTENDED else 0) | (_KEY_UP if up else 0)
    return _Input(type=_INPUT_KEYBOARD, ki=_KeyInput(code, _user32.MapVirtualKeyW(code, 0), flags, 0, 0))


def _unicode(unit: int, up: bool = False) -> Any:
    return _Input(type=_INPUT_KEYBOARD, ki=_KeyInput(0, unit, _KEY_UNICODE | (_KEY_UP if up else 0), 0, 0))


def _thread_init() -> None:
    """Haathon ka thread asli pixels mein dekhe (cursor ki jagah, windows ke naap)."""
    try:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:  # noqa: BLE001 — purana Windows: naap phir bhi aapas mein mutabiq rehte hain
        pass


# Ek lambi tehreer ke harf ek saath nahi bheje jate: kuch apps tez aamad mein
# harf gira deti hain. Itne harf, phir chhota sa waqfa.
_TYPE_BURST = 12
_TYPE_GAP_SECS = 0.012
MAX_TYPE_CHARS = 8000
MAX_WAIT_SECS = 30.0
MAX_SCROLL = 30
MAX_KEY_REPEAT = 100


class Hands:
    """Mouse, keyboard aur tasveer. `run()` ke zariye bulao: har kaam apne thread par, ek ke baad ek."""

    def __init__(self) -> None:
        self.available = IS_WINDOWS
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stonic-hands", initializer=_thread_init if IS_WINDOWS else None)
        self._cancel = threading.Event()
        # Jahan haathon ne cursor chhora (screen ka hissa). User ne is ke baad
        # mouse hilaya ho to agla kaam nahi hota.
        self._left_at: tuple[float, float] | None = None

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, functools.partial(fn, *args, **kwargs))

    # ── Rokna ───────────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Chalta kaam (lambi tehreer, drag, intezar) abhi ruke. Kisi bhi thread se."""
        self._cancel.set()

    def resume(self) -> None:
        self._cancel.clear()

    def forget_cursor(self) -> None:
        """Naya kaam: pichhli jagah ka hisaab nahi (user beech mein mouse chala chuka hoga)."""
        self._left_at = None

    def _pause(self, seconds: float) -> None:
        if self._cancel.wait(max(0.0, seconds)):
            raise HandsStopped("Stopped.")

    def _check(self) -> None:
        if self._cancel.is_set():
            raise HandsStopped("Stopped.")

    # ── Dekhna ──────────────────────────────────────────────────────────────

    @staticmethod
    def _grab() -> Any:
        from PIL import ImageGrab

        return ImageGrab.grab()

    @staticmethod
    def _encode(image: Any, quality: int) -> Shot:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=quality)
        return Shot(data=buffer.getvalue(), width=image.width, height=image.height)

    def capture(self, max_dim: int, quality: int = 85, multiple: int = 1) -> Shot:
        """Poori primary screen, lamba rukh `max_dim` tak."""
        from PIL import Image

        image = self._grab()
        size = fit_size(image.width, image.height, max_dim, multiple)
        if size != image.size:
            image = image.resize(size, Image.LANCZOS)
        return self._encode(image, quality)

    def zoom(self, region: tuple[float, float, float, float], max_dim: int, quality: int = 90) -> Shot:
        """Screen ka ek hissa (hisson mein: baayan, upar, daayan, neeche), bara kar ke."""
        from PIL import Image

        image = self._grab()
        left, top, right, bottom = (min(max(float(value), 0.0), 1.0) for value in region)
        box = (int(left * image.width), int(top * image.height), math.ceil(right * image.width), math.ceil(bottom * image.height))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            raise HandsError("That region is too small to zoom into. Give two opposite corners of the area.")
        crop = image.crop(box)
        # Chhota hissa bara dikhe (parhne ke liye), magar teen guna se zyada nahi: us ke aage sirf dhundla hota hai.
        scale = min(max_dim / max(crop.width, crop.height), 3.0)
        size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
        return self._encode(crop.resize(size, Image.LANCZOS), quality)

    # ── Mouse ───────────────────────────────────────────────────────────────

    @staticmethod
    def _absolute(fx: float, fy: float) -> tuple[int, int]:
        return round(min(max(fx, 0.0), 1.0) * 65535), round(min(max(fy, 0.0), 1.0) * 65535)

    def _move(self, fx: float, fy: float) -> None:
        dx, dy = self._absolute(fx, fy)
        _send(_mouse(_MOVE | _ABSOLUTE, dx, dy))
        self._left_at = (fx, fy)

    def cursor(self) -> tuple[float, float]:
        """Cursor abhi kahan hai (screen ka hissa)."""
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        width, height = max(_user32.GetSystemMetrics(0), 1), max(_user32.GetSystemMetrics(1), 1)
        return point.x / width, point.y / height

    def user_moved(self) -> bool:
        """Haathon ke aakhri kaam ke baad user ne khud mouse hilaya?"""
        if self._left_at is None:
            return False
        width, height = max(_user32.GetSystemMetrics(0), 1), max(_user32.GetSystemMetrics(1), 1)
        fx, fy = self.cursor()
        moved = math.hypot((fx - self._left_at[0]) * width, (fy - self._left_at[1]) * height)
        # 1080p par 60 pixel (asal computer agent wali hadd), bari screen par usi nisbat se.
        return moved > max(40.0, width * 0.031)

    def _with_modifiers(self, modifiers: list[int], action: Callable[[], None]) -> None:
        held: list[int] = []
        try:
            for code in modifiers:
                _send(_key(code))
                held.append(code)
            if held:
                self._pause(0.03)
            action()
        finally:
            for code in reversed(held):
                try:
                    _send(_key(code, up=True))
                except HandsError:
                    pass

    def move(self, fx: float, fy: float) -> None:
        self._check()
        self._move(fx, fy)

    def click(self, fx: float, fy: float, button: str = "left", count: int = 1, modifiers: list[int] | None = None) -> None:
        self._check()
        down, up = _BUTTONS[button]

        def press() -> None:
            self._move(fx, fy)
            self._pause(0.06)  # app ko cursor ke aane ki khabar ho (hover), phir dabao
            for index in range(max(1, min(int(count), 3))):
                if index:
                    self._pause(0.07)
                _send(_mouse(down))
                self._pause(0.03)
                _send(_mouse(up))

        self._with_modifiers(modifiers or [], press)

    def drag(self, start: tuple[float, float], end: tuple[float, float], modifiers: list[int] | None = None) -> None:
        self._check()
        down, up = _BUTTONS["left"]

        def pull() -> None:
            self._move(*start)
            self._pause(0.08)
            _send(_mouse(down))
            try:
                self._pause(0.1)
                # Beech ke qadam: bohat si apps drag tab hi manti hain jab mouse chalta hua aaye.
                steps = 14
                for step in range(1, steps + 1):
                    ratio = step / steps
                    self._move(start[0] + (end[0] - start[0]) * ratio, start[1] + (end[1] - start[1]) * ratio)
                    self._pause(0.02)
                self._pause(0.1)
            finally:
                _send(_mouse(up))

        self._with_modifiers(modifiers or [], pull)

    def scroll(self, direction: str, amount: int, at: tuple[float, float] | None = None, modifiers: list[int] | None = None) -> None:
        self._check()
        notches = max(1, min(int(amount), MAX_SCROLL))
        flag, sign = {"up": (_WHEEL, 1), "down": (_WHEEL, -1), "right": (_HWHEEL, 1), "left": (_HWHEEL, -1)}[direction]

        def roll() -> None:
            if at is not None:
                self._move(*at)
                self._pause(0.05)
            for _ in range(notches):
                _send(_mouse(flag, data=sign * _WHEEL_NOTCH))
                self._pause(0.015)
            if at is None:
                self._left_at = self.cursor()

        self._with_modifiers(modifiers or [], roll)

    # ── Keyboard ────────────────────────────────────────────────────────────

    def type_text(self, text: str) -> None:
        """Tehreer, harf ba harf — kisi bhi zubaan mein. Nayi line = Enter, tab = Tab."""
        self._check()
        text = str(text or "")
        if len(text) > MAX_TYPE_CHARS:
            raise HandsError(f"That is {len(text)} characters; type at most {MAX_TYPE_CHARS} at a time, or write a file instead.")
        burst: list[Any] = []

        def flush() -> None:
            if burst:
                _send(*burst)
                burst.clear()
                self._pause(_TYPE_GAP_SECS)

        for char in text.replace("\r\n", "\n").replace("\r", "\n"):
            if char in "\n\t":
                flush()
                code = _VK_RETURN if char == "\n" else _VK_TAB
                _send(_key(code), _key(code, up=True))
                self._pause(_TYPE_GAP_SECS)
                continue
            encoded = char.encode("utf-16-le")
            for offset in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[offset:offset + 2], "little")
                burst.extend((_unicode(unit), _unicode(unit, up=True)))
            if len(burst) >= _TYPE_BURST * 2:
                flush()
        flush()
        self._left_at = self.cursor()

    def press(self, chords: list[list[int]], repeat: int = 1) -> None:
        """Keys dabana. Har dabao mein keys tarteeb se dabti hain aur ulti tarteeb se uthti hain."""
        self._check()
        for _ in range(max(1, min(int(repeat), MAX_KEY_REPEAT))):
            for chord in chords:
                held: list[int] = []
                try:
                    for code in chord:
                        _send(_key(code))
                        held.append(code)
                        self._pause(0.012)
                finally:
                    for code in reversed(held):
                        try:
                            _send(_key(code, up=True))
                        except HandsError:
                            pass
                self._pause(0.03)
        self._left_at = self.cursor()

    def wait(self, seconds: float) -> None:
        self._pause(min(max(float(seconds), 0.0), MAX_WAIT_SECS))

# Mouse ek hai, aur Stonic mein use chalane wale do hain: ye haath (Beta ka
# dimagh) aur asal computer agent (chat, phone ya asal voice assistant ka diya
# hua kaam — stonic-computer/runner.py). App batati hai ke doosra kab chal raha
# hai (POST /beta/mouse); us dauran ye haath screen nahi chherte.
_taken_elsewhere = False


def set_mouse_taken(taken: bool) -> None:
    global _taken_elsewhere
    _taken_elsewhere = bool(taken)


def mouse_taken() -> bool:
    return _taken_elsewhere


_shared: Hands | None = None


def shared_hands() -> Hands:
    """Mouse ek hai, is liye haath bhi ek: har baat-cheet yehi istemal karti hai."""
    global _shared
    if _shared is None:
        _shared = Hands()
    return _shared
