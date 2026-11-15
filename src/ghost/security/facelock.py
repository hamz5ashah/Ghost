"""Face Lock — an opt-in lock on the Ghost desktop app (Stonic port, Phase 4).

Ported from Stonic's ``facelock-core.js`` (the arithmetic) and
``facelock-manager.js`` (the lock state). Off unless ``[stonic].facelock`` is
on; with it off nothing here runs — no camera, no models, no lock screen.

Face recognition has three parts, and only the last one lives here:

1. find the face and prove it is live (a natural blink) → the WebView, next
   to the camera;
2. turn the face into 128 numbers (the SFace model)      → the WebView;
3. compare those numbers with the saved ones             → **here**.

The third part is here so that the "unlock" decision is made where page
JavaScript cannot reach. The WebView only ever sends numbers; the saved
numbers (templates) never go back to it, and the lock opens only on an
``ok: True`` from this module. A loopback gate (:class:`FaceLockGate`) refuses
the desktop UI's API calls while locked, so a reload or devtools cannot get
past the lock screen either.

The honest limit: an ordinary webcam has no depth sensor, so this is a lock
for the people around your home or office, not a bank vault. The thresholds
below were chosen with that in mind — strict enough that another face does not
open it, lenient enough that the owner is not pushed to the PIN in poor light.

What is stored: the face numbers and a scrypt hash of the PIN, in
``<config>/face_lock.dpapi``, encrypted with Windows DPAPI so only this
Windows user can read it. No picture is ever stored, and nothing leaves the
PC. The lock protects the *app*, not the files on disk: anyone with access to
this Windows account can delete that file to remove the lock — which is also
the "forgot my PIN" path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 128

# Enrollment: one straight-on print plus the head-turn angles.
MIN_TEMPLATES = 3
MAX_TEMPLATES = 12

# One attempt = a few frames from before and after the blink.
MIN_PROBES = 3
MAX_PROBES = 8

# Cosine-similarity threshold. SFace's authors give 0.363 as the "best"
# threshold (where false accepts and false rejects are equal). For a lock a
# false accept costs more, so the threshold is set higher.
MATCH_THRESHOLD = 0.5

# The frames of one attempt must also match each other — otherwise someone
# else blinked and a different face was shown.
PROBE_COHERENCE_MIN = 0.45

# The enrollment templates must be one person (the angles allow some spread,
# so this threshold is lenient).
TEMPLATE_COHERENCE_MIN = 0.3

PIN_LENGTH = 6
WEAK_PINS = frozenset({"123456", "654321", "123123", "112233", "121212"})

# Five free attempts, then a growing wait after every failure.
MAX_FREE_ATTEMPTS = 5
LOCKOUT_STEPS_MS = (30_000, 60_000, 5 * 60_000, 15 * 60_000)

# Settings asks for a re-check before "turn off / scan again / change PIN";
# the ticket that re-check earns lasts this long.
TICKET_TTL_MS = 2 * 60_000

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_KEYLEN = 16384, 8, 1, 32

FILE_NAME = "face_lock.dpapi"


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------


def normalize_embedding(raw: Any) -> Optional[List[float]]:
    """128 numbers → unit length. ``None`` if anything is wrong."""
    if not isinstance(raw, (list, tuple)) or len(raw) != EMBEDDING_DIM:
        return None
    out: List[float] = []
    for value in raw:
        if isinstance(value, bool):
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
            return None
        out.append(v)
    norm = math.sqrt(sum(v * v for v in out))
    if not norm > 1e-6:
        return None
    return [v / norm for v in out]


def _normalize_list(raw: Any, lo: int, hi: int) -> Optional[List[List[float]]]:
    if not isinstance(raw, (list, tuple)) or not lo <= len(raw) <= hi:
        return None
    out = []
    for item in raw:
        vec = normalize_embedding(item)
        if vec is None:
            return None
        out.append(vec)
    return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Both are already unit length, so this is just the dot product."""
    return sum(x * y for x, y in zip(a, b))


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _min_pairwise(vectors: Sequence[Sequence[float]]) -> float:
    low = 1.0
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            low = min(low, cosine(vectors[i], vectors[j]))
    return low


