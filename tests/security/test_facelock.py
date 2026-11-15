"""Face Lock (Stonic port, Phase 4): matching, PIN, lockout, tickets, gate."""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from ghost.security import facelock as fl

DIM = fl.EMBEDDING_DIM


def _unit(seed: int):
    rnd = random.Random(seed)
    v = [rnd.gauss(0, 1) for _ in range(DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _near(base, amount: float, seed: int):
    """A vector at roughly ``1 - amount`` cosine from ``base``."""
    noise = _unit(seed)
    v = [b * (1 - amount) + n * amount for b, n in zip(base, noise)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


OWNER = _unit(1)
STRANGER = _unit(2)
TEMPLATES = [OWNER] + [_near(OWNER, 0.25, s) for s in range(10, 14)]
PIN = "482915"


class MemoryStore:
    def __init__(self, supported: bool = True):
        self.supported = supported
        self.files: dict = {}
        self.fail_writes = False

    def encryption_available(self):
        return self.supported

    def read_object(self, path: Path):
        return self.files.get(str(path))

    def write_object(self, path: Path, obj):
        if self.fail_writes:
            raise OSError("disk full")
        self.files[str(path)] = obj

    def delete(self, path: Path):
        self.files.pop(str(path), None)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _manager(tmp_path, store=None, clock=None):
    return fl.FaceLockManager(
        tmp_path, store=store or MemoryStore(), now=clock or Clock()
    )


def _enrolled(tmp_path, clock=None, store=None):
    store = store or MemoryStore()
    m = _manager(tmp_path, store, clock)
    assert m.enroll(TEMPLATES, PIN)["ok"]
    return m, store


def _probes(base, n=4, amount=0.15):
    return [_near(base, amount, 100 + i) for i in range(n)]


# -- vectors -------------------------------------------------------------------


class TestVectors:
    def test_normalize_rejects_bad_shapes(self):
        assert fl.normalize_embedding([1.0] * 127) is None
        assert fl.normalize_embedding([0.0] * DIM) is None
        assert fl.normalize_embedding([float("nan")] + [1.0] * 127) is None
        assert fl.normalize_embedding([True] * DIM) is None
        assert fl.normalize_embedding("x" * DIM) is None

    def test_normalize_gives_unit_length(self):
        v = fl.normalize_embedding([3.0] * DIM)
        assert abs(sum(x * x for x in v) - 1) < 1e-9

    def test_owner_matches_stranger_does_not(self):
        assert fl.match_probes(_probes(OWNER), TEMPLATES).ok
        result = fl.match_probes(_probes(STRANGER), TEMPLATES)
        assert not result.ok and result.reason == "no_match"

    def test_probe_count_limits(self):
        assert fl.match_probes(_probes(OWNER, 2), TEMPLATES).reason == "bad_input"
        assert fl.match_probes(_probes(OWNER, 9), TEMPLATES).reason == "bad_input"

    def test_mixed_faces_in_one_attempt_are_incoherent(self):
        # Someone blinks, then a photo of the owner is held up.
        probes = _probes(OWNER, 2) + _probes(STRANGER, 2)
        assert fl.match_probes(probes, TEMPLATES).reason == "incoherent"

    def test_one_bad_frame_cannot_decide(self):
        probes = _probes(OWNER, 4)
        probes[0] = _near(OWNER, 0.6, 999)
        assert fl.match_probes(probes, TEMPLATES).ok

    def test_templates_must_be_one_person(self):
        assert fl.prepare_templates(TEMPLATES)["ok"]
        assert fl.prepare_templates([OWNER, STRANGER, OWNER])["reason"] == "incoherent"
        assert fl.prepare_templates([OWNER, OWNER])["reason"] == "bad_input"


# -- PIN -----------------------------------------------------------------------


class TestPin:
    @pytest.mark.parametrize(
        "pin", ["123456", "000000", "111111", "12345", "1234567", "abcdef", "١٢٣٤٥٧"]
    )
    def test_weak_or_malformed_pins_are_refused(self, pin):
        assert not fl.is_valid_pin(pin)

    def test_good_pin(self):
        assert fl.is_valid_pin(PIN)

    def test_hash_round_trip_and_salt(self):
        a, b = fl.hash_pin(PIN), fl.hash_pin(PIN)
        assert a["salt"] != b["salt"] and a["hash"] != b["hash"]
        assert fl.verify_pin(PIN, a)
        assert not fl.verify_pin("482916", a)
        assert not fl.verify_pin(PIN, {"salt": "zz", "hash": "00"})
        assert not fl.verify_pin(None, a)


# -- lockout -------------------------------------------------------------------


class TestLockout:
    def test_five_free_then_growing_waits(self):
        state = {}
        for _ in range(4):
            state = fl.register_failure(state, 0)
            assert state["lockoutUntil"] == 0
        waits = []
        for _ in range(6):
            state = fl.register_failure(state, 0)
            waits.append(state["lockoutUntil"])
        assert waits == [30_000, 60_000, 300_000, 900_000, 900_000, 900_000]

    def test_clock_set_back_never_waits_longer_than_the_longest_step(self):
        assert fl.lockout_remaining({"lockoutUntil": 10**12}, 0) == 900_000


# -- manager -------------------------------------------------------------------


class TestManager:
    def test_off_by_default_and_never_locked(self, tmp_path):
        m = _manager(tmp_path)
        assert not m.is_enabled() and not m.is_locked()
        assert m.verify_face(_probes(OWNER))["reason"] == "not_enabled"

    def test_unsupported_without_encryption(self, tmp_path):
        m = _manager(tmp_path, MemoryStore(supported=False))
        assert m.enroll(TEMPLATES, PIN) == {"ok": False, "reason": "unsupported"}
        assert m.status()["supported"] is False

    def test_enroll_keeps_this_session_open_next_boot_is_locked(self, tmp_path):
        m, store = _enrolled(tmp_path)
        assert m.is_enabled() and not m.is_locked()
        reboot = _manager(tmp_path, store)
        assert reboot.is_locked()

    def test_status_never_exposes_templates_or_pin(self, tmp_path):
        m, _ = _enrolled(tmp_path)
        text = repr(m.status())
        assert "templates" not in text and "hash" not in text and "salt" not in text
        assert m.status()["templateCount"] == len(TEMPLATES)

    def test_face_unlock(self, tmp_path):
        m, store = _enrolled(tmp_path)
        m = _manager(tmp_path, store)
        events = []
        m.on_change(events.append)
        assert not m.verify_face(_probes(STRANGER))["ok"]
        assert m.is_locked()
        reply = m.verify_face(_probes(OWNER))
        assert reply["ok"] and reply["reason"] == "match"
        assert not m.is_locked()
        assert [e["type"] for e in events] == ["unlocked"]

    def test_bad_input_does_not_count_as_an_attempt(self, tmp_path):
        m, _ = _enrolled(tmp_path)
        m.verify_face([[1.0] * 3])
        assert m.status()["attemptsLeft"] == fl.MAX_FREE_ATTEMPTS

    def test_pin_fallback_and_lockout(self, tmp_path):
        clock = Clock()
        m, store = _enrolled(tmp_path, clock)
        m = _manager(tmp_path, store, clock)
        for _ in range(fl.MAX_FREE_ATTEMPTS):
            assert m.verify_pin("999999")["reason"] == "wrong_pin"
        # Locked out: even the right PIN waits, and the wait survives a reboot.
        assert m.verify_pin(PIN)["reason"] == "lockout"
        assert _manager(tmp_path, store, clock).verify_pin(PIN)["reason"] == "lockout"
        clock.t += 30_001
        assert m.verify_pin(PIN)["ok"]
        assert m.status()["attemptsLeft"] == fl.MAX_FREE_ATTEMPTS

    def test_manage_ticket_is_single_use_and_expires(self, tmp_path):
        clock = Clock()
        m, _ = _enrolled(tmp_path, clock)
        assert m.change_pin("736251", None)["reason"] == "verify_first"
        ticket = m.verify_pin(PIN, purpose="manage")["ticket"]
        assert m.change_pin("736251", ticket)["ok"]
        assert m.change_pin("846251", ticket)["reason"] == "verify_first"
        ticket = m.verify_face(_probes(OWNER), purpose="manage")["ticket"]
        clock.t += fl.TICKET_TTL_MS + 1
        assert m.disable(ticket)["reason"] == "verify_first"

    def test_re_enroll_needs_a_ticket(self, tmp_path):
        m, _ = _enrolled(tmp_path)
        assert m.enroll(TEMPLATES, PIN)["reason"] == "verify_first"
        ticket = m.verify_pin(PIN, purpose="manage")["ticket"]
        assert m.enroll(TEMPLATES, "736251", ticket)["ok"]

    def test_disable_removes_the_file(self, tmp_path):
        m, store = _enrolled(tmp_path)
        ticket = m.verify_pin(PIN, purpose="manage")["ticket"]
        assert m.disable(ticket)["ok"]
        assert store.files == {}
        assert not _manager(tmp_path, store).is_locked()

    def test_lock_now_and_manage_ticket_cleared_on_lock(self, tmp_path):
        m, _ = _enrolled(tmp_path)
        ticket = m.verify_pin(PIN, purpose="manage")["ticket"]
        assert m.lock("manual") and m.is_locked()
        assert m.disable(ticket)["reason"] == "verify_first"

    def test_corrupt_file_means_no_lock(self, tmp_path):
        store = MemoryStore()
        store.files[str(tmp_path / fl.FILE_NAME)] = {"enabled": True, "templates": []}
        assert not _manager(tmp_path, store).is_locked()

    def test_failed_save_keeps_count_in_memory(self, tmp_path):
        m, store = _enrolled(tmp_path)
        store.fail_writes = True
        m.verify_pin("999999")
        assert m.status()["attemptsLeft"] == fl.MAX_FREE_ATTEMPTS - 1


# -- session watcher -----------------------------------------------------------


class TestSessionWatcher:
    def test_locks_on_windows_lock_and_sleep(self, tmp_path):
        m, _ = _enrolled(tmp_path)
        locked = {"v": False}
        wall = {"t": 0.0}
        w = fl.SessionWatcher(m, is_locked=lambda: locked["v"], clock=lambda: wall["t"])
        wall["t"] = 2
        w.tick()
        assert not m.is_locked()
        locked["v"] = True
        wall["t"] = 4
        w.tick()
        assert m.is_locked()

        m.verify_pin(PIN)
        locked["v"] = False
        wall["t"] = 6
        w.tick()
        wall["t"] = 600  # thread woke ten minutes late: the PC slept
        w.tick()
        assert m.is_locked()


# -- gate ----------------------------------------------------------------------


class TestGate:
    def test_blocks_only_loopback_api_calls_while_locked(self):
        assert fl.gate_blocks("/v1/chat/completions", locked=True, loopback=True)
        assert fl.gate_blocks("/api/offer", locked=True, loopback=True)
        assert not fl.gate_blocks("/v1/chat/completions", locked=False, loopback=True)
        # The phone app uses its API key and is not locked out.
        assert not fl.gate_blocks("/v1/chat/completions", locked=True, loopback=False)
        # The UI's own files and health stay reachable.
        assert not fl.gate_blocks("/", locked=True, loopback=True)
        assert not fl.gate_blocks("/health", locked=True, loopback=True)

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/security/facelock/status",
            "/v1/security/facelock/verify-pin",
            "/v1/stonic/flags",
            "/v1/cloud/reload",
        ],
    )
    def test_lock_screen_routes_pass(self, path):
        assert not fl.gate_blocks(path, locked=True, loopback=True)
