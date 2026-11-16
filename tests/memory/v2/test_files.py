"""File memory (Stonic port, phase 6): the official memory-tool commands, path safety, index, grep, migration."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from ghost.memory.v2.db import open_memory_db
from ghost.memory.v2.files import official
from ghost.memory.v2.files.migrate import (
    parse_entries,
    plan_migration,
    run_migration,
    slug_from,
)
from ghost.memory.v2.files.rules import build_classic_card, build_index_only
from ghost.memory.v2.files.store import FileMemory, glob_to_regex, parse_note
from ghost.memory.v2.store import MemoryStore

NOTE = "---\nname: sister-ayesha\ndescription: Sister Ayesha lives in Lahore\nmetadata:\n  type: user\n---\n\nAyesha is their younger sister.\n"


@pytest.fixture
def fm(tmp_path):
    return FileMemory(tmp_path / "notes", now=lambda: 1794000000000).ensure()


def run(fm, **args):
    return fm.command(args)


class TestMemoryTool:
    def test_create_view_replace_insert_rename_delete(self, fm):
        r = run(fm, command="create", path="/memories/sister-ayesha.md", file_text=NOTE)
        assert r["ok"] and r["text"] == official.memory_tool.created(
            "/memories/sister-ayesha.md"
        )
        raw = (fm.dir / "sister-ayesha.md").read_text()
        assert "node_type: memory" in raw and "modified: " in raw
        assert run(
            fm, command="create", path="/memories/sister-ayesha.md", file_text="x"
        )["text"].startswith("Error: File")
        view = run(fm, command="view", path="/memories/sister-ayesha.md")
        assert view["text"].startswith(
            "Here's the content of /memories/sister-ayesha.md with line numbers:"
        )
        assert "     1\t---" in view["text"]
        rep = run(
            fm,
            command="str_replace",
            path="/memories/sister-ayesha.md",
            old_str="younger",
            new_str="elder",
        )
        assert (
            rep["ok"]
            and "Here is the snippet showing the change" in rep["text"]
            and "elder" in rep["text"]
        )
        assert (
            "did not appear verbatim"
            in run(
                fm,
                command="str_replace",
                path="/memories/sister-ayesha.md",
                old_str="zzz",
                new_str="y",
            )["text"]
        )
        assert run(
            fm,
            command="insert",
            path="/memories/sister-ayesha.md",
            insert_line=0,
            insert_text="top",
        )["ok"]
        assert (
            "Invalid `insert_line`"
            in run(
                fm,
                command="insert",
                path="/memories/sister-ayesha.md",
                insert_line=999,
                insert_text="x",
            )["text"]
        )
        assert run(
            fm,
            command="rename",
            old_path="/memories/sister-ayesha.md",
            new_path="/memories/people/ayesha.md",
        )["ok"]
        listing = run(fm, command="view", path="/memories")
        assert "/memories/people/ayesha.md" in listing["text"] and listing[
            "text"
        ].startswith("Here're the files")
        assert (
            run(fm, command="delete", path="/memories/people/ayesha.md")["text"]
            == "Successfully deleted /memories/people/ayesha.md"
        )
        assert (
            run(fm, command="delete", path="/memories")["text"]
            == official.memory_tool.cannot_delete_root
        )
        assert run(fm, command="fly")["text"] == "Error: unknown command fly"

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "/memories/../secret.md",
            "/memories/.hidden.md",
            "/memories/con.md",
            "/memories/a:b.md",
            "memories/x.md",
            "/memoriesx/a.md",
        ],
    )
    def test_paths_are_confined(self, fm, path):
        r = run(fm, command="create", path=path, file_text="x")
        assert not r["ok"]

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_symlink_escape(self, fm, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (fm.dir / "link").symlink_to(outside, target_is_directory=True)
        assert (
            "escape"
            in run(fm, command="create", path="/memories/link/x.md", file_text="x")[
                "text"
            ]
        )

    def test_size_cap_refuses(self, fm):
        r = run(fm, command="create", path="/memories/big.md", file_text="x" * 60_001)
        assert not r["ok"] and "limit is 60000" in r["text"]

    def test_index_backfill_and_notice(self, fm):
        fm.bulk_write(
            [
                {"rel": "a.md", "text": NOTE},
                {"rel": "b.md", "text": "---\ndescription: Likes chai\n---\nchai"},
            ]
        )
        index = fm.read_index()
        assert (
            "- [a](a.md) — Sister Ayesha lives in Lahore" in index and "(b.md)" in index
        )
        big = "\n".join(f"- [n{i}](n{i}.md) — x" for i in range(170))
        run(fm, command="delete", path="/memories/MEMORY.md")
        r = run(fm, command="create", path="/memories/MEMORY.md", file_text=big)
        assert "approaching the 200-line read limit" in r["text"]
        (fm.dir / "MEMORY.md").write_text(index)
        assert (
            fm.delete_note("/memories/a.md")["ok"] and "(a.md)" not in fm.read_index()
        )

    def test_grep(self, fm):
        fm.bulk_write(
            [
                {"rel": "a.md", "text": NOTE},
                {"rel": "b.md", "text": "chai\nmore chai\n"},
            ]
        )
        # The index line for b.md mentions chai too — grep searches the index like Stonic's.
        assert fm.grep({"pattern": "chai"})["text"].startswith("Found 2 files")
        assert fm.grep({"pattern": "chai", "glob": "b.md"})["text"].startswith(
            "Found 1 file"
        )
        assert (
            "Found 2 total occurrences across 1 file."
            in fm.grep({"pattern": "chai", "glob": "b.md", "output_mode": "count"})[
                "text"
            ]
        )
        content = fm.grep({"pattern": "(?i)AYESHA", "output_mode": "content"})["text"]
        assert "/memories/a.md:" in content
        assert fm.grep({"pattern": "zzz"})["text"] == "No files found"
        assert not fm.grep({"pattern": "("})["ok"]
        assert glob_to_regex("*.{md,txt}").match("x.md") and not glob_to_regex(
            "*.md"
        ).match("a/x.md")

    def test_parse_note(self):
        p = parse_note(NOTE)
        assert p["name"] == "sister-ayesha" and p["type"] == "user" and p["has_header"]
        assert parse_note("# Heading\n\nfirst line")["description"] == "Heading"

    def test_rules(self, fm):
        card = build_classic_card("- [a](a.md) — x", 1794000000000)
        assert (
            card.startswith("# Memory")
            and "Today's date is" in card
            and "Contents of /memories/MEMORY.md" in card
        )
        assert build_index_only("").__eq__("")
        assert build_index_only("- [a](a.md)").startswith("YOUR NOTES ABOUT THEM")


class TestMigration:
    def test_entries_and_slugs(self):
        assert [
            e["text"] for e in parse_entries("a fact\n§\n[2026-01-02] another")
        ] == ["a fact", "another"]
        assert [
            e["text"]
            for e in parse_entries("# Notes\n\n- likes chai\n- lives in Lahore\n")
        ] == ["likes chai", "lives in Lahore"]
        assert slug_from("Ali bought a new motorcycle") == "ali-bought-new-motorcycle"
        assert slug_from("امی") == ""

    def test_plan_is_complete_and_deterministic(self):
        legacy = {
            "memories": [
                {"uid": "m_1", "type": "identity", "text_original": "Lahore mein rehta hai", "text_en": "Lives in Lahore", "pinned": 1, "scope": "user", "mentioned_at": 1, "recorded_at": 1, "updated_at": 1},
                {"uid": "m_2", "type": "preference", "text_original": "Lives in Lahore", "text_en": "", "pinned": 0, "scope": "user", "mentioned_at": 1, "recorded_at": 1, "updated_at": 1},
            ],
            "intentions": [{"uid": "i_1", "what": "Call Ali", "what_en": "Call Ali", "due_ts": 1794000000000, "character_id": "female_soulmate", "created_at": 1}],
            "episodes": [{"uid": "e1", "title": "Wedding", "summary_en": "Talked.", "character_id": None, "start_ts": 1794000000000}],
        }  # fmt: skip
        a = plan_migration(
            legacy,
            [{"text": "Likes chai", "noted": "", "source": "x"}],
            set(),
            1794000000000,
        )
        b = plan_migration(
            legacy,
            [{"text": "Likes chai", "noted": "", "source": "x"}],
            set(),
            1794000000000,
        )
        assert [n["rel"] for n in a["notes"]] == [n["rel"] for n in b["notes"]]
        assert a["counts"] == {
            "memories": 1,
            "follow_ups": 1,
            "conversation_notes": 1,
            "extras": 1,
            "duplicates_skipped": 1,
        }
        follow = next(n for n in a["notes"] if n["rel"].startswith("follow-up"))
        assert "Promised by Soulmate" in follow["text"]

    def test_run_once_with_backup(self, tmp_path, fm):
        core_dir = tmp_path / "core"
        opened = open_memory_db(core_dir)
        store = MemoryStore(opened.db)
        store.add_memory(
            text="Works as a backend engineer", type="identity", origin="stated"
        )
        opened.db.conn.close()
        mem_md = tmp_path / "MEMORY.md"
        mem_md.write_text("- Prefers short answers\n")
        sources = {
            "core_db": core_dir / "memory.db",
            "memory_md": mem_md,
            "user_md": None,
            "facts_jsonl": None,
        }
        first = run_migration(fm, sources)
        assert first["ok"] and first["marker"]["counts"]["notes_written"] == 2
        backup = first["marker"]["backup"]
        assert (
            tmp_path / "notes" / ".old-memory-backup"
        ).exists() and "memory.db" in first["marker"]["copied"]
        assert mem_md.read_text() == "- Prefers short answers\n"  # original untouched
        assert run_migration(fm, sources)["skipped"]
        assert json.loads((fm.dir / ".migration.json").read_text())["backup"] == backup
        # The notes are invisible to the memory tool's listing of hidden files.
        assert (
            ".old-memory-backup"
            not in run(fm, command="view", path="/memories")["text"]
        )
        sqlite3.connect(str(core_dir / "memory.db")).close()
