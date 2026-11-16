"""On-demand Python sidecar runtimes for the ported Stonic engines.

The two Stonic engines do not live in Ghost's own environment. Their pins were
resolved for Python 3.11 and together they weigh about 820 MB — the voice
runtime alone is 706 MB, 342 MB of which is the ``claude.exe`` bundled inside
``claude-agent-sdk``. Merging that into ``uv.lock`` would push the installer
past a gigabyte and put Ghost's own dependency resolution at risk, so each
engine gets its own venv under ``<config dir>/runtimes/`` instead, created the
first time the feature is used.

This mirrors how the Kokoro voice model is fetched on demand
(:mod:`ghost.speech.kokoro_onnx_tts`): a presence check, a status dict the UI
can poll, and a background install.

Nothing here runs unless a ``[stonic]`` feature flag is on. See
``/STONIC_PORT_PLAN.md``.
"""

from __future__ import annotations

from ghost.runtimes.sidecar import (
    SPECS,
    RuntimeSpec,
    install_status,
    is_installed,
    requirements_path,
    runtime_dir,
    runtimes_dir,
    start_install,
    venv_python,
)

__all__ = [
    "SPECS",
    "RuntimeSpec",
    "install_status",
    "is_installed",
    "requirements_path",
    "runtime_dir",
    "runtimes_dir",
    "start_install",
    "venv_python",
]
