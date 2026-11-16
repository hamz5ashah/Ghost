"""Memory Core's store — the ONLY door for writing and reading (Stonic port, phase 6).

Ported from Stonic's ``memory/store.js``. Nothing writes to the database except
through here; search, digest and the service all call these methods. Every
write needs four things, and in one place they are never forgotten:

1. secrets removed      (``secrets.py`` — password, OTP, card)
2. search shapes        (``normalize.py`` — fold and skel)
3. an audit entry       (``memory_events`` — who, what, why)
4. limits               (``config.py`` — pin count, length)

Three things never happen here:

* A memory is never ERASED (except the user's "erase completely"). A change
  supersedes it, a mistake retracts it — both stay in history.
* Nothing is silently cut at a limit. Over the pin limit a write is REFUSED
  and the reason comes back, so the user knows.
* After "forget", the same thing cannot come back (tombstone) — unless the
  user says it again themselves.
"""

from __future__ import annotations

import json
import re
import secrets as _rand
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ghost.memory.v2.config import (
    AUTOMATED_PREFIXES,
    CHANNELS,
    DIGEST,
    LIMITS,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    ORIGINS,
    SPEAKERS,
    TURN_FLAGS,
)
from ghost.memory.v2.db import MemoryDb
from ghost.memory.v2.normalize import (
    content_hash,
    fold_text,
    normalize_for_search,
    query_tokens,
    skeleton,
)
from ghost.memory.v2.secrets import redact_secrets
from ghost.memory.v2.timefmt import now_ms

Row = Dict[str, Any]


def uid(prefix: str) -> str:
    return f"{prefix}_{_rand.token_hex(8)}"


def clip(text: str, limit: int) -> str:
    return text[:limit] if len(text) > limit else text


def clean(text: object) -> str:
    return re.sub(r"\s+", " ", "" if text is None else str(text)).strip()


def parse_json(raw: object, fallback: Any) -> Any:
    try:
        v = json.loads(raw) if isinstance(raw, (str, bytes)) else None
        return fallback if v is None else v
    except (ValueError, TypeError):
        return fallback


def session_key(channel: str, session_id: str) -> str:
    return f"{channel}:{session_id}"


def word_overlap(a: str, b: str) -> float:
    """Content-word overlap of two sentences (0..1), relative to the shorter.

    A one-word sentence only matches a one-word sentence like it — a lone
    "call" must not match every promise with a call in it.
    """
    wa = {t.fold for t in query_tokens(a)}
    wb = {t.fold for t in query_tokens(b)}
    if not wa or not wb:
        return 0.0
    hits = len(wa & wb)
    if min(len(wa), len(wb)) < 2:
        return 1.0 if len(wa) == len(wb) and hits == len(wa) else 0.0
    return hits / min(len(wa), len(wb))


def scopes_for(character_id: Optional[str]) -> List[str]:
    """What a character sees: everyone's shared memories + its own."""
    return ["user", f"character:{character_id}"] if character_id else ["user"]


_ONLY_SECRETS = re.compile(r"^(\[redacted [a-z_]+\]\s*)+$")


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


