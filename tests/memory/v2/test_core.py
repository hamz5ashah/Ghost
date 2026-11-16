"""Memory Core (Stonic port, phase 6): store, search, state card, digest, recall agent, service."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from ghost.memory.v2 import timefmt
from ghost.memory.v2.config import DAY, DIGEST, HOUR, LIMITS, MIN, TURN_FLAGS
from ghost.memory.v2.context import MemoryContext
from ghost.memory.v2.db import backup_now, list_backups, open_memory_db, verify_backup
from ghost.memory.v2.digest import MemoryDigest
from ghost.memory.v2.jobs import JobQueue
from ghost.memory.v2.llm_relay import LlmError, LlmReply
from ghost.memory.v2.recall_agent import RecallAgent
from ghost.memory.v2.search import MemorySearch
from ghost.memory.v2.service import MemoryCoreService
from ghost.memory.v2.store import MemoryStore, word_overlap

T0 = int(datetime(2026, 11, 16, 10, 0).timestamp() * 1000)


class Clock:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def core(tmp_path, clock):
    opened = open_memory_db(tmp_path / "mem", now=clock())
    store = MemoryStore(opened.db, now=clock)
    search = MemorySearch(store)
    yield store, search, opened
    opened.db.conn.close()


# -- time -----------------------------------------------------------------------


class TestTime:
    def test_parse_when_forms(self):
        now = T0  # Monday 10:00
        assert timefmt.parse_when("+10m", now) == now + 10 * MIN
        assert timefmt.parse_when("in 2 hours", now) == now + 2 * HOUR
        tomorrow9 = int(datetime(2026, 11, 17, 9, 0).timestamp() * 1000)
        assert timefmt.parse_when("tomorrow", now) == tomorrow9
        assert timefmt.parse_when("17:00", now) == int(
            datetime(2026, 11, 16, 17, 0).timestamp() * 1000
        )
        # Already past today -> tomorrow.
        assert timefmt.parse_when("9am", now) == tomorrow9
        assert timefmt.parse_when("fri 15:00", now) == int(
            datetime(2026, 11, 20, 15, 0).timestamp() * 1000
        )
        assert timefmt.parse_when("2026-12-01 10:00", now) == int(
            datetime(2026, 12, 1, 10, 0).timestamp() * 1000
        )
        for bad in ("5", "yesterday", "2020-01-01", "banana", "", "2099-01-01"):
            assert timefmt.parse_when(bad, now) is None

    def test_words(self):
        assert timefmt.relative_time(T0 - DAY, T0) == "yesterday"
        assert timefmt.relative_time(T0 - 21 * DAY, T0) == "3 weeks ago"
        assert timefmt.when_text(T0 - 21 * DAY, T0).startswith("3 weeks ago (")
        assert timefmt.due_text(T0 + 10 * MIN, T0).startswith("in 10 minutes (")
        assert timefmt.due_text(T0 + 30 * HOUR, T0).startswith("tomorrow at ")


# -- the store ----------------------------------------------------------------------


class TestStore:
    def test_turns_are_redacted_before_disk(self, core):
        store, _, _ = core
        res = store.append_turns(
            "chat", "s1", [{"speaker": "user", "text": "mera password Abc123! hai"}]
        )
        assert res["redacted"] == 1
        turn = store.get_turn(res["turn_ids"][0])
        assert "Abc123" not in turn["text"] and turn["flags"] & TURN_FLAGS["redacted"]

    def test_source_ref_dedupes_and_grows(self, core):
        store, _, _ = core
        store.append_turns(
            "voice", "s", [{"speaker": "user", "text": "hello the", "source_ref": "m1"}]
        )
        res = store.append_turns(
            "voice",
            "s",
            [{"speaker": "user", "text": "hello there friend", "source_ref": "m1"}],
        )
        assert res["updated"] == 1 and res["added"] == 0
        assert store.stats()["turns"] == 1

    def test_add_memory_dedupes_and_refuses(self, core):
        store, _, _ = core
        a = store.add_memory(text="Likes chai with cardamom", origin="stated")
        b = store.add_memory(text="likes   CHAI with cardamom!", origin="stated")
        assert b["duplicate_of"] == a["id"]
        assert store.add_memory(text="")["reason"] == "empty"
        assert (
            store.add_memory(text="x" * (LIMITS["memory_max_chars"] + 1))["reason"]
            == "too_long"
        )
        kept = store.add_memory(text="wifi password: Hunter22!", origin="stated")
        assert "Hunter22" not in store.get_memory(kept["id"])["text_original"]
        assert store.add_memory(text="4111 1111 1111 1111")["reason"] == "secret_only"

    def test_inferred_needs_two_sessions(self, core):
        store, _, _ = core
        m = store.add_memory(
            text="Probably works night shifts",
            origin="inferred",
            source_session="chat:a",
        )
        assert store.get_memory(m["id"])["status"] == "candidate"
        store.add_memory(
            text="Probably works night shifts",
            origin="inferred",
            source_session="chat:b",
        )
        assert store.get_memory(m["id"])["status"] == "active"

    def test_forget_leaves_a_tombstone(self, core):
        store, _, _ = core
        m = store.add_memory(text="Smokes two cigarettes a day", origin="stated")
        store.forget_memory(m["id"])
        assert (
            store.add_memory(text="Smokes two cigarettes a day", origin="inferred")[
                "reason"
            ]
            == "forgotten"
        )
        # The user saying it again brings it back.
        again = store.add_memory(
            text="Smokes two cigarettes a day", origin="manual", user_requested=True
        )
        assert again["ok"]

    def test_hard_forget_erases_the_quote(self, core):
        store, _, _ = core
        t = store.append_turns(
            "chat",
            "s",
            [{"speaker": "user", "text": "My sister Ayesha lives in Lahore"}],
        )["turn_ids"][0]
        m = store.add_memory(
            text="Sister Ayesha lives in Lahore",
            origin="stated",
            evidence=[{"turn_id": t, "quote": "sister Ayesha lives in Lahore"}],
        )
        store.forget_memory(m["id"], hard=True)
        assert "Ayesha" not in store.get_turn(t)["text"]
        assert store.get_memory(m["id"]) is None
        ops = [e["op"] for e in store.list_audit()]
        assert "hard_forget" in ops and all(
            "Ayesha" not in (e["before"] or "")
            for e in store.list_audit()
            if e["op"] == "hard_forget"
        )

    def test_supersede_moves_the_pin(self, core):
        store, _, _ = core
        old = store.add_memory(
            text="Lives in Karachi", type="identity", origin="stated", pinned=True
        )
        new = store.supersede(
            old["id"], {"text": "Lives in Lahore", "origin": "stated"}
        )
        assert store.get_memory(old["id"])["status"] == "superseded"
        assert store.get_memory(new["id"])["pinned"] == 1

    def test_pin_limit_is_refused_not_cut(self, core):
        store, _, _ = core
        for i in range(LIMITS["pinned_max"]):
            assert store.add_memory(
                text=f"Pinned fact number {i}", origin="manual", pinned=True
            )["ok"]
        assert store.add_memory(text="One too many", origin="manual", pinned=True)[
            "reason"
        ].startswith("pin_limit_count")

    def test_intentions(self, core, clock):
        store, _, _ = core
        a = store.add_intention(
            what="Call Ali about the payouts", due_ts=clock() + HOUR
        )
        assert (
            store.add_intention(what="call ali about the payouts")["duplicate_of"]
            == a["id"]
        )
        assert store.due_intentions(now=clock()) == []
        assert len(store.due_intentions(now=clock() + 2 * HOUR)) == 1
        store.add_intention(
            what="Old thing", due_ts=clock() - DAY, expiry_ts=clock() - 1
        )
        assert store.expire_intentions(clock()) == 1

    def test_word_overlap(self):
        assert (
            word_overlap(
                "payouts check karna", "Usman ko payouts check karne ka yaad dilana"
            )
            >= 0.6
        )
        assert word_overlap("call", "call Ali at 5") == 0


# -- search ------------------------------------------------------------------------


class TestSearch:
    def test_cross_script_and_synonyms(self, core):
        store, search, _ = core
        store.add_memory(
            text="User's mother (Ammi) has her birthday on 12 March",
            type="relationship",
            origin="stated",
            entities=[{"name": "Ammi", "aliases": ["Mother", "امی"]}],
        )
        store.add_memory(
            text="Tankhwa aati hai har mahine ki pehli tareekh ko", origin="stated"
        )
        assert search.recall("امی کی سالگرہ")["status"] == "FOUND"
        assert search.recall("mother birthday")["status"] == "FOUND"
        assert search.recall("salary kab aati hai")["status"] in ("FOUND", "WEAK")
        assert search.recall("quantum chromodynamics")["status"] == "NONE"

    def test_gates(self, core):
        store, search, _ = core
        store.add_memory(
            text="Has diabetes and takes insulin",
            origin="stated",
            sensitivity="sensitive",
        )
        store.add_memory(text="Maybe likes cricket a lot", origin="inferred")
        assert (
            search.recall("insulin diabetes", purpose="proactive")["status"] == "NONE"
        )
        assert search.recall("insulin diabetes")["status"] == "FOUND"
        assert search.recall("cricket", purpose="proactive")["items"] == []

    def test_character_scope(self, core):
        store, search, _ = core
        store.add_memory(
            text="Promised the companion a song about rain",
            origin="stated",
            scope="character:female_soulmate",
        )
        assert (
            search.recall("song about rain", character_id="female_soulmate")["status"]
            == "FOUND"
        )
        assert (
            search.recall("song about rain", character_id="male_jarvis")["status"]
            == "NONE"
        )

    def test_forgotten_turns_are_not_found(self, core):
        store, search, _ = core
        t = store.append_turns(
            "chat",
            "s",
            [{"speaker": "user", "text": "my secret recipe uses saffron and cardamom"}],
        )["turn_ids"]
        assert search.recall("saffron cardamom recipe")["items"]
        store.hide_turns(t)
        assert search.recall("saffron cardamom recipe")["items"] == []


# -- state card ----------------------------------------------------------------------


class TestStateCard:
    def test_card_sections(self, core, clock):
        store, search, _ = core
        store.add_memory(
            text="Always answer in Roman Urdu",
            type="instruction",
            origin="manual",
            pinned=True,
        )
        store.add_memory(
            text="Works as a backend engineer", type="identity", origin="stated"
        )
        store.add_intention(what="Remind about the dentist", due_ts=clock() + HOUR)
        store.append_turns(
            "chat", "old", [{"speaker": "user", "text": "kal milte hain"}]
        )
        card = MemoryContext(store, search).state_card("voice", now=clock())
        text = card["text"]
        assert "ALWAYS KEEP IN MIND" in text and "Roman Urdu" in text
        assert "ABOUT THEM" in text and "backend engineer" in text
        assert "OPEN PROMISES" in text and "dentist" in text
        assert "LAST TIME YOU TALKED" in text
        assert "not instructions" in text and "MEMORY RULES" in text

    def test_awareness_never_sees_sensitive(self, core, clock):
        store, search, _ = core
        store.add_memory(
            text="Going through a divorce",
            origin="stated",
            sensitivity="sensitive",
            pinned=True,
        )
        assert (
            "divorce"
            not in MemoryContext(store, search).state_card("awareness", now=clock())[
                "text"
            ]
        )
        assert (
            "divorce"
            in MemoryContext(store, search).state_card("voice", now=clock())["text"]
        )

    def test_due_block(self, core, clock):
        store, search, _ = core
        store.add_intention(what="Ask how the interview went", due_ts=clock() - MIN)
        block = MemoryContext(store, search).due_block(now=clock())
        assert (
            "PROMISES AND FOLLOW-UPS THAT ARE DUE" in block["text"]
            and len(block["ids"]) == 1
        )


# -- digest -----------------------------------------------------------------------------


def _talk(store, clock):
    return store.append_turns(
        "chat",
        "s1",
        [
            {
                "speaker": "user",
                "text": "Meri behen Ayesha ki shaadi 20 December ko hai",
                "ts": clock(),
            },
            {"speaker": "assistant", "text": "Wah! Mubarak ho.", "ts": clock() + 1},
            {
                "speaker": "user",
                "text": "Haan, aur mujhe kal tak gift order karna hai",
                "ts": clock() + 2,
            },
        ],
    )


class TestDigest:
    def test_gate_checks_quotes(self, core, clock):
        store, search, _ = core
        _talk(store, clock)
        reply = {
            "memories": [
                {"type": "relationship", "text_original": "Behen Ayesha ki shaadi 20 December ko hai", "text_en": "Sister Ayesha's wedding is on 20 December",
                 "quote": "Meri behen Ayesha ki shaadi 20 December ko hai", "turn": 1, "origin": "stated", "event_date": "2026-12-20",
                 "entities": [{"name": "Ayesha", "type": "person", "aliases": ["behen", "sister"]}]},
                {"type": "preference", "text_original": "Loves jazz", "text_en": "Loves jazz", "quote": "I really love jazz music", "turn": 3, "origin": "stated"},
                {"type": "preference", "text_original": "Likes Mubarak", "text_en": "x", "quote": "Wah! Mubarak ho.", "turn": 2, "origin": "stated"},
            ],
            "intentions": [{"what": "Gift order karna", "what_en": "Order a gift", "due": "2026-11-17", "quote": "mujhe kal tak gift order karna hai", "turn": 3}],
            "episodes": [{"first": 1, "last": 3, "title": "Sister's wedding", "summary_en": "Talked about Ayesha's wedding.", "mood": "happy", "salience": 0.7}],
        }  # fmt: skip
        calls = []

        def llm(system, user, max_tokens):
            calls.append(user)
            return LlmReply(json.dumps(reply))

        digest = MemoryDigest(store, search, llm)
        res = digest.digest_session("chat:s1", clock() + DIGEST["quiet_ms"])
        assert (
            res["ok"]
            and res["memories"] == 1
            and res["intentions"] == 1
            and res["episodes"] == 1
        )
        whys = sorted(r["why"] for r in res["rejected"])
        # Invented quote, and an assistant line as a "fact".
        assert whys == ["quote_not_found", "quote_not_found"]
        assert "CONVERSATION" in calls[0] and "[1] USER:" in calls[0]
        assert search.recall("sister wedding")["status"] == "FOUND"
        # Re-running finds nothing new.
        assert digest.digest_session("chat:s1")["reason"] == "nothing_new"

    def test_small_talk_is_skipped(self, core, clock):
        store, search, _ = core
        store.append_turns("chat", "s2", [{"speaker": "user", "text": "hi"}])
        res = MemoryDigest(store, search, lambda *a: LlmReply("{}")).digest_session(
            "chat:s2"
        )
        assert res["skipped"] and res["reason"] == "too_small"

    def test_llm_failure_is_a_reason(self, core, clock):
        store, search, _ = core
        _talk(store, clock)

        def broken(*_a):
            raise LlmError("timeout")

        assert (
            MemoryDigest(store, search, broken).digest_session("chat:s1")["reason"]
            == "llm:timeout"
        )


# -- recall agent ------------------------------------------------------------------------


class TestRecallAgent:
    def test_sources_must_be_real(self, core, clock):
        store, search, _ = core
        m = store.add_memory(text="Client Tanaka is from Osaka", origin="stated")
        agent = RecallAgent(store, search, name="Ghost")
        pack = agent.pack("the japanese client?", now=clock())
        assert f"[m{m['id']}]" in pack["system"] and "GHOST ASKS:" in pack["user"]
        ok = agent.finish(
            pack["id"],
            json.dumps(
                {
                    "status": "found",
                    "answer": "Tanaka, from Osaka.",
                    "sources": [f"m{m['id']}"],
                }
            ),
        )
        assert ok["status"] == "FOUND" and ok["memory_ids"] == [m["id"]]
        pack2 = agent.pack("the japanese client?", now=clock())
        made_up = agent.finish(
            pack2["id"],
            json.dumps({"status": "found", "answer": "Suzuki.", "sources": ["m999"]}),
        )
        assert made_up["status"] == "UNSURE"
        pack3 = agent.pack("x", now=clock())
        assert agent.finish(pack3["id"], "not json")["reason"] == "bad_reply"

    def test_ask_falls_back(self, core, clock):
        store, search, _ = core
        store.add_memory(text="Client Tanaka is from Osaka", origin="stated")
        agent = RecallAgent(store, search)
        assert agent.ask("tanaka", None)["reason"] == "no_llm"


# -- jobs and backups ------------------------------------------------------------------------


class TestJobsAndBackups:
    def test_queue(self, core, clock):
        store, _, _ = core
        jobs = JobQueue(store)
        a = jobs.enqueue("digest", {"k": 1}, "key-1", run_after=clock() + MIN)
        assert not jobs.enqueue("digest", {"k": 1}, "key-1")["created"]
        assert jobs.claim() is None
        clock.advance(2 * MIN)
        job = jobs.claim()
        assert job["id"] == a["id"] and job["attempts"] == 1
        assert jobs.fail(job["id"], "boom", max_attempts=1) == "dead"

    def test_backup_opens(self, core, tmp_path, clock):
        store, _, opened = core
        store.add_memory(text="Something to keep", origin="stated")
        made = backup_now(opened.db, tmp_path / "mem", clock())
        assert verify_backup(made["file"])["memories"] == 1
        assert list_backups(tmp_path / "mem")


# -- the service -------------------------------------------------------------------------------


class TestService:
    def test_never_raises_and_kill_switch(self, tmp_path):
        folder = tmp_path / "m"
        folder.mkdir()
        (folder / "DISABLED").write_text("")
        svc = MemoryCoreService(folder)
        assert svc.init()["reason"] == "disabled"
        assert svc.remember("x")["message"] == "Memory is off."

    def test_remember_follow_up_forget(self, tmp_path, clock):
        svc = MemoryCoreService(tmp_path / "m", now=clock)
        svc.append_turns(
            "voice",
            "v1",
            [{"speaker": "user", "text": "yaad rakhna meri favourite chai karak hai"}],
        )
        r = svc.remember("Favourite chai is karak", channel="voice", session_id="v1")
        assert r["ok"] and svc.store.get_memory(r["id"])["origin"] == "stated"
        f = svc.follow_up("Ask about the karak chai place", when="+10m")
        assert f["ok"] and "due in 10 minutes" in f["message"]
        assert svc.follow_up("x", when="banana")["reason"] == "bad_when"
        assert svc.follow_up("Ask about the karak chai place", done=True)["ok"]
        assert svc.forget_by_query("karak chai")["ok"]
        assert svc.recall("karak chai")["status"] == "NONE"

    def test_tick_runs_the_digest(self, tmp_path, clock):
        svc = MemoryCoreService(tmp_path / "m", now=clock)
        svc.append_turns("chat", "c", [
            {"speaker": "user", "text": "I just started learning the violin"},
            {"speaker": "user", "text": "practising every evening now"},
        ])  # fmt: skip
        seen = []

        def llm(system, user, n):
            seen.append(user)
            return LlmReply(json.dumps({"memories": [{"type": "routine", "text_original": "Practises violin every evening",
                "text_en": "Practises violin every evening", "quote": "practising every evening now", "turn": 2, "origin": "stated"}]}))  # fmt: skip

        svc.set_llm(llm)
        assert svc.tick()["ran"] is None  # the conversation has not gone quiet yet
        clock.advance(DIGEST["quiet_ms"] + MIN)
        out = svc.tick()
        assert out["ran"] == "digest" and out["result"]["memories"] == 1
        assert svc.recall("violin")["status"] == "FOUND"
        svc.set_busy(True)
        svc.append_turns(
            "chat",
            "c2",
            [{"speaker": "user", "text": "a"}, {"speaker": "user", "text": "b"}],
        )
        clock.advance(DIGEST["quiet_ms"] + MIN)
        assert svc.tick()["ran"] is None  # voice busy: the digest waits

    def test_ask_uses_agent_then_search(self, tmp_path, clock):
        svc = MemoryCoreService(tmp_path / "m", now=clock)
        svc.remember("Client Tanaka is from Osaka", source="user")
        assert svc.ask("tanaka")["via"] == "search"

        def llm(system, user, n):
            tag = next(part.split("]")[0] for part in system.split("[m")[1:])
            return LlmReply(
                json.dumps(
                    {
                        "status": "found",
                        "answer": "Tanaka from Osaka.",
                        "sources": [f"m{tag}"],
                    }
                )
            )

        svc.set_llm(llm)
        res = svc.ask("the japanese client")
        assert res["via"] == "agent" and res["status"] == "FOUND"
