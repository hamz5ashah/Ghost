"""Engine ki settings: do jagah se, ek hi shakal mein.

    settings.toml   engine ke apne numbers (sunna, turn, awaaz) — Voice Lab mein naape hue
    app ka config   user ki cheezein (chabiyan, dimagh, soch, kirdar) — Ghost bhejta hai
                    (POST /beta/config), sirf yaad-dasht mein rehti hain, disk par kabhi nahi

Ghost port (Phase 3, talk-only). Stonic had two modes — "cloud" (its gateway,
under the user's plan) and "custom" (the user's own keys) — and a Claude brain
that also *worked* (Claude Agent SDK, with PowerShell, files and screen tools).
Ghost keeps exactly one path:

  - the user's own keys only, read by Ghost from the OS keyring and sent here
    in memory; no gateway, no plan, no sign-in
  - talk-only brains (Claude, Gemini, DeepSeek or OpenRouter through Pipecat's
    own services): the brain answers, it does not act on the computer
  - ears and voice on Gemini (Live transcription; Gemini Live or 3.8 TTS)

Awaaz do mein se ek:
    live  Gemini Live se line bulwana — muft key par bhi chalta hai
          (requests ki hadd nahi), ada kam (default)
    tts   Gemini 3.8 TTS — poori ada, magar key par requests ki tang hadd

Pipeline har session par `load_settings()` bulati hai, is liye Settings mein
kuch badle to agli Connect se lag jata hai — engine dobara chalana nahi parta.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import tomllib

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = Path(os.environ.get("GHOST_VOICE_SETTINGS") or ROOT / "settings.toml")
PROMPT_FILE = ROOT / "prompt.md"


def _characters_dir() -> Path:
    """Ghost's shared configs/characters (Phase 1), in the repo and in the bundle."""
    override = os.environ.get("GHOST_VOICE_CHARACTERS", "").strip()
    if override:
        return Path(override)
    # <root>/src/ghost/voice_sidecar -> <root>/configs/characters
    return ROOT.parents[2] / "configs" / "characters"


CHARACTERS_DIR = _characters_dir()

# Pipeline ke audio rates. 16k = Google transcription ka pasandeeda rate,
# 24k = Gemini TTS ka apna rate (koi resample nahi).
AUDIO_IN_SAMPLE_RATE = 16000
AUDIO_OUT_SAMPLE_RATE = 24000

# Stonic: anthropic, gemini, groq, openrouter. Ghost swaps Groq for DeepSeek:
# every brain here has a key Ghost's keyring manages.
PROVIDERS = ("anthropic", "gemini", "deepseek", "openrouter")
THINKING_LEVELS = ("low", "medium", "high")
GENDER_NAMES = ("male", "female")
VOICE_ENGINES = ("live", "tts")
KEY_NAMES = ("gemini", "gemini_backup", "anthropic", "deepseek", "openrouter")
DEFAULT_NAME = "Ghost"

# App ki taraf se aane wale text ki haddein.
MAX_PROMPT_CHARS = 6000
MAX_NAME_CHARS = 60
MAX_MODEL_CHARS = 120
MAX_KEY_CHARS = 400
# Purani chat dobara khuli: pichli baat-cheet prompt mein. Sirf taaza hissa —
# lambi history pehle lafz ko der karti hai aur har jumle par dobara parhi jati hai.
MAX_HISTORY_TURNS = 40
MAX_HISTORY_TURN_CHARS = 1500
MAX_HISTORY_CHARS = 12_000
_CHARACTER_ID = re.compile(r"[a-z0-9_-]{1,64}")
_MODEL_ID = re.compile(r"[A-Za-z0-9._:/@-]+")


