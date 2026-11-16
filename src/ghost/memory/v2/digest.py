"""Digest: after a conversation, a cheap model call distils what to remember.

Ported from Stonic's ``memory/digest.js``. Two parts — and the second is the
real work:

1. PROMPT: the model gets the conversation and PROPOSES, as JSON, memories,
   updates, promises and episodes.
2. GATE: code checks every proposal. Nothing the model says goes straight into
   the store.

The lesson from research: memory systems fail at WRITING, not finding — they
pile up junk or make things up. So the model only proposes; code decides.

Gate checks:

* QUOTE CHECK — every memory comes with the user's exact words, and code
  verifies those words really are in one of the user's turns. Not found ->
  rejected. (This is where invented memories are stopped.)
* facts about the user come ONLY from user lines; assistant lines are context.
* text shown to the model earlier AS a memory never goes back in — otherwise
  the same thing would keep being written as "new".
* a session that read a web page / document yields only things the user said
  themselves (no inferences); sensitive things only when stated.
* a per-session cap; a known thing is reinforced, not duplicated; a forgotten
  thing does not come back (both in ``store.add_memory``).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from ghost.memory.v2.config import (
    DAY,
    DIGEST,
    INTENTIONS,
    LIMITS,
    MEMORY_TYPES,
    SINGLE_VALUED_TYPES,
    TURN_FLAGS,
)
from ghost.memory.v2.llm_relay import LlmError, LlmReply
from ghost.memory.v2.normalize import fold_text
from ghost.memory.v2.search import MemorySearch
from ghost.memory.v2.store import MemoryStore, word_overlap
from ghost.memory.v2.timefmt import absolute_date, relative_time, start_of_day


def one_line(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def short_id(value: object) -> str:
    return str(value or "")[:10]


def iso_local(ts: float) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


SYSTEM_PROMPT = """You maintain the long-term memory of a personal AI companion. You are given one finished conversation between the USER and the companion (ASSISTANT). Decide what, if anything, is worth remembering about the USER for months to come. You output one JSON object and nothing else.

The USER may speak Urdu, Roman Urdu, Hindi or English, often mixed. Understand all of them.

# memories — lasting facts about the USER, taken ONLY from USER lines
Types: identity (name, city, job, age, family facts) · preference (likes, dislikes, how they want to be treated) · relationship (a person in their life and who that person is) · project (ongoing work, goals, decisions made) · routine (habits, schedule) · event (a specific dated thing that happened or will happen) · opinion (a view they hold) · instruction (a standing "always / never do X" for the companion).
For each memory give:
- "type"
- "text_original": one short, complete sentence in the USER's own language and script, in third person, that still makes sense when read alone months later.
- "text_en": the same fact as one short English sentence.
- "quote": the exact words copied from the USER line that prove it (3 to 25 words, copied character for character).
- "turn": the number of that USER line.
- "origin": "stated" if the USER said it directly, "inferred" if you concluded it.
- "event_date": "YYYY-MM-DD" when the fact is about a specific day (resolve words like kal, parson, next Friday against the conversation date), otherwise null.
- "entities": people, places, organisations or projects named: [{"name", "type": "person|place|org|project|other", "aliases": [other names used for the same thing]}].
- "sensitivity": "sensitive" for health, religion, politics, money trouble, sexuality or legal trouble; otherwise "normal".
- "explicit": true only if the USER asked for this to be remembered.
- "emotion": 0 to 1, how emotionally charged this was for the USER.
- "triggers": up to 3 single words (a name, a project) that should bring this to mind — only when "explicit" is true, otherwise [].

# updates — a USER line repeats or changes something in KNOWN MEMORIES
{"id", "relation": "same" | "refines" | "contradicts", "text_original", "text_en", "quote", "turn"}
"same": they simply said it again (text fields may be empty). "refines": same fact, more detail. "contradicts": it is no longer true and this replaces it. Never also list it under memories.

