"""Tests for the on-demand sidecar runtime manager.

The contract: locating and reporting a runtime never touches the network, never
raises, and never reports READY for a venv that does not match the requirements
file it was built from. Actual installation is exercised with a tiny stub
requirements file so the test stays offline and fast.
"""

from __future__ import annotations

import sys

import pytest

from ghost.runtimes import sidecar
from ghost.runtimes.sidecar import (
    ERROR,
    INSTALLING,
    MISSING,
    READY,
    SPECS,
    STALE,
    RuntimeSpec,
    install_status,
    is_installed,
    requirements_path,
    runtime_dir,
    runtimes_dir,
    start_install,
    venv_python,
)


@pytest.fixture(autouse=True)
def _clean_state():
    sidecar._reset_state_for_tests()
    yield
    sidecar._reset_state_for_tests()


class TestSpecs:
    def test_both_stonic_runtimes_are_declared(self):
        assert set(SPECS) == {"voice", "computer"}

    @pytest.mark.parametrize("name", ["voice", "computer"])
    def test_spec_is_complete(self, name):
        spec = SPECS[name]
        assert isinstance(spec, RuntimeSpec)
        assert spec.name == name
        assert spec.requirements.endswith("-requirements.txt")
        assert spec.entry_module
        assert spec.pinned_python == "3.11"
        assert spec.description

    def test_unknown_runtime_is_rejected_by_name(self):
        with pytest.raises(ValueError, match="unknown sidecar runtime"):
            install_status("nope")

    def test_unknown_runtime_error_lists_the_valid_names(self):
        with pytest.raises(ValueError, match="computer, voice"):
            start_install("nope")


class TestPaths:
    def test_runtimes_live_under_the_config_dir(self):
        from ghost.core.paths import get_config_dir

        assert runtimes_dir() == get_config_dir() / "runtimes"

    def test_each_runtime_gets_its_own_directory(self):
        assert runtime_dir("voice") != runtime_dir("computer")
        assert runtime_dir("voice").parent == runtimes_dir()

    def test_venv_python_matches_the_platform_layout(self, tmp_path):
        path = venv_python("voice", tmp_path)
        if sys.platform == "win32":
            assert path == tmp_path / "Scripts" / "python.exe"
        else:
            assert path == tmp_path / "bin" / "python"

    def test_venv_python_is_returned_even_when_absent(self, tmp_path):
        # Callers report the expected location for a missing runtime.
        assert not venv_python("voice", tmp_path).exists()

    @pytest.mark.parametrize("name", ["voice", "computer"])
    def test_shipped_requirements_file_is_found(self, name):
        path = requirements_path(name)
        assert path is not None, f"{name} requirements not found under configs/"
        assert path.is_file()
        assert path.read_text().strip()

    def test_requirements_dir_can_be_overridden(self, tmp_path, monkeypatch):
        (tmp_path / "voice-requirements.txt").write_text("anyio\n")
        monkeypatch.setenv("GHOST_RUNTIME_REQUIREMENTS_DIR", str(tmp_path))
        assert requirements_path("voice") == tmp_path / "voice-requirements.txt"

    def test_missing_requirements_reports_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GHOST_RUNTIME_REQUIREMENTS_DIR", str(tmp_path))
        absent = RuntimeSpec(
            name="voice",
            requirements="absent-requirements.txt",
            entry_module="server",
            pinned_python="3.11",
            description="missing on purpose",
        )
        monkeypatch.setattr(sidecar, "_spec", lambda name: absent)
        assert requirements_path("voice") is None


class TestStatus:
    def test_absent_runtime_reports_missing(self, tmp_path):
        status = install_status("voice", tmp_path / "nothing-here")
        assert status["state"] == MISSING
        assert status["name"] == "voice"
        assert status["description"]
        assert status["error"] == ""

    def test_status_reports_where_the_interpreter_would_live(self, tmp_path):
        status = install_status("voice", tmp_path)
        assert status["python"] == str(venv_python("voice", tmp_path))

    def test_venv_without_marker_reports_stale(self, tmp_path):
        python = venv_python("voice", tmp_path)
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        assert install_status("voice", tmp_path)["state"] == STALE
        assert is_installed("voice", tmp_path) is False

    def test_matching_marker_reports_ready(self, tmp_path):
        python = venv_python("voice", tmp_path)
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        digest = sidecar._requirements_digest(requirements_path("voice"))
        (tmp_path / sidecar.MARKER_NAME).write_text(digest)
        assert is_installed("voice", tmp_path) is True
        assert install_status("voice", tmp_path)["state"] == READY

    def test_stale_marker_reports_stale(self, tmp_path):
        python = venv_python("voice", tmp_path)
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        (tmp_path / sidecar.MARKER_NAME).write_text("0000000000000000")
        assert is_installed("voice", tmp_path) is False
        assert install_status("voice", tmp_path)["state"] == STALE

    def test_status_never_touches_the_network(self, tmp_path, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("status must not run a subprocess")

        monkeypatch.setattr(sidecar, "_run", explode)
        assert install_status("computer", tmp_path)["state"] == MISSING


class TestInstall:
    def test_missing_requirements_is_reported_as_error_not_raised(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sidecar, "requirements_path", lambda name: None)
        result = start_install("voice", base=tmp_path / "rt", block=True)
        assert result["state"] == ERROR
        assert "not found" in result["error"]

    def test_failed_venv_creation_is_reported_as_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sidecar,
            "_create_venv",
            lambda python, target: (_ for _ in ()).throw(RuntimeError("no python")),
        )
        result = start_install("voice", base=tmp_path / "rt", block=True)
        assert result["state"] == ERROR
        assert "no python" in result["error"]

    def test_successful_install_writes_the_marker_and_reports_ready(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "rt"

        def fake_create(python, dest):
            interpreter = venv_python("voice", dest)
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!/bin/sh\n")

        monkeypatch.setattr(sidecar, "_create_venv", fake_create)
        monkeypatch.setattr(
            sidecar, "_install_requirements", lambda dest, req: "installed 1 package"
        )
        result = start_install("voice", base=target, block=True)
        assert result["state"] == READY
        assert (target / sidecar.MARKER_NAME).is_file()
        assert is_installed("voice", target) is True

    def test_install_does_not_run_concurrently(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(sidecar, "_create_venv", lambda p, t: calls.append(t))
        monkeypatch.setattr(sidecar, "_install_requirements", lambda dest, req: "")
        sidecar._state["voice"] = {"state": INSTALLING, "error": "", "log": ""}
        result = start_install("voice", base=tmp_path, block=True)
        assert result["state"] == INSTALLING
        assert calls == []

    def test_install_uses_the_requested_interpreter(self, tmp_path, monkeypatch):
        seen = {}

        def fake_create(python, dest):
            seen["python"] = python
            interpreter = venv_python("voice", dest)
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!/bin/sh\n")

        monkeypatch.setattr(sidecar, "_create_venv", fake_create)
        monkeypatch.setattr(sidecar, "_install_requirements", lambda d, r: "")
        start_install("voice", python="python3.11", base=tmp_path / "rt", block=True)
        assert seen["python"] == "python3.11"
