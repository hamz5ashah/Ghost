"""
Esc = stop — magar sirf ASLI keyboard ka Esc.
============================================

Agent screen par kaam kar raha ho to user ka sab se fitri raasta usay rokne ka
ek hi hai: Esc daba do. Mushkil ye hai ke agent KHUD bhi Esc dabata hai (popup
ya menu band karne ke liye). Saada global hotkey dono ko ek hi cheez samajhta:
agent apna hi task rok deta, aur us ka Esc app tak pohnchta hi nahi.

Hal: Windows ka low-level keyboard hook (WH_KEYBOARD_LL) har key ke saath batata
hai ke wo software ne bheji (LLKHF_INJECTED) ya asli keyboard se aayi. pyautogui
ki har key injected hoti hai. To:

    asli Esc   -> task rokne ka ishara, aur key app tak NAHI jati
    agent ka Esc -> bilkul nahi chhua jata, seedha app tak

Sirf "akela" Esc gina jata hai. Ctrl+Shift+Esc (Task Manager), Alt+Esc aur
Ctrl+Alt+Esc (Electron ka purana emergency shortcut) jaise Windows ke apne
shortcuts yahan se guzar jate hain.

Hook sirf task ke dauran laga hota hai (arm/disarm) — baqi waqt keyboard par
hamara koi asar nahi. Ye apne alag thread par chalta hai jis ka kaam sirf
paigham sunna hai: hook ka callback jitni der le, poore system ki har key utni
der rukti hai, is liye callback mein koi IO nahi — bas ek faisla aur ek ishara.

Jahan ye kaam NAHI karta (aur Ctrl+Alt+Esc / title bar ka Stop tab bhi chalte hain):
  - Saamne admin (elevated) window ho: Windows hamein us ki keys dikhata hi nahi.
  - Remote desktop / on-screen keyboard: un ka Esc bhi "injected" hota hai.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import threading
from ctypes import wintypes
from typing import Callable, Optional, Set, Tuple

log = logging.getLogger("esc-guard")

_WINDOWS = platform.system() == "Windows"

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_QUIT = 0x0012
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
PM_NOREMOVE = 0x0000
LLKHF_INJECTED = 0x10
VK_ESCAPE = 0x1B
# Shift, Ctrl, Alt, Win — aam aur dayen/bayen wale dono naam.
MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})


class EscFilter:
    """Har key par ek faisla: nigalni hai? stop ka ishara dena hai?

    Hook se alag rakha gaya hai taake bina Windows ke parkha ja sake.

    ``is_down(vk)`` key ki abhi ki asli halat batata hai. Modifiers ke liye
    dono cheezein dekhi jati hain: hook ne use asli keyboard se dabte dekha
    HO, aur wo abhi bhi dabi HO. Sirf pehli par bharosa karte to ek chhooti hui
    "key up" (admin window ke saamne hook andha hota hai) Ctrl ko hamesha ke
    liye "daba hua" chhor deti aur Esc phir kabhi kaam na karta. Sirf doosri par
    karte to agent ka apna dabaya hua Ctrl user ke Esc ko rad kar deta.
    """

    def __init__(self, is_down: Optional[Callable[[int], bool]] = None) -> None:
        self._is_down = is_down or (lambda _vk: True)
        self._mods: Set[int] = set()
        self._esc_ours = False

    def _modifier_held(self) -> bool:
        self._mods = {vk for vk in self._mods if self._is_down(vk)}
        return bool(self._mods)

    def feed(self, vk: int, down: bool, injected: bool) -> Tuple[bool, bool]:
        """(swallow, fire) lautata hai."""
        if injected:
            return False, False
        if vk in MODIFIER_VKS:
            if down:
                self._mods.add(vk)
            else:
                self._mods.discard(vk)
            return False, False
        if vk != VK_ESCAPE:
            return False, False
        if down:
            if self._esc_ours:
                return True, False      # key dabi hui hai — auto-repeat
            if self._modifier_held():
                return False, False     # Windows ka apna shortcut, hamara nahi
            self._esc_ours = True
            return True, True
        if self._esc_ours:
            # Jis ka "down" nigla tha us ka "up" bhi — app ko aadhi key na mile.
            self._esc_ours = False
            return True, False
        return False, False


if _WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _LRESULT = ctypes.c_ssize_t
    _HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    _PKBD = ctypes.POINTER(_KBDLLHOOKSTRUCT)

    # 64-bit par handle 64-bit hote hain — restype na dein to ctypes unhein
    # 32-bit int samajh kar kaat deta hai aur hook lagta hi nahi.
    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    _user32.CallNextHookEx.restype = _LRESULT
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    def _key_is_down(vk: int) -> bool:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


class _Session:
    """Ek arm() ki zindagi — agle arm() se bilkul alag."""

    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.thread_id = 0
        self.ready = threading.Event()
        self.ok = False
        self.cancelled = False


class EscGuard:
    """Task ke dauran asli Esc sunta hai aur ``on_escape`` bulata hai.

    ``on_escape`` hook ke thread par, hook ke andar se chalta hai — usay foran
    lautna hai (queue mein ek cheez daal dena kaafi hai), warna keyboard atakta hai.
    """

    def __init__(self, on_escape: Callable[[], None]) -> None:
        self._on_escape = on_escape
        self._lock = threading.Lock()
        self._session: Optional[_Session] = None

    @property
    def armed(self) -> bool:
        s = self._session
        return bool(s and s.ok and s.thread is not None and s.thread.is_alive())

    def arm(self, timeout: float = 1.0) -> bool:
        """Hook lagao. True = Esc ab sach mein rokta hai."""
        if not _WINDOWS:
            return False
        with self._lock:
            if self.armed:
                return True
            session = _Session()
            session.thread = threading.Thread(target=self._run, args=(session,), name="esc-guard", daemon=True)
            self._session = session
            session.thread.start()
            session.ready.wait(timeout)
            if not session.ok:
                log.warning("Esc hook nahi laga — rokne ke baqi raaste chalte rahenge.")
            return session.ok

    def disarm(self) -> None:
        with self._lock:
            session, self._session = self._session, None
        if session is None or session.thread is None:
            return
        session.cancelled = True
        # Thread ki message queue ban chuki ho tabhi WM_QUIT us tak pohnchta hai.
        session.ready.wait(1.0)
        if session.thread_id:
            _user32.PostThreadMessageW(session.thread_id, WM_QUIT, 0, 0)
        session.thread.join(1.0)

    def _run(self, session: _Session) -> None:
        flt = EscFilter(_key_is_down)

        def proc(n_code: int, w_param: int, l_param: int) -> int:
            try:
                if n_code == HC_ACTION:
                    kb = ctypes.cast(l_param, _PKBD).contents
                    down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    swallow, fire = flt.feed(int(kb.vkCode), down, bool(kb.flags & LLKHF_INJECTED))
                    if fire:
                        self._on_escape()
                    if swallow:
                        return 1
            except Exception:  # noqa: BLE001 — hook se exception bahar gayi to keyboard atak jata hai
                pass
            return _user32.CallNextHookEx(None, n_code, w_param, l_param)

        proc_ref = _HOOKPROC(proc)   # hook ke hatne tak zinda rehna zaroori hai
        msg = wintypes.MSG()
        session.thread_id = _kernel32.GetCurrentThreadId()
        # Pehla PeekMessage thread ki message queue banata hai.
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc_ref, _kernel32.GetModuleHandleW(None), 0)
        if not hook:
            hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc_ref, None, 0)
        if not hook:
            log.warning("SetWindowsHookEx fail: error %s", ctypes.get_last_error())
        session.ok = bool(hook)
        session.ready.set()
        if not hook:
            return
        try:
            while not session.cancelled:
                got = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
        finally:
            _user32.UnhookWindowsHookEx(hook)
