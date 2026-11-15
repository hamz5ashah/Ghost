"""Character voice definitions ported from Stonic (Phase 1).

A character file describes *how* a reply is delivered aloud, not who Ghost is:
a one-line description of the natural voice, an example of the script
convention in that character's own idiom, and a few bracket-performance
examples. Ghost's identity stays in
``frontend/src/lib/assistant-prompt.ts``; see
``configs/prompts/voice_delivery.md`` for what was deliberately left behind.

The bracket performance system only reaches the user through a TTS engine that
understands it, which arrives with the realtime voice engine in Phase 3.
Loading a character is therefore inert today beyond listing it in the UI.

Files live in ``configs/characters/*.toml`` and ship with the desktop app via
the Tauri ``configs/`` resource mapping. The file stem is the character id.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import tomllib

logger = logging.getLogger(__name__)

# Characters whose definition exists but which are not offered in the UI.
# female_soulmate was marked dev-only in Stonic's own file.
DEV_ONLY = frozenset({"female_soulmate"})


@dataclass(slots=True)
class Character:
    """One character's spoken-delivery profile."""

    id: str
    natural_voice: str = ""
    script_example_roman: str = ""
    script_example_devanagari: str = ""
    examples: str = ""
    # Stonic's female_soulmate carried a [life] table of persistent-persona
    # rules. Kept as raw data: nothing consumes it before Phase 3.
    life: Dict[str, Any] = field(default_factory=dict)
    dev_only: bool = False

    @property
    def display_name(self) -> str:
        """Human label, e.g. ``male_jarvis`` -> ``Jarvis``."""
        stem = self.id.split("_", 1)[-1] if "_" in self.id else self.id
        return stem.replace("_", " ").title()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "natural_voice": self.natural_voice,
            "dev_only": self.dev_only,
            "has_examples": bool(self.examples.strip()),
        }


def characters_dir() -> Optional[Path]:
    """Locate ``configs/characters``, preferring an explicit override.

    Returns ``None`` when it cannot be found, which callers report as "no
    characters available" rather than raising.
    """
    override = os.environ.get("GHOST_CHARACTERS_DIR")
    if override:
        path = Path(override)
        return path if path.is_dir() else None
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "configs" / "characters"
        if candidate.is_dir():
            return candidate
        if parent.name == "src":
            candidate = parent.parent / "configs" / "characters"
            if candidate.is_dir():
                return candidate
            break
    return None


def _parse(path: Path) -> Optional[Character]:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # A malformed character file must not take the voice settings down.
        logger.warning("Skipping character %s: %s", path.name, exc)
        return None
    if not isinstance(data, dict):
        return None
    character_id = path.stem
    life = data.get("life")
    return Character(
        id=character_id,
        natural_voice=str(data.get("natural_voice") or ""),
        script_example_roman=str(data.get("script_example_roman") or ""),
        script_example_devanagari=str(data.get("script_example_devanagari") or ""),
        examples=str(data.get("examples") or ""),
        life=dict(life) if isinstance(life, dict) else {},
        dev_only=character_id in DEV_ONLY,
    )


def load_characters(directory: Optional[Path] = None) -> List[Character]:
    """Every character on disk, sorted by id. Never raises."""
    base = directory if directory is not None else characters_dir()
    if base is None or not base.is_dir():
        return []
    found: List[Character] = []
    try:
        paths = sorted(base.glob("*.toml"))
    except OSError as exc:
        logger.warning("Cannot list characters in %s: %s", base, exc)
        return []
    for path in paths:
        character = _parse(path)
        if character is not None:
            found.append(character)
    return found


def load_character(
    character_id: str, directory: Optional[Path] = None
) -> Optional[Character]:
    """One character by id, or ``None`` when absent or malformed."""
    base = directory if directory is not None else characters_dir()
    if base is None:
        return None
    # Reject anything that could escape the characters directory.
    if not character_id or "/" in character_id or "\\" in character_id:
        return None
    if character_id != Path(character_id).name or character_id.startswith("."):
        return None
    path = base / f"{character_id}.toml"
    if not path.is_file():
        return None
    return _parse(path)


def selectable_characters(directory: Optional[Path] = None) -> List[Character]:
    """Characters offered in the UI (dev-only ones excluded)."""
    return [c for c in load_characters(directory) if not c.dev_only]


def script_rule(character: Character) -> str:
    """The ``{{SCRIPT_RULE}}`` line for ``voice_delivery.md``.

    Empty when the character gives no example, so the prompt simply omits it
    rather than carrying a dangling instruction.
    """
    roman = character.script_example_roman.strip()
    deva = character.script_example_devanagari.strip()
    if not roman or not deva:
        return ""
    return (
        "When you answer in Urdu or Hindi, write it in Roman letters the way "
        f'they would be typed in a message — "{roman}", not "{deva}".'
    )


def natural_voice_clause(character: Character) -> str:
    """The ``{{NATURAL_VOICE}}`` fragment, including its leading separator."""
    voice = character.natural_voice.strip()
    return f" — {voice} —" if voice else ""


def perform_examples_block(character: Character) -> str:
    """The ``{{PERFORM_EXAMPLES}}`` block, or empty when the file gives none."""
    examples = character.examples.strip()
    if not examples:
        return ""
    return f"Examples of how you sound:\n\n{examples}"


def render_delivery_prompt(template: str, character: Character) -> str:
    """Fill a ``voice_delivery.md`` template for one character."""
    return (
        template.replace("{{SCRIPT_RULE}}", script_rule(character))
        .replace("{{NATURAL_VOICE}}", natural_voice_clause(character))
        .replace("{{PERFORM_EXAMPLES}}", perform_examples_block(character))
    )
