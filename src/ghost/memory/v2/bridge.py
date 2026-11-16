"""Memory v2 inside Ghost: the flag, the fact-store backend, migration, rollback.

Everything here is behind ``[stonic].memory_v2`` (off by default). With the
flag off Ghost's memory is exactly what it was: ``ghost.memory``'s JSONL fact
store, its service and its CLI.

With the flag on:

* ``V2FactStore`` becomes the fact store behind the UNCHANGED ``FactStore``
  interface (``ghost.memory.service`` and every caller keep working). It
  DUAL-WRITES: the v1 JSONL stays the system of record for the legacy API
  (same dedupe, quarantine, trust tiers, indices), and every recallable fact is
  mirrored into Memory Core. ``list()`` returns the v1 facts and then the
  memories only Memory Core knows (from the digest, ``remember``, imports), so
  context injection gets the richer memory through the same old path.
* Forward migration (once): the v1 JSONL is copied to
  ``memory_v2/backups/pre-v2-memory_facts-<time>.jsonl`` and every recallable
  fact is imported into Memory Core (``origin=imported``).
* Rollback: ``rollback_to_v1()`` writes every v2-only active memory into the
  v1 JSONL (dedupe, ``source=memory_v2``), then the flag can go off with
  nothing lost. Memory Core's file is untouched, so turning v2 back on later
  picks up where it was. The schema itself only ever adds (``schema.py``), so
  an older Ghost opens a newer file too.
* Memory Core runs in the server: chat exchanges are recorded as turns
  (channel "chat", one session per source per day), the digest distils them
  after 15 quiet minutes with Ghost's own model, and awareness gets the memory
  card and due promises.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ghost.memory.store import (
    RECALLABLE_TRUST_TIERS,
    TRUST_AUTO,
    TRUST_TRUSTED,
    TRUST_UNTRUSTED,
    Fact,
    FactStore,
    LocalFactStore,
)
from ghost.memory.v2.normalize import content_hash

logger = logging.getLogger(__name__)

V1_CREATOR = "ghost_v1"
_META_IMPORTED = "ghost_v1_imported_at"
_META_ROLLED_BACK = "ghost_v1_rolled_back_at"
TICK_S = 60.0
#: A raised promise is not offered to awareness again for this long.
PROMISE_RESURFACE_MS = 2 * 60 * 60 * 1000


def _config_dir() -> Path:
    from ghost.core.paths import get_config_dir

    return get_config_dir()


def v2_dir() -> Path:
    return _config_dir() / "memory_v2"


_app_state: Any = None
_disk_flag: Dict[str, Any] = {"at": 0.0, "on": False}


def memory_v2_enabled(config: Any = None) -> bool:
    """``[stonic].memory_v2``: from *config*, else the running server's config,
    else ``config.toml`` (cached for 2 s)."""
    if config is None and _app_state is not None:
        config = getattr(_app_state, "config", None)
    if config is None:
        if time.monotonic() - _disk_flag["at"] < 2.0:
            return bool(_disk_flag["on"])
        try:
            from ghost.core.config import load_config

            config = load_config()
        except Exception:  # noqa: BLE001 — no config = off
            config = None
        stonic = getattr(config, "stonic", None)
        _disk_flag.update(
            at=time.monotonic(), on=bool(getattr(stonic, "memory_v2", False))
        )
        return bool(_disk_flag["on"])
    stonic = getattr(config, "stonic", None)
    return bool(getattr(stonic, "memory_v2", False))


# -- the shared Memory Core -------------------------------------------------------

_core = None
_core_lock = threading.Lock()


def get_core(base_dir: Optional[Path] = None):
    """The process-wide Memory Core service (opened on first use, never raises)."""
    global _core
    from ghost.memory.v2.service import MemoryCoreService

    with _core_lock:
        if _core is None:
            _core = MemoryCoreService(base_dir or v2_dir())
            _core.init()
        return _core


def reset_core_for_tests() -> None:
    global _core, _app_state, _notes
    _app_state = None
    _notes = None
    _disk_flag.update(at=0.0, on=False)
    _subscribed.clear()
    with _core_lock:
        if _core is not None:
            _core.shutdown()
        _core = None
    _stop_ticker()


_notes = None


def get_notes():
    """The file-memory folder (``memory_v2/notes``), created on first use."""
    global _notes
    from ghost.memory.v2.files.store import FileMemory

    with _core_lock:
        if _notes is None:
            _notes = FileMemory(v2_dir() / "notes").ensure()
        return _notes


def migrate_notes() -> Dict[str, Any]:
    """Stonic's one-time move into the notes folder: Memory Core, Ghost's
    MEMORY.md / USER.md and the v1 facts, backed up first."""
    from ghost.memory.v2.files.migrate import run_migration

    config_dir = _config_dir()
    core = get_core()
    sources = {
        "core_db": core.base_dir / "memory.db"
        if (core.base_dir / "memory.db").exists()
        else None,
        "memory_md": config_dir / "MEMORY.md",
        "user_md": config_dir / "USER.md",
        "facts_jsonl": _default_facts_path(),
    }
    if core.ready and core.db is not None:
        try:
            core.db.execute("pragma wal_checkpoint(PASSIVE)")
        except Exception:  # noqa: BLE001
            pass
    return run_migration(get_notes(), sources)


def voice_memory_card(character_id: Optional[str] = None) -> str:
    """The voice's state card (pins, profile, promises, last time) for its prompt."""
    if not memory_v2_enabled():
        return ""
    core = get_core()
    if not core.ensure():
        return ""
    return str(
        core.context_block("voice", character_id=character_id, with_rules=False).get(
            "text"
        )
        or ""
    )