@dataclass(frozen=True)
class Match:
    ok: bool
    score: float
    reason: str  # match | no_match | incoherent | bad_input | not_enrolled


def match_probes(
    raw_probes: Any,
    templates: Sequence[Sequence[float]],
    threshold: float = MATCH_THRESHOLD,
) -> Match:
    """Decide one attempt.

    Each frame scores its closest template; the attempt scores the median of
    those — one good or one bad frame cannot change the outcome on its own.
    """
    probes = _normalize_list(raw_probes, MIN_PROBES, MAX_PROBES)
    if probes is None:
        return Match(False, 0.0, "bad_input")
    if not templates:
        return Match(False, 0.0, "not_enrolled")
    per_probe = [max(cosine(p, t) for t in templates) for p in probes]
    score = _median(per_probe)
    if _min_pairwise(probes) < PROBE_COHERENCE_MIN:
        return Match(False, score, "incoherent")
    if score < threshold:
        return Match(False, score, "no_match")
    return Match(True, score, "match")


def prepare_templates(raw: Any) -> Dict[str, Any]:
    """Enrollment templates: right shape, right count, and one person."""
    templates = _normalize_list(raw, MIN_TEMPLATES, MAX_TEMPLATES)
    if templates is None:
        return {"ok": False, "reason": "bad_input"}
    # The first template is the straight-on one; the rest are measured from it.
    for t in templates[1:]:
        if cosine(templates[0], t) < TEMPLATE_COHERENCE_MIN:
            return {"ok": False, "reason": "incoherent"}
    # Six decimals on disk is plenty (more than float32 precision anyway).
    return {"ok": True, "templates": [[round(v, 6) for v in t] for t in templates]}


# ---------------------------------------------------------------------------
# PIN
# ---------------------------------------------------------------------------


def is_valid_pin(pin: Any) -> bool:
    if not isinstance(pin, str) or len(pin) != PIN_LENGTH or not pin.isascii():
        return False
    if not pin.isdigit():
        return False
    if len(set(pin)) == 1:  # 000000, 111111 …
        return False
    return pin not in WEAK_PINS


def hash_pin(pin: str, salt_hex: Optional[str] = None) -> Dict[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        str(pin).encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEYLEN,
    )
    return {"salt": salt.hex(), "hash": digest.hex()}


