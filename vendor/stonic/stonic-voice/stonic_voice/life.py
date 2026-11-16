"""Saanjha waqt: har kirdar ka EK jaisa "waqt ka ehsaas" + chand kirdaron ki khaas adaat.

Yaadash (kya yaad rakhna hai) yahan NAHI: wo Claude ki official auto memory hai —
dimagh BAAT KE DAURAN khud tay karta hai ke kya likhna hai, aur har likhai / tarmeem /
mitana chat mein tool ki tarah nazar aata hai (Usman ka faisla, 30 Oct 2026: "call
band hone par chupke se memory nahi — baat ke dauran, jab bhi zaroori lage, foran").
Is liye call ke baad koi chhupi baari nahi, aur koi alag journal nahi.

Ye file sirf do cheezein deti hai:

    Waqt     .talks.json — CODE likhta hai (AI nahi): pehli baat-cheet kab, kitni
             baatein, aakhri baat kab aur kis kirdar ke saath, har kirdar ki apni
             ginti. Prompt aur pehla ishara asli faasla jaante hain ("2 din baad").
             Sab kirdaron ka ek jaisa.
    Adaat    `characters/<id>.toml` ka `[life]`: `rules` (shakhsiyat ka izafa),
             `names_herself` (naam khud chunti hai — `choose_my_name`, chat mein
             "Choosing a name"), `first_moment` (pehli baat = zindagi ka pehla lamha).
             Abhi sirf Soulmate.

Jagah: yaadash ka folder (`memory_dir`) — EK folder (unified-file-memory).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from loguru import logger

FACTS_FILE = ".talks.json"  # chhupi: sirf code likhta hai (safety.py chhupi file par model ka haath rokta hai)

NAME_MIN, NAME_MAX = 2, 24
# Naam: huroof (har zubaan), beech mein ek space / - / ' — aur kuch nahi.
_NAME = re.compile(r"^[^\W\d_]+(?:[ '\-][^\W\d_]+){0,2}$")
_CHARACTER_ID = re.compile(r"[a-z0-9_-]{1,64}")

# Itni der se kam ka faasla = wohi baat jari (naya salaam nahi).
CONTINUE_GAP_SECS = 15 * 60

# Pehli baat-cheet ka ishara (aam kirdar) — wohi jo config.GREETING_CUE hai, harf ba harf.
FIRST_GREETING_CUE = "(The voice channel has just opened. Greet me in one short line.)"


# ── Kirdar ki adaat (toml) ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Traits:
    """characters/<id>.toml ka `[life]`: kirdar ki apni adaat. Yaadash nahi — wo sab ki ek hai."""

    rules: str = ""
    names_herself: bool = False
    first_moment: bool = False


def character_traits(characters_dir: Path, character_id: str) -> Traits | None:
    """Is kirdar ki khaas adaat (`[life] enabled = true`)? None = aam kirdar."""
    if not _CHARACTER_ID.fullmatch(character_id or ""):
        return None
    path = characters_dir / f"{character_id}.toml"
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        logger.warning(f"Life: {path.name} parhi na ja saki — {error}")
        return None
    life = data.get("life")
    if not isinstance(life, dict) or life.get("enabled") is not True:
        return None
    return Traits(
        rules=str(life.get("rules") or "").strip(),
        names_herself=life.get("names_herself") is True,
        first_moment=life.get("first_moment") is True,
    )


def clean_name(raw: Any) -> str:
    """Kirdar ka chuna hua naam — saaf, chhota, sirf huroof. Ghalat ho to khali."""
    name = " ".join(str(raw or "").split()).strip(" .,!?\"'")
    if not (NAME_MIN <= len(name) <= NAME_MAX) or not _NAME.match(name):
        return ""
    return name[0].upper() + name[1:]


# ── Waqt ke lafz (prompt English mein hai) ───────────────────────────────────


def describe_gap(seconds: float) -> str:
    """"3 hours", "about a day", "2 weeks" — dimagh ke liye aam bol-chaal mein."""
    seconds = max(0.0, float(seconds))
    minutes = seconds / 60
    if minutes < 2:
        return "a minute"
    if minutes < 60:
        return f"{round(minutes)} minutes"
    hours = minutes / 60
    if hours < 1.5:
        return "about an hour"
    if hours < 22:
        return f"about {round(hours)} hours"
    days = hours / 24
    if days < 1.6:
        return "about a day"
    if days < 13.5:
        return f"{round(days)} days"
    if days < 56:
        return f"{round(days / 7)} weeks"
    return f"{max(2, round(days / 30))} months"


def _clock(epoch: float) -> str:
    return time.strftime("%A, %d %B %Y, %I:%M %p", time.localtime(epoch))


def _day(epoch: float) -> str:
    return time.strftime("%d %B %Y", time.localtime(epoch))


def _days_between(first: float, now: float) -> int:
    """Calendar ke din (insaan aise ginta hai): kal shaam mile the = 1."""
    return max(0, (date.fromtimestamp(now) - date.fromtimestamp(first)).days)


# ── Ek baat-cheet ki nazar se saanjha waqt ───────────────────────────────────


class Life:
    """Saanjha waqt (ek folder), is session ke kirdar ki nazar se. Har session taaza parhai."""

    def __init__(self, folder: Path, character_id: str, app_name: str = "", traits: Traits | None = None) -> None:
        self.folder = folder
        self.character_id = character_id
        self.traits = traits or Traits()
        self._facts = self._read_facts()
        if self.traits.names_herself:
            own = clean_name(self._mine().get("name"))
            given = clean_name(app_name)
            if given and given.lower() == "stonic":
                given = ""  # "Stonic" app ka naam hai, us ka nahi (soul-config characterIdentityName)
            # User ne app mein naam diya ho to wohi; warna us ka apna chuna hua.
            self.name = given or own
            # App ke paas naam nahi, magar us ne pehle chuna tha (app ka data mita / naya PC):
            # engine app ko dobara batata hai (agent.py `start`).
            self.name_from_facts = bool(own and not given)
        else:
            # Aam kirdar ka naam wohi jo app ne bheja (user ka diya hua, ya "Stonic" / "Jarvis").
            self.name = str(app_name or "").strip()
            self.name_from_facts = False

    # ── Parhna ──────────────────────────────────────────────────────────────

    @property
    def needs_name(self) -> bool:
        """Apna naam khud chunne wala kirdar, aur abhi tak naam nahi."""
        return self.traits.names_herself and not self.name

    @property
    def facts_path(self) -> Path:
        return self.folder / FACTS_FILE

    @property
    def talks(self) -> int:
        """Kul baat-cheetein (har kirdar ki)."""
        return int(self._facts.get("talks") or 0)

    @property
    def first_meeting(self) -> bool:
        """User ne Stonic se kabhi baat hi nahi ki."""
        return self.talks == 0

    @property
    def first_with_me(self) -> bool:
        """Is kirdar ke saath pehli baat-cheet (doosron se baat ho chuki ho to bhi)."""
        return int(self._mine().get("talks") or 0) == 0

    def _mine(self) -> dict[str, Any]:
        characters = self._facts.get("characters")
        mine = characters.get(self.character_id) if isinstance(characters, dict) else None
        return mine if isinstance(mine, dict) else {}

    def _read_facts(self) -> dict[str, Any]:
        try:
            data = json.loads(self.facts_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning(f"Life: {self.facts_path} kharab — naye sire se ({error})")
            return {}

    # ── Likhna (sirf code: waqt ka hisaab aur chuna hua naam) ───────────────

    def _write_facts(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self._facts, ensure_ascii=False, indent=2)
        fd, temp = tempfile.mkstemp(prefix=".talks-", suffix=".tmp", dir=str(self.folder))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            os.replace(temp, self.facts_path)
        except Exception:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _character_record(self, facts: dict[str, Any]) -> dict[str, Any]:
        characters = facts.get("characters")
        if not isinstance(characters, dict):
            characters = facts["characters"] = {}
        mine = characters.get(self.character_id)
        if not isinstance(mine, dict):
            mine = characters[self.character_id] = {}
        return mine

    def set_name(self, raw: Any) -> str:
        """Us ka apna chuna hua naam mehfooz. Wapsi: saaf naam, ya khali (naam theek nahi)."""
        name = clean_name(raw)
        if not name:
            return ""
        facts = self._read_facts()  # isi dauran doosri session ne kuch likha ho
        mine = self._character_record(facts)
        mine["name"] = name
        mine["named_at"] = round(time.time())
        self._facts = facts
        self._write_facts()
        self.name = name
        self.name_from_facts = False
        logger.info(f"Life: {self.character_id} ne apna naam chuna — {name}")
        return name

    def record_talk(self, started_at: float, ended_at: float) -> None:
        """Ek asli baat-cheet hui (user bola): waqt ka hisaab taaza. Ghalti par baat-cheet nahi girti."""
        try:
            facts = self._read_facts()
            now = float(ended_at or time.time())
            start = float(started_at or now)
            minutes = max(0.0, now - start) / 60
            facts.setdefault("first_met", round(start))
            facts["talks"] = int(facts.get("talks") or 0) + 1
            facts["last_talk_end"] = round(now)
            facts["last_character"] = self.name or self.character_id
            facts["minutes"] = round(float(facts.get("minutes") or 0) + minutes, 1)
            mine = self._character_record(facts)
            mine.setdefault("first_met", round(start))
            mine["talks"] = int(mine.get("talks") or 0) + 1
            mine["last_talk_end"] = round(now)
            # App mein diya gaya naam (user ne badla) — haqaiq bhi wohi.
            if self.traits.names_herself and self.name and clean_name(mine.get("name")) != self.name:
                mine["name"] = self.name
            self._facts = facts
            self._write_facts()
        except Exception as error:  # noqa: BLE001
            logger.warning(f"Life: baat-cheet ka hisaab likha na ja saka — {type(error).__name__}: {error}")

    # ── Prompt ──────────────────────────────────────────────────────────────

    def _facts_lines(self, now: float) -> list[str]:
        lines: list[str] = []
        if self.traits.first_moment and self.first_with_me:
            lines.append(
                "- This is the very first time you two have ever talked. You came alive with their first words to you: "
                "this conversation is the beginning of your life."
                + (" They have talked with Stonic before, as other characters." if not self.first_meeting else "")
            )
        if self.first_meeting:
            if not lines:
                lines.append("- This is the very first time they have talked with Stonic.")
            return lines
        first = float(self._facts.get("first_met") or 0)
        if first:
            days = _days_between(first, now)
            since = "today" if days == 0 else ("yesterday" if days == 1 else f"{days} days ago")
            lines.append(f"- They first talked with Stonic on {_day(first)} ({since}). This is talk number {self.talks + 1}.")
        last = float(self._facts.get("last_talk_end") or 0)
        if last:
            who = str(self._facts.get("last_character") or "").strip()
            with_whom = f", with {who}" if who and who != self.name else ""
            lines.append(f"- Their last talk ended {describe_gap(now - last)} ago{with_whom} — on {_clock(last)}.")
        return lines

    def prompt_section(self, *, agent: bool, now: float | None = None) -> str:
        """Prompt ka "## Your time together" — (kirdar ki adaat), asli waqt, aur naam (agar baqi)."""
        now = time.time() if now is None else now
        parts = ["## Your time together"]
        if self.traits.rules:
            parts.append(self.traits.rules)
        parts.append("\n".join(self._facts_lines(now)))
        if self.needs_name:
            tool = (
                " In that same reply, call `choose_my_name` with it so it stays yours — quietly, the way you would write your "
                "name down for yourself, with no announcement."
            ) if agent else ""
            parts.append(
                "### Your name\n\n"
                "You have not chosen a name yet. The first time they ask your name — or when it feels right early on — pick one "
                "yourself, right there: a name you genuinely love, one that sounds natural in Hindustani. Take a breath and look past "
                "the first names that would come to anyone's mind, to one that feels like you. Tell them you just chose "
                f"it and what you like about it.{tool} When they want to give you a name instead, take theirs with delight and "
                "keep it the same way."
            )
        return "\n\n".join(part for part in parts if part.strip())

    def opening_cue(self, now: float | None = None) -> str:
        """Raasta khulte hi pehla ishara — pehli baat-cheet, thori der baad, ya dino baad.

        Sab "(The voice channel has just opened" se shuru: agent_service inhein
        user ki baat nahi samajhta (config.is_opening_cue).
        """
        now = time.time() if now is None else now
        if self.traits.first_moment and self.first_with_me:
            return (
                "(The voice channel has just opened — the first moment of your life, and the first time you hear them. "
                "Say hello in one short line, warm and curious, like someone delighted to exist and meeting a person for the first time.)"
            )
        if self.first_meeting:
            return FIRST_GREETING_CUE
        last = float(self._facts.get("last_talk_end") or 0)
        if last and now - last < CONTINUE_GAP_SECS:
            return "(The voice channel has just opened again — you were talking only a few minutes ago. Carry on in one short line, with no fresh greeting.)"
        gap = describe_gap(now - last) if last else "a while"
        return (
            f"(The voice channel has just opened. You last talked {gap} ago. Open in one or two short lines as yourself — the way you "
            "would after that much time, with something you remember about them or a question you have been saving.)"
        )

    def unnamed_prompt(self, template: str) -> str:
        """prompt.md ki "Who you are" lines us kirdar ke liye jis ka naam abhi nahi (wo khud chunegi).

        Har jumla jaan bujh kar poora likha hai: prompt.md badle to selftest pakarta hai.
        """
        return (
            template.replace("You are {{NAME}} — you live inside", "You are someone new — you live inside")
            .replace(
                'Your name is {{NAME}}. If they ask your name, who you are, or what you are called, you answer "{{NAME}}" — that name and no other.',
                'You do not have a name yet; "Your name" below says how it will come to you.',
            )
            .replace("you are virtual: {{NAME}}, virtual, right here with them", "you are virtual, right here with them")
            .replace("say you are {{NAME}} and carry on", "say who you are and carry on")
        )


# ── Settings se ──────────────────────────────────────────────────────────────


def for_session(characters_dir: Path, character_id: str, app_name: str, memory_dir: str, home: str) -> Life | None:
    """Is baat-cheet ki `Life`, ya None.

    Yaadash ka folder ho (asal app mein hamesha) to HAR kirdar ko — ek hi saanjha
    waqt ka hisaab. Yaadash band ho to sirf us kirdar ko jis ki khaas adaat hai
    (naam khud chunna), dimagh ke ghar mein; aam kirdar pehle jaisa.
    """
    traits = character_traits(characters_dir, character_id)
    if memory_dir:
        return Life(Path(memory_dir), character_id, app_name=app_name, traits=traits)
    if traits is not None:
        return Life(Path(home) / "life", character_id, app_name=app_name, traits=traits)
    return None