# -- the FactStore backend -----------------------------------------------------------


def _trust_of(row: Dict[str, Any]) -> str:
    if row.get("status") == "candidate":
        return TRUST_AUTO
    return TRUST_TRUSTED if row.get("origin") == "manual" else TRUST_AUTO


class V2FactStore(FactStore):
    """The legacy ``FactStore`` API over v1 JSONL + Memory Core (dual write)."""

    def __init__(
        self, path: str | Path | None = None, max_facts: int = 1000, *, core: Any = None
    ) -> None:
        self._v1 = LocalFactStore(path, max_facts=max_facts)
        self._core = core or get_core()
        self._lock = threading.RLock()
        if self._ready:
            migrate_forward(self._v1, self._core)

    @property
    def _ready(self) -> bool:
        return bool(self._core and self._core.ensure() and self._core.store)

    @property
    def path(self) -> Path:
        return self._v1.path

    # -- mirroring -------------------------------------------------------------------

    def _mirror(self, text: str, source: str, trust: str) -> None:
        if not self._ready:
            return
        store = self._core.store
        digest = content_hash(text)
        existing = store._one(
            "select id, status from memories where content_hash = ? and deleted = 0 order by id limit 1",
            (digest,),
        )
        trust = (trust or "").strip().lower()
        if trust not in RECALLABLE_TRUST_TIERS:
            # Quarantined: never recallable. If a recallable copy exists, retract it.
            if existing and existing["status"] in ("active", "candidate"):
                store.forget_memory(
                    int(existing["id"]), actor="system", cause="quarantined"
                )
            return
        if existing:
            if trust == TRUST_TRUSTED:
                store.update_memory(
                    int(existing["id"]), {}, actor="user", cause="trusted"
                )
            return
        store.add_memory(
            text=text, origin="manual" if trust == TRUST_TRUSTED else "imported", status="active",
            trust_tier=1 if trust == TRUST_TRUSTED else 2, created_by=V1_CREATOR, actor=V1_CREATOR,
            source_session=None, cause=f"fact:{source or 'auto'}", user_requested=trust == TRUST_TRUSTED,
        )  # fmt: skip

    def _v2_only(self) -> List[Dict[str, Any]]:
        if not self._ready:
            return []
        return self._core.store._all(
            "select * from memories where deleted = 0 and status = 'active' and scope = 'user' "
            "and coalesce(created_by, '') != ? order by id",
            (V1_CREATOR,),
        )

    # -- FactStore API -----------------------------------------------------------------

    def add(self, text: str, source: str = "", trust: str = "") -> bool:
        with self._lock:
            added = self._v1.add(text, source=source, trust=trust)
            try:
                self._mirror((text or "").strip(), source, trust)
            except Exception:  # noqa: BLE001 — Memory Core never breaks the v1 write
                logger.debug("memory v2 mirror failed", exc_info=True)
            return added

    def add_with_trust(self, text: str, source: str = "", trust: str = "") -> bool:
        return self.add(text, source=source, trust=trust)

    def set_trust(self, index: int, trust: str) -> bool:
        with self._lock:
            facts = self._v1.list()
            if 0 <= index < len(facts):
                ok = self._v1.set_trust(index, trust)
                if ok:
                    self._mirror(facts[index].text, facts[index].source, trust)
                return ok
            trust = (trust or "").strip().lower()
            if trust not in {TRUST_AUTO, TRUST_TRUSTED, TRUST_UNTRUSTED}:
                raise ValueError(f"Unknown fact trust tier: {trust!r}")
            extra = self._v2_only()
            j = index - len(facts)
            if not 0 <= j < len(extra):
                return False
            row = extra[j]
            if trust == TRUST_UNTRUSTED:
                return bool(
                    self._core.store.forget_memory(
                        int(row["id"]), actor="user", cause="untrusted"
                    ).get("ok")
                )
            return bool(
                self._core.store.update_memory(
                    int(row["id"]), {}, actor="user", cause="trusted"
                ).get("ok")
            )

    def promote_reviewed(self, index: int, expected_text: str) -> bool:
        with self._lock:
            facts = self._v1.list()
            if 0 <= index < len(facts):
                ok = self._v1.promote_reviewed(index, expected_text)
                if ok:
                    self._mirror(expected_text, facts[index].source, TRUST_TRUSTED)
                return ok
            extra = self._v2_only()
            j = index - len(facts)
            if not 0 <= j < len(extra) or extra[j]["text_original"] != expected_text:
                return False
            # v2-only memories are already recallable: nothing to promote.
            return False

    def list(self) -> List[Fact]:
        with self._lock:
            facts = self._v1.list()
            seen = {f.text.strip().lower() for f in facts}
            for row in self._v2_only():
                text = row["text_original"]
                if text.strip().lower() in seen:
                    continue
                seen.add(text.strip().lower())
                facts.append(
                    Fact(
                        text=text,
                        source="memory_v2",
                        created_at=(row["recorded_at"] or 0) / 1000,
                        trust=_trust_of(row),
                    )
                )
            return facts

    def clear(self) -> int:
        with self._lock:
            removed = self._v1.clear()
            if self._ready:
                for row in self._core.store.list_memories(
                    status=["active", "candidate"], limit=1000
                ):
                    self._core.store.forget_memory(
                        int(row["id"]), hard=True, actor="user", cause="clear"
                    )
                    removed += 0 if row["created_by"] == V1_CREATOR else 1
            return removed

    def count(self) -> int:
        return len(self.list())


