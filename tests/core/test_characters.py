"""Tests for the ported Stonic character delivery profiles (Phase 1).

The contract: loading characters never raises, a malformed file is skipped
rather than taking the voice settings down, dev-only characters stay out of the
UI list, and a character id can never escape the characters directory.
"""

from __future__ import annotations

import pytest

from ghost.speech.characters import (
    DEV_ONLY,
    Character,
    characters_dir,
    load_character,
    load_characters,
    natural_voice_clause,
    perform_examples_block,
    render_delivery_prompt,
    script_rule,
    selectable_characters,
)


class TestShippedCharacters:
    def test_characters_directory_is_found(self):
        assert characters_dir() is not None

    def test_both_recovered_characters_load(self):
        ids = {c.id for c in load_characters()}
        assert {"male_jarvis", "female_soulmate"} <= ids

    def test_jarvis_carries_its_delivery_profile(self):
        jarvis = load_character("male_jarvis")
        assert jarvis is not None
        assert jarvis.natural_voice == "calm, composed, a touch dry"
        assert jarvis.script_example_roman
        assert jarvis.script_example_devanagari
        assert "[chuckle]" in jarvis.examples

    def test_soulmate_is_the_offered_default(self):
        # Phase 6: Stonic's female character is Ghost's default companion.
        from ghost.speech.characters import DEFAULT_CHARACTER

        assert "female_soulmate" not in DEV_ONLY
        soulmate = load_character("female_soulmate")
        assert soulmate is not None and soulmate.dev_only is False
        assert "female_soulmate" in {c.id for c in selectable_characters()}
        assert DEFAULT_CHARACTER == "female_soulmate"

    def test_jarvis_is_offered(self):
        assert "male_jarvis" in {c.id for c in selectable_characters()}

    def test_soulmate_life_table_is_preserved(self):
        soulmate = load_character("female_soulmate")
        assert soulmate is not None
        # Raw data for Phase 3; nothing consumes it yet.
        assert soulmate.life.get("enabled") is True
        assert soulmate.life.get("names_herself") is True

    def test_display_name_drops_the_gender_prefix(self):
        assert Character(id="male_jarvis").display_name == "Jarvis"
        assert Character(id="female_soulmate").display_name == "Soulmate"
        assert Character(id="butler").display_name == "Butler"

    def test_to_dict_exposes_no_prompt_text(self):
        payload = load_character("male_jarvis").to_dict()
        assert set(payload) == {
            "id",
            "display_name",
            "natural_voice",
            "dev_only",
            "has_examples",
        }
        assert payload["has_examples"] is True