class MemoryStore:
    def __init__(self, db: MemoryDb, now: Optional[Callable[[], int]] = None) -> None:
        self.db = db
        self.now = now or now_ms

    # -- helpers -------------------------------------------------------------

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Row]:
        row = self.db.one(sql, tuple(params))
        return dict(row) if row is not None else None

    def _all(self, sql: str, params: Sequence[Any] = ()) -> List[Row]:
        return [dict(r) for r in self.db.all(sql, tuple(params))]

    def _run(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.db.execute(sql, tuple(params))

    # -- meta ------------------------------------------------------------------

    def get_meta(self, key: str, fallback: Optional[str] = None) -> Optional[str]:
        row = self._one("select value from meta where key = ?", (key,))
        return row["value"] if row else fallback

    def set_meta(self, key: str, value: object) -> None:
        self._run(
            "insert into meta(key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
            (key, str(value)),
        )

    def is_paused(self) -> bool:
        """Paused: nothing is written and nothing is offered as a memory."""
        return self.get_meta("paused", "0") == "1"

    def set_paused(self, paused: bool, actor: str = "user") -> None:
        self.set_meta("paused", "1" if paused else "0")
        self.audit(
            actor, "pause" if paused else "resume", "store", None, None, None, None
        )

    # -- audit -----------------------------------------------------------------

    def audit(
        self,
        actor: str,
        op: str,
        target_kind: str,
        target_id: object,
        before: Any,
        after: Any,
        cause: Any,
    ) -> None:
        def pack(v: Any) -> Optional[str]:
            if v is None:
                return None
            return clip(
                v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 2000
            )

        self._run(
            "insert into memory_events(ts, actor, op, target_kind, target_id, before, after, cause) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.now(),
                str(actor or "system"),
                op,
                target_kind,
                None if target_id is None else str(target_id),
                pack(before),
                pack(after),
                clip(str(cause), 500) if cause else None,
            ),
        )

    def list_audit(
        self,
        limit: int = 100,
        target_kind: Optional[str] = None,
        target_id: object = None,
    ) -> List[Row]:
        if target_kind and target_id is not None:
            return self._all(
                "select * from memory_events where target_kind = ? and target_id = ? order by id desc limit ?",
                (target_kind, str(target_id), limit),
            )
        return self._all(
            "select * from memory_events order by id desc limit ?", (limit,)
        )

    # -- sessions and turns (THE RECORD) -----------------------------------------

    def append_turns(
        self,
        channel: str,
        session_id: str,
        turns: Iterable[Dict[str, Any]],
        *,
        character_id: Optional[str] = None,
        title: Optional[str] = None,
        external: bool = False,
    ) -> Dict[str, Any]:
        """Write conversation turns. The same message (``source_ref``) is written
        once; if it comes back LONGER (a partial transcript completed) it grows."""
        channel = channel if channel in CHANNELS else ""
        sid = clean(session_id)
        out: Dict[str, Any] = {
            "ok": False, "session_key": "", "added": 0, "updated": 0, "skipped": 0, "redacted": 0, "turn_ids": [],
        }  # fmt: skip
        if not channel or not sid:
            return out
        out["session_key"] = session_key(channel, sid)
        if self.is_paused():
            out.update(ok=True, paused=True)
            return out
        char = clip(clean(character_id), 80) if character_id else None
        now = self.now()
        with self.db.tx():
            last_ts = 0
            for t in turns:
                speaker = (
                    t.get("speaker") if t and t.get("speaker") in SPEAKERS else None
                )
                raw = str(
                    "" if not t or t.get("text") is None else t.get("text")
                ).strip()
                if not speaker or not raw:
                    out["skipped"] += 1
                    continue
                flags = int(_num(t.get("flags")))
                red = redact_secrets(raw)
                text = red.text
                if red.found:
                    flags |= TURN_FLAGS["redacted"]
                    out["redacted"] += len(red.found)
                if len(text) > LIMITS["turn_max_chars"]:
                    text = text[: LIMITS["turn_max_chars"]]
                    flags |= TURN_FLAGS["truncated"]
                if any(text.startswith(p) for p in AUTOMATED_PREFIXES):
                    flags |= TURN_FLAGS["automated"]
                if external:
                    flags |= TURN_FLAGS["external"]
                ts = int(_num(t.get("ts"))) if _num(t.get("ts")) > 0 else now
                ref = (
                    clip(clean(t.get("source_ref")), 200)
                    if t.get("source_ref")
                    else None
                )
                norm = normalize_for_search(text)
                last_ts = max(last_ts, ts)
                self._ensure_session(out["session_key"], channel, char, title, ts)
                if ref:
                    existing = self._one(
                        "select id, text from turns where channel = ? and source_ref = ?",
                        (channel, ref),
                    )
                    if existing:
                        if existing["text"] != text and len(text) > len(
                            existing["text"]
                        ):
                            self._run(
                                "update turns set text = ?, fold = ?, skel = ?, script = ?, flags = flags | ? where id = ?",
                                (
                                    text,
                                    norm.fold,
                                    norm.skel,
                                    norm.script,
                                    flags,
                                    existing["id"],
                                ),
                            )
                            out["updated"] += 1
                            out["turn_ids"].append(int(existing["id"]))
                        else:
                            out["skipped"] += 1
                        continue
                cur = self._run(
                    "insert into turns(session_id, ts, speaker, channel, character_id, text, fold, skel, script, source_ref, flags, created_at) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        out["session_key"],
                        ts,
                        speaker,
                        channel,
                        char,
                        text,
                        norm.fold,
                        norm.skel,
                        norm.script,
                        ref,
                        flags,
                        now,
                    ),
                )
                out["added"] += 1
                out["turn_ids"].append(int(cur.lastrowid))
            if out["added"]:
                self._run(
                    "update sessions set turn_count = turn_count + ?, last_turn_at = max(last_turn_at, ?), "
                    "external_content = max(external_content, ?), character_id = coalesce(?, character_id), ended_at = null "
                    "where id = ?",
                    (
                        out["added"],
                        last_ts or now,
                        1 if external else 0,
                        char,
                        out["session_key"],
                    ),
                )
        out["ok"] = True
        return out

    def _ensure_session(
        self,
        key: str,
        channel: str,
        character_id: Optional[str],
        title: Optional[str],
        ts: int,
    ) -> None:
        self._run(
            "insert into sessions(id, channel, character_id, title, started_at, last_turn_at) values (?, ?, ?, ?, ?, ?) "
            "on conflict(id) do nothing",
            (
                key,
                channel,
                character_id,
                clip(clean(title), 200) if title else None,
                ts,
                ts,
            ),
        )

    def end_session(self, channel: str, session_id: str) -> str:
        key = session_key(channel, session_id)
        self._run("update sessions set ended_at = ? where id = ?", (self.now(), key))
        return key

    def get_session(self, key: str) -> Optional[Row]:
        return self._one("select * from sessions where id = ?", (key,))

    def list_sessions(
        self, limit: int = 50, offset: int = 0, channel: Optional[str] = None
    ) -> List[Row]:
        if channel:
            return self._all(
                "select * from sessions where channel = ? order by last_turn_at desc limit ? offset ?",
                (channel, limit, offset),
            )
        return self._all(
            "select * from sessions order by last_turn_at desc limit ? offset ?",
            (limit, offset),
        )

    def get_turns(self, key: str, after_id: int = 0, limit: int = 2000) -> List[Row]:
        return self._all(
            "select * from turns where session_id = ? and id > ? order by id limit ?",
            (key, after_id, limit),
        )

    def get_turn(self, turn_id: int) -> Optional[Row]:
        return self._one("select * from turns where id = ?", (turn_id,))

    def get_turn_window(self, turn_id: int, radius: int = 1) -> List[Row]:
        turn = self.get_turn(turn_id)
        if not turn:
            return []
        before = self._all(
            "select * from turns where session_id = ? and id < ? order by id desc limit ?",
            (turn["session_id"], turn["id"], radius),
        )
        after = self._all(
            "select * from turns where session_id = ? and id > ? order by id limit ?",
            (turn["session_id"], turn["id"], radius),
        )
        return list(reversed(before)) + [turn] + after

    def sessions_needing_digest(
        self, now: Optional[int] = None, quiet_ms: int = DIGEST["quiet_ms"]
    ) -> List[Row]:
        now = self.now() if now is None else now
        return self._all(
            "select s.* from sessions s where s.last_turn_at <= ? and s.digest_attempts < ? "
            "and exists (select 1 from turns t where t.session_id = s.id and t.id > s.digested_upto) order by s.last_turn_at",
            (now - quiet_ms, DIGEST["max_attempts"]),
        )

    def mark_digested(self, key: str, upto_turn_id: int) -> None:
        self._run(
            "update sessions set digested_upto = max(digested_upto, ?), digest_attempts = 0 where id = ?",
            (upto_turn_id, key),
        )

    def bump_digest_attempt(self, key: str) -> None:
        self._run(
            "update sessions set digest_attempts = digest_attempts + 1 where id = ?",
            (key,),
        )

    # -- entities (people, places, projects) ---------------------------------------

    def upsert_entity(self, entity: Dict[str, Any]) -> Optional[int]:
        """The same entity if a name or alias matches, otherwise a new one."""
        name = clip(clean(entity.get("name") if entity else ""), 80)
        if not name:
            return None
        names = [name] + [
            a for a in (clip(clean(x), 80) for x in entity.get("aliases") or []) if a
        ]
        folds = list(dict.fromkeys(f for f in (fold_text(n) for n in names) if f))
        if not folds:
            return None
        ident = None
        for f in folds:
            row = self._one(
                "select entity_id from entity_aliases where alias_fold = ? limit 1",
                (f,),
            )
            if row:
                ident = int(row["entity_id"])
                break
        if ident is None:
            cur = self._run(
                "insert into entities(uid, canonical_name, type, created_at) values (?, ?, ?, ?)",
                (uid("e"), name, clean(entity.get("type")) or "other", self.now()),
            )
            ident = int(cur.lastrowid)
        for n in names:
            f = fold_text(n)
            if f:
                self._run(
                    "insert or ignore into entity_aliases(entity_id, alias_fold, alias_skel) values (?, ?, ?)",
                    (ident, f, skeleton(n)),
                )
        return ident

    def entity_names(self, memory_id: int) -> List[Row]:
        return self._all(
            "select e.canonical_name as name, a.alias_fold as alias from memory_entities me "
            "join entities e on e.id = me.entity_id left join entity_aliases a on a.entity_id = e.id where me.memory_id = ?",
            (memory_id,),
        )

    def entities_by_aliases(
        self, folds: Iterable[str], skels: Iterable[str] = ()
    ) -> List[int]:
        ids: Dict[int, None] = {}
        for f in folds:
            for r in self._all(
                "select entity_id from entity_aliases where alias_fold = ?", (f,)
            ):
                ids[int(r["entity_id"])] = None
        for s in skels:
            if not s or len(s) < 3:
                continue  # a short skeleton collides with too many words
            for r in self._all(
                "select entity_id from entity_aliases where alias_skel = ?", (s,)
            ):
                ids[int(r["entity_id"])] = None
        return list(ids)

    def memories_for_entities(
        self, entity_ids: Sequence[int], limit: int = 30
    ) -> List[int]:
        if not entity_ids:
            return []
        marks = ",".join("?" * len(entity_ids))
        rows = self._all(
            f"select distinct m.id, m.mentioned_at from memories m join memory_entities me on me.memory_id = m.id "
            f"where me.entity_id in ({marks}) and m.deleted = 0 order by m.mentioned_at desc limit ?",
            (*entity_ids, limit),
        )
        return [int(r["id"]) for r in rows]

    # -- memories ----------------------------------------------------------------------

    def _search_text(
        self, text: str, text_en: str, triggers: List[str], memory_id: Optional[int]
    ):
        extra: List[str] = []
        if memory_id:
            for r in self.entity_names(memory_id):
                extra += [x for x in (r["name"], r["alias"]) if x]
        return normalize_for_search(
            " ".join(x for x in [text, text_en, *triggers, *extra] if x)
        )

    def _norm_triggers(self, words: object) -> List[str]:
        out: List[str] = []
        for w in words if isinstance(words, list) else []:
            f = fold_text(clean(w))
            if f and len(f) >= 2 and f not in out:
                out.append(f)
            if len(out) >= LIMITS["trigger_words_per_memory"]:
                break
        return out

    def pinned_usage(self) -> Dict[str, int]:
        row = self._one(
            "select count(*) as n, coalesce(sum(length(text_original)), 0) as chars from memories "
            "where pinned = 1 and deleted = 0 and status = 'active'"
        ) or {"n": 0, "chars": 0}
        return {
            "count": int(row["n"]),
            "chars": int(row["chars"]),
            "max": LIMITS["pinned_max"],
            "max_chars": LIMITS["pinned_max_chars"],
        }

    def _pin_room(self, extra_chars: int) -> Optional[str]:
        use = self.pinned_usage()
        if use["count"] + 1 > use["max"]:
            return f"pin_limit_count:{use['max']}"
        if use["chars"] + extra_chars > use["max_chars"]:
            return f"pin_limit_chars:{use['max_chars']}"
        return None

    def add_memory(self, **m: Any) -> Dict[str, Any]:
        """A new memory. If the same thing is already known, the old one is reinforced.

        Returns {ok, id?, uid?, duplicate_of?, reason?}; reason is one of
        'empty' | 'too_long' | 'secret_only' | 'forgotten' | 'pin_limit_count:N' | 'pin_limit_chars:N'.
        """
        actor = m.get("actor") or "system"
        text = redact_secrets(clean(m.get("text"))).text
        if not text:
            return {"ok": False, "reason": "empty"}
        # Not cut: a half memory can mean the wrong thing. Refused with a reason.
        if len(text) > LIMITS["memory_max_chars"]:
            return {"ok": False, "reason": "too_long"}
        if _ONLY_SECRETS.match(text):
            return {"ok": False, "reason": "secret_only"}
        text_en = clip(
            redact_secrets(clean(m.get("text_en"))).text, LIMITS["memory_max_chars"]
        )
        mtype = m.get("type") if m.get("type") in MEMORY_TYPES else "other"
        origin = m.get("origin") if m.get("origin") in ORIGINS else "inferred"
        scope = clean(m.get("scope")) or "user"
        status = (
            m.get("status")
            if m.get("status") in MEMORY_STATUSES
            else ("candidate" if origin == "inferred" else "active")
        )
        digest = content_hash(text)
        now = self.now()
        explicit = origin in ("manual", "stated", "assistant_tool")

        if self.is_tombstoned(digest):
            # "Forget" was said. Only the user saying it again brings it back.
            if not (explicit and m.get("user_requested")):
                return {"ok": False, "reason": "forgotten"}
            self._run("delete from tombstones where content_hash = ?", (digest,))

        dup = self._one(
            "select id, status from memories where content_hash = ? and scope = ? and deleted = 0 "
            "and status in ('active', 'candidate') order by id limit 1",
            (digest, scope),
        )
        if dup:
            self.reinforce(
                int(dup["id"]),
                evidence=m.get("evidence") or [],
                source_session=m.get("source_session"),
                promote=status == "active",
            )
            if m.get("pinned"):
                self.set_pinned(int(dup["id"]), True, actor=actor)
            return {"ok": True, "id": int(dup["id"]), "duplicate_of": int(dup["id"])}

        if m.get("pinned"):
            full = self._pin_room(len(text))
            if full:
                return {"ok": False, "reason": full}

        triggers = self._norm_triggers(m.get("triggers"))
        norm = self._search_text(text, text_en, triggers, None)
        sal = m.get("salience") or {}
        conf = m.get("confidence")
        confidence = (
            max(0.0, min(1.0, float(conf)))
            if isinstance(conf, (int, float))
            else (0.6 if origin == "inferred" else 1.0)
        )
        with self.db.tx():
            cur = self._run(
                "insert into memories(uid, type, text_original, text_en, fold, skel, origin, trust_tier, confidence, "
                "evidence_count, session_count, scope, sensitivity, status, pinned, triggers, event_start, event_end, "
                "mentioned_at, recorded_at, valid_from, sal_explicit, sal_future, sal_emotion, sal_surprise, "
                "content_hash, source_session, created_by, prompt_version, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid("m"), mtype, text, text_en, norm.fold, norm.skel, origin,
                    int(_num(m.get("trust_tier"))) or (2 if origin == "inferred" else 1),
                    confidence, scope, "sensitive" if m.get("sensitivity") == "sensitive" else "normal", status,
                    1 if m.get("pinned") else 0, json.dumps(triggers, ensure_ascii=False),
                    m.get("event_start"), m.get("event_end"),
                    m.get("mentioned_at") or now, now, m.get("valid_from") or m.get("mentioned_at") or now,
                    _num(sal.get("explicit")) or (1 if explicit else 0), _num(sal.get("future")),
                    _num(sal.get("emotion")), _num(sal.get("surprise")),
                    digest, m.get("source_session"), m.get("created_by") or actor, m.get("prompt_version"), now,
                ),
            )  # fmt: skip
            memory_id = int(cur.lastrowid)
            self._add_evidence(memory_id, m.get("evidence"))
            entities = m.get("entities") or []
            if entities:
                for e in entities:
                    eid = self.upsert_entity(e)
                    if eid is not None:
                        self._run(
                            "insert or ignore into memory_entities(memory_id, entity_id) values (?, ?)",
                            (memory_id, eid),
                        )
                # Names join the search text: "Ammi" finds the "Mother" memory.
                with_names = self._search_text(text, text_en, triggers, memory_id)
                self._run(
                    "update memories set fold = ?, skel = ? where id = ?",
                    (with_names.fold, with_names.skel, memory_id),
                )
            self.audit(
                actor, "add", "memory", memory_id, None,
                {"type": mtype, "text": text, "origin": origin, "status": status, "scope": scope, "pinned": bool(m.get("pinned"))},
                m.get("cause"),
            )  # fmt: skip
        made = self.get_memory(memory_id)
        return {"ok": True, "id": memory_id, "uid": made["uid"] if made else None}

    def _add_evidence(self, memory_id: int, evidence: object) -> None:
        for ev in evidence if isinstance(evidence, list) else []:
            turn = (
                self.get_turn(ev.get("turn_id")) if ev and ev.get("turn_id") else None
            )
            self._run(
                "insert into memory_evidence(memory_id, turn_id, session_id, quote, created_at) values (?, ?, ?, ?, ?)",
                (
                    memory_id,
                    turn["id"] if turn else None,
                    turn["session_id"] if turn else (ev or {}).get("session_id"),
                    clip(clean((ev or {}).get("quote")), 400),
                    self.now(),
                ),
            )

    def reinforce(
        self,
        memory_id: int,
        *,
        evidence: Optional[list] = None,
        source_session: Optional[str] = None,
        promote: bool = False,
    ) -> bool:
        """Said again: evidence grows; from a new session, ``session_count`` too —
        and with enough sessions an inference becomes CERTAIN."""
        m = self.get_memory(memory_id)
        if not m:
            return False
        evidence = evidence or []
        sessions = {
            r["session_id"]
            for r in self._all(
                "select distinct session_id from memory_evidence where memory_id = ? and session_id is not null",
                (memory_id,),
            )
        }
        if m["source_session"]:
            sessions.add(m["source_session"])
        before = len(sessions)
        self._add_evidence(memory_id, evidence)
        for ev in evidence:
            turn = (
                self.get_turn(ev.get("turn_id")) if ev and ev.get("turn_id") else None
            )
            if turn:
                sessions.add(turn["session_id"])
        if source_session:
            sessions.add(source_session)
        count = max(int(m["session_count"] or 1), len(sessions), before)
        should_promote = m["status"] == "candidate" and (
            promote or count >= DIGEST["promote_after_sessions"]
        )
        confidence = (
            max(m["confidence"], 0.9)
            if should_promote
            else min(0.9, m["confidence"] + 0.1)
        )
        now = self.now()
        self._run(
            "update memories set evidence_count = evidence_count + 1, session_count = ?, mentioned_at = ?, "
            "confidence = ?, status = ?, updated_at = ? where id = ?",
            (
                count, now, confidence if m["origin"] == "inferred" else m["confidence"],
                "active" if should_promote else m["status"], now, memory_id,
            ),
        )  # fmt: skip
        if should_promote:
            self.audit(
                "system",
                "promote",
                "memory",
                memory_id,
                {"status": "candidate"},
                {"status": "active"},
                "evidence",
            )
        return True

    def get_memory(self, memory_id: int) -> Optional[Row]:
        return self._one(
            "select * from memories where id = ? and deleted = 0", (memory_id,)
        )

    def get_memory_by_uid(self, memory_uid: str) -> Optional[Row]:
        return self._one(
            "select * from memories where uid = ? and deleted = 0", (memory_uid,)
        )

    def find_memory_by_short_id(self, short: str) -> Optional[Row]:
        s = re.sub(r"[^a-z0-9_]", "", clean(short), flags=re.IGNORECASE)
        if len(s) < 6:
            return None
        like = f"{s}%" if s.startswith("m_") else f"m_{s}%"
        rows = self._all(
            "select * from memories where uid like ? and deleted = 0 limit 2", (like,)
        )
        return rows[0] if len(rows) == 1 else None

    def get_evidence(self, memory_id: int) -> List[Row]:
        return self._all(
            "select ev.*, t.text as turn_text, t.ts as turn_ts, t.speaker as turn_speaker "
            "from memory_evidence ev left join turns t on t.id = ev.turn_id where ev.memory_id = ? order by ev.id",
            (memory_id,),
        )

    def list_memories(
        self,
        status: object = None,
        type: Optional[str] = None,  # noqa: A002 — Stonic's filter name
        scopes: Optional[Sequence[str]] = None,
        pinned: Optional[bool] = None,
        limit: int = 200,
        offset: int = 0,
        order: str = "recent",
    ) -> List[Row]:
        where = ["deleted = 0"]
        args: List[Any] = []
        statuses = (
            list(status)
            if isinstance(status, (list, tuple))
            else [status]
            if status
            else ["active", "candidate"]
        )
        where.append(f"status in ({','.join('?' * len(statuses))})")
        args += statuses
        if type:
            where.append("type = ?")
            args.append(type)
        if scopes:
            where.append(f"scope in ({','.join('?' * len(scopes))})")
            args += list(scopes)
        if pinned is not None:
            where.append("pinned = ?")
            args.append(1 if pinned else 0)
        order_by = (
            "type, pinned desc, mentioned_at desc"
            if order == "type"
            else "pinned desc, mentioned_at desc"
        )
        return self._all(
            f"select * from memories where {' and '.join(where)} order by {order_by} limit ? offset ?",
            (*args, min(int(limit or 200), 1000), int(offset or 0)),
        )

    def update_memory(
        self,
        memory_id: int,
        patch: Dict[str, Any],
        *,
        actor: str = "user",
        cause: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The user's edit. A text change moves the hash, search shapes and index together."""
        m = self.get_memory(memory_id)
        if not m:
            return {"ok": False, "reason": "not_found"}
        text = (
            redact_secrets(clean(patch["text"])).text
            if patch.get("text") is not None
            else m["text_original"]
        )
        if not text:
            return {"ok": False, "reason": "empty"}
        if patch.get("text") is not None and len(text) > LIMITS["memory_max_chars"]:
            return {"ok": False, "reason": "too_long"}
        text_en = (
            clip(clean(patch["text_en"]), LIMITS["memory_max_chars"])
            if patch.get("text_en") is not None
            else m["text_en"]
        )
        triggers = (
            self._norm_triggers(patch["triggers"])
            if patch.get("triggers") is not None
            else parse_json(m["triggers"], [])
        )
        mtype = patch.get("type") if patch.get("type") in MEMORY_TYPES else m["type"]
        sensitivity = (
            patch.get("sensitivity")
            if patch.get("sensitivity") in ("sensitive", "normal")
            else m["sensitivity"]
        )
        scope = (
            (clean(patch["scope"]) or "user")
            if patch.get("scope") is not None
            else m["scope"]
        )
        norm = self._search_text(text, text_en, triggers, memory_id)
        by_user = 1 if actor == "user" else 0
        self._run(
            "update memories set text_original = ?, text_en = ?, fold = ?, skel = ?, type = ?, triggers = ?, "
            "sensitivity = ?, scope = ?, content_hash = ?, updated_at = ?, "
            "origin = case when ? then 'manual' else origin end, "
            "status = case when ? and status = 'candidate' then 'active' else status end, "
            "confidence = case when ? then 1.0 else confidence end where id = ?",
            (
                text, text_en, norm.fold, norm.skel, mtype, json.dumps(triggers, ensure_ascii=False), sensitivity, scope,
                content_hash(text), self.now(), by_user, by_user, by_user, memory_id,
            ),
        )  # fmt: skip
        self.audit(
            actor, "update", "memory", memory_id,
            {"text": m["text_original"], "type": m["type"], "triggers": m["triggers"]},
            {"text": text, "type": mtype, "triggers": triggers}, cause,
        )  # fmt: skip
        return {"ok": True, "id": memory_id}

    def set_pinned(
        self, memory_id: int, pinned: bool, *, actor: str = "user"
    ) -> Dict[str, Any]:
        m = self.get_memory(memory_id)
        if not m:
            return {"ok": False, "reason": "not_found"}
        if bool(m["pinned"]) == bool(pinned):
            return {"ok": True}
        if pinned:
            if m["status"] != "active":
                return {"ok": False, "reason": "not_active"}
            full = self._pin_room(len(m["text_original"]))
            if full:
                return {"ok": False, "reason": full}
        self._run(
            "update memories set pinned = ?, updated_at = ? where id = ?",
            (1 if pinned else 0, self.now(), memory_id),
        )
        self.audit(
            actor, "pin" if pinned else "unpin", "memory", memory_id, None, None, None
        )
        return {"ok": True}

    def supersede(
        self,
        old_id: int,
        new: Dict[str, Any],
        *,
        actor: str = "system",
        cause: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The fact CHANGED (it wasn't wrong): the old one is superseded, both stay in history."""
        old = self.get_memory(old_id)
        if not old:
            return self.add_memory(**{**new, "actor": actor, "cause": cause})
        made = self.add_memory(
            **{
                "scope": old["scope"], "type": old["type"], "sensitivity": old["sensitivity"],
                "triggers": parse_json(old["triggers"], []),
                **new, "actor": actor, "cause": cause, "pinned": False,
            }
        )  # fmt: skip
        if not made.get("ok") or made.get("id") == old_id:
            return made
        now = self.now()
        with self.db.tx():
            self._run(
                "update memories set status = 'superseded', valid_to = ?, superseded_by = ?, pinned = 0, updated_at = ? where id = ?",
                (now, made["id"], now, old_id),
            )
            # The pin moves on: the user pinned the FACT, not its words.
            if old["pinned"]:
                self._run("update memories set pinned = 1 where id = ?", (made["id"],))
            self.audit(
                actor,
                "supersede",
                "memory",
                old_id,
                {"text": old["text_original"]},
                {"by": made["id"]},
                cause,
            )
        return made

    def weaken(
        self,
        memory_id: int,
        amount: float = 0.2,
        *,
        actor: str = "system",
        cause: Optional[str] = None,
    ) -> bool:
        m = self.get_memory(memory_id)
        if not m:
            return False
        nxt = max(0.1, m["confidence"] - amount)
        self._run(
            "update memories set confidence = ?, updated_at = ? where id = ?",
            (nxt, self.now(), memory_id),
        )
        self.audit(
            actor,
            "weaken",
            "memory",
            memory_id,
            {"confidence": m["confidence"]},
            {"confidence": nxt},
            cause,
        )
        return True

    def forget_memory(
        self,
        memory_id: int,
        *,
        hard: bool = False,
        actor: str = "user",
        cause: Optional[str] = None,
    ) -> Dict[str, Any]:
        """soft (default): retracted — out of search and the profile, kept in history.
        hard: the memory, its evidence and its sentence in the record are erased.
        Both leave a tombstone so the next digest cannot learn it again."""
        m = self.get_memory(memory_id)
        if not m:
            return {"ok": False, "reason": "not_found"}
        now = self.now()
        with self.db.tx():
            self._run(
                "insert or ignore into tombstones(content_hash, created_at, note) values (?, ?, ?)",
                (m["content_hash"], now, "hard" if hard else "soft"),
            )
            if not hard:
                # The sentence it came from hides too, or raw-turn search brings it back.
                self.hide_turns(
                    [
                        ev["turn_id"]
                        for ev in self.get_evidence(memory_id)
                        if ev["turn_id"]
                    ]
                )
                self._run(
                    "update memories set status = 'retracted', pinned = 0, valid_to = ?, updated_at = ? where id = ?",
                    (now, now, memory_id),
                )
                self.audit(
                    actor,
                    "forget",
                    "memory",
                    memory_id,
                    {"text": m["text_original"]},
                    {"status": "retracted"},
                    cause,
                )
            else:
                for ev in self.get_evidence(memory_id):
                    if ev["turn_id"] and ev["quote"]:
                        self._redact_in_turn(ev["turn_id"], ev["quote"])
                self._run("delete from memories where id = ?", (memory_id,))
                # The audit of a hard forget never holds the erased text.
                self.audit(actor, "hard_forget", "memory", memory_id, None, None, cause)
        return {"ok": True, "hard": hard}

    def hide_turns(self, turn_ids: Sequence[int]) -> int:
        for tid in turn_ids:
            self._run(
                "update turns set flags = flags | ? where id = ?",
                (TURN_FLAGS["forgotten"], tid),
            )
        return len(turn_ids)

    def _redact_in_turn(self, turn_id: int, quote: str) -> bool:
        turn = self.get_turn(turn_id)
        if not turn:
            return False
        mark = "[removed by user]"
        text = turn["text"]
        at = text.lower().find(str(quote).lower())
        # Quote not found verbatim: the whole turn goes — a half erase is worse.
        text = f"{text[:at]}{mark}{text[at + len(str(quote)) :]}" if at >= 0 else mark
        norm = normalize_for_search(text)
        self._run(
            "update turns set text = ?, fold = ?, skel = ?, flags = flags | ? where id = ?",
            (text, norm.fold, norm.skel, TURN_FLAGS["redacted"], turn_id),
        )
        return True

    def is_tombstoned(self, digest: str) -> bool:
        return (
            self._one("select 1 as x from tombstones where content_hash = ?", (digest,))
            is not None
        )

    def mark_used(self, ids: Sequence[int], mind: str, useful: bool = False) -> None:
        """Retrieved (and useful or not). ``useful_count`` grows only when it helped."""
        now = self.now()
        with self.db.tx():
            for mid in ids:
                self._run(
                    "insert into access_log(memory_id, ts, mind, useful) values (?, ?, ?, ?)",
                    (mid, now, mind, 1 if useful else 0),
                )
                if useful:
                    self._run(
                        "update memories set useful_count = useful_count + 1, last_used_at = ? where id = ?",
                        (now, mid),
                    )

    def activation_times(self, memory_id: int, limit: int = 12) -> List[int]:
        rows = self._all(
            "select ts from access_log where memory_id = ? and useful = 1 order by ts desc limit ?",
            (memory_id, limit),
        )
        return [int(r["ts"]) for r in rows]

    # -- intentions (promises and follow-ups) ------------------------------------------

    def add_intention(self, **i: Any) -> Dict[str, Any]:
        what = clip(redact_secrets(clean(i.get("what"))).text, 300)
        if not what:
            return {"ok": False, "reason": "empty"}
        digest = content_hash(what)
        dup = self._one(
            "select id from intentions where content_hash = ? and status = 'open' limit 1",
            (digest,),
        )
        if dup:
            return {"ok": True, "id": int(dup["id"]), "duplicate_of": int(dup["id"])}
        conf = i.get("confidence")
        cur = self._run(
            "insert into intentions(uid, what, what_en, who_for, cue_type, cue_text, due_ts, expiry_ts, source_turn, "
            "source_session, quote, confidence, created_by, character_id, scope, created_at, content_hash) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid("i"), what, clip(clean(i.get("what_en")), 300),
                clip(clean(i.get("who_for")), 80) if i.get("who_for") else None,
                i.get("cue_type") if i.get("cue_type") in ("time", "event", "context") else "time",
                clip(clean(i.get("cue_text")), 200) if i.get("cue_text") else None,
                i.get("due_ts"), i.get("expiry_ts"), i.get("source_turn"), i.get("source_session"),
                clip(clean(i.get("quote")), 400), float(conf) if isinstance(conf, (int, float)) else 1.0,
                i.get("created_by") or "system", i.get("character_id"), clean(i.get("scope")) or "user", self.now(), digest,
            ),
        )  # fmt: skip
        iid = int(cur.lastrowid)
        self.audit(
            i.get("actor") or i.get("created_by") or "system",
            "add",
            "intention",
            iid,
            None,
            {"what": what, "due": i.get("due_ts")},
            i.get("cause"),
        )
        return {"ok": True, "id": iid}

    def get_intention(self, intention_id: int) -> Optional[Row]:
        return self._one("select * from intentions where id = ?", (intention_id,))

    def find_intention_by_short_id(self, short: str) -> Optional[Row]:
        s = re.sub(r"[^a-z0-9_]", "", clean(short), flags=re.IGNORECASE)
        if len(s) < 6:
            return None
        rows = self._all(
            "select * from intentions where uid like ? limit 2",
            (f"{s}%" if s.startswith("i_") else f"i_{s}%",),
        )
        return rows[0] if len(rows) == 1 else None

    def intentions_from_session(self, key: str, created_by: str) -> List[Row]:
        return self._all(
            "select * from intentions where source_session = ? and created_by = ? order by id",
            (key, created_by),
        )

    def list_intentions(self, status: str = "open", limit: int = 100) -> List[Row]:
        return self._all(
            "select * from intentions where status = ? order by coalesce(due_ts, created_at) limit ?",
            (status, limit),
        )

    def due_intentions(
        self,
        now: Optional[int] = None,
        ahead_ms: int = 0,
        scopes: Sequence[str] = ("user",),
    ) -> List[Row]:
        """Promises whose time has come (or comes within ``ahead_ms``). Undated
        promises never appear here — only among the "open promises" in context."""
        now = self.now() if now is None else now
        marks = ",".join("?" * len(scopes))
        return self._all(
            f"select * from intentions where status = 'open' and due_ts is not null and due_ts <= ? "
            f"and (expiry_ts is null or expiry_ts > ?) and scope in ({marks}) order by due_ts",
            (now + ahead_ms, now, *scopes),
        )

    def resolve_intention(
        self,
        intention_id: int,
        status: str = "done",
        *,
        actor: str = "user",
        cause: Optional[str] = None,
    ) -> Dict[str, Any]:
        if status not in ("done", "dismissed", "expired", "open"):
            return {"ok": False, "reason": "bad_status"}
        row = self.get_intention(intention_id)
        if not row:
            return {"ok": False, "reason": "not_found"}
        self._run(
            "update intentions set status = ?, resolved_at = ? where id = ?",
            (status, None if status == "open" else self.now(), intention_id),
        )
        self.audit(
            actor,
            status,
            "intention",
            intention_id,
            {"status": row["status"]},
            {"status": status},
            cause,
        )
        return {"ok": True}

    def mark_intention_surfaced(self, intention_id: int) -> None:
        self._run(
            "update intentions set last_surfaced_at = ?, surfaced_count = surfaced_count + 1 where id = ?",
            (self.now(), intention_id),
        )

    def expire_intentions(self, now: Optional[int] = None) -> int:
        now = self.now() if now is None else now
        cur = self._run(
            "update intentions set status = 'expired', resolved_at = ? where status = 'open' and expiry_ts is not null and expiry_ts <= ?",
            (now, now),
        )
        return int(cur.rowcount or 0)

    # -- the character's raw record ------------------------------------------------------

    def add_character_event(self, **e: Any) -> Dict[str, Any]:
        """What the character thought / said / held back, and how it was answered.
        Append-only; never a user fact and never a digest source."""
        character_id = clean(e.get("character_id"))
        kind = clean(e.get("kind"))
        if not character_id or not kind or self.is_paused():
            return {"ok": False}
        text = clip(redact_secrets(str(e.get("text") or "").strip()).text, 2000)
        ts = int(_num(e.get("ts"))) if _num(e.get("ts")) > 0 else self.now()
        cur = self._run(
            "insert or ignore into character_events(ts, character_id, kind, text, meta, screen_derived, session_id, source_ref) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, character_id[:80], kind[:40], text, clip(json.dumps(e.get("meta") or {}, ensure_ascii=False), 2000),
                1 if e.get("screen_derived") else 0, e.get("session_id"),
                clip(clean(e.get("source_ref")), 200) if e.get("source_ref") else None,
            ),
        )  # fmt: skip
        return {"ok": True, "added": (cur.rowcount or 0) > 0}

    def list_character_events(
        self,
        character_id: str,
        since: int = 0,
        limit: int = 200,
        kinds: Optional[Sequence[str]] = None,
    ) -> List[Row]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            return self._all(
                f"select * from character_events where character_id = ? and ts >= ? and kind in ({marks}) order by ts desc limit ?",
                (character_id, since, *kinds, limit),
            )
        return self._all(
            "select * from character_events where character_id = ? and ts >= ? order by ts desc limit ?",
            (character_id, since, limit),
        )

    # -- episodes --------------------------------------------------------------------------

    def add_episode(self, **e: Any) -> int:
        title = clip(clean(e.get("title")), 160) or "Conversation"
        summary = clip(clean(e.get("summary_en")), 1200)
        norm = normalize_for_search(f"{title} {summary}")
        sal = e.get("salience")
        cur = self._run(
            "insert into episodes(uid, session_id, channel, character_id, first_turn, last_turn, start_ts, end_ts, "
            "title, summary_en, fold, skel, mood, salience, digest_version, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid("ep"), e["session_key"], e["channel"], e.get("character_id"), e["first_turn"], e["last_turn"],
                e["start_ts"], e["end_ts"], title, summary, norm.fold, norm.skel,
                clip(clean(e.get("mood")), 40) if e.get("mood") else None,
                max(0.0, min(1.0, float(sal))) if isinstance(sal, (int, float)) else 0.0, e.get("digest_version"), self.now(),
            ),
        )  # fmt: skip
        return int(cur.lastrowid)

    def get_episode(self, episode_id: int) -> Optional[Row]:
        return self._one("select * from episodes where id = ?", (episode_id,))

    def recent_episodes(
        self,
        limit: int = 5,
        character_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> List[Row]:
        where: List[str] = []
        args: List[Any] = []
        if character_id:
            where.append("character_id = ?")
            args.append(character_id)
        if channel:
            where.append("channel = ?")
            args.append(channel)
        clause = f"where {' and '.join(where)}" if where else ""
        return self._all(
            f"select * from episodes {clause} order by end_ts desc limit ?",
            (*args, limit),
        )

    # -- counts and export --------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        def n(sql: str) -> int:
            row = self.db.one(sql)
            return int(row[0] or 0) if row else 0

        return {
            "turns": n("select count(*) from turns"),
            "sessions": n("select count(*) from sessions"),
            "memories": n(
                "select count(*) from memories where deleted = 0 and status = 'active'"
            ),
            "candidates": n(
                "select count(*) from memories where deleted = 0 and status = 'candidate'"
            ),
            "pinned": n(
                "select count(*) from memories where deleted = 0 and status = 'active' and pinned = 1"
            ),
            "episodes": n("select count(*) from episodes"),
            "open_intentions": n(
                "select count(*) from intentions where status = 'open'"
            ),
            "character_events": n("select count(*) from character_events"),
            "first_turn_at": n("select min(ts) from turns") or None,
            "last_turn_at": n("select max(ts) from turns") or None,
            "paused": self.is_paused(),
        }

    def export_all(self) -> Dict[str, Any]:
        """The user's own memory in a readable shape (JSON export)."""
        return {
            "exported_at": datetime.fromtimestamp(
                self.now() / 1000, tz=timezone.utc
            ).isoformat(),
            "stats": self.stats(),
            "memories": self._all(
                "select uid, type, text_original, text_en, origin, status, scope, sensitivity, pinned, triggers, "
                "confidence, evidence_count, event_start, mentioned_at, valid_to from memories where deleted = 0 order by id"
            ),
            "intentions": self._all(
                "select uid, what, who_for, due_ts, status, created_at from intentions order by id"
            ),
            "episodes": self._all(
                "select uid, session_id, title, summary_en, start_ts, end_ts from episodes order by id"
            ),
            "sessions": self._all(
                "select id, channel, character_id, started_at, last_turn_at, turn_count from sessions order by started_at"
            ),
            "turns": self._all(
                "select session_id, ts, speaker, character_id, text from turns order by id"
            ),
        }


__all__ = [
    "MemoryStore",
    "clean",
    "clip",
    "parse_json",
    "scopes_for",
    "session_key",
    "uid",
    "word_overlap",
]
