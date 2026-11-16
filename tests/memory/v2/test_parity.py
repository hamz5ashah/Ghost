"""Memory v2 parity with Stonic's originals.

The golden files were produced by running Stonic's own JavaScript
(``memory/normalize.js``, ``memory/secrets.js``, ``file-memory/official.js``)
on the same inputs; the Python port must give byte-identical results. A
difference in normalisation would silently break recall of memories stored
by the other implementation; a difference in redaction could let a secret
reach the disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghost.memory.v2 import normalize, secrets
from ghost.memory.v2.files import official

HERE = Path(__file__).parent


def _load(name: str):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


NORMALIZE = _load("normalize_golden.json")
SECRETS = _load("secrets_golden.json")
OFFICIAL = _load("official_golden.json")


@pytest.mark.parametrize("case", NORMALIZE, ids=lambda c: repr(c["t"])[:40])
def test_normalize_matches_stonic(case):
    got = normalize.normalize_for_search(case["t"])
    assert (got.fold, got.skel, got.script) == (
        case["fold"],
        case["skel"],
        case["script"],
    )
    assert [[t.raw, t.fold, t.skel] for t in normalize.query_tokens(case["t"])] == case[
        "q"
    ]
    assert normalize.content_hash(case["t"]) == case["h"]


@pytest.mark.parametrize("case", SECRETS, ids=lambda c: repr(c["t"])[:40])
def test_secrets_match_stonic(case):
    got = secrets.redact_secrets(case["t"])
    assert got.text == case["text"]
    assert got.found == case["found"]
    assert secrets.has_secret(case["t"]) == case["has"]


def test_golden_sets_are_substantial():
    assert len(NORMALIZE) > 400
    assert sum(1 for c in SECRETS if c["found"]) > 150


class TestOfficialMemoryTexts:
    def test_load_index(self):
        for raw, exp in OFFICIAL["loadIndex"]:
            got = official.load_index(raw)
            assert {
                "content": got["content"], "lineCount": got["line_count"],
                "charCount": got["char_count"], "truncated": got["truncated"],
            } == exp  # fmt: skip

    def test_write_notice(self):
        for raw, exp in OFFICIAL["notice"]:
            got = official.index_write_notice(raw)
            assert (
                None
                if got is None
                else {"text": got["text"], "overCap": got["over_cap"]}
            ) == exp

    def test_stamp_note(self):
        for text, exp in OFFICIAL["stamp"]:
            assert (
                official.stamp_note(text, session_id="sess-1", now=1794000000123) == exp
            )

    def test_backfill_sizes_clip_section_age(self):
        for rel, desc, exp in OFFICIAL["backfill"]:
            assert official.backfill_line(rel, desc) == exp
        for n, a, b in OFFICIAL["size"]:
            assert (official.format_size(n), official.format_list_size(n)) == (a, b)
        for text, n, exp in OFFICIAL["clip"]:
            assert official.clip(text, n) == exp
        assert official.memory_section(tool="memory") == OFFICIAL["section"][0]
        assert (
            official.memory_section(folder="/home/u/notes", tool="write")
            == OFFICIAL["section"][1]
        )
        for mtime, now, exp in OFFICIAL["age"]:
            assert official.age_note(mtime, now) == exp


class TestSecretsBehaviour:
    """A few cases spelled out, so the intent is readable without the golden file."""

    @pytest.mark.parametrize(
        "text,kind",
        [
            ("mera password sunshine hai", "password"),
            ("Your OTP for login is 482913", "otp"),
            ("card 4111 1111 1111 1111", "card"),
            ("CNIC 12345-1234567-1", "cnic"),
            ("PK36SCBL0000001123456702", "iban"),
            ("key sk-ant-api03-Xk9fQ2mZ7pL4vR8tN1wB6yH3cJ5dG0sE", "api_key"),
        ],
    )
    def test_redacts(self, text, kind):
        assert kind in secrets.redact_secrets(text).found

    @pytest.mark.parametrize(
        "text",
        [
            "I paid 10,000 rupees",
            "call me at 0300 1234567 tomorrow",
            "password bhool gaya",
            "ye code line 1200 par crash",
            "order 48213 otp nahi aya",
        ],
    )
    def test_keeps_ordinary_numbers(self, text):
        assert secrets.redact_secrets(text).text == text

    def test_idempotent(self):
        once = secrets.redact_secrets("otp 482913 aur pin 4821 hai").text
        assert secrets.redact_secrets(once).text == once