class TestRobustness:
    def test_missing_directory_yields_no_characters(self, tmp_path):
        assert load_characters(tmp_path / "absent") == []

    def test_empty_directory_yields_no_characters(self, tmp_path):
        assert load_characters(tmp_path) == []

    def test_malformed_file_is_skipped_not_raised(self, tmp_path):
        (tmp_path / "broken.toml").write_text("this is not = valid toml [[[")
        (tmp_path / "good.toml").write_text('natural_voice = "fine"\n')
        found = load_characters(tmp_path)
        assert [c.id for c in found] == ["good"]

    def test_file_with_no_known_keys_still_loads(self, tmp_path):
        (tmp_path / "bare.toml").write_text("unrelated = 1\n")
        found = load_characters(tmp_path)
        assert len(found) == 1
        assert found[0].natural_voice == ""
        assert found[0].examples == ""

    def test_non_dict_life_table_is_ignored(self, tmp_path):
        (tmp_path / "odd.toml").write_text('life = "not a table"\n')
        assert load_characters(tmp_path)[0].life == {}

    @pytest.mark.parametrize(
        "bad_id",
        ["../secrets", "../../etc/passwd", "/etc/passwd", "a/b", "a\\b", "", ".hidden"],
    )
    def test_character_id_cannot_escape_the_directory(self, bad_id, tmp_path):
        assert load_character(bad_id, tmp_path) is None

    def test_unknown_id_returns_none(self, tmp_path):
        assert load_character("nope", tmp_path) is None

    def test_override_directory_is_honoured(self, tmp_path, monkeypatch):
        (tmp_path / "custom.toml").write_text('natural_voice = "custom"\n')
        monkeypatch.setenv("GHOST_CHARACTERS_DIR", str(tmp_path))
        assert [c.id for c in load_characters()] == ["custom"]

    def test_override_pointing_nowhere_yields_no_characters(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GHOST_CHARACTERS_DIR", str(tmp_path / "absent"))
        assert load_characters() == []


class TestPromptRendering:
    def test_script_rule_quotes_both_scripts(self):
        rule = script_rule(load_character("male_jarvis"))
        assert "Roman letters" in rule
        assert "ji Sir, abhi karta hoon" in rule
        assert "जी सर, अभी करता हूँ" in rule

    def test_script_rule_is_empty_without_both_examples(self):
        assert script_rule(Character(id="x", script_example_roman="only")) == ""
        assert script_rule(Character(id="x", script_example_devanagari="only")) == ""
        assert script_rule(Character(id="x")) == ""

    def test_natural_voice_clause_wraps_with_dashes(self):
        assert natural_voice_clause(Character(id="x", natural_voice="calm")) == (
            " — calm —"
        )

    def test_natural_voice_clause_is_empty_when_unset(self):
        # The sentence has to read correctly with nothing substituted in.
        assert natural_voice_clause(Character(id="x")) == ""

    def test_examples_block_is_empty_when_unset(self):
        assert perform_examples_block(Character(id="x")) == ""

    def test_render_fills_every_placeholder(self):
        template = "A {{SCRIPT_RULE}} B{{NATURAL_VOICE}} C {{PERFORM_EXAMPLES}}"
        out = render_delivery_prompt(template, load_character("male_jarvis"))
        for placeholder in (
            "{{SCRIPT_RULE}}",
            "{{NATURAL_VOICE}}",
            "{{PERFORM_EXAMPLES}}",
        ):
            assert placeholder not in out

    def test_render_leaves_no_placeholder_for_a_bare_character(self):
        template = "A {{SCRIPT_RULE}} B{{NATURAL_VOICE}} C {{PERFORM_EXAMPLES}}"
        out = render_delivery_prompt(template, Character(id="bare"))
        assert "{{" not in out


class TestShippedDeliveryPrompt:
    def _template(self):
        base = characters_dir().parent / "prompts" / "voice_delivery.md"
        return base.read_text()

    def test_delivery_prompt_ships(self):
        assert "How you perform" in self._template()

    def test_delivery_prompt_does_not_deny_being_an_ai(self):
        # Ghost's baseline prompt states plainly that it is an AI assistant;
        # Stonic's original told the character never to say so. That stance was
        # deliberately dropped — assert it stays dropped.
        text = self._template().lower()
        body = text.split("---", 1)[-1]
        assert "never call yourself an ai" not in body
        assert "you never use the word" not in body

    def test_delivery_prompt_claims_no_other_brand(self):
        body = self._template().split("---", 1)[-1]
        assert "Stonic" not in body
        assert "Usman" not in body

    def test_delivery_prompt_keeps_the_bracket_system(self):
        body = self._template().split("---", 1)[-1]
        for marker in ("[laugh]", "[short pause]", "[soft and slow]"):
            assert marker in body


class TestCharactersEndpoint:
    """`GET /v1/stonic/characters` is advisory and flag-gated."""

    def _call(self, characters_enabled: bool):
        import asyncio

        from ghost.core.config import GhostConfig
        from ghost.server.api_routes import stonic_characters

        config = GhostConfig()
        config.stonic.characters = characters_enabled

        class _Obj:
            pass

        state, app, request = _Obj(), _Obj(), _Obj()
        state.config = config
        app.state = state
        request.app = app
        return asyncio.run(stonic_characters(request))

    def test_reports_nothing_while_the_flag_is_off(self):
        result = self._call(False)
        assert result == {"characters": [], "enabled": False}

    def test_lists_selectable_characters_once_enabled(self):
        result = self._call(True)
        assert result["enabled"] is True
        ids = [c["id"] for c in result["characters"]]
        assert "male_jarvis" in ids
        # The female companion is offered, and is the default.
        assert "female_soulmate" in ids
        assert result["default"] == "female_soulmate"

    def test_payload_carries_no_prompt_text(self):
        result = self._call(True)
        for entry in result["characters"]:
            assert "examples" not in entry
            assert "life" not in entry
