"""Recall — with no AI at all, entirely on this PC (Stonic port, phase 6).

Ported from Stonic's ``memory/search.js``. A question comes in ("Ali wala
masla kya tha?"); its meaningful words go to several channels:

* memories (distilled): whole words · fragments · consonant skeleton · names
  (entities) · trigger words
* raw conversation: whole words · fragments · skeleton
* episodes: whole words · skeleton

Each channel ranks its own candidates; Reciprocal Rank Fusion merges the
rankings by POSITION only, so channel scores never have to be comparable.
Then GATES (superseded? another character's? sensitive?) and three light
weights: how fresh / useful (A), how important (S), how certain (C)::

    Final = relevance x (1 + 0.25·A + 0.25·S) x C

Multiplicative with small weights: relevance always dominates, so an
"important but off-topic" memory can never rise to the top.

FOUND / WEAK / NONE: RRF scores are not absolute, so "found or not" is decided
by COVERAGE — how much of the question's (idf-weighted) words the top result
really contains. The model gets these three states so it can tell "sure",
"maybe" and "don't remember" apart instead of guessing.

No LLM call, no network, no writes — safe on the voice path.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from ghost.memory.v2.config import DAY, SEARCH, TURN_FLAGS
from ghost.memory.v2.lexicon import synonyms_of
from ghost.memory.v2.normalize import QueryToken, fold_text, query_tokens, skeleton
from ghost.memory.v2.store import MemoryStore, parse_json, scopes_for
from ghost.memory.v2.timefmt import when_text

# Filler words in a question ("cigarette WALI BAAT", "YAAD hai..."): about the
# memory, not in it. Only removed from QUERIES, so changing this list never
# changes the stored index.
QUERY_FILLERS = frozenset(
    fold_text(w)
    for w in [
        "wali", "wala", "wale", "baat", "baatein", "cheez", "sab", "yaad", "batao", "bata", "bataya",
        "tell", "remember", "recall", "rakhna", "sahab", "sahib", "abhi", "thing", "things", "something",
    ]
)  # fmt: skip


def _quote(term: str) -> str:
    """FTS5 terms are always quoted — otherwise "or", "and", "-" become operators."""
    return '"' + str(term).replace('"', '""') + '"'


def stem_of(fold: str) -> Optional[str]:
    """Roman Urdu words change at the end (karni / karna / karne): drop the final vowels."""
    if len(fold) < 5 or re.search(r"[^a-z]", fold):
        return None
    stem = re.sub(r"[aeiouy]+$", "", fold)
    return stem if len(stem) >= 3 and stem != fold else None


def edit_distance(a: str, b: str, limit: int) -> int:
    """Letters apart, stopping early past ``limit`` ("osama" / "usama")."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        low = i
        for j in range(1, len(b) + 1):
            cur.append(
                min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1),
                )
            )
            low = min(low, cur[j])
        if low > limit:
            return limit + 1
        prev = cur
    return prev[len(b)]


@dataclass
class Token:
    fold: str
    skel: str
    raw: str = ""
    alts: List[tuple] = field(default_factory=list)
    present: bool = False
    weight: float = 1.0
    idf: float = 0.0
    rare: bool = False


@dataclass
class Coverage:
    coverage: float = 0.0
    strong: bool = False
    matched: int = 0
    rare_matched: bool = False
    absent: int = 0
    size: int = 0