def register_backend() -> None:
    from ghost.core.registry import FactStoreRegistry

    if not FactStoreRegistry.contains("v2"):
        FactStoreRegistry.register_value("v2", V2FactStore)


# -- migration and rollback ----------------------------------------------------------------


def migrate_forward(v1: LocalFactStore, core: Any) -> Dict[str, Any]:
    """Once: back up the v1 JSONL and import its recallable facts into Memory Core."""
    store = core.store
    if store is None:
        return {"ok": False, "reason": core.reason or "off"}
    if store.get_meta(_META_IMPORTED):
        return {"ok": True, "skipped": True}
    backup = None
    src = v1.path
    if src.exists():
        folder = core.base_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        backup = (
            folder
            / f"pre-v2-memory_facts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        )
        shutil.copyfile(src, backup)
    imported = 0
    skipped = 0
    with store.db.tx():
        for fact in v1.list():
            text = fact.text.strip()
            if not text or not fact.trusted_for_recall:
                skipped += 1
                continue
            trusted = (fact.trust or "").lower() == TRUST_TRUSTED
            made = store.add_memory(
                text=text, origin="manual" if trusted else "imported", status="active", trust_tier=1 if trusted else 2,
                created_by=V1_CREATOR, actor=V1_CREATOR, cause="migrate_forward",
                mentioned_at=int((fact.created_at or 0) * 1000) or None,
            )  # fmt: skip
            if made.get("ok") and not made.get("duplicate_of"):
                imported += 1
        store.set_meta(_META_IMPORTED, int(time.time() * 1000))
        store.set_meta("ghost_v1_backup", str(backup) if backup else "")
        store.audit(
            "system",
            "migrate_forward",
            "store",
            None,
            None,
            {"imported": imported, "skipped": skipped},
            str(src),
        )
    logger.info(
        "[Memory v2] imported %d v1 fact(s) (%d quarantined/empty left in v1)",
        imported,
        skipped,
    )
    return {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "backup": str(backup) if backup else None,
    }


def rollback_to_v1(
    facts_path: Optional[Path] = None, *, core: Any = None
) -> Dict[str, Any]:
    """Write every v2-only active memory into the v1 JSONL so the flag can go off with nothing lost."""
    core = core or get_core()
    if not core.ensure() or core.store is None:
        return {"ok": False, "reason": core.reason or "off"}
    v1 = LocalFactStore(facts_path or _default_facts_path(), max_facts=100_000)
    rows = core.store._all(
        "select text_original, origin, status from memories where deleted = 0 and status = 'active' and scope = 'user' "
        "and coalesce(created_by, '') != ? order by id",
        (V1_CREATOR,),
    )
    written = 0
    for row in rows:
        trust = TRUST_TRUSTED if row["origin"] == "manual" else TRUST_AUTO
        if v1.add(row["text_original"], source="memory_v2", trust=trust):
            written += 1
    core.store.set_meta(_META_ROLLED_BACK, int(time.time() * 1000))
    core.store.audit(
        "user",
        "rollback_to_v1",
        "store",
        None,
        None,
        {"written": written},
        str(v1.path),
    )
    return {
        "ok": True,
        "written": written,
        "considered": len(rows),
        "facts_path": str(v1.path),
    }


def _default_facts_path() -> Path:
    try:
        from ghost.core.config import load_config

        return Path(load_config().memory.facts_path).expanduser()
    except Exception:  # noqa: BLE001
        return _config_dir() / "memory_facts.jsonl"


# -- in the server ------------------------------------------------------------------------

_ticker: Optional[threading.Thread] = None
_ticker_stop = threading.Event()
_subscribed: set = set()


def _session_for(source: str, now_ms: int) -> str:
    day = datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
    return f"{(source or 'chat').replace(':', '_')}:{day}"


def record_exchange(
    core: Any, user_text: str, assistant_text: str = "", *, source: str = "chat"
) -> Dict[str, Any]:
    """One finished chat exchange -> two verbatim turns (secrets removed first)."""
    now = core.now()
    turns = [{"speaker": "user", "text": user_text, "ts": now}]
    if assistant_text:
        turns.append({"speaker": "assistant", "text": assistant_text, "ts": now + 1})
    return core.append_turns("chat", _session_for(source, now), turns)


def attach(app_state: Any) -> bool:
    """Start Memory Core for the server (flag on only). Safe to call more than once."""
    global _app_state
    _app_state = app_state
    config = getattr(app_state, "config", None)
    if not memory_v2_enabled(config):
        return False
    core = get_core()
    if not core.ensure():
        logger.warning("[Memory v2] not started: %s", core.reason)
        return False
    from ghost.memory.v2.llm_relay import make_engine_llm

    core.set_llm(
        make_engine_llm(
            lambda: getattr(app_state, "engine", None),
            lambda: getattr(app_state, "model", "") or "",
        )
    )
    bus = getattr(app_state, "bus", None)
    if bus is not None and id(bus) not in _subscribed:
        try:
            from ghost.core.events import EventType

            def on_exchange(event: Any) -> None:
                data = getattr(event, "data", None) or (
                    event if isinstance(event, dict) else {}
                )
                if not memory_v2_enabled():
                    return
                try:
                    record_exchange(
                        core,
                        data.get("user_text") or "",
                        data.get("assistant_text") or "",
                        source=data.get("source") or "chat",
                    )
                except Exception:  # noqa: BLE001 — memory never breaks chat
                    logger.debug("memory v2: exchange not recorded", exc_info=True)

            bus.subscribe(EventType.CHAT_EXCHANGE_COMPLETED, on_exchange)
            _subscribed.add(id(bus))
        except Exception:  # noqa: BLE001
            logger.debug("memory v2: event bus subscription failed", exc_info=True)
    core.sweep()
    _start_ticker(core)
    return True


def _start_ticker(core: Any) -> None:
    global _ticker
    if _ticker is not None and _ticker.is_alive():
        return
    _ticker_stop.clear()

    def loop() -> None:
        while not _ticker_stop.wait(TICK_S):
            if not memory_v2_enabled():
                continue
            try:
                core.tick()
                _offer_due_promises(core)
            except Exception:  # noqa: BLE001
                logger.debug("memory v2 tick failed", exc_info=True)

    _ticker = threading.Thread(target=loop, name="memory-v2", daemon=True)
    _ticker.start()


def _stop_ticker() -> None:
    _ticker_stop.set()


# -- awareness --------------------------------------------------------------------------------


def awareness_memory(
    core: Any, character_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The awareness mind's memory: its state card and the promises that are due."""
    if not memory_v2_enabled() or not core.ensure():
        return None
    card = core.context_block("awareness", character_id=character_id, with_rules=False)
    due = core.due_intentions(character_id=character_id)
    return {
        "card": card.get("text") or "",
        "due": due.get("text") or "",
        "due_ids": [int(i["id"]) for i in due.get("items") or []],
    }


def _offer_due_promises(core: Any) -> None:
    try:
        from ghost.daemon.awareness import service as aw
    except Exception:  # noqa: BLE001
        return
    svc = getattr(aw, "_service", None)
    if svc is None or not getattr(svc, "enabled", False) or not core.ready:
        return
    now = core.now()
    fresh = [
        i
        for i in core.store.due_intentions(now=now)
        if not i["last_surfaced_at"]
        or now - i["last_surfaced_at"] >= PROMISE_RESURFACE_MS
    ]
    if fresh:
        svc.engine.on_promise_due(len(fresh))


def attach_awareness(awareness_service: Any, core: Any = None) -> None:
    """Give the awareness engine memory, promises and its own record — each call
    checks the flag, so turning memory_v2 off restores Phase 5 behaviour."""
    core = core or get_core()
    engine = awareness_service.engine

    def raised(ids: List[int]) -> None:
        if memory_v2_enabled() and core.ensure():
            for ident in ids:
                core.store.mark_intention_surfaced(ident)

    def character_event(event: Dict[str, Any]) -> None:
        if memory_v2_enabled() and core.ensure():
            core.character_event(**event)

    engine.memory_fn = lambda character_id: awareness_memory(core, character_id)
    engine.promise_raised_fn = raised
    engine.character_event_fn = character_event


__all__ = [
    "V1_CREATOR",
    "V2FactStore",
    "attach",
    "attach_awareness",
    "awareness_memory",
    "get_core",
    "get_notes",
    "migrate_notes",
    "voice_memory_card",
    "memory_v2_enabled",
    "migrate_forward",
    "record_exchange",
    "register_backend",
    "reset_core_for_tests",
    "rollback_to_v1",
    "v2_dir",
]