# intentions — something to do or follow up later
Said by the USER, or promised to the USER by the ASSISTANT. Do not list one that is already in OPEN PROMISES.
{"what" (in the USER's language), "what_en", "who_for", "due": "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" or null, "quote", "turn"}

# resolved — ids from OPEN PROMISES that this conversation shows are finished
{"id", "how": "done" | "cancelled"}

# episodes — split the conversation into 1 to 4 topic segments, covering every line
{"first", "last", "title" (max 8 words), "summary_en" (2 to 4 sentences: what was discussed, what was decided, concrete names, numbers and dates), "mood" (one word for the USER's mood), "salience": 0 to 1}

# DO NOT EXTRACT as a memory
- anything the ASSISTANT said, suggested or assumed — ASSISTANT lines are context only
- questions the USER asked, general knowledge, facts about the world
- passing states ("tired today", "busy right now") — these belong in the episode summary only
- judgements about the USER's personality or psychology
- a mistaken belief of the USER stated as if it were true; a view they hold is type "opinion"
- passwords, codes, card or account numbers, keys
- anything already in KNOWN MEMORIES that has not changed
- anything you are unsure about. When in doubt, leave it out.

Most conversations produce 0 to 3 memories. Zero is a normal and correct result. Never invent a quote.

# Output
{"memories": [], "updates": [], "intentions": [], "resolved": [], "episodes": []}"""

_QUESTION_NOT_FACT = re.compile(
    r"^(the )?user (asked|asks|is asking|wants to know|wanted to know|greeted|said (hi|hello))",
    re.I,
)
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")

LlmCall = Callable[[str, str, int], LlmReply]


class MemoryDigest:
    def __init__(self, store: MemoryStore, search: MemorySearch, llm: LlmCall) -> None:
        self.store = store
        self.search = search
        self.llm = llm

    def pending_turns(self, session: Dict[str, Any]) -> List[Dict[str, Any]]:
        skip = (
            TURN_FLAGS["automated"] | TURN_FLAGS["recalled"] | TURN_FLAGS["forgotten"]
        )
        return [
            t
            for t in self.store.get_turns(
                session["id"], after_id=session["digested_upto"]
            )
            if not t["flags"] & skip
        ]

    @staticmethod
    def chunks(turns: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        out: List[List[Dict[str, Any]]] = []
        cur: List[Dict[str, Any]] = []
        size = 0
        for t in turns:
            if cur and size + len(t["text"]) > DIGEST["max_chars_per_call"]:
                out.append(cur)
                cur = []
                size = 0
            cur.append(t)
            size += len(t["text"]) + 20
        if cur:
            out.append(cur)
        return out

    def related(self, turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """A short "already known" list: so the model doesn't rewrite them and can spot changes."""
        user_text = " ".join(t["text"] for t in turns if t["speaker"] == "user")[:2000]
        found = self.search.recall(
            user_text, mind="hermes", kinds=["memory"], k=14, purpose="answer"
        )
        by_id: Dict[int, Optional[Dict[str, Any]]] = {}
        for it in found["items"]:
            by_id[it["id"]] = self.store.get_memory(it["id"])
        for m in self.store.list_memories(status="active", pinned=True, limit=20):
            by_id[int(m["id"])] = m
        return [m for m in by_id.values() if m][:24]

    @staticmethod
    def build_user_prompt(
        session: Dict[str, Any],
        turns: List[Dict[str, Any]],
        known: List[Dict[str, Any]],
        open_: List[Dict[str, Any]],
        now: int,
    ) -> str:
        when = turns[0]["ts"] if turns else now
        d = datetime.fromtimestamp(when / 1000)
        lines = [
            f"[{i + 1}] {'USER' if t['speaker'] == 'user' else 'ASSISTANT'}: {one_line(t['text'])}"
            for i, t in enumerate(turns)
        ]
        known_lines = [
            f"- ({short_id(m['uid'])}) [{m['type']}] {one_line(m['text_original'])}"
            for m in known
        ]
        intent_lines = [
            f"- ({short_id(i['uid'])}) {one_line(i['what'])}"
            + (f" — due {iso_local(i['due_ts'])}" if i["due_ts"] else "")
            for i in open_
        ]
        parts = [
            f"Conversation date: {_WEEKDAYS[d.weekday()]}, {absolute_date(when)} ({iso_local(when)}), {relative_time(when, now) or 'today'}.",
            "NOTE: during this conversation the assistant read outside content (a web page, document or email). Extract only "
            "what the USER stated in their own words. Do not infer anything."
            if session.get("external_content")
            else "",
            "",
            'KNOWN MEMORIES (do not repeat these; use "updates" if one changed)',
            "\n".join(known_lines) if known_lines else "(none)",
            "",
            "OPEN PROMISES",
            "\n".join(intent_lines) if intent_lines else "(none)",
            "",
            "CONVERSATION",
            "\n".join(lines),
        ]
        return "\n".join(p for p in parts if p != "")

    @staticmethod
    def parse(text: object) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """The JSON object out of the model's reply, or None."""
        raw = str(text or "")
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start : end + 1])
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None

        def arr(v: Any) -> List[Dict[str, Any]]:
            return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

        return {
            k: arr(obj.get(k))
            for k in ("memories", "updates", "intentions", "resolved", "episodes")
        }

    @staticmethod
    def find_quote(
        quote: object, turn_no: object, turns: List[Dict[str, Any]], user_only: bool
    ) -> Optional[Dict[str, Any]]:
        """QUOTE CHECK: which turn holds the quote? The model's turn first, then the rest."""
        q = fold_text(one_line(quote))
        if len(q) < 6 or len(q.split(" ")) < 2:
            return None

        def ok(t: Optional[Dict[str, Any]]) -> bool:
            return (
                bool(t)
                and (not user_only or t["speaker"] == "user")
                and q in fold_text(t["text"])
            )

        try:
            idx = int(turn_no) - 1  # type: ignore[arg-type]
        except (TypeError, ValueError):
            idx = -1
        named = turns[idx] if 0 <= idx < len(turns) else None
        if ok(named):
            return named
        return next((t for t in turns if ok(t)), None)

    @staticmethod
    def parse_date(value: object, anchor: float) -> Optional[int]:
        if not value or not isinstance(value, str):
            return None
        m = _DATE.match(value.strip())
        if not m:
            return None
        has_time = m.group(4) is not None
        try:
            d = datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)) if has_time else 9,
                int(m.group(5)) if has_time else 0,
            )
        except ValueError:
            return None
        ts = int(d.timestamp() * 1000)
        # The model sometimes writes the wrong year: dates out of reason are refused.
        if ts < anchor - 20 * 365 * DAY or ts > anchor + 5 * 365 * DAY:
            return None
        return ts

    def already_noted(
        self,
        session: Dict[str, Any],
        items: Sequence[Dict[str, Any]],
        turns: List[Dict[str, Any]],
        anchor: float,
    ) -> Set[int]:
        """Proposed promises the voice already saved itself this conversation
        (the ``follow_up`` tool). Each saved promise blocks at most ONE proposal."""
        taken: Set[int] = set()
        made = self.store.intentions_from_session(session["id"], "voice")
        if not made:
            return taken
        pairs = []
        for index, item in enumerate(items):
            what = one_line(item.get("what") or item.get("what_en"))
            if not what:
                continue
            turn = self.find_quote(item.get("quote"), item.get("turn"), turns, False)
            due = self.parse_date(item.get("due"), anchor)
            for slot, row in enumerate(made):
                overlap = max(
                    word_overlap(what, row["what"]),
                    word_overlap(one_line(item.get("what_en")), row["what"]),
                )
                same_turn = (
                    bool(turn)
                    and row["source_turn"] is not None
                    and int(row["source_turn"]) == int(turn["id"])
                )
                same_day = (
                    bool(due)
                    and bool(row["due_ts"])
                    and start_of_day(due) == start_of_day(row["due_ts"])
                )
                cue = same_turn or same_day
                if overlap >= (
                    INTENTIONS["same_overlap_with_cue"]
                    if cue
                    else INTENTIONS["same_overlap"]
                ):
                    pairs.append((overlap + (1 if cue else 0), index, slot))
        used: Set[int] = set()
        for _, index, slot in sorted(pairs, key=lambda p: -p[0]):
            if index in taken or slot in used:
                continue
            taken.add(index)
            used.add(slot)
        return taken

    def apply(
        self,
        session: Dict[str, Any],
        turns: List[Dict[str, Any]],
        parsed: Dict[str, List[Dict[str, Any]]],
        now: int,
    ) -> Dict[str, Any]:
        """Pass the model's proposals through the gate into the store."""
        out: Dict[str, Any] = {
            "memories": 0,
            "updates": 0,
            "intentions": 0,
            "resolved": 0,
            "episodes": 0,
            "rejected": [],
        }

        def reject(kind: str, why: str, item: Dict[str, Any]) -> None:
            text = one_line(
                item.get("text_en")
                or item.get("text_original")
                or item.get("what_en")
                or item.get("what")
                or ""
            )
            out["rejected"].append({"kind": kind, "why": why, "text": text[:120]})

        anchor = turns[0]["ts"] if turns else now
        external = bool(session.get("external_content"))
        base = {
            "source_session": session["id"],
            "created_by": "digest",
            "prompt_version": DIGEST["prompt_version"],
            "actor": "digest",
        }

        with self.store.db.tx():
            for item in parsed["memories"]:
                if out["memories"] >= LIMITS["digest_memories_per_session"]:
                    reject("memory", "session_cap", item)
                    continue
                text = one_line(item.get("text_original") or item.get("text_en"))
                if not text:
                    reject("memory", "empty", item)
                    continue
                if item.get("type") not in MEMORY_TYPES:
                    reject("memory", "bad_type", item)
                    continue
                turn = self.find_quote(item.get("quote"), item.get("turn"), turns, True)
                if not turn:
                    reject("memory", "quote_not_found", item)
                    continue
                origin = "stated" if item.get("origin") == "stated" else "inferred"
                if external and origin != "stated":
                    reject("memory", "external_inferred", item)
                    continue
                sensitive = item.get("sensitivity") == "sensitive"
                if sensitive and origin != "stated":
                    reject("memory", "sensitive_inferred", item)
                    continue
                if _QUESTION_NOT_FACT.match(one_line(item.get("text_en"))):
                    reject("memory", "question_not_fact", item)
                    continue
                event_start = self.parse_date(item.get("event_date"), anchor)
                explicit = item.get("explicit") is True
                try:
                    emotion = max(0.0, min(1.0, float(item.get("emotion") or 0)))
                except (TypeError, ValueError):
                    emotion = 0.0
                made = self.store.add_memory(
                    **base,
                    type=item["type"], text=text, text_en=one_line(item.get("text_en")), origin=origin, scope="user",
                    sensitivity="sensitive" if sensitive else "normal", event_start=event_start, mentioned_at=turn["ts"],
                    triggers=(item.get("triggers") or [])[:3] if explicit and isinstance(item.get("triggers"), list) else [],
                    evidence=[{"turn_id": turn["id"], "quote": one_line(item.get("quote"))}],
                    entities=(item.get("entities") or [])[:6] if isinstance(item.get("entities"), list) else [],
                    salience={"explicit": 1 if explicit else 0, "future": 1 if event_start and event_start > now else 0, "emotion": emotion},
                    cause="digest",
                )  # fmt: skip
                if made.get("ok"):
                    out["memories"] += 1
                else:
                    reject("memory", made.get("reason") or "rejected", item)

            for item in parsed["updates"]:
                old = self.store.find_memory_by_short_id(str(item.get("id") or ""))
                if not old:
                    reject("update", "unknown_id", item)
                    continue
                turn = self.find_quote(item.get("quote"), item.get("turn"), turns, True)
                if not turn:
                    reject("update", "quote_not_found", item)
                    continue
                evidence = [
                    {"turn_id": turn["id"], "quote": one_line(item.get("quote"))}
                ]
                relation = item.get("relation")
                if relation == "same":
                    self.store.reinforce(
                        int(old["id"]), evidence=evidence, source_session=session["id"]
                    )
                    out["updates"] += 1
                    continue
                text = one_line(item.get("text_original") or item.get("text_en"))
                if not text:
                    reject("update", "empty", item)
                    continue
                nxt = {
                    **base, "text": text, "text_en": one_line(item.get("text_en")), "origin": "stated", "evidence": evidence,
                    "mentioned_at": turn["ts"], "cause": f"digest:{relation}",
                }  # fmt: skip
                if relation == "refines" or old["type"] in SINGLE_VALUED_TYPES:
                    made = self.store.supersede(
                        int(old["id"]), nxt, actor="digest", cause=f"digest:{relation}"
                    )
                    if made.get("ok"):
                        out["updates"] += 1
                    else:
                        reject("update", made.get("reason") or "rejected", item)
                else:
                    # Accumulating types (people, projects): the new one doesn't end
                    # the old; both stay, the old one loses a little confidence.
                    made = self.store.add_memory(
                        **{**nxt, "type": old["type"], "scope": old["scope"]}
                    )
                    if made.get("ok"):
                        self.store.weaken(
                            int(old["id"]),
                            0.2,
                            actor="digest",
                            cause="digest:contradicts",
                        )
                        out["updates"] += 1
                    else:
                        reject("update", made.get("reason") or "rejected", item)

            noted = self.already_noted(session, parsed["intentions"], turns, anchor)
            for index, item in enumerate(parsed["intentions"]):
                if index in noted:
                    reject("intention", "already_saved", item)
                    continue
                if out["intentions"] >= LIMITS["digest_intentions_per_session"]:
                    reject("intention", "session_cap", item)
                    continue
                what = one_line(item.get("what") or item.get("what_en"))
                if not what:
                    reject("intention", "empty", item)
                    continue
                # A promise may be the assistant's too — both speakers' turns count here.
                turn = self.find_quote(
                    item.get("quote"), item.get("turn"), turns, False
                )
                if not turn:
                    reject("intention", "quote_not_found", item)
                    continue
                due = self.parse_date(item.get("due"), anchor)
                made = self.store.add_intention(
                    what=what, what_en=one_line(item.get("what_en")),
                    who_for=one_line(item.get("who_for")) if item.get("who_for") else None, cue_type="time", due_ts=due,
                    expiry_ts=due + INTENTIONS["expiry_after_due_ms"] if due else turn["ts"] + INTENTIONS["expiry_undated_ms"],
                    source_turn=turn["id"], source_session=session["id"], quote=one_line(item.get("quote")),
                    created_by="digest", character_id=session.get("character_id"), actor="digest", cause="digest",
                )  # fmt: skip
                if made.get("ok") and not made.get("duplicate_of"):
                    out["intentions"] += 1

            for item in parsed["resolved"]:
                row = self.store.find_intention_by_short_id(str(item.get("id") or ""))
                if not row or row["status"] != "open":
                    continue
                self.store.resolve_intention(
                    int(row["id"]),
                    "dismissed" if item.get("how") == "cancelled" else "done",
                    actor="digest",
                    cause="digest",
                )
                out["resolved"] += 1

            for item in parsed["episodes"][:4]:
                try:
                    first_no = max(1, int(item.get("first") or 1))
                    last_no = min(len(turns), int(item.get("last") or len(turns)))
                except (TypeError, ValueError):
                    continue
                first = turns[first_no - 1] if 0 < first_no <= len(turns) else None
                last = turns[last_no - 1] if 0 < last_no <= len(turns) else None
                if not first or not last or last["id"] < first["id"]:
                    continue
                try:
                    salience = float(item.get("salience") or 0)
                except (TypeError, ValueError):
                    salience = 0.0
                self.store.add_episode(
                    session_key=session["id"], channel=session["channel"], character_id=session.get("character_id"),
                    first_turn=first["id"], last_turn=last["id"], start_ts=first["ts"], end_ts=last["ts"],
                    title=one_line(item.get("title")), summary_en=one_line(item.get("summary_en")), mood=item.get("mood"),
                    salience=salience, digest_version=DIGEST["prompt_version"],
                )  # fmt: skip
                out["episodes"] += 1

            self.store.mark_digested(session["id"], turns[-1]["id"])
        return out

    def digest_session(self, key: str, now: Optional[int] = None) -> Dict[str, Any]:
        """Digest one session. Never raises: failure is ok=False with a reason."""
        now = now or self.store.now()
        total: Dict[str, Any] = {
            "ok": True, "calls": 0, "cost_usd": 0.0, "memories": 0, "updates": 0, "intentions": 0, "resolved": 0,
            "episodes": 0, "rejected": [],
        }  # fmt: skip
        session = self.store.get_session(key)
        if not session:
            return {**total, "ok": False, "reason": "no_session"}
        if self.store.is_paused():
            return {**total, "ok": False, "reason": "paused"}
        pending = self.pending_turns(session)
        every = self.store.get_turns(session["id"], after_id=session["digested_upto"])
        if not every:
            return {**total, "skipped": True, "reason": "nothing_new"}
        last_id = every[-1]["id"]
        # Awareness lines are not user facts, and a talk of under two user
        # sentences (hello / hi) teaches nothing: both are marked done.
        user_turns = sum(1 for t in pending if t["speaker"] == "user")
        if session["channel"] == "awareness" or user_turns < DIGEST["min_user_turns"]:
            self.store.mark_digested(session["id"], last_id)
            return {
                **total,
                "skipped": True,
                "reason": "awareness"
                if session["channel"] == "awareness"
                else "too_small",
            }
        for chunk in self.chunks(pending):
            known = self.related(chunk)
            open_ = self.store.list_intentions(status="open", limit=20)
            try:
                reply = self.llm(
                    SYSTEM_PROMPT,
                    self.build_user_prompt(session, chunk, known, open_, now),
                    1800,
                )
            except LlmError as exc:
                return {**total, "ok": False, "reason": f"llm:{exc.code}"}
            except Exception as exc:  # noqa: BLE001 — the digest never raises
                return {**total, "ok": False, "reason": f"llm:{type(exc).__name__}"}
            total["calls"] += 1
            total["cost_usd"] += (
                reply.cost_usd
                if reply.cost_usd is not None
                else DIGEST["fallback_cost_usd"]
            )
            parsed = self.parse(reply.text)
            if not parsed:
                return {**total, "ok": False, "reason": "bad_json"}
            res = self.apply(session, chunk, parsed, now)
            for k in ("memories", "updates", "intentions", "resolved", "episodes"):
                total[k] += res[k]
            total["rejected"] += res["rejected"]
        self.store.mark_digested(session["id"], last_id)
        return total


__all__ = ["SYSTEM_PROMPT", "MemoryDigest"]