def verify_pin(pin: Any, record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    salt, expected = record.get("salt"), record.get("hash")
    if not isinstance(salt, str) or not isinstance(expected, str):
        return False
    if not isinstance(pin, str) or len(pin) != PIN_LENGTH:
        return False
    try:
        actual = hash_pin(pin, salt)["hash"]
        return hmac.compare_digest(bytes.fromhex(expected), bytes.fromhex(actual))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Failed attempts
# ---------------------------------------------------------------------------


def lockout_remaining(state: Optional[Dict[str, Any]], now_ms: float) -> float:
    """How long the wait still is (ms). 0 = an attempt may be made."""
    until = float((state or {}).get("lockoutUntil") or 0)
    if until <= now_ms:
        return 0
    # If the clock was set back, never wait longer than the longest step.
    return min(until - now_ms, LOCKOUT_STEPS_MS[-1])


def attempts_left(state: Optional[Dict[str, Any]]) -> int:
    return max(0, MAX_FREE_ATTEMPTS - int((state or {}).get("failCount") or 0))


def register_failure(state: Optional[Dict[str, Any]], now_ms: float) -> Dict[str, Any]:
    """Record a failure. Returns the new ``{failCount, lockoutUntil}``."""
    fail_count = int((state or {}).get("failCount") or 0) + 1
    lockout_until = 0.0
    if fail_count >= MAX_FREE_ATTEMPTS:
        step = min(fail_count - MAX_FREE_ATTEMPTS, len(LOCKOUT_STEPS_MS) - 1)
        lockout_until = now_ms + LOCKOUT_STEPS_MS[step]
    return {"failCount": fail_count, "lockoutUntil": lockout_until}


CLEAN_ATTEMPTS = {"failCount": 0, "lockoutUntil": 0}


# ---------------------------------------------------------------------------
# State on disk
# ---------------------------------------------------------------------------


def sanitize_state(raw: Any) -> Optional[Dict[str, Any]]:
    """Clean what came from the file. Anything partial or broken → ``None``.

    ``None`` means "no lock". Deliberately: locking on a half-written file
    would shut the owner out of their own app, and deleting the file removes
    the lock anyway.
    """
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    templates = _normalize_list(raw.get("templates"), MIN_TEMPLATES, MAX_TEMPLATES)
    if templates is None:
        return None
    pin = raw.get("pin")
    if (
        not isinstance(pin, dict)
        or not isinstance(pin.get("salt"), str)
        or not isinstance(pin.get("hash"), str)
    ):
        return None

    def num(key: str) -> float:
        try:
            v = float(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0
        return v if math.isfinite(v) else 0

    return {
        "version": 1,
        "enabled": True,
        "templates": templates,
        "pin": {"salt": pin["salt"], "hash": pin["hash"]},
        "createdAt": num("createdAt"),
        "updatedAt": num("updatedAt"),
        "failCount": max(0, int(num("failCount"))),
        "lockoutUntil": max(0.0, num("lockoutUntil")),
    }


@dataclass
class Ticket:
    value: str
    expires_at: float


def new_ticket(now_ms: float) -> Ticket:
    return Ticket(secrets.token_hex(24), now_ms + TICKET_TTL_MS)


# ---------------------------------------------------------------------------
# Encrypted store (Windows DPAPI, like Stonic's key-store)
# ---------------------------------------------------------------------------


class DpapiStore:
    """JSON objects sealed with ``CryptProtectData`` for the current user.

    Face Lock is Windows-only, as in Stonic: face prints are never written in
    the clear, and DPAPI is what makes that possible without a password.
    """

    def encryption_available(self) -> bool:
        return sys.platform == "win32"

    def read_object(self, path: Path) -> Any:
        if not path.is_file():
            return None
        return json.loads(_dpapi(path.read_bytes(), protect=False).decode("utf-8"))

    def write_object(self, path: Path, obj: Any) -> None:
        data = _dpapi(json.dumps(obj, separators=(",", ":")).encode("utf-8"), True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)


def _dpapi(data: bytes, protect: bool) -> bytes:  # pragma: no cover - Windows
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    entropy_raw = b"ghost-facelock-v1"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = Blob(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (
        (ctypes.byref(src), "Ghost Face Lock", ctypes.byref(entropy), None, None)
        if protect
        else (ctypes.byref(src), None, ctypes.byref(entropy), None, None)
    )
    if not fn(*args, flags, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

Listener = Callable[[Dict[str, Any]], None]


def _now_ms() -> float:
    return time.time() * 1000


class FaceLockManager:
    """Lock state, in the backend.

    When enabled:

    * the app starts **locked** (``locked`` lives here, not in the page — a
      reload, a relaunch or a new window does not open it);
    * it locks again when Windows locks or the PC sleeps
      (:class:`SessionWatcher`);
    * it opens only on an ``ok`` from :meth:`verify_face` / :meth:`verify_pin`.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        store: Any = None,
        now: Callable[[], float] = _now_ms,
    ) -> None:
        self.path = Path(config_dir) / FILE_NAME
        self.store = store if store is not None else DpapiStore()
        self.now = now
        self._lock = threading.RLock()
        self._listeners: List[Listener] = []
        self._ticket: Optional[Ticket] = None
        self.state = self._read()
        # Boot: if the lock is on, it is locked.
        self.locked = self.state is not None

    # -- outward ---------------------------------------------------------------

    def on_change(self, listener: Listener) -> Callable[[], None]:
        """``listener({"type": "locked"|"unlocked"|"changed", "reason", ...})``"""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def is_supported(self) -> bool:
        try:
            return bool(self.store.encryption_available())
        except Exception:  # noqa: BLE001
            return False

    def is_enabled(self) -> bool:
        return self.state is not None

    def is_locked(self) -> bool:
        return self.locked and self.state is not None

    def status(self) -> Dict[str, Any]:
        """What the WebView may see. Never the templates or the PIN hash."""
        with self._lock:
            now = self.now()
            return {
                "supported": self.is_supported(),
                "enabled": self.is_enabled(),
                "locked": self.is_locked(),
                "templateCount": len(self.state["templates"]) if self.state else 0,
                "enrolledAt": self.state["createdAt"] if self.state else 0,
                "attemptsLeft": attempts_left(self.state),
                "maxAttempts": MAX_FREE_ATTEMPTS,
                "lockoutMs": lockout_remaining(self.state, now) if self.state else 0,
                "threshold": MATCH_THRESHOLD,
                "pinLength": PIN_LENGTH,
            }

    def verify_face(self, embeddings: Any, purpose: str = "unlock") -> Dict[str, Any]:
        """Recognise by face. ``unlock`` opens the lock; ``manage`` earns a ticket."""
        with self._lock:
            gate = self._gate()
            if gate:
                return gate
            result = match_probes(embeddings, self.state["templates"])
            # Bad input is not an attempt — that is the page's fault, not a person's.
            if result.reason == "bad_input":
                return self._reply({"ok": False, "reason": "bad_input"})
            return self._settle(
                result.ok,
                purpose,
                {"score": round(result.score, 3), "reason": result.reason},
            )

    def verify_pin(self, pin: Any, purpose: str = "unlock") -> Dict[str, Any]:
        with self._lock:
            gate = self._gate()
            if gate:
                return gate
            ok = verify_pin(str(pin if pin is not None else ""), self.state["pin"])
            return self._settle(ok, purpose, {"reason": "match" if ok else "wrong_pin"})

    def enroll(
        self, templates: Any, pin: Any, ticket: Optional[str] = None
    ) -> Dict[str, Any]:
        """New face + PIN. First time (lock off) directly; when the lock is
        already on a ticket is required — otherwise anyone at the open app
        could add their own face."""
        with self._lock:
            if not self.is_supported():
                return {"ok": False, "reason": "unsupported"}
            if self.state is not None and not self._use_ticket(ticket):
                return {"ok": False, "reason": "verify_first"}
            prepared = prepare_templates(templates)
            if not prepared["ok"]:
                return {"ok": False, "reason": prepared["reason"]}
            if not is_valid_pin(pin):
                return {"ok": False, "reason": "weak_pin"}
            now = self.now()
            nxt = {
                "version": 1,
                "enabled": True,
                "templates": prepared["templates"],
                "pin": hash_pin(pin),
                "createdAt": now,
                "updatedAt": now,
                **CLEAN_ATTEMPTS,
            }
            if not self._write(nxt):
                return {"ok": False, "reason": "save_failed"}
            self.state = sanitize_state(nxt)
            # The owner just set it up — it stays open for this session.
            self.locked = False
            self._emit("changed", "enrolled")
            return {"ok": True, "status": self.status()}

    def change_pin(self, pin: Any, ticket: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            if self.state is None:
                return {"ok": False, "reason": "not_enabled"}
            if not self._use_ticket(ticket):
                return {"ok": False, "reason": "verify_first"}
            if not is_valid_pin(pin):
                return {"ok": False, "reason": "weak_pin"}
            nxt = {**self.state, "pin": hash_pin(pin), "updatedAt": self.now()}
            if not self._write(nxt):
                return {"ok": False, "reason": "save_failed"}
            self.state = sanitize_state(nxt)
            self._emit("changed", "pin")
            return {"ok": True, "status": self.status()}

    def disable(self, ticket: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            if self.state is None:
                return {"ok": True, "status": self.status()}
            if not self._use_ticket(ticket):
                return {"ok": False, "reason": "verify_first"}
            try:
                self.store.delete(self.path)
            except OSError:
                return {"ok": False, "reason": "save_failed"}
            self.state = None
            self.locked = False
            self._emit("changed", "disabled")
            return {"ok": True, "status": self.status()}

    def lock(self, reason: str = "manual") -> bool:
        """Lock (if enabled). ``True`` = it is now locked."""
        with self._lock:
            if self.state is None:
                return False
            if self.locked:
                return True
            self.locked = True
            self._ticket = None
            self._emit("locked", reason)
            return True

    # -- inward ----------------------------------------------------------------

    def _gate(self) -> Optional[Dict[str, Any]]:
        """Guards before an attempt. A reply if blocked, else ``None``."""
        if self.state is None:
            return {"ok": False, "reason": "not_enabled", "status": self.status()}
        if lockout_remaining(self.state, self.now()) > 0:
            return self._reply({"ok": False, "reason": "lockout"})
        return None

    def _settle(self, ok: bool, purpose: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        if not ok:
            self._save_attempts(register_failure(self.state, self.now()))
            return self._reply({"ok": False, **extra})
        if self.state.get("failCount") or self.state.get("lockoutUntil"):
            self._save_attempts(CLEAN_ATTEMPTS)
        if purpose == "manage":
            self._ticket = new_ticket(self.now())
            return self._reply({"ok": True, **extra, "ticket": self._ticket.value})
        was_locked = self.locked
        self.locked = False
        if was_locked:
            self._emit("unlocked", "verified")
        return self._reply({"ok": True, **extra})

    def _reply(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return {**body, "status": self.status()}

    def _use_ticket(self, value: Optional[str]) -> bool:
        ticket, self._ticket = self._ticket, None  # single use
        return bool(
            ticket
            and isinstance(value, str)
            and hmac.compare_digest(value.encode(), ticket.value.encode())
            and ticket.expires_at > self.now()
        )

    def _save_attempts(self, attempts: Dict[str, Any]) -> None:
        nxt = {**self.state, **attempts}
        # Even if saving fails, keep the count in memory so this session stays guarded.
        self._write(nxt)
        self.state = nxt

    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            if not self.is_supported():
                return None
            return sanitize_state(self.store.read_object(self.path))
        except Exception as exc:  # noqa: BLE001 - unreadable = no lock
            logger.warning("Face Lock state unreadable, treating as off: %s", exc)
            return None

    def _write(self, nxt: Dict[str, Any]) -> bool:
        try:
            self.store.write_object(self.path, nxt)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Face Lock could not save its state: %s", exc)
            return False

    def _emit(self, kind: str, reason: str) -> None:
        event = {"type": kind, "reason": reason, "status": self.status()}
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Face Lock listener failed: %s", exc)


# ---------------------------------------------------------------------------
# Lock again when Windows locks or the PC sleeps
# ---------------------------------------------------------------------------


def _workstation_locked() -> bool:  # pragma: no cover - Windows
    """True while the Windows lock screen (or UAC/secure desktop) is up."""
    if sys.platform != "win32":
        return False
    import ctypes  # noqa: PLC0415

    user32 = ctypes.windll.user32
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    try:
        return not user32.SwitchDesktop(desk)
    finally:
        user32.CloseDesktop(desk)


class SessionWatcher:
    """Stonic hooked Electron's powerMonitor; Ghost polls instead.

    * Windows lock: the input desktop cannot be switched to while it is up.
    * Sleep: a thread that sleeps 2 s and wakes 30 s+ later was suspended.
    """

    def __init__(
        self,
        manager: FaceLockManager,
        *,
        interval: float = 2.0,
        sleep_gap: float = 30.0,
        is_locked: Callable[[], bool] = _workstation_locked,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.manager = manager
        self.interval = interval
        self.sleep_gap = sleep_gap
        self._is_locked = is_locked
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._was_locked = False
        self._last = clock()

    def tick(self) -> None:
        """One check. Public so tests can drive it without a thread."""
        now = self._clock()
        if now - self._last > self.sleep_gap:
            self.manager.lock("system-sleep")
        self._last = now
        try:
            locked = self._is_locked()
        except Exception:  # noqa: BLE001
            locked = False
        if locked and not self._was_locked:
            self.manager.lock("system-lock")
        self._was_locked = locked

    def start(self) -> None:
        if self._thread is not None:
            return
        self._last = self._clock()

        def run() -> None:
            while not self._stop.wait(self.interval):
                self.tick()

        self._thread = threading.Thread(
            target=run, name="ghost-facelock-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Enforce the lock on the desktop UI's API calls
# ---------------------------------------------------------------------------

# What the lock screen itself needs while locked.
GATE_ALLOW_PREFIXES = (
    "/v1/security/facelock/",
    "/v1/stonic/flags",
    # The desktop shell pushes keyring keys at startup; refusing it would leave
    # the backend keyless after the owner unlocks.
    "/v1/cloud/reload",
    # The desktop shell's awareness sensor: write-only, nothing comes back.
    # Refusing it would make awareness miss the unlock it should notice.
    "/v1/awareness/signals",
)


def gate_blocks(path: str, *, locked: bool, loopback: bool) -> bool:
    """Whether a request is refused while locked. Pure.

    Only loopback requests — the desktop UI on this PC — are gated. Remote
    clients (the phone app) present Ghost's API key, a separate credential,
    and are not affected by the desktop being locked.
    """
    if not locked or not loopback:
        return False
    if not (path.startswith("/v1/") or path.startswith("/api/")):
        return False
    return not any(path.startswith(p) for p in GATE_ALLOW_PREFIXES)


class FaceLockGate:
    """ASGI middleware: 423 for the desktop UI's API calls while locked."""

    def __init__(self, app: Any, get_manager: Callable[[], Optional[FaceLockManager]]):
        self.app = app
        self.get_manager = get_manager

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope.get("type") == "http":
            manager = self.get_manager()
            if manager is not None and manager.is_locked():
                from starlette.requests import Request  # noqa: PLC0415
                from starlette.responses import JSONResponse  # noqa: PLC0415

                from ghost.server.auth_middleware import (  # noqa: PLC0415
                    is_trusted_loopback_request,
                )

                request = Request(scope)
                if gate_blocks(
                    scope.get("path", ""),
                    locked=True,
                    loopback=is_trusted_loopback_request(request),
                ):
                    response = JSONResponse(
                        {"ok": False, "error": "locked", "message": "Ghost is locked"},
                        status_code=423,
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Process-wide instance (created only when the flag is on)
# ---------------------------------------------------------------------------

_manager: Optional[FaceLockManager] = None
_watcher: Optional[SessionWatcher] = None
_init_lock = threading.Lock()


def get_manager() -> Optional[FaceLockManager]:
    return _manager


def ensure_manager(config_dir: Optional[Path] = None) -> FaceLockManager:
    global _manager, _watcher
    with _init_lock:
        if _manager is None:
            if config_dir is None:
                from ghost.core.paths import get_config_dir  # noqa: PLC0415

                config_dir = get_config_dir()
            _manager = FaceLockManager(config_dir)
            _watcher = SessionWatcher(_manager)
            _watcher.start()
        return _manager


def reset_for_tests() -> None:
    global _manager, _watcher
    with _init_lock:
        if _watcher is not None:
            _watcher.stop()
        _manager = None
        _watcher = None