class ConfigError(ValueError):
    """Config adhoora ya ghalat. `code` app ko batata hai kya dikhana hai."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── settings.toml ────────────────────────────────────────────────────────────


@dataclass
class Ears:
    model: str = "gemini-3.5-transcribe-live"
    languages: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=lambda: [DEFAULT_NAME])
    # Khamoshi Google ko nahi jati (ears.py "gate"). false = har awaaz (purana tareeqa).
    gate: bool = True
    gate_confidence: float = 0.5
    gate_min_volume: float = 0.35
    gate_start_secs: float = 0.06
    gate_hangover_secs: float = 0.8
    gate_preroll_secs: float = 1.0


@dataclass
class Turn:
    vad_start_secs: float = 0.2
    vad_stop_secs: float = 0.2
    vad_confidence: float = 0.7
    smart_turn: bool = True
    smart_turn_max_wait_secs: float = 3.0
    # Smart Turn "adhoori" kahe magar Google ka jumla full stop / sawal par khatam ho: itni khamoshi kaafi (turns.py). 0 = band.
    finished_wait_secs: float = 1.2
    speech_timeout_secs: float = 0.6


@dataclass
class Voice:
    model: str = "gemini-3.8-flash-tts"
    live_model: str = "gemini-3.8-live"
    male_voice: str = "Charon"
    female_voice: str = "Aoede"
    style: str = ""
    script: str = "roman"  # roman | devanagari
    chunk_min_chars: int = 220
    chunk_next_min_chars: int = 400
    chunk_max_hold_secs: float = 1.0


@dataclass
class BrainLimits:
    max_tokens: int = 16000
    prompt_caching: bool = True


@dataclass
class Session:
    greeting: bool = True
    idle_timeout_secs: float = 2400.0  # 40 minute khamoshi; 0 = kabhi nahi


# ── app ka config ────────────────────────────────────────────────────────────


@dataclass
class Brain:
    provider: str = "anthropic"  # anthropic | gemini | deepseek | openrouter
    # Stonic ka default: sab se tez pehla lafz.
    model: str = "claude-haiku-5-5"
    thinking: str = "low"  # low | medium | high


@dataclass
class Character:
    id: str = ""
    name: str = ""
    gender: str = "male"  # male | female
    prompt: str = ""


@dataclass
class Keys:
    gemini: str = ""
    gemini_backup: str = ""
    anthropic: str = ""
    deepseek: str = ""
    openrouter: str = ""

    def for_brain(self, provider: str) -> str:
        return {
            "anthropic": self.anthropic,
            "gemini": self.gemini,
            "deepseek": self.deepseek,
            "openrouter": self.openrouter,
        }.get(provider, "")


@dataclass
class HistoryTurn:
    role: str  # "user" ya "assistant"
    text: str


@dataclass
class AppConfig:
    """App ka bheja hua config, jaancha hua."""

    voice_engine: str
    brain: Brain
    character: Character
    keys: Keys
    user_name: str = ""
    history: list[HistoryTurn] = field(default_factory=list)


@dataclass
class Settings:
    ears: Ears = field(default_factory=Ears)
    turn: Turn = field(default_factory=Turn)
    voice: Voice = field(default_factory=Voice)
    limits: BrainLimits = field(default_factory=BrainLimits)
    session: Session = field(default_factory=Session)
    brain: Brain = field(default_factory=Brain)
    character: Character = field(default_factory=Character)
    keys: Keys = field(default_factory=Keys)
    voice_engine: str = "live"  # live | tts
    user_name: str = ""
    history: list[HistoryTurn] = field(default_factory=list)

    @property
    def voice_name(self) -> str:
        """Kirdar ki jins se awaaz: Charon / Aoede."""
        return (
            self.voice.female_voice
            if self.character.gender == "female"
            else self.voice.male_voice
        )

    @property
    def voice_model(self) -> str:
        """Wo model jo waqai bolega — log aur status ke liye."""
        return (
            self.voice.live_model if self.voice_engine == "live" else self.voice.model
        )

    def public(self) -> dict[str, Any]:
        """Log aur status ke liye — is mein koi chabi nahi."""
        return {
            "brain": {
                "provider": self.brain.provider,
                "model": self.brain.model,
                "thinking": self.brain.thinking,
            },
            "character": {
                "id": self.character.id,
                "name": self.character.name,
                "gender": self.character.gender,
            },
            "voice": {
                "engine": self.voice_engine,
                "model": self.voice_model,
                "voice": self.voice_name,
            },
            "ears": {"model": self.ears.model},
            "keys": {name: bool(getattr(self.keys, name)) for name in KEY_NAMES},
            "history": len(self.history),
        }


_FILE_SECTIONS = {
    "ears": "ears",
    "turn": "turn",
    "voice": "voice",
    "brain": "limits",
    "session": "session",
}


def _file_settings() -> Settings:
    """settings.toml parho. Ghalat naam ki line par ruk jao — chup chaap default
    par chalna sab se bura hai: banda samjhe setting lagi hai aur lagi na ho."""
    settings = Settings()
    data = (
        tomllib.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if SETTINGS_FILE.exists()
        else {}
    )
    for section_name, values in data.items():
        attribute = _FILE_SECTIONS.get(section_name)
        if attribute is None:
            raise ConfigError(
                "bad_settings_file",
                f"settings.toml: [{section_name}] koi section nahi hai",
            )
        section = getattr(settings, attribute)
        known = {f.name for f in fields(section)}
        for key, value in values.items():
            if key not in known:
                raise ConfigError(
                    "bad_settings_file",
                    f"settings.toml: [{section_name}] mein '{key}' koi setting nahi hai",
                )
            setattr(section, key, value)
    if settings.voice.script not in _SCRIPT_RULES:
        raise ConfigError(
            "bad_settings_file",
            f"settings.toml: voice.script '{settings.voice.script}' — roman ya devanagari likho",
        )
    return settings


def _text(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _parse_history(raw: Any) -> list[HistoryTurn]:
    """Pichli baat-cheet: sirf taaza hissa, haddon ke andar, purani se nayi tak."""
    if not isinstance(raw, list):
        return []
    turns: list[HistoryTurn] = []
    for item in raw[-MAX_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            continue
        role = _text(item.get("role"), 20).lower()
        text = _text(item.get("text"), MAX_HISTORY_TURN_CHARS)
        if role in ("user", "assistant") and text:
            turns.append(HistoryTurn(role=role, text=text))
    total = 0
    kept: list[HistoryTurn] = []
    for turn in reversed(turns):
        total += len(turn.text)
        if total > MAX_HISTORY_CHARS:
            break
        kept.append(turn)
    return list(reversed(kept))


def parse_app_config(payload: Any) -> AppConfig:
    """App ka bheja hua config jaancho. Jo ghalat ho us par saaf wajah."""
    if not isinstance(payload, dict):
        raise ConfigError("bad_config", "config: object chahiye")
    mode = _text(payload.get("mode"), 20).lower()
    if mode and mode != "custom":
        # Stonic Cloud existed in Stonic only. Refused by name, not ignored.
        raise ConfigError(
            "bad_config",
            f"mode '{mode}' is not supported: Ghost uses your own keys only",
        )
    raw_keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    raw_brain = payload.get("brain") if isinstance(payload.get("brain"), dict) else {}
    raw_character = (
        payload.get("character") if isinstance(payload.get("character"), dict) else {}
    )

    voice_engine = _text(payload.get("voice"), 20).lower() or "live"
    if voice_engine not in VOICE_ENGINES:
        raise ConfigError(
            "bad_config",
            f"voice '{voice_engine}' — {', '.join(VOICE_ENGINES)} mein se ek",
        )

    keys = Keys(
        **{f.name: _text(raw_keys.get(f.name), MAX_KEY_CHARS) for f in fields(Keys)}
    )

    thinking = _text(raw_brain.get("thinking"), 20).lower() or Brain.thinking
    if thinking not in THINKING_LEVELS:
        raise ConfigError(
            "bad_config",
            f"brain.thinking '{thinking}' — {', '.join(THINKING_LEVELS)} mein se ek",
        )
    brain = Brain(
        provider=_text(raw_brain.get("provider"), 20).lower() or Brain.provider,
        model=_text(raw_brain.get("model"), MAX_MODEL_CHARS),
        thinking=thinking,
    )
    if brain.provider not in PROVIDERS:
        raise ConfigError(
            "bad_config",
            f"brain.provider '{brain.provider}' — {', '.join(PROVIDERS)} mein se ek",
        )
    if not brain.model or not _MODEL_ID.fullmatch(brain.model):
        raise ConfigError("bad_config", "brain.model: model ka naam chahiye")

    character = Character(
        id=_text(raw_character.get("id"), 64).lower(),
        name=_text(raw_character.get("name"), MAX_NAME_CHARS),
        gender=_text(raw_character.get("gender"), 10).lower() or Character.gender,
        prompt=_text(raw_character.get("prompt"), MAX_PROMPT_CHARS),
    )
    if character.gender not in GENDER_NAMES:
        raise ConfigError(
            "bad_config", f"character.gender '{character.gender}' — male ya female"
        )
    if character.id and not _CHARACTER_ID.fullmatch(character.id):
        raise ConfigError("bad_config", "character.id: chhote harf, adad, - aur _ hi")
    return AppConfig(
        voice_engine=voice_engine,
        brain=brain,
        character=character,
        keys=keys,
        user_name=_text(payload.get("user_name"), MAX_NAME_CHARS),
        history=_parse_history(payload.get("history")),
    )


_app_config: AppConfig | None = None


def _with_app_config(settings: Settings, app: AppConfig) -> Settings:
    settings.voice_engine = app.voice_engine
    settings.brain, settings.character, settings.keys = (
        app.brain,
        app.character,
        app.keys,
    )
    settings.user_name, settings.history = app.user_name, app.history
    return settings


def set_app_config(payload: Any) -> Settings:
    """App ka config yaad rakho (sirf yaad-dasht mein). Agli session isi par chalti hai."""
    global _app_config
    parsed = parse_app_config(payload)
    file_settings = _file_settings()  # file ki ghalti bhi yahin pakri jaye
    _app_config = parsed
    return _with_app_config(file_settings, parsed)


def clear_app_config() -> None:
    global _app_config
    _app_config = None


def has_app_config() -> bool:
    return _app_config is not None


def load_settings() -> Settings:
    if _app_config is None:
        raise ConfigError("no_config", "App ne abhi config nahi bheja")
    return _with_app_config(_file_settings(), _app_config)


def missing_keys(settings: Settings) -> list[str]:
    """Kaun si chabi ke baghair ye session nahi chal sakti. Sunna aur bolna
    hamesha Gemini key par hain, dimagh apne provider ki key par."""
    missing = [] if settings.keys.gemini else ["gemini"]
    if (
        not settings.keys.for_brain(settings.brain.provider)
        and settings.brain.provider not in missing
    ):
        missing.append(settings.brain.provider)
    return missing


# ── Prompt ───────────────────────────────────────────────────────────────────

_SCRIPT_RULES = {
    "roman": (
        'When you speak Hindi or Urdu, write it in Roman letters ("{example}") '
        "and keep English words in normal English spelling."
    ),
    "devanagari": (
        'When you speak Hindi or Urdu, write those words in Devanagari ("{example}") '
        "so the voice pronounces them natively, and keep English words in normal English spelling."
    ),
}

# Hindi/Urdu mein fe'l bolne wale ki jins batata hai ("karta hoon" / "karti hoon").
# Mard ki awaaz par koi line nahi jurti; aurat ki awaaz par dimagh ko batana
# parta hai, warna aurat "karta hoon" bolti (Voice Lab mein naapa: 15 mein se 0
# ghalat fe'l is line ke saath).
GENDERS: dict[str, dict[str, str]] = {
    "male": {"rule": "", "roman": "abhi karta hoon", "devanagari": "अभी करता हूँ"},
    "female": {
        "rule": (
            "Your voice is a woman's, so in Hindi and Urdu you speak of yourself in the feminine "
            '("main karti hoon", "main aa gayi").'
        ),
        "roman": "abhi karti hoon",
        "devanagari": "अभी करती हूँ",
    },
}

# Connect hote hi pehli line. Ye user ke role mein jata hai kyunke baat-cheet
# user ke message se hi shuru ho sakti hai.
GREETING_CUE = "(The voice channel has just opened. Greet me in one short line.)"
# Purani chat mein wapsi: naya salaam nahi, baat wahin se.
RESUME_CUE = "(The voice channel has just opened again in our earlier conversation. Pick it back up in one short line — no fresh greeting.)"
OPENING_CUE_PREFIX = "(The voice channel has just opened"


def is_opening_cue(text: str) -> bool:
    return str(text or "").lstrip().startswith(OPENING_CUE_PREFIX)


def opening_cue(settings: Settings) -> str:
    """Raasta khulte hi dimagh ko pehla ishara: nayi chat par salaam, purani par wahin se."""
    return RESUME_CUE if settings.history else GREETING_CUE


_EXTRA_KEYS = (
    "natural_voice",
    "examples",
    "script_example_roman",
    "script_example_devanagari",
)


def character_extras(character_id: str) -> dict[str, str]:
    """configs/characters/<id>.toml: us kirdar ki ada ke chhote hisse, agar file ho."""
    path = CHARACTERS_DIR / f"{character_id}.toml"
    if (
        not character_id
        or not _CHARACTER_ID.fullmatch(character_id)
        or not path.exists()
    ):
        return {key: "" for key in _EXTRA_KEYS}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {key: str(data.get(key, "")).strip() for key in _EXTRA_KEYS}


_DEFAULT_CHARACTER = (
    "Ghost, the user's personal AI assistant: composed, warm and quick, with a dry "
    "sense of humour. You help with whatever they bring you."
)


def system_prompt(settings: Settings) -> str:
    """prompt.md ka dhancha + chuna hua kirdar + likhai aur jins ke qaide."""
    voice = settings.voice
    gender = GENDERS[settings.character.gender]
    extras = character_extras(settings.character.id)
    script_example = extras[f"script_example_{voice.script}"] or gender[voice.script]
    template = PROMPT_FILE.read_text(encoding="utf-8")
    return (
        template.replace("{{NAME}}", settings.character.name or DEFAULT_NAME)
        .replace("{{CHARACTER}}", settings.character.prompt or _DEFAULT_CHARACTER)
        .replace(
            "{{SCRIPT_RULE}}",
            _SCRIPT_RULES[voice.script].format(example=script_example),
        )
        .replace("\n{{GENDER_RULE}}", f"\n- {gender['rule']}" if gender["rule"] else "")
        .replace(
            "{{NATURAL_VOICE}}",
            f" — {extras['natural_voice']} —" if extras["natural_voice"] else "",
        )
        .replace(
            "\n\n{{PERFORM_EXAMPLES}}",
            f"\n\n{extras['examples']}" if extras["examples"] else "",
        )
        .strip()
    )


def history_prompt(settings: Settings) -> str:
    """Isi chat ki pehle ki baat-cheet. Khali = nayi chat (prompt mein kuch nahi)."""
    if not settings.history:
        return ""
    who = settings.user_name or "User"
    lines = [
        f"{who if turn.role == 'user' else 'You'}: {turn.text}"
        for turn in settings.history
    ]
    return (
        "## Earlier in this conversation\n\n"
        "This chat was opened again, so the two of you have already been talking. These are the latest lines, oldest first. "
        "Carry on from where it left off as one continuous conversation — do not greet them again, and do not repeat or "
        "summarize it unless they ask.\n\n" + "\n".join(lines)
    )


def conversation_prompt(settings: Settings) -> str:
    """Dimagh ka prompt: kirdar + pichli baat."""
    parts = (system_prompt(settings), history_prompt(settings))
    return "\n\n".join(part for part in parts if part)


def hint_phrases(settings: Settings) -> list[str]:
    """Wo lafz jo sunne wali service ghalat sunti hai: file wale + kirdar ka naam."""
    phrases = list(settings.ears.phrases)
    if settings.character.name and settings.character.name not in phrases:
        phrases.append(settings.character.name)
    return phrases


# ── Dimagh ke request params ─────────────────────────────────────────────────
#
# App mein ek hi control hai: "Thinking: Low / Medium / High". Har provider use
# apni zubaan mein leta hai. Low = bolne se pehle kam se kam soch (sab se tez).


def claude_thinking(model: str, level: str) -> dict[str, str] | None:
    """Claude ka `thinking` field, model ke qaide ke mutabiq. None = field bhejo hi mat.

    Soch band karna har model par alag hai:
      Haiku 5.5   {"type": "disabled"} qabool karta hai
      Sonnet 5.5  disabled par 400 deta hai, us ka "band" = between_tools
      Opus 5.5 / Fable   soch band hoti hi nahi — field chhor do, effort se kaam lo
    Medium / High par model khud tay karta hai kitna sochna hai (adaptive).
    """
    name = model.lower()
    if "opus-5-5" in name or "fable" in name or "mythos" in name:
        return None
    if level != "low":
        # 4.5 wale purane models adaptive nahi jaante; un par soch band hi rehti hai.
        return {"type": "disabled"} if _is_claude_45(name) else {"type": "adaptive"}
    if "sonnet-5-5" in name:
        return {"type": "between_tools"}
    return {"type": "disabled"}


def claude_extra(model: str, level: str) -> dict[str, Any]:
    """Wo params jo Pipecat ke apne settings mein nahi hain (effort)."""
    if _is_claude_45(model.lower()):
        return {}  # 4.5 wale models effort par error dete hain
    return {"output_config": {"effort": level}}


def _is_claude_45(name: str) -> bool:
    return "haiku-4-5" in name or "sonnet-4-5" in name or "opus-4-5" in name


def gemini_thinking(model: str, level: str) -> dict[str, Any] | None:
    """Gemini ka thinking config. None = Pipecat ka apna tez default (kam se kam soch).

    Gemini 3 "thinking_level" leta hai, 2.5 "thinking_budget" (tokens).
    """
    name = model.lower()
    if level == "low":
        return None
    if name.startswith("gemini-2.5"):
        return {"thinking_budget": 1024 if level == "medium" else -1}
    # Gemini 3 Pro sirf low / high jaanta hai.
    if "pro" in name:
        return {"thinking_level": "high"} if level == "high" else None
    return {"thinking_level": level}


def openrouter_reasoning(level: str) -> dict[str, str]:
    """OpenRouter ka ek jaisa `reasoning` field — har model ke liye OpenRouter khud
    tarjuma karta hai, aur jo model sochta hi nahi us par chup chaap nazar-andaz."""
    return {"effort": level}
