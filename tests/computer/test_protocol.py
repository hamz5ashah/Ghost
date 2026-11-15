"""Tests for the computer-use provider, coordinate and OCR rules.

The coordinate tests matter most: a wrong convention here does not fail
loudly, it just puts every click in the wrong place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ghost.computer import protocol as p

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class TestProviderTable:
    def test_the_four_direct_providers_are_declared(self):
        assert set(p.PROVIDERS) == {"openrouter", "anthropic", "gemini", "deepseek"}

    @pytest.mark.parametrize("pid", sorted(p.PROVIDERS))
    def test_every_provider_reads_a_key_ghost_manages(self, pid):
        # These are the names the Tauri shell injects from the OS keyring.
        managed = {
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
            "DEEPSEEK_API_KEY",
        }
        assert set(p.PROVIDERS[pid].key_envs) <= managed

    def test_no_provider_points_at_the_old_stonic_gateway(self):
        for spec in p.PROVIDERS.values():
            assert "stonic" not in (spec.base_url or "").lower()

    def test_gemini_has_the_openai_compatible_endpoint_gui_agents_requires(self):
        # gui-agents' Gemini engine raises without a base_url.
        assert p.PROVIDERS["gemini"].base_url.startswith(
            "https://generativelanguage.googleapis.com/"
        )

    def test_deepseek_is_planner_only(self):
        assert not p.can_ground("deepseek")
        assert p.PROVIDERS["deepseek"].default_grounding_model is None

    @pytest.mark.parametrize("pid", ["openrouter", "anthropic", "gemini"])
    def test_grounding_providers_can_ground(self, pid):
        assert p.can_ground(pid)

    def test_unknown_provider_is_rejected_by_name(self):
        with pytest.raises(ValueError, match="unknown provider"):
            p.provider("stonic-cloud")

    def test_grounding_preference_never_lists_a_provider_that_cannot_ground(self):
        for pid in p.GROUNDING_PREFERENCE:
            assert p.can_ground(pid)


class TestKeys:
    def test_key_is_read_from_the_env(self):
        assert p.api_key_for("anthropic", {"ANTHROPIC_API_KEY": "sk-a"}) == "sk-a"

    def test_whitespace_only_key_counts_as_missing(self):
        assert p.api_key_for("anthropic", {"ANTHROPIC_API_KEY": "  "}) == ""

    def test_gemini_falls_back_to_google_api_key(self):
        assert p.api_key_for("gemini", {"GOOGLE_API_KEY": "g"}) == "g"

    def test_gemini_prefers_gemini_api_key(self):
        env = {"GEMINI_API_KEY": "first", "GOOGLE_API_KEY": "second"}
        assert p.api_key_for("gemini", env) == "first"


class TestChoose:
    def test_auto_picks_claude_to_plan_and_openrouter_to_ground(self):
        env = {"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"}
        choice = p.choose(env)
        assert choice.planner.provider == "anthropic"
        assert choice.grounding.provider == "openrouter"
        assert choice.grounding.model == "bytedance/ui-tars-1.5-7b"

    def test_a_single_claude_key_covers_both_roles(self):
        choice = p.choose({"ANTHROPIC_API_KEY": "a"})
        assert choice.planner.provider == "anthropic"
        assert choice.grounding.provider == "anthropic"

    def test_a_single_gemini_key_covers_both_roles(self):
        choice = p.choose({"GEMINI_API_KEY": "g"})
        assert (choice.planner.provider, choice.grounding.provider) == (
            "gemini",
            "gemini",
        )

    def test_deepseek_alone_cannot_run_computer_use(self):
        # It can plan, but nothing can ground.
        with pytest.raises(p.ProviderError) as exc:
            p.choose({"DEEPSEEK_API_KEY": "d"})
        assert exc.value.code == "no_key"
        assert "grounding" in str(exc.value)

    def test_deepseek_plans_when_something_else_grounds(self):
        choice = p.choose(
            {"DEEPSEEK_API_KEY": "d", "GEMINI_API_KEY": "g"},
            planner_spec="deepseek",
        )
        assert choice.planner.provider == "deepseek"
        assert choice.planner.model == "deepseek-flash"
        assert choice.grounding.provider == "gemini"

    def test_explicit_model_overrides_the_default(self):
        choice = p.choose(
            {"ANTHROPIC_API_KEY": "a"}, planner_spec="anthropic:claude-opus-5-5"
        )
        assert choice.planner.model == "claude-opus-5-5"
        assert choice.grounding.model == "claude-sonnet-5-5"

    def test_explicit_provider_without_a_key_is_explained(self):
        with pytest.raises(p.ProviderError) as exc:
            p.choose({"ANTHROPIC_API_KEY": "a"}, planner_spec="gemini")
        assert exc.value.code == "no_key"
        assert "Gemini" in str(exc.value)
        assert "Settings → Cloud providers" in str(exc.value)

    def test_deepseek_as_grounding_is_refused_with_a_reason(self):
        with pytest.raises(p.ProviderError) as exc:
            p.choose(
                {"DEEPSEEK_API_KEY": "d", "ANTHROPIC_API_KEY": "a"},
                grounding_spec="deepseek",
            )
        assert exc.value.code == "grounding_unsupported"

    def test_no_keys_at_all_is_explained(self):
        with pytest.raises(p.ProviderError) as exc:
            p.choose({})
        assert exc.value.code == "no_key"

    def test_spec_is_case_and_space_tolerant(self):
        choice = p.choose({"ANTHROPIC_API_KEY": "a"}, planner_spec="  Anthropic  ")
        assert choice.planner.provider == "anthropic"

    def test_keys_are_not_in_the_repr(self):
        choice = p.choose({"ANTHROPIC_API_KEY": "sk-very-secret"})
        assert "sk-very-secret" not in repr(choice)


class TestScreenshotCap:
    def test_claude_anywhere_caps_screenshots_at_1280(self):
        choice = p.choose({"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"})
        # Planner Claude (1280) + grounding UI-TARS (1920): the smaller wins.
        assert choice.screenshot_max_dim == 1280

    def test_ui_tars_and_gemini_keep_stonics_1920(self):
        choice = p.choose(
            {"GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": "o"}, planner_spec="gemini"
        )
        assert choice.screenshot_max_dim == 1920


class TestEngineParams:
    def test_openrouter_params_match_what_stonic_sent(self):
        choice = p.ModelChoice("openrouter", "bytedance/ui-tars-1.5-7b", "o")
        assert p.engine_params(choice) == {
            "engine_type": "open_router",
            "model": "bytedance/ui-tars-1.5-7b",
            "api_key": "o",
            "temperature": None,
            "base_url": "https://openrouter.ai/api/v1",
        }

    def test_anthropic_uses_the_native_engine_without_a_base_url(self):
        params = p.engine_params(p.ModelChoice("anthropic", "claude-sonnet-5-5", "a"))
        assert params["engine_type"] == "anthropic"
        assert "base_url" not in params

    def test_deepseek_goes_through_the_openai_engine_at_its_own_url(self):
        params = p.engine_params(p.ModelChoice("deepseek", "deepseek-flash", "d"))
        assert params["engine_type"] == "openai"
        assert params["base_url"] == "https://api.deepseek.com"


# ---------------------------------------------------------------------------
# Coordinates — where clicks actually land
# ---------------------------------------------------------------------------


def _land(choice, reply, screen, shot):
    """Run the full path gui-agents takes: parse, then resize_coordinates."""
    gw, gh = p.grounding_size(choice, *shot)
    x, y = p.parse_point(choice, reply)
    return round(x * screen[0] / gw), round(y * screen[1] / gh)


class TestScaledSize:
    def test_a_1080p_screen_is_untouched(self):
        assert p.scaled_size(1920, 1080, 1920) == (1920, 1080)

    def test_a_2k_screen_shrinks_to_1080p(self):
        assert p.scaled_size(2560, 1440, 1920) == (1920, 1080)

    def test_claudes_cap_shrinks_1080p(self):
        assert p.scaled_size(1920, 1080, 1280) == (1280, 720)

    def test_truncates_like_stonic(self):
        # 3440x1440 ultrawide: 1440 * 1920/3440 = 803.7 -> 803, not 804.
        assert p.scaled_size(3440, 1440, 1920) == (1920, 803)

    def test_portrait_screens_cap_the_tall_edge(self):
        assert p.scaled_size(1080, 1920, 1280) == (720, 1280)

    def test_rejects_a_zero_screen(self):
        with pytest.raises(ValueError):
            p.scaled_size(0, 1080, 1920)


class TestGroundingSize:
    def test_pixel_models_use_the_screenshot_size(self):
        choice = p.ModelChoice("openrouter", "bytedance/ui-tars-1.5-7b", "o")
        assert p.grounding_size(choice, 1920, 1080) == (1920, 1080)

    def test_gemini_uses_its_normalized_range_whatever_the_image(self):
        choice = p.ModelChoice("gemini", "gemini-3.8-flash", "g")
        assert p.grounding_size(choice, 1920, 1080) == (1000, 1000)
        assert p.grounding_size(choice, 1280, 720) == (1000, 1000)

    def test_deepseek_cannot_ground(self):
        with pytest.raises(ValueError):
            p.grounding_size(p.ModelChoice("deepseek", "deepseek-flash", "d"), 1, 1)


class TestParsePoint:
    def test_pixel_reply_is_read_as_x_then_y(self):
        choice = p.ModelChoice("openrouter", "ui-tars", "o")
        assert p.parse_point(choice, "(812, 344)") == [812, 344]

    def test_gemini_reply_is_swapped_from_y_x(self):
        choice = p.ModelChoice("gemini", "gemini-3.8-flash", "g")
        # Gemini answers [y, x].
        assert p.parse_point(choice, "[300, 700]") == [700, 300]

    def test_gemini_reply_inside_json_is_still_read(self):
        choice = p.ModelChoice("gemini", "gemini-3.8-flash", "g")
        reply = '```json\n[{"point": [250, 900], "label": "Save"}]\n```'
        assert p.parse_point(choice, reply) == [900, 250]

    def test_gemini_values_are_clamped_to_its_range(self):
        choice = p.ModelChoice("gemini", "gemini-3.8-flash", "g")
        assert p.parse_point(choice, "[1200, -5]") == [0, 1000]

    def test_a_reply_with_one_number_is_refused_not_clicked_at_zero(self):
        choice = p.ModelChoice("anthropic", "claude-sonnet-5-5", "a")
        with pytest.raises(ValueError, match="no point"):
            p.parse_point(choice, "I clicked 3 times")

    def test_an_empty_reply_is_refused(self):
        with pytest.raises(ValueError):
            p.parse_point(p.ModelChoice("anthropic", "m", "a"), "")


class TestClicksLandWhereTheModelPointed:
    """End to end: the same on-screen spot, through each provider's convention.

    The target is the centre of a 2560x1440 screen, (1280, 720).
    """

    SCREEN = (2560, 1440)

    def test_ui_tars_on_a_2k_screen(self):
        choice = p.ModelChoice("openrouter", "bytedance/ui-tars-1.5-7b", "o")
        shot = p.scaled_size(*self.SCREEN, 1920)  # (1920, 1080)
        assert _land(choice, "(960, 540)", self.SCREEN, shot) == (1280, 720)

    def test_claude_on_a_2k_screen(self):
        choice = p.ModelChoice("anthropic", "claude-sonnet-5-5", "a")
        shot = p.scaled_size(*self.SCREEN, 1280)  # (1280, 720)
        assert _land(choice, "(640, 360)", self.SCREEN, shot) == (1280, 720)

    def test_gemini_on_a_2k_screen(self):
        choice = p.ModelChoice("gemini", "gemini-3.8-flash", "g")
        shot = p.scaled_size(*self.SCREEN, 1920)
        # Centre in Gemini's convention is [y=500, x=500].
        assert _land(choice, "[500, 500]", self.SCREEN, shot) == (1280, 720)

    def test_gemini_off_centre_is_not_transposed(self):
        # The bug a naive port would have: [y, x] read as (x, y). A point near
        # the top-right corner must land top-right, not bottom-left.
        choice = p.ModelChoice("gemini", "gemini-3.8-flash", "g")
        shot = p.scaled_size(*self.SCREEN, 1920)
        x, y = _land(choice, "[100, 900]", self.SCREEN, shot)
        assert x > self.SCREEN[0] * 0.8  # right
        assert y < self.SCREEN[1] * 0.2  # top


class TestGroundingPrompt:
    def test_pixel_prompt_is_gui_agents_own_verbatim(self):
        choice = p.ModelChoice("openrouter", "ui-tars", "o")
        assert p.grounding_prompt(choice, "Save button") == (
            "Query:Save button\n"
            "Output only the coordinate of one point in your response.\n"
        )

    def test_gemini_prompt_asks_for_its_trained_convention(self):
        prompt = p.grounding_prompt(
            p.ModelChoice("gemini", "gemini-3.8-flash", "g"), "Save button"
        )
        assert "Save button" in prompt
        assert "[y, x]" in prompt
        assert "0-1000" in prompt


# ---------------------------------------------------------------------------
# Tesseract
# ---------------------------------------------------------------------------


def _fake_tesseract(root: Path, with_eng: bool = True) -> Path:
    exe = root / p.TESSERACT_EXE
    exe.write_text("")
    if with_eng:
        (root / "tessdata").mkdir()
        (root / "tessdata" / "eng.traineddata").write_text("")
    return exe


class TestLocateTesseract:
    def _no_path(self, name):
        return None

    def test_explicit_path_wins(self, tmp_path):
        exe = _fake_tesseract(tmp_path)
        loc = p.locate_tesseract(explicit=str(exe), env={}, which=self._no_path)
        assert (loc.path, loc.source) == (str(exe), "explicit")
        assert loc.tessdata == str(tmp_path / "tessdata")

    def test_a_wrong_explicit_path_is_reported_missing_not_silently_replaced(
        self, tmp_path
    ):
        bundled = _fake_tesseract(tmp_path)
        loc = p.locate_tesseract(
            explicit=str(tmp_path / "nope.exe"),
            env={"GHOST_TESSERACT": str(bundled)},
            which=self._no_path,
        )
        assert loc.source == "missing"
        assert loc.path is None

    def test_bundled_copy_from_the_desktop_app_is_found(self, tmp_path):
        exe = _fake_tesseract(tmp_path)
        loc = p.locate_tesseract(env={"GHOST_TESSERACT": str(exe)}, which=self._no_path)
        assert (loc.path, loc.source) == (str(exe), "bundled")

    def test_bundled_beats_path(self, tmp_path):
        exe = _fake_tesseract(tmp_path)
        loc = p.locate_tesseract(
            env={"GHOST_TESSERACT": str(exe)}, which=lambda n: "/usr/bin/tesseract"
        )
        assert loc.source == "bundled"

    def test_path_is_used_when_nothing_is_bundled(self, tmp_path):
        exe = _fake_tesseract(tmp_path)
        loc = p.locate_tesseract(env={}, which=lambda n: str(exe))
        assert loc.source == "path"

    def test_standard_windows_install_folder_is_checked(self, tmp_path):
        install = tmp_path / "Programs" / "Tesseract-OCR"
        install.mkdir(parents=True)
        (install / "tesseract.exe").write_text("")
        loc = p.locate_tesseract(
            env={"LOCALAPPDATA": str(tmp_path)}, which=self._no_path, platform="win32"
        )
        assert loc.source == "system"
        assert loc.path == str(install / "tesseract.exe")

    def test_nothing_anywhere_is_missing(self):
        loc = p.locate_tesseract(env={}, which=self._no_path, platform="linux")
        assert (loc.path, loc.source) == (None, "missing")

    def test_tessdata_falls_back_to_the_env_prefix(self, tmp_path):
        exe = _fake_tesseract(tmp_path, with_eng=False)
        data = tmp_path / "elsewhere"
        data.mkdir()
        loc = p.locate_tesseract(
            explicit=str(exe), env={"TESSDATA_PREFIX": str(data)}, which=self._no_path
        )
        assert loc.tessdata == str(data)


def _completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr
    )


class TestProbeTesseract:
    LOC = p.TesseractLocation("/opt/tess/tesseract", "/opt/tess/tessdata", "bundled")

    def _runner(self, version, langs, code=0):
        def run(cmd, **kwargs):
            if "--version" in cmd:
                return _completed(stdout=version, code=code)
            return _completed(stdout=langs)

        return run

    def test_a_working_install_with_english_is_available(self):
        status = p.probe_tesseract(
            self.LOC,
            run=self._runner(
                "tesseract v5.4.0.20240606\n leptonica-1.84.1",
                'List of available languages in "/opt/tess/tessdata/" (2):\neng\nosd\n',
            ),
        )
        assert status.available
        assert status.version == "5.4.0.20240606"
        assert status.languages == ["eng", "osd"]
        assert status.error == ""

    def test_version_printed_on_stderr_is_still_read(self):
        def run(cmd, **kwargs):
            if "--version" in cmd:
                return _completed(stderr="tesseract 5.3.4")
            return _completed(stdout="List of available languages (1):\neng\n")

        assert p.probe_tesseract(self.LOC, run=run).version == "5.3.4"

    def test_missing_english_data_is_not_available_and_says_why(self):
        status = p.probe_tesseract(
            self.LOC,
            run=self._runner(
                "tesseract 5.4.0", "List of available languages (1):\nosd\n"
            ),
        )
        assert not status.available
        assert "eng.traineddata" in status.error

    def test_a_missing_binary_explains_what_breaks(self):
        status = p.probe_tesseract(p.TesseractLocation(None, None, "missing"))
        assert not status.available
        assert "Text selection" in status.error

    def test_a_binary_that_will_not_start_is_reported_not_raised(self):
        def run(cmd, **kwargs):
            raise OSError("[WinError 193] not a valid Win32 application")

        status = p.probe_tesseract(self.LOC, run=run)
        assert not status.available
        assert "would not start" in status.error

    def test_a_hung_binary_is_reported_not_raised(self):
        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 15)

        assert not p.probe_tesseract(self.LOC, run=run).available

    def test_a_crashing_binary_is_not_available(self):
        status = p.probe_tesseract(
            self.LOC,
            run=self._runner("error while loading shared libraries", "", code=127),
        )
        assert not status.available
        assert "failed to run" in status.error

    def test_the_probe_points_tesseract_at_its_tessdata(self):
        seen = {}

        def run(cmd, **kwargs):
            seen.update(kwargs["env"])
            return _completed(stdout="tesseract 5.4.0\neng\n")

        p.probe_tesseract(self.LOC, run=run)
        assert seen["TESSDATA_PREFIX"] == "/opt/tess/tessdata"

    def test_to_dict_is_json_ready(self):
        status = p.probe_tesseract(
            self.LOC, run=self._runner("tesseract 5.4.0", "List of (1):\neng\n")
        )
        payload = status.to_dict()
        assert payload["available"] is True
        assert payload["has_english"] is True
        assert payload["source"] == "bundled"


class TestParseLanguages:
    def test_skips_the_header_line(self):
        out = (
            'List of available languages in "C:\\tess\\tessdata\\" (3):\n'
            "eng\nosd\nurd\n"
        )
        assert p.parse_languages(out) == ["eng", "osd", "urd"]

    def test_ignores_noise_lines(self):
        assert p.parse_languages("Warning: something odd\neng\n") == ["eng"]

    def test_empty_output_is_no_languages(self):
        assert p.parse_languages("") == []


# ---------------------------------------------------------------------------
# Sidecar env + fingerprint
# ---------------------------------------------------------------------------


class TestSidecarEnv:
    CHOICE = p.AgentChoice(
        planner=p.ModelChoice("anthropic", "claude-sonnet-5-5", "sk-ant"),
        grounding=p.ModelChoice("openrouter", "bytedance/ui-tars-1.5-7b", "sk-or"),
    )
    OCR = p.TesseractLocation("C:/tess/tesseract.exe", "C:/tess/tessdata", "bundled")

    def test_carries_both_roles(self):
        env = p.sidecar_env(self.CHOICE, self.OCR, {})
        assert env["GHOST_COMPUTER_PLANNER_PROVIDER"] == "anthropic"
        assert env["GHOST_COMPUTER_PLANNER_API_KEY"] == "sk-ant"
        assert env["GHOST_COMPUTER_GROUNDING_PROVIDER"] == "openrouter"
        assert env["GHOST_COMPUTER_GROUNDING_API_KEY"] == "sk-or"
        assert env["GHOST_COMPUTER_SCREENSHOT_MAX_DIM"] == "1280"

    def test_strips_every_provider_key_it_does_not_need(self):
        base = {
            "PATH": "/bin",
            "OPENAI_API_KEY": "nope",  # not a computer-use provider; passes through
            "ANTHROPIC_API_KEY": "sk-ant",
            "DEEPSEEK_API_KEY": "sk-ds",
            "GEMINI_API_KEY": "sk-gem",
        }
        env = p.sidecar_env(self.CHOICE, self.OCR, base)
        assert "DEEPSEEK_API_KEY" not in env
        assert "GEMINI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env  # delivered by role, not by name
        assert env["PATH"] == "/bin"

    def test_carries_the_ocr_paths(self):
        env = p.sidecar_env(self.CHOICE, self.OCR, {})
        assert env["GHOST_COMPUTER_TESSERACT"] == "C:/tess/tesseract.exe"
        assert env["TESSDATA_PREFIX"] == "C:/tess/tessdata"

    def test_no_ocr_means_no_ocr_vars(self):
        env = p.sidecar_env(self.CHOICE, p.TesseractLocation(None, None, "missing"), {})
        assert "GHOST_COMPUTER_TESSERACT" not in env

    def test_unbuffered_utf8_output_for_ndjson(self):
        env = p.sidecar_env(self.CHOICE, self.OCR, {})
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["PYTHONIOENCODING"] == "utf-8"


class TestFingerprint:
    BASE = TestSidecarEnv.CHOICE
    OCR = TestSidecarEnv.OCR

    def test_is_stable(self):
        assert p.fingerprint(self.BASE, self.OCR) == p.fingerprint(self.BASE, self.OCR)

    def test_changes_when_a_key_changes(self):
        other = p.AgentChoice(
            planner=p.ModelChoice("anthropic", "claude-sonnet-5-5", "sk-NEW"),
            grounding=self.BASE.grounding,
        )
        assert p.fingerprint(other, self.OCR) != p.fingerprint(self.BASE, self.OCR)

    def test_changes_when_ocr_moves(self):
        moved = p.TesseractLocation("D:/t/tesseract.exe", None, "explicit")
        assert p.fingerprint(self.BASE, moved) != p.fingerprint(self.BASE, self.OCR)

    def test_never_contains_a_key(self):
        assert "sk-ant" not in p.fingerprint(self.BASE, self.OCR)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
    def test_permanent_statuses_give_up_at_once(self, status):
        assert p.is_permanent(status, "")

    def test_a_plain_rate_limit_is_retried(self):
        assert not p.is_permanent(429, "slow down")

    def test_an_exhausted_quota_is_not_retried(self):
        assert p.is_permanent(429, '{"error": "insufficient_balance"}')

    def test_a_server_error_is_retried(self):
        assert not p.is_permanent(503, "")

    def test_no_message_mentions_stonic_or_a_plan(self):
        for pid in p.PROVIDERS:
            for status in (401, 402, 403, 404, 429, 500):
                _, message = p.explain_error(pid, status, "")
                assert message
                assert "Stonic" not in message
                assert "plan" not in message.lower().split()

    def test_invalid_key_names_the_provider_and_the_fix(self):
        kind, message = p.explain_error("gemini", 401, "")
        assert kind == "invalid_key"
        assert "Gemini" in message
        assert "Settings → Cloud providers" in message

    def test_openrouter_key_limit_keeps_stonics_specific_advice(self):
        kind, message = p.explain_error("openrouter", 403, "Key limit exceeded")
        assert kind == "key_limit"
        assert "openrouter.ai" in message

    def test_unknown_status_is_left_alone(self):
        assert p.explain_error("anthropic", 418, "") == (None, None)

    def test_no_status_is_left_alone(self):
        assert p.explain_error("anthropic", None, "") == (None, None)


class TestProviderFromUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://openrouter.ai/api/v1/chat/completions", "openrouter"),
            ("https://api.anthropic.com/v1/messages", "anthropic"),
            (
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "gemini",
            ),
            ("https://api.deepseek.com/chat/completions", "deepseek"),
        ],
    )
    def test_maps_each_provider_host(self, url, expected):
        assert p.provider_from_url(url) == expected

    def test_unknown_host_uses_the_fallback(self):
        assert p.provider_from_url("https://example.com", "anthropic") == "anthropic"

    def test_none_uses_the_fallback(self):
        assert p.provider_from_url(None, "gemini") == "gemini"
