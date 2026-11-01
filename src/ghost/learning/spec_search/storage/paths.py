"""Filesystem path resolution for the spec-search subsystem.

The keystone of artifact isolation (spec §11): the resolved spec-search root
must NEVER be inside the Ghost source tree. ``resolve_spec_search_root``
walks up from this module's ``__file__`` looking for a ``pyproject.toml`` that
identifies the Ghost source root, then refuses to operate if the resolved
root is inside it. Defense in depth — if a user accidentally points
``GHOST_HOME`` at the repo, the system fails loudly instead of silently
writing artifacts into the working tree.
"""

from __future__ import annotations

from pathlib import Path

from ghost.core.paths import ConfigurationError, get_config_dir
from ghost.security.file_utils import secure_mkdir

# ``ConfigurationError`` is re-exported from ``ghost.core.paths`` (it used
# to be defined here). Spec search now resolves the home dir through the unified
# core resolver, which raises the same exception type on a source-tree path, so
# we alias rather than redefine to keep ``except ConfigurationError`` callers and
# existing tests working.
__all__ = [
    "ConfigurationError",
    "ensure_spec_search_dirs",
    "resolve_spec_search_root",
]


def _find_source_root() -> Path | None:
    """Walk upward from this module to find the Ghost source root.

    Returns the directory containing the Ghost ``pyproject.toml``, or
    ``None`` if no such file is found (e.g. when running from an installed
    wheel rather than a source checkout).
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        py = candidate / "pyproject.toml"
        if py.exists():
            try:
                content = py.read_text(encoding="utf-8")
            except OSError:
                continue
            if 'name = "ghost"' in content.lower():
                return candidate
    return None


def _resolve_ghost_home() -> Path:
    """Resolve the Ghost home directory via the unified core resolver.

    Delegates to ``get_config_dir`` so spec-search honors the same env-aware
    resolution (GHOST_HOME and XDG) as the rest of the framework.
    """
    return get_config_dir()


def resolve_spec_search_root() -> Path:
    """Return the absolute path of the spec-search root directory.

    The root is ``$GHOST_HOME/learning`` (or ``~/.ghost/learning``
    by default). Raises ``ConfigurationError`` if the resolved path lies
    inside the Ghost source tree, to prevent dev artifacts from leaking
    into the repo.
    """
    home = _resolve_ghost_home()
    source_root = _find_source_root()
    if source_root is not None:
        try:
            home.relative_to(source_root)
        except ValueError:
            pass  # Good — not inside the source tree.
        else:
            raise ConfigurationError(
                f"GHOST_HOME ({home}) is inside the source tree "
                f"({source_root}). Spec search refuses to write runtime "
                "artifacts inside the Ghost repo. Set GHOST_HOME "
                "to a directory outside the repo (default: ~/.ghost)."
            )
    return home / "learning"


def ensure_spec_search_dirs() -> Path:
    """Create the spec-search directory layout if missing.

    Returns the spec-search root. Creates ``sessions/``, ``benchmarks/``,
    ``benchmarks/reference_outputs/``, and ``pending_review/`` underneath it,
    all with restrictive ``0o700`` permissions via ``secure_mkdir``.
    """
    root = resolve_spec_search_root()
    secure_mkdir(root)
    secure_mkdir(root / "sessions")
    secure_mkdir(root / "benchmarks")
    secure_mkdir(root / "benchmarks" / "reference_outputs")
    secure_mkdir(root / "pending_review")
    return root