class MemorySearch:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.db = store.db
        self._vocab_ready: Optional[bool] = None

    def _fts(self, table: str, match: str, limit: int) -> List[int]:
        if not match:
            return []
        try:
            rows = self.db.all(
                f"select rowid as id from {table} where {table} match ? order by rank limit ?",
                (match, limit),
            )
            return [int(r["id"]) for r in rows]
        except sqlite3.Error:
            # A malformed query never breaks search — that channel is just empty.
            return []

    def _matches(self, tokens: Sequence[Token]) -> Dict[str, str]:
        uni: List[str] = []
        tri: List[str] = []
        skel: List[str] = []
        for t in tokens:
            if not t.fold:
                continue
            # Prefix on long words: "meeting" also finds "meetings".
            uni.append(f"{_quote(t.fold)}*" if len(t.fold) >= 5 else _quote(t.fold))
            if len(t.fold) >= 3:
                tri.append(_quote(t.fold))
            stem = stem_of(t.fold)
            if stem:
                tri.append(_quote(stem))
            if t.skel and len(t.skel) >= 2:
                skel.append(_quote(t.skel))
            # Synonyms (lexicon): asked "salary", also search "tankhwa".
            for alt_fold, alt_skel in t.alts:
                uni.append(_quote(alt_fold))
                if alt_skel and len(alt_skel) >= 3:
                    skel.append(_quote(alt_skel))
        return {
            "uni": " OR ".join(uni),
            "tri": " OR ".join(dict.fromkeys(tri)),
            "skel": " OR ".join(dict.fromkeys(skel)),
        }

    def _weigh(self, tokens: Iterable[QueryToken]) -> List[Token]:
        """Each word's WEIGHT (idf): rare words say more. A word absent from the
        whole record gets a low weight — its absence only shows other words were used."""
        if self._vocab_ready is None:
            try:
                for name, table in (
                    ("mem_vocab_turns", "turns_uni"),
                    ("mem_vocab_turns_skel", "turns_skel"),
                    ("mem_vocab_mem", "memories_uni"),
                    ("mem_vocab_mem_skel", "memories_skel"),
                ):
                    self.db.execute(
                        f"create virtual table if not exists temp.{name} using fts5vocab('main', '{table}', 'row')"
                    )
                self._vocab_ready = True
            except sqlite3.Error:
                self._vocab_ready = False
        row = self.db.one(
            "select (select count(*) from turns) + (select count(*) from memories) as n"
        )
        total = int(row["n"] or 0) if row else 0

        def df(table: str, term: str) -> int:
            if not self._vocab_ready or not term:
                return 0
            try:
                r = self.db.one(f"select doc from temp.{table} where term = ?", (term,))
                return int(r["doc"]) if r else 0
            except sqlite3.Error:
                return 0

        small = total < SEARCH["idf_min_docs"]

        def idf_of(d: int) -> float:
            return 1.0 if small else math.log(1 + (total - d + 0.5) / (d + 0.5))

        top = idf_of(0)
        rare_below = max(3, total * SEARCH["rare_share"])
        out: List[Token] = []
        for q in tokens:
            alts = synonyms_of(q.fold)
            exact = df("mem_vocab_turns", q.fold) + df("mem_vocab_mem", q.fold)
            for alt_fold, _ in alts:
                exact += df("mem_vocab_turns", alt_fold) + df("mem_vocab_mem", alt_fold)
            via_skel = (
                df("mem_vocab_turns_skel", q.skel) + df("mem_vocab_mem_skel", q.skel)
                if q.skel and len(q.skel) >= 2
                else 0
            )
            seen = exact or via_skel
            out.append(
                Token(
                    fold=q.fold, skel=q.skel, raw=q.raw, alts=alts, present=seen > 0,
                    weight=idf_of(seen) if seen > 0 else top * SEARCH["absent_weight"],
                    idf=idf_of(seen) if seen > 0 else 0.0, rare=0 < seen <= rare_below,
                )
            )  # fmt: skip
        return out

    def _coverage(self, tokens: Sequence[Token], fold: str, skel: str) -> Coverage:
        """How much of the question is in this item (weighted 0..1)."""
        if not tokens:
            return Coverage()
        text = str(fold or "")
        words = set(text.split(" "))
        skels = set(str(skel or "").split(" "))
        got = 0.0
        total = 0.0
        strong = False
        matched = 0
        rare_matched = False
        for t in tokens:
            best = 0.0
            if t.fold in words:
                best = 1.0
            else:
                stem = stem_of(t.fold)
                for w in words:
                    if len(t.fold) >= 4 and w.startswith(t.fold):
                        best = max(best, 0.9)
                        break
                    if stem and w.startswith(stem):
                        best = max(best, 0.8)
                if best < 0.8 and len(t.fold) >= 4 and t.fold in text:
                    best = 0.8
                if best < 0.9:
                    for alt_fold, alt_skel in t.alts:
                        if alt_fold in words:
                            best = 0.9
                            break
                        if (
                            best < 0.75
                            and alt_skel
                            and len(alt_skel) >= 4
                            and alt_skel in skels
                        ):
                            best = 0.75
                if best < 0.8 and t.skel and t.skel in skels:
                    by_skel = (
                        0.85 if len(t.skel) >= 4 else 0.7 if len(t.skel) == 3 else 0.4
                    )
                    best = max(best, by_skel)
                    # Same consonants, a letter or two apart = same word, other spelling.
                    if best < 0.85 and len(t.fold) >= 4:
                        allow = 2 if len(t.fold) >= 7 else 1
                        for w in words:
                            if abs(len(w) - len(t.fold)) > allow:
                                continue
                            if (
                                edit_distance(w, t.fold, allow) <= allow
                                and skeleton(w) == t.skel
                            ):
                                best = 0.85
                                break
            if best >= 0.8:
                strong = True
            if best >= 0.4:
                matched += 1
            if best >= 0.7 and t.rare:
                rare_matched = True
            got += t.weight * best
            total += t.weight
        absent = sum(1 for t in tokens if not t.present)
        return Coverage(
            got / total if total > 0 else 0.0, strong, matched, rare_matched, absent
        )

    @staticmethod
    def _activation(times: Iterable[Optional[float]], now: float) -> float:
        """ACT-R: a fresh, often-useful memory is more "awake". 0..1."""
        total = 0.0
        for t in times:
            if not t:
                continue
            days = max(0.0, (now - t) / DAY) + 1
            total += days ** -SEARCH["activation_decay"]
        if total <= 0:
            return 0.0
        return 1 / (1 + math.exp(-math.log(total)))

    @staticmethod
    def _salience(m: Dict[str, Any]) -> float:
        w = SEARCH["salience"]
        u = float(m.get("useful_count") or 0)
        return (
            w["explicit"] * (m.get("sal_explicit") or 0)
            + w["future"] * (m.get("sal_future") or 0)
            + w["emotion"] * (m.get("sal_emotion") or 0)
            + w["surprise"] * (m.get("sal_surprise") or 0)
            + w["useful"] * (u / (1 + u))
        )

    def recall(
        self,
        query: str,
        *,
        query_en: str = "",
        mind: str = "voice",
        character_id: Optional[str] = None,
        k: int = 0,
        purpose: str = "answer",
        since: Optional[int] = None,
        until: Optional[int] = None,
        include_past: bool = False,
        kinds: Optional[Sequence[str]] = None,
        now: Optional[int] = None,
        keep_none: bool = False,
    ) -> Dict[str, Any]:
        """Returns {status: FOUND|WEAK|NONE, items, tokens, took_ms}."""
        started = time.monotonic()
        now = now or self.store.now()
        k = max(1, min(int(k or SEARCH["default_k"]), 20))
        kind_set = set(kinds or ("memory", "turn", "episode"))

        def empty(
            status: str = "NONE", tokens: Optional[List[str]] = None
        ) -> Dict[str, Any]:
            return {
                "status": status,
                "items": [],
                "tokens": tokens or [],
                "took_ms": int((time.monotonic() - started) * 1000),
            }

        if self.store.is_paused():
            return empty()

        def meaningful(text: str) -> List[QueryToken]:
            every = [t for t in query_tokens(text or "") if t.fold]
            kept = [t for t in every if t.fold not in QUERY_FILLERS]
            return kept or every

        # The question in its own language + (if given) English. Coverage is
        # measured per form and the better one is taken: writing the question
        # twice must not punish either language.
        own = self._weigh(meaningful(query))
        english = self._weigh(meaningful(query_en))
        seen: Set[str] = set()
        tokens: List[Token] = []
        for t in own + english:
            if t.fold not in seen:
                seen.add(t.fold)
                tokens.append(t)
        if not tokens:
            return empty()
        variants = [lst for lst in (own, english) if lst]

        def coverage_of(fold: str, skel: str) -> Coverage:
            best: Optional[Coverage] = None
            for lst in variants:
                c = self._coverage(lst, fold, skel)
                if best is None or c.coverage > best.coverage:
                    c.size = len(lst)
                    best = c
            return best or Coverage()

        match = self._matches(tokens)
        n = SEARCH["per_channel"]
        w = SEARCH["weights"]
        fused: Dict[str, Dict[str, Any]] = {}

        def add(kind: str, ids: List[int], weight: float, channel: str) -> None:
            for rank, ident in enumerate(ids):
                key = f"{kind}:{ident}"
                cur = fused.setdefault(
                    key, {"kind": kind, "id": ident, "rrf": 0.0, "channels": set()}
                )
                cur["rrf"] += weight / (SEARCH["rrf_k"] + rank + 1)
                cur["channels"].add(channel)

        scopes = ["user"] if mind == "hermes" else scopes_for(character_id)
        if "memory" in kind_set:
            add(
                "memory", self._fts("memories_uni", match["uni"], n), w["memUni"], "uni"
            )
            add(
                "memory", self._fts("memories_tri", match["tri"], n), w["memTri"], "tri"
            )
            add(
                "memory",
                self._fts("memories_skel", match["skel"], n),
                w["memSkel"],
                "skel",
            )
            entity_ids = self.store.entities_by_aliases(
                [t.fold for t in tokens], [t.skel for t in tokens]
            )
            add(
                "memory",
                self.store.memories_for_entities(entity_ids, n),
                w["memEntity"],
                "entity",
            )
            add("memory", self._triggered(tokens, scopes), w["memTrigger"], "trigger")
        if "turn" in kind_set:
            add("turn", self._fts("turns_uni", match["uni"], n), w["turnUni"], "uni")
            add("turn", self._fts("turns_tri", match["tri"], n), w["turnTri"], "tri")
            add(
                "turn", self._fts("turns_skel", match["skel"], n), w["turnSkel"], "skel"
            )
        if "episode" in kind_set:
            add(
                "episode",
                self._fts("episodes_uni", match["uni"], n),
                w["episodeUni"],
                "uni",
            )
            add(
                "episode",
                self._fts("episodes_skel", match["skel"], n),
                w["episodeSkel"],
                "skel",
            )
        if not fused:
            return empty(tokens=[t.fold for t in tokens])

        max_rrf = max(f["rrf"] for f in fused.values())
        ctx = {"now": now, "mind": mind, "purpose": purpose, "scopes": scopes, "character_id": character_id,
               "since": since, "until": until, "include_past": include_past}  # fmt: skip
        scored: List[Dict[str, Any]] = []
        for f in fused.values():
            item = self._load(f, ctx)
            if not item:
                continue
            cov = coverage_of(item["_fold"], item["_skel"])
            triggered = "trigger" in f["channels"]
            coverage = (
                max(cov.coverage, SEARCH["found_coverage"])
                if triggered
                else cov.coverage
            )
            rel = f["rrf"] / max_rrf
            item["coverage"] = round(coverage, 3)
            item["strong"] = cov.strong or triggered or "entity" in f["channels"]
            item["channels"] = sorted(f["channels"])
            item["_gate"] = {
                # SURE: most of the weight matched, a word matched firmly, two or
                # more words matched (for multi-word questions), and no question
                # word is absent from the whole record.
                "found": triggered
                or (
                    coverage >= SEARCH["found_coverage"]
                    and item["strong"]
                    and cov.absent == 0
                    and (cov.size == 1 or cov.matched >= 2)
                ),
                # MAYBE: a rare word (a name) matched, or enough of the weight.
                "weak": cov.rare_matched
                or coverage >= SEARCH["weak_coverage"]
                or "entity" in f["channels"],
            }
            boost = (
                SEARCH["user_turn_boost"]
                if item["kind"] == "turn" and item.get("speaker") == "user"
                else 1.0
            )
            relevance = coverage * (
                SEARCH["coverage_floor"] + (1 - SEARCH["coverage_floor"]) * rel
            )
            item["score"] = (
                relevance
                * boost
                * (
                    1
                    + SEARCH["activation_weight"] * item["_A"]
                    + SEARCH["salience_weight"] * item["_S"]
                )
                * item["_C"]
            )
            scored.append(item)
        scored.sort(key=lambda it: -it["score"])

        ranked = self._dedupe(scored)
        first = ranked[0] if ranked else None
        status = "NONE"
        if first:
            if first["_gate"]["found"]:
                status = "FOUND"
            elif first["_gate"]["weak"]:
                status = "WEAK"
        # Speaking unasked has a strict gate: silence beats a weak memory.
        if purpose == "proactive" and not (
            first
            and first["_gate"]["found"]
            and first["coverage"] >= SEARCH["proactive_coverage"]
        ):
            status = "NONE"
        kept = (
            ranked
            if status == "FOUND" or keep_none
            else [it for it in ranked if it["_gate"]["weak"] or it["_gate"]["found"]]
        )
        items = []
        for it in kept[:k]:
            clean_item = {key: v for key, v in it.items() if not key.startswith("_")}
            clean_item["score"] = round(it["score"], 4)
            items.append(clean_item)
        return {
            "status": status,
            "items": [] if status == "NONE" and not keep_none else items,
            "tokens": [t.fold for t in tokens],
            "took_ms": int((time.monotonic() - started) * 1000),
        }

    def _triggered(self, tokens: Sequence[Token], scopes: Sequence[str]) -> List[int]:
        """Memories whose trigger word was in the question — not at the mercy of search."""
        folds = {t.fold for t in tokens}
        marks = ",".join("?" * len(scopes))
        rows = self.db.all(
            f"select id, triggers from memories where deleted = 0 and status = 'active' and triggers != '[]' "
            f"and scope in ({marks}) order by pinned desc, mentioned_at desc",
            tuple(scopes),
        )
        return [
            int(r["id"])
            for r in rows
            if any(w in folds for w in parse_json(r["triggers"], []))
        ]

    def _load(self, f: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Open a candidate and pass it through the GATES. None = out."""
        now = ctx["now"]
        since, until = ctx["since"], ctx["until"]
        if f["kind"] == "memory":
            m = self.store.get_memory(f["id"])
            if not m or m["scope"] not in ctx["scopes"] or m["status"] == "retracted":
                return None
            if m["status"] == "superseded" and not ctx["include_past"]:
                return None
            # An inference only for a direct question, never unasked.
            if m["status"] == "candidate" and ctx["purpose"] != "answer":
                return None
            if m["sensitivity"] == "sensitive" and ctx["purpose"] == "proactive":
                return None
            when = m["event_start"] or m["mentioned_at"]
            if since and when < since:
                return None
            if until and when > until:
                return None
            ev = self.db.one(
                "select quote, turn_id from memory_evidence where memory_id = ? order by id limit 1",
                (m["id"],),
            )
            return {
                "kind": "memory", "id": int(m["id"]), "uid": m["uid"], "type": m["type"],
                "text": m["text_original"], "text_en": m["text_en"], "when": when, "when_text": when_text(when, now),
                "origin": m["origin"], "status": m["status"], "pinned": bool(m["pinned"]),
                "sensitive": m["sensitivity"] == "sensitive", "confidence": m["confidence"],
                "quote": ev["quote"] if ev else "", "turn_id": ev["turn_id"] if ev else None,
                "past": m["status"] == "superseded",
                "_fold": m["fold"], "_skel": m["skel"],
                "_A": self._activation([m["mentioned_at"], m["recorded_at"], *self.store.activation_times(m["id"])], now),
                "_S": self._salience(m),
                "_C": min(m["confidence"], 0.7) if m["status"] == "candidate" else m["confidence"],
            }  # fmt: skip
        if f["kind"] == "turn":
            t = self.store.get_turn(f["id"])
            if not t or t["flags"] & (
                TURN_FLAGS["automated"] | TURN_FLAGS["forgotten"]
            ):
                return None
            char = ctx["character_id"]
            if (
                ctx["mind"] != "hermes"
                and t["character_id"]
                and char
                and t["character_id"] != char
            ):
                return None
            if since and t["ts"] < since:
                return None
            if until and t["ts"] > until:
                return None
            # Never raw conversation unasked — only firm memories.
            if ctx["purpose"] == "proactive":
                return None
            window = [
                {"id": int(x["id"]), "speaker": x["speaker"], "text": x["text"]}
                for x in self.store.get_turn_window(t["id"], SEARCH["turn_window"])
                if not x["flags"] & (TURN_FLAGS["automated"] | TURN_FLAGS["forgotten"])
            ]
            return {
                "kind": "turn", "id": int(t["id"]), "text": t["text"], "speaker": t["speaker"], "channel": t["channel"],
                "session_key": t["session_id"], "character_id": t["character_id"], "when": t["ts"],
                "when_text": when_text(t["ts"], now), "context": window, "confidence": 1,
                "_fold": t["fold"], "_skel": t["skel"], "_A": self._activation([t["ts"]], now), "_S": 0, "_C": 1,
            }  # fmt: skip
        if f["kind"] == "episode":
            e = self.store.get_episode(f["id"])
            if not e:
                return None
            char = ctx["character_id"]
            if (
                ctx["mind"] != "hermes"
                and e["character_id"]
                and char
                and e["character_id"] != char
            ):
                return None
            if since and e["end_ts"] < since:
                return None
            if until and e["start_ts"] > until:
                return None
            if ctx["purpose"] == "proactive":
                return None
            return {
                "kind": "episode", "id": int(e["id"]), "uid": e["uid"], "text": e["title"], "summary": e["summary_en"],
                "session_key": e["session_id"], "first_turn": e["first_turn"], "last_turn": e["last_turn"],
                "when": e["end_ts"], "when_text": when_text(e["end_ts"], now), "confidence": 0.9,
                "_fold": e["fold"], "_skel": e["skel"], "_A": self._activation([e["end_ts"]], now),
                "_S": float(e["salience"] or 0), "_C": 0.9,
            }  # fmt: skip
        return None

    @staticmethod
    def _dedupe(ranked: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """One thing, one appearance: a memory and its own evidence turn, or
        neighbouring turns of one conversation. The better-scored stays."""
        out: List[Dict[str, Any]] = []
        taken: Set[int] = set()
        for it in ranked:
            if it["kind"] == "memory":
                if it.get("turn_id"):
                    taken.add(it["turn_id"])
                out.append(it)
            elif it["kind"] == "turn":
                near = [c["id"] for c in it["context"]]
                if any(i in taken for i in near):
                    continue
                taken.update(near)
                out.append(it)
            else:
                out.append(it)
        return out


__all__ = ["MemorySearch", "QUERY_FILLERS", "edit_distance", "stem_of"]
