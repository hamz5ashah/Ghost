"""Memory Core's service — the one face for the rest of Ghost (Stonic port, phase 6).

Ported from Stonic's ``memory/service.js``. It joins store, search, state card,
digest and queue, and keeps two promises:

1. GHOST NEVER FAILS BECAUSE OF MEMORY. A missing driver, a damaged file,
   anything — ``init()`` never raises; the service goes ``ready=False`` and
   every method answers a clean "off". Callers fall back to their old path.
2. NOTHING HEAVY WHILE SPEAKING. Writes and searches take milliseconds. The
   digest (an LLM call) only runs from the queue, after a conversation, and
   waits while voice is busy.

Stonic kept one folder per signed-in Stonic account; Ghost has no accounts, so
there is one folder: ``<config>/memory_v2/``. Kill switch: a file named
``DISABLED`` in that folder turns memory off (support), the app runs as before.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ghost.memory.v2.config import (
    AGENT,
    DIGEST,
    HOUR,
    INTENTIONS,
    JOBS,
    LIMITS,
    MIN,
    MINDS,
    TURN_FLAGS,
)
from ghost.memory.v2.consolidate import run_maintenance
from ghost.memory.v2.context import MemoryContext
from ghost.memory.v2.db import (
    MemoryDb,
    backup_now,
    close_memory_db,
    list_backups,
    open_memory_db,
    quick_check,
)
from ghost.memory.v2.digest import MemoryDigest
from ghost.memory.v2.jobs import JobQueue
from ghost.memory.v2.llm_relay import LlmFn, LlmReply
from ghost.memory.v2.normalize import query_tokens
from ghost.memory.v2.recall_agent import RecallAgent
from ghost.memory.v2.search import MemorySearch
from ghost.memory.v2.store import MemoryStore, parse_json, session_key, word_overlap
from ghost.memory.v2.timefmt import due_text, now_ms, parse_when

logger = logging.getLogger(__name__)

MAINTENANCE_EVERY_MS = 6 * HOUR
_DEFER_LONG = re.compile(r"^llm:(blocked|no_key|auth)")
_DEFER = re.compile(
    r"^llm:(blocked|no_key|no_llm|network|auth|timeout|aborted|rate_limited)"
)


def _off(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "enabled": False, "reason": reason, **extra}


class MemoryCoreService:
    def __init__(
        self,
        base_dir: Path,
        *,
        enabled: bool = True,
        now: Optional[Callable[[], int]] = None,
        mirror_dir: Optional[Path] = None,
        character_name: str = "Ghost",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.enabled = enabled
        self.now = now or now_ms
        self.mirror_dir = Path(mirror_dir) if mirror_dir else None
        self.character_name = character_name
        self.ready = False
        self.reason = "not_started"
        self._tried = False
        self.db: Optional[MemoryDb] = None
        self.store: Optional[MemoryStore] = None
        self.search: Optional[MemorySearch] = None
        self.context: Optional[MemoryContext] = None
        self.jobs: Optional[JobQueue] = None
        self.digest: Optional[MemoryDigest] = None
        self.agent: Optional[RecallAgent] = None
        self.llm: Optional[LlmFn] = None
        #: While a voice session runs, the digest waits.
        self.busy = False
        self._remember_count: Dict[str, int] = {}
        self._follow_up_count: Dict[str, int] = {}
        self._ask_count: Dict[str, int] = {}
        self._ticking = threading.Lock()
        self._last_maintenance = 0
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # -- life ------------------------------------------------------------------

    def init(self) -> Dict[str, Any]:
        """Open the file. NEVER raises."""
        if self.ready:
            return {"ok": True}
        self._tried = True
        if not self.enabled:
            self.reason = "disabled"
            return _off("disabled")
        try:
            if (self.base_dir / "DISABLED").exists():
                self.reason = "disabled"
                return _off("disabled")
        except OSError:
            pass
        try:
            opened = open_memory_db(self.base_dir, now=self.now())
            self.db = opened.db
            if not quick_check(self.db):
                # Writing to a damaged file spreads the damage. Stay off; backups exist.
                close_memory_db(self.db)
                self.db = None
                self.reason = "corrupt"
                logger.error(
                    "[Memory] database health check failed — memory off. Backups: %s",
                    self.base_dir / "backups",
                )
                return _off("corrupt")
            self.store = MemoryStore(self.db, now=self.now)
            self.search = MemorySearch(self.store)
            self.context = MemoryContext(self.store, self.search)
            self.jobs = JobQueue(self.store)
            self.digest = MemoryDigest(self.store, self.search, self._call_llm)
            self.agent = RecallAgent(self.store, self.search, name=self.character_name)
            self.ready = True
            self.reason = ""
            if opened.migrated["applied"]:
                logger.info(
                    "[Memory] schema v%s -> v%s%s", opened.migrated["from"], opened.migrated["to"],
                    " (copy made first)" if opened.pre_migration_backup else "",
                )  # fmt: skip
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001 — memory never takes Ghost down
            self.reason = "open_failed"
            logger.error("[Memory] could not start (%s): %s", self.reason, exc)
            return _off(self.reason)

    def ensure(self) -> bool:
        if self.ready:
            return True
        if self._tried:
            return False
        return bool(self.init().get("ok"))

    def shutdown(self) -> None:
        """Never raises; safe to call twice."""
        self.ready = False
        self.reason = "stopped"
        self._tried = True
        try:
            close_memory_db(self.db)
        except Exception:  # noqa: BLE001
            pass
        self.db = None

    def set_llm(self, fn: Optional[LlmFn]) -> None:
        """The digest's model; None = digest off (turns are still written)."""
        self.llm = fn

    def set_busy(self, busy: bool) -> None:
        self.busy = bool(busy)

    def set_character_name(self, name: str) -> None:
        self.character_name = name or "Ghost"
        if self.agent:
            self.agent.name = self.character_name

    def on_change(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def _call_llm(self, system: str, user: str, max_tokens: int) -> LlmReply:
        from ghost.memory.v2.llm_relay import LlmError

        if not self.llm:
            raise LlmError("no_llm")
        return self.llm(system, user, max_tokens)

    def _changed(self, kind: str) -> None:
        event = {"kind": kind, "at": self.now()}
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001 — a listener never breaks a write
                logger.debug("memory change listener failed", exc_info=True)

    def _month_start(self) -> int:
        d = datetime.fromtimestamp(self.now() / 1000)
        return int(datetime(d.year, d.month, 1).timestamp() * 1000)

    def status(self) -> Dict[str, Any]:
        if not self.ensure():
            return {"ok": True, "enabled": False, "ready": False, "reason": self.reason}
        assert self.store and self.jobs
        backups = list_backups(self.base_dir)
        return {
            "ok": True,
            "enabled": True,
            "ready": True,
            "paused": self.store.is_paused(),
            "stats": self.store.stats(),
            "pinned": self.store.pinned_usage(),
            "digest": {
                "enabled": self.store.get_meta("digest_enabled", "1") == "1",
                "has_model": bool(self.llm),
                "spent_this_month_usd": round(
                    self.jobs.spent_since(self._month_start()), 4
                ),
                "monthly_cap_usd": float(
                    self.store.get_meta(
                        "digest_cap_usd", str(DIGEST["monthly_cap_usd"])
                    )
                    or 0
                ),
                "queue": self.jobs.counts(),
                "last_error": self.store.get_meta("digest_last_error", ""),
            },
            "backup": {
                "count": len(backups),
                "last_at": backups[0].at if backups else None,
                "last_check_ok": self.store.get_meta("last_backup_check_ok", ""),
                "mirror_dir": str(self.mirror_dir) if self.mirror_dir else None,
            },
            "folder": str(self.base_dir),
        }

    # -- writing: conversation -------------------------------------------------------

    def append_turns(
        self, channel: str, session_id: str, turns: List[Dict[str, Any]], **kw: Any
    ) -> Dict[str, Any]:
        """Conversation turns; afterwards the session's digest is queued (runs after quiet)."""
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        res = self.store.append_turns(channel, session_id, turns, **kw)
        if res.get("ok") and (res.get("added") or res.get("updated")):
            self._schedule_digest(res["session_key"])
            self._changed("turns")
        return res

    def session_ended(self, channel: str, session_id: str) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        key = self.store.end_session(channel, session_id)
        self._schedule_digest(key)
        for counter in (self._remember_count, self._follow_up_count, self._ask_count):
            counter.pop(key, None)
        return {"ok": True, "session_key": key}

    def _schedule_digest(self, key: str) -> None:
        assert self.store and self.jobs
        session = self.store.get_session(key)
        if not session or session["channel"] == "awareness":
            return
        # The key holds the digest mark (digested_upto): until digested, the same
        # job stays (its time moving on); once digested, new turns get a new job.
        self.jobs.enqueue(
            "digest",
            {"session_key": key},
            f"digest:{key}:{session['digested_upto']}:{DIGEST['prompt_version']}",
            run_after=self.now() + DIGEST["quiet_ms"],
        )

    # -- writing: "remember this" ------------------------------------------------------

    def remember(
        self,
        text: str,
        *,
        source: str = "voice",
        character_id: Optional[str] = None,
        channel: Optional[str] = None,
        session_id: Optional[str] = None,
        pin: bool = False,
        about: str = "",
        triggers: Optional[List[str]] = None,
        type: Optional[str] = None,  # noqa: A002 — Stonic's field name
        scope: str = "user",
        sensitivity: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Written at once and confirmed — no LLM on this path and no silent failure."""
        if not self.ensure():
            return {**_off(self.reason), "message": "Memory is off."}
        assert self.store
        if self.store.is_paused():
            return {
                "ok": False,
                "reason": "paused",
                "message": "Memory is paused, so this was not saved.",
            }
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not text:
            return {"ok": False, "reason": "empty", "message": "Nothing to remember."}
        key = session_key(channel, session_id) if channel and session_id else None
        if source != "user" and key:
            used = self._remember_count.get(key, 0)
            if used >= LIMITS["remember_per_session"]:
                return {
                    "ok": False,
                    "reason": "session_limit",
                    "message": "Too many things saved in this conversation already; this one was not saved.",
                }
            self._remember_count[key] = used + 1
        # Evidence: the user's recent sentence in this conversation this came from.
        evidence = self._evidence_for(text, key) if key else []
        origin = (
            "manual"
            if source == "user"
            else ("stated" if evidence else "assistant_tool")
        )
        about = str(about or "").strip()
        base = {
            "type": type, "text": text, "origin": origin, "scope": scope or "user", "sensitivity": sensitivity,
            "status": "active", "triggers": triggers or ([about] if pin and about else []), "evidence": evidence,
            "entities": [{"name": about, "type": "person"}] if about else [], "source_session": key,
            "created_by": source, "actor": source, "user_requested": True, "cause": "remember",
        }  # fmt: skip
        made = self.store.add_memory(**base, pinned=bool(pin))
        pin_full = False
        if not made.get("ok") and str(made.get("reason") or "").startswith("pin_limit"):
            # No pin room: it is still saved (unpinned), and the user is told.
            pin_full = True
            made = self.store.add_memory(**base, pinned=False)
        if not made.get("ok"):
            why = "It could not be saved."
            if made.get("reason") == "secret_only":
                why = "That looks like a password or code, which is never stored."
            if made.get("reason") == "too_long":
                why = (
                    f"Not saved: that is {len(text)} characters and one memory holds at most {LIMITS['memory_max_chars']}. "
                    "Save it as two or more memories, one topic each."
                )
            return {"ok": False, "reason": made.get("reason"), "message": why}
        self._changed("memories")
        m = self.store.get_memory(made["id"])
        message = (
            "Saved, but the always-remember list is full, so it is saved as a normal memory. They can free a slot in Settings."
            if pin_full
            else "Already remembered; noted again."
            if made.get("duplicate_of")
            else "Saved to memory."
        )
        return {
            "ok": True,
            "id": made["id"],
            "uid": m["uid"] if m else None,
            "pinned": bool(m and m["pinned"]),
            "duplicate": bool(made.get("duplicate_of")),
            "message": message,
        }

    def _evidence_for(self, text: str, key: Optional[str]) -> List[Dict[str, Any]]:
        assert self.store
        want = [t.fold for t in query_tokens(text)]
        if not want or not key:
            return []
        recent = self.store._all(
            "select id, text, fold from turns where session_id = ? and speaker = 'user' and (flags & ?) = 0 order by id desc limit 8",
            (key, TURN_FLAGS["automated"] | TURN_FLAGS["recalled"]),
        )
        best = None
        best_hits = 0
        for t in recent:
            words = set(t["fold"].split(" "))
            hits = sum(1 for w in want if w in words)
            if hits > best_hits:
                best, best_hits = t, hits
        # At least two words or a third of them — one common word is not evidence.
        if not best or best_hits < min(2, len(want)) or best_hits / len(want) < 0.34:
            return []
        return [{"turn_id": best["id"], "quote": best["text"][:300]}]

    def forget_by_query(
        self,
        query: str,
        *,
        query_en: str = "",
        mind: str = "voice",
        character_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ "Forget that." Only a CLEAR match is retracted; when in doubt, ask."""
        if not self.ensure():
            return {**_off(self.reason), "message": "Memory is off."}
        assert self.search and self.store
        found = self.search.recall(
            query,
            query_en=query_en,
            mind=mind,
            character_id=character_id,
            kinds=["memory"],
            k=3,
            purpose="answer",
        )
        items = found["items"]
        top = items[0] if items else None
        second = items[1] if len(items) > 1 else None
        clear = top and (
            found["status"] == "FOUND"
            or (
                top["strong"]
                and top["coverage"] >= 0.5
                and (not second or top["score"] >= 1.3 * second["score"])
            )
        )
        if not clear:
            return {
                "ok": False,
                "reason": "not_found",
                "message": "No clear match in memory. Ask them which memory they mean, or tell them they can remove it in Settings.",
            }
        self._hide_source_turns(top["id"])
        res = self.store.forget_memory(
            top["id"], hard=False, actor=mind, cause="forget_by_query"
        )
        if res.get("ok"):
            self._changed("memories")
        return {
            "ok": bool(res.get("ok")),
            "id": top["id"],
            "forgotten": top["text"],
            "message": f'Forgotten: "{top["text"]}". It will not come back.'
            if res.get("ok")
            else "Could not forget it.",
        }

    # -- writing: promises (follow-ups) ---------------------------------------------------

    def follow_up(
        self,
        what: str,
        *,
        when: str = "",
        done: bool = False,
        source: str = "voice",
        character_id: Optional[str] = None,
        channel: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save a promise now (the digest runs 15 min later — too late for "remind me in 10 minutes"),
        or close a finished one. ``when``: ``timefmt.parse_when`` forms; empty = no time."""
        if not self.ensure():
            return {**_off(self.reason), "message": "Memory is off."}
        assert self.store
        if self.store.is_paused():
            return {
                "ok": False,
                "reason": "paused",
                "message": "Memory is paused, so nothing was saved or changed.",
            }
        what = re.sub(r"\s+", " ", str(what or "")).strip()
        if not what:
            return {
                "ok": False,
                "reason": "empty",
                "message": "Nothing to follow up on.",
            }
        if done:
            return self._close_follow_up(what, source)
        now = self.now()
        when_raw = str(when or "").strip()
        due = parse_when(when_raw, now) if when_raw else None
        if when_raw and due is None:
            return {
                "ok": False,
                "reason": "bad_when",
                "message": (
                    f'Not saved: "{when_raw[:40]}" is not a time you can use (unreadable, or already past). Write it as '
                    '"+10m", "+2h", "today 17:00", "tomorrow 09:00", "fri 15:00" or "2026-10-12 10:00".'
                ),
            }
        key = session_key(channel, session_id) if channel and session_id else None
        if key:
            used = self._follow_up_count.get(key, 0)
            if used >= LIMITS["follow_ups_per_session"]:
                return {
                    "ok": False,
                    "reason": "session_limit",
                    "message": "Too many follow-ups were saved in this conversation already; this one was not saved.",
                }
            self._follow_up_count[key] = used + 1
        evidence = self._evidence_for(what, key) if key else []
        ev = evidence[0] if evidence else None
        made = self.store.add_intention(
            what=what, cue_type="time", due_ts=due,
            expiry_ts=due + INTENTIONS["expiry_after_due_ms"] if due else now + INTENTIONS["expiry_undated_ms"],
            source_turn=ev["turn_id"] if ev else None, source_session=key, quote=ev["quote"] if ev else "",
            created_by=source, character_id=character_id, actor=source, cause="follow_up",
        )  # fmt: skip
        if not made.get("ok"):
            return {
                "ok": False,
                "reason": made.get("reason"),
                "message": "It could not be saved.",
            }
        if made.get("duplicate_of"):
            return {
                "ok": True,
                "id": made["id"],
                "duplicate": True,
                "due_ts": due,
                "message": "Already on the follow-up list; nothing new was added.",
            }
        self._changed("intentions")
        return {
            "ok": True,
            "id": made["id"],
            "due_ts": due,
            "message": f"Saved. It is due {due_text(due, now)}."
            if due
            else "Saved to follow-ups, with no time set.",
        }

    def _close_follow_up(self, what: str, actor: str) -> Dict[str, Any]:
        """ "That's done." Only a CLEAR match closes; a tie or no good match closes nothing."""
        assert self.store
        scored = sorted(
            (
                (
                    max(
                        word_overlap(what, row["what"]),
                        word_overlap(what, row["what_en"] or ""),
                    ),
                    row,
                )
                for row in self.store.list_intentions(status="open", limit=200)
            ),
            key=lambda p: -p[0],
        )
        top = scored[0] if scored else None
        second = scored[1] if len(scored) > 1 else None
        if (
            not top
            or top[0] < INTENTIONS["same_overlap"]
            or (second and second[0] >= top[0])
        ):
            return {
                "ok": False,
                "reason": "not_found",
                "message": "No clear match among the open follow-ups, so nothing was closed. Ask them which one they mean, or tell them they can close it in Settings.",
            }
        row = top[1]
        res = self.store.resolve_intention(
            int(row["id"]), "done", actor=actor, cause="follow_up"
        )
        if res.get("ok"):
            self._changed("intentions")
        return {
            "ok": bool(res.get("ok")), "id": int(row["id"]), "closed": row["what"],
            "message": f'Closed: "{row["what"]}". You will not bring it up again.' if res.get("ok") else "Could not close it.",
        }  # fmt: skip

    # -- reading ---------------------------------------------------------------------------

    def recall(
        self, query: str, *, mind: str = "voice", for_prompt: bool = False, **kw: Any
    ) -> Dict[str, Any]:
        if not self.ensure():
            return {
                "ok": False,
                "enabled": False,
                "status": "NONE",
                "items": [],
                "text": "",
            }
        assert self.search and self.store and self.context
        mind = mind if mind in MINDS else "voice"
        result = self.search.recall(query, mind=mind, **kw)
        ids = [i["id"] for i in result["items"] if i["kind"] == "memory"]
        if ids:
            self.store.mark_used(ids, mind, False)
        return {
            "ok": True, "status": result["status"], "items": result["items"],
            "text": self.context.format_recall(result, mind=mind, for_prompt=for_prompt), "took_ms": result["took_ms"],
        }  # fmt: skip

    def ask(self, question: str, *, keywords: str = "", character_id: Optional[str] = None,
            channel: Optional[str] = None, session_id: Optional[str] = None, recent: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:  # fmt: skip
        """The Recall Agent with a search fallback: the answer text for the model."""
        if not self.ensure():
            return {**_off(self.reason), "text": ""}
        assert self.store and self.agent
        if not str(question or "").strip():
            return {"ok": False, "reason": "empty", "text": ""}
        use_agent = not self.store.is_paused() and self.llm is not None
        key = session_key(channel, session_id) if channel and session_id else None
        if use_agent and key:
            used = self._ask_count.get(key, 0)
            if used >= AGENT["asks_per_session"]:
                use_agent = False
            else:
                self._ask_count[key] = used + 1
        if use_agent:
            res = self.agent.ask(
                question,
                self.llm,
                keywords=keywords,
                character_id=character_id,
                now=self.now(),
                recent=recent,
            )
            if res.get("ok"):
                if res.get("memory_ids"):
                    self.store.mark_used(res["memory_ids"], "voice", True)
                return {**res, "via": "agent"}
        # Free, on this PC: lexical search.
        found = self.recall(
            " ".join(x for x in (question, keywords) if x),
            mind="voice",
            character_id=character_id,
        )
        return {**found, "via": "search"}

    def recall_usage(self, character_id: Optional[str] = None) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store and self.agent
        return {
            "ok": True,
            "paused": self.store.is_paused(),
            **self.agent.usage(character_id),
        }

    def context_block(
        self,
        mind: str = "voice",
        *,
        channel: Optional[str] = None,
        session_id: Optional[str] = None,
        **kw: Any,
    ) -> Dict[str, Any]:
        """The state card — for voice on connect, for chat prompts, for awareness before it thinks."""
        if not self.ensure():
            return {"ok": False, "enabled": False, "text": ""}
        assert self.context
        mind = mind if mind in MINDS else "voice"
        exclude = session_key(channel, session_id) if channel and session_id else None
        return {
            "ok": True,
            **self.context.state_card(
                mind, exclude_session=exclude, now=self.now(), **kw
            ),
        }

    def due_intentions(
        self,
        *,
        character_id: Optional[str] = None,
        ahead_ms: int = 0,
        surface: bool = False,
    ) -> Dict[str, Any]:
        """For awareness: promises whose time has come; ``surface`` counts it as raised."""
        if not self.ensure():
            return {"ok": False, "enabled": False, "text": "", "items": []}
        assert self.store and self.context
        if self.store.is_paused():
            return {"ok": True, "text": "", "items": []}
        block = self.context.due_block(
            character_id=character_id, now=self.now(), ahead_ms=ahead_ms
        )
        if surface:
            for ident in block["ids"]:
                self.store.mark_intention_surfaced(ident)
        return {
            "ok": True,
            "text": block["text"],
            "items": [
                i for i in (self.store.get_intention(x) for x in block["ids"]) if i
            ],
        }

    # -- the memory page (UI) ------------------------------------------------------------------

    def list_memories(self, **flt: Any) -> Dict[str, Any]:
        if not self.ensure():
            return {**_off(self.reason), "items": []}
        assert self.store
        return {
            "ok": True,
            "items": [
                {**m, "triggers": parse_json(m["triggers"], [])}
                for m in self.store.list_memories(**flt)
            ],
        }

    def memory_detail(self, memory_id: int) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        m = self.store.get_memory(memory_id)
        if not m:
            return {"ok": False, "reason": "not_found"}
        return {
            "ok": True, "memory": {**m, "triggers": parse_json(m["triggers"], [])}, "evidence": self.store.get_evidence(memory_id),
            "history": self.store.list_audit(target_kind="memory", target_id=memory_id, limit=30),
        }  # fmt: skip

    def update_memory(self, memory_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        res = self.store.update_memory(
            memory_id, patch or {}, actor="user", cause="edit"
        )
        if res.get("ok"):
            self._changed("memories")
        return res

    def set_pinned(self, memory_id: int, pinned: bool) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        res = self.store.set_pinned(memory_id, pinned, actor="user")
        if res.get("ok"):
            self._changed("memories")
        return res

    def _hide_source_turns(self, memory_id: int) -> int:
        """A memory with no evidence turn (the model noted it itself): find its source
        sentence in that conversation and hide it — only on a FIRM match."""
        assert self.store and self.search
        m = self.store.get_memory(memory_id)
        if (
            not m
            or not m["source_session"]
            or any(ev["turn_id"] for ev in self.store.get_evidence(memory_id))
        ):
            return 0
        found = self.search.recall(
            m["text_original"],
            query_en=m["text_en"],
            mind="hermes",
            kinds=["turn"],
            k=5,
        )
        ids = [
            it["id"]
            for it in found["items"]
            if it.get("session_key") == m["source_session"]
            and it.get("speaker") == "user"
            and it["strong"]
            and it["coverage"] >= 0.6
        ]
        return self.store.hide_turns(ids)

    def forget_memory(self, memory_id: int, *, hard: bool = False) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        if not hard:
            self._hide_source_turns(memory_id)
        res = self.store.forget_memory(
            memory_id, hard=hard, actor="user", cause="memory_page"
        )
        if res.get("ok"):
            self._changed("memories")
        return res

    def list_intentions(self, status: str = "open", limit: int = 100) -> Dict[str, Any]:
        if not self.ensure():
            return {**_off(self.reason), "items": []}
        assert self.store
        return {
            "ok": True,
            "items": self.store.list_intentions(status=status, limit=limit),
        }

    def add_intention(self, **i: Any) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        res = self.store.add_intention(
            **{**i, "created_by": i.get("created_by") or "user", "actor": "user"}
        )
        if res.get("ok"):
            self._changed("intentions")
        return res

    def resolve_intention(self, intention_id: int, status: str) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        res = self.store.resolve_intention(intention_id, status, actor="user")
        if res.get("ok"):
            self._changed("intentions")
        return res

    def list_sessions(self, **flt: Any) -> Dict[str, Any]:
        if not self.ensure():
            return {**_off(self.reason), "items": []}
        assert self.store
        return {"ok": True, "items": self.store.list_sessions(**flt)}

    def session_turns(self, key: str, limit: int = 500) -> Dict[str, Any]:
        if not self.ensure():
            return {**_off(self.reason), "items": []}
        assert self.store
        return {
            "ok": True, "session": self.store.get_session(key), "items": self.store.get_turns(key, limit=min(int(limit or 500), 2000)),
            "episodes": self.store._all("select * from episodes where session_id = ? order by start_ts", (key,)),
        }  # fmt: skip

    def set_paused(self, paused: bool) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        self.store.set_paused(bool(paused), "user")
        self._changed("settings")
        return {"ok": True, "paused": bool(paused)}

    def set_digest_enabled(self, enabled: bool) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        self.store.set_meta("digest_enabled", "1" if enabled else "0")
        self._changed("settings")
        return {"ok": True}

    # -- the character's record ----------------------------------------------------------------

    def character_event(self, **e: Any) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        return self.store.add_character_event(**e)

    def character_events(self, character_id: str, **flt: Any) -> Dict[str, Any]:
        if not self.ensure():
            return {**_off(self.reason), "items": []}
        assert self.store
        return {
            "ok": True,
            "items": self.store.list_character_events(character_id, **flt),
        }

    # -- export and backup ----------------------------------------------------------------------

    def export_to(self, file: Path) -> Dict[str, Any]:
        """The whole memory as one readable JSON file. The user owns their memory."""
        if not self.ensure():
            return _off(self.reason)
        assert self.store
        try:
            Path(file).write_text(
                json.dumps(self.store.export_all(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.store.audit("user", "export", "store", None, None, None, None)
            return {"ok": True, "file": str(file)}
        except OSError as exc:
            return {"ok": False, "reason": str(exc)}

    def backup(self) -> Dict[str, Any]:
        if not self.ensure():
            return _off(self.reason)
        assert self.db and self.store
        try:
            made = backup_now(self.db, self.base_dir, self.now(), self.mirror_dir)
            self.store.set_meta("last_backup_at", self.now())
            return {"ok": True, **made}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    # -- background ------------------------------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        """On every clock beat (and when a session ends). One job at a time; the digest waits for voice."""
        if not self.ensure() or not self._ticking.acquire(blocking=False):
            return {"ran": None}
        try:
            assert self.store and self.jobs and self.digest
            now = self.now()
            if now - self._last_maintenance >= MAINTENANCE_EVERY_MS and not self.busy:
                self._last_maintenance = now
                m = run_maintenance(self.store, self.jobs, self.base_dir, now)
                if not m["healthy"]:
                    logger.error("[Memory] health check failed — no backup made.")
                if m["promoted"] or m["expired"]:
                    self._changed("memories")
                if m["backup_wanted"]:
                    b = self.backup()
                    if not b.get("ok"):
                        logger.warning("[Memory] backup failed: %s", b.get("reason"))
            if self.store.is_paused() or self.busy:
                return {"ran": None}
            if self.store.get_meta("digest_enabled", "1") != "1" or not self.llm:
                return {"ran": None}
            # Spending caps — BEFORE a job is claimed.
            cap = float(
                self.store.get_meta("digest_cap_usd", str(DIGEST["monthly_cap_usd"]))
                or 0
            )
            if self.jobs.spent_since(self._month_start()) >= cap:
                return {"ran": None, "result": {"capped": "month"}}
            day = datetime.fromtimestamp(now / 1000)
            day_start = int(datetime(day.year, day.month, day.day).timestamp() * 1000)
            if self.jobs.ran_since(day_start, "digest") >= DIGEST["daily_call_cap"]:
                return {"ran": None, "result": {"capped": "day"}}
            job = self.jobs.claim(["digest"])
            if not job:
                return {"ran": None}
            key = job["payload"].get("session_key") or job["payload"].get("sessionKey")
            res = self.digest.digest_session(key, now)
            if res.get("ok"):
                self.jobs.complete(job["id"], cost_usd=res.get("cost_usd", 0))
                self.store.set_meta("digest_last_error", "")
                self.store.set_meta("last_digest_at", self.now())
                if (
                    res["memories"]
                    or res["updates"]
                    or res["intentions"]
                    or res["episodes"]
                ):
                    self._changed("memories")
                if not res.get("skipped"):
                    logger.info(
                        "[Memory] digest: %s -> %d memories, %d updates, %d promises, %d rejected",
                        key, res["memories"], res["updates"], res["intentions"], len(res["rejected"]),
                    )  # fmt: skip
            elif _DEFER.match(res.get("reason") or "") or res.get("reason") == "paused":
                # No key / no network / rate-limited: not the conversation's fault.
                # Retry later without counting an attempt.
                self.jobs.defer(
                    job["id"],
                    HOUR if _DEFER_LONG.match(res.get("reason") or "") else 10 * MIN,
                )
                self.store.set_meta("digest_last_error", res.get("reason") or "")
            else:
                self.store.bump_digest_attempt(key)
                state = self.jobs.fail(
                    job["id"], res.get("reason"), cost_usd=res.get("cost_usd", 0)
                )
                self.store.set_meta("digest_last_error", res.get("reason") or "error")
                logger.warning(
                    "[Memory] digest failed (%s) — %s",
                    res.get("reason"),
                    "given up" if state == "dead" else "will retry",
                )
            return {"ran": "digest", "result": res}
        except Exception as exc:  # noqa: BLE001 — the tick never takes Ghost down
            logger.error("[Memory] tick: %s", exc)
            return {"ran": None}
        finally:
            self._ticking.release()

    def sweep(self) -> int:
        """On launch: queue sessions whose digest was left behind (crash, close)."""
        if not self.ensure():
            return 0
        assert self.store and self.jobs
        n = 0
        for s in self.store.sessions_needing_digest(self.now(), 0):
            if s["channel"] == "awareness":
                continue
            r = self.jobs.enqueue(
                "digest", {"session_key": s["id"]}, f"digest:{s['id']}:{s['digested_upto']}:{DIGEST['prompt_version']}",
                run_after=max(self.now() + 2 * MIN, s["last_turn_at"] + DIGEST["quiet_ms"]),
            )  # fmt: skip
            if r["created"]:
                n += 1
        return n


__all__ = ["JOBS", "MAINTENANCE_EVERY_MS", "MemoryCoreService"]
