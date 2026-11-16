"""Engine ki settings: do jagah se, ek hi shakal mein.

    settings.toml   engine ke apne numbers (sunna, turn, awaaz) — Voice Lab mein naape hue
    app ka config   user ki cheezein (raasta, chabiyan, dimagh, soch, kirdar) — app bhejti hai
                    (POST /beta/config), sirf yaad-dasht mein rehti hain, disk par kabhi nahi

Do raaste (`mode`), user Settings mein chunta hai:

    cloud    Stonic Cloud — kaan, dimagh aur munh teeno hamare gateway se, user ke
             plan ke andar. User ke paas koi key nahi; awaaz poori jazbaati
             (Gemini 3.8 TTS).
    custom   user ki apni keys. Dimagh us ki marzi ka. Awaaz do mein se ek:
               live  Gemini Live se line bulwana — muft key par bhi chalta hai
                     (requests ki hadd nahi), ada kam (default)
               tts   Gemini 3.8 TTS — poori ada, magar key par requests ki tang
                     hadd (Tier 1: 100 fi din)

Dimagh Claude ho (apni key par, ya Stonic Cloud jab gateway Claude de) to wo
sirf bolta nahi, kaam bhi karta hai — agent.py (Claude Agent SDK). Us ke liye
app do cheezein aur bhejti hai (`agent`): Stonic ke apne tools ka naqsha aur
yaadash ka card. settings.toml ka `[agent]` us ki haddein rakhta hai.

Agent ko screen ke haath bhi mil sakte hain (hands.py, screen.py): app `screen.aim`
bhejti hai — "off", "claude" (Claude pixel khud batata hai) ya "uitars" (Claude
cheez batata hai, UI-TARS us ki jagah; us ke liye `keys.openrouter` — sirf apni
keys par; Stonic Cloud par nishana hamesha Claude ka).
settings.toml ka `[screen]` tasveer ke naap aur waqfe rakhta hai.

Pipeline har session par `load_settings()` bulati hai, is liye Settings mein
kuch badle to agli Connect se lag jata hai — engine dobara chalana nahi parta.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from stonic_voice import life as life_module
from stonic_voice.floor import FloorLimits

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = Path(os.environ.get("STONIC_VOICE_SETTINGS") or ROOT / "settings.toml")
PROMPT_FILE = ROOT / "prompt.md"
CHARACTERS_DIR = ROOT / "characters"

# Pipeline ke audio rates. 16k = Google transcription ka pasandeeda rate,
# 24k = Gemini TTS ka apna rate (koi resample nahi).
AUDIO_IN_SAMPLE_RATE = 16000
AUDIO_OUT_SAMPLE_RATE = 24000

PROVIDERS = ("anthropic", "gemini", "groq", "openrouter")
THINKING_LEVELS = ("low", "medium", "high")
GENDER_NAMES = ("male", "female")
MODES = ("cloud", "custom")
VOICE_ENGINES = ("live", "tts")
# Screen par nishana kaun lagaye. "off" = dimagh ko screen ke haath nahi milte.
SCREEN_AIMS = ("off", "claude", "uitars")
# Cloud par dimagh ka "provider" — sirf log aur ghalti ke code ke liye.
CLOUD_PROVIDER = "cloud"

# App ki taraf se aane wale text ki haddein. Kirdar ka prompt app mein 2,500
# huroof tak hota hai (soul-config.ts CHARACTER_PROMPT_MAX); yahan gunjaish hai.
MAX_PROMPT_CHARS = 6000
MAX_NAME_CHARS = 60
MAX_MODEL_CHARS = 120
MAX_KEY_CHARS = 400
MAX_TOKEN_CHARS = 4096  # login token (JWT) lamba hota hai; device key chhoti
MAX_URL_CHARS = 300
MAX_TOOLS = 40
MAX_TOOL_DESCRIPTION_CHARS = 2000
MAX_MEMORY_CHARS = 40_000
# Yaadash ka folder (Claude Agent SDK ki auto memory, `autoMemoryDirectory`).
MAX_MEMORY_DIR_CHARS = 1024
# Awareness ka block (app: lib/awareness/runtime.ts getAwarenessPromptBlock) — Standard voice jitna.
MAX_AWARENESS_CHARS = 8000
# Purani chat dobara khuli (ya usi chat mein Standard ke baad Beta): pichli
# baat-cheet prompt mein. Sirf taaza hissa — lambi history pehle lafz ko der
# karti hai aur har jumle par dobara parhi jati hai.
MAX_HISTORY_TURNS = 40
MAX_HISTORY_TURN_CHARS = 1500
MAX_HISTORY_CHARS = 12_000
# User ki skills (app ka Skills card): Claude Code ke official plugin raaste se.
MAX_SKILLS = 200
MAX_SKILLS_PLUGIN_CHARS = 1024
# Skill ka naam = us ke folder ka naam (Claude Code plugin skills ka naam folder se leta hai —
# naapa, CLI 2.1.318): chhote harf, adad, beech mein hyphen; 64 tak (Agent Skills ka qaida).
_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SKILL_NAME_CHARS = 64
_SKILLS_VERSION = re.compile(r"[A-Za-z0-9_-]{1,80}")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{1,48}")
_CHARACTER_ID = re.compile(r"[a-z0-9_-]{1,64}")
_MODEL_ID = re.compile(r"[A-Za-z0-9._:/@-]+")
# SDK ka session id: UUID hi (Claude Code `--session-id` / `--resume` yehi maangta hai).
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
    phrases: list[str] = field(default_factory=lambda: ["Stonic"])
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


@dataclass
class AgentLimits:
    """settings.toml ka [agent]: dimagh jab kaam bhi karta hai (agent.py)."""

    enabled: bool = True
    # Ek jumle ke jawab mein zyada se zyada itne qadam (tool calls), phir ruk kar batana.
    max_steps: int = 40
    # App ke andar chalne wale tool ka intezar (second).
    tool_timeout_secs: float = 90.0
    # SDK ke apne tools jo dimagh ko milte hain.
    tools: list[str] = field(default_factory=lambda: ["PowerShell", "Read", "Write", "Edit", "Glob", "Grep", "TaskStop"])


@dataclass
class ScreenLimits:
    """settings.toml ka [screen]: dimagh ke screen wale haath (hands.py, screen.py)."""

    # Claude ko jane wali tasveer ka lamba rukh. Anthropic ki sifarish 1280-1366;
    # 1920 se upar nahi (har tasveer ke tokens naap ke saath barhte hain).
    max_dim: int = 1366
    # UI-TARS ko jane wali tasveer: wohi naap jis par asal computer agent chalta hai.
    aim_max_dim: int = 1920
    jpeg_quality: int = 85
    # Kaam ke baad itni der, phir tasveer (screen ko badalne ka waqt).
    settle_ms: int = 600
    # Ek hi qadam ke do kaamon ke beech (click, phir type).
    between_ms: int = 120
    # User bolna band kare to haath itni der aur thehre rehte hain: us ka jumla
    # (jo kaam rok ya badal sakta hai) is dauran pohnch jata hai.
    hold_after_ms: int = 1000
    # UI-TARS ka pata aur naam (user ki apni OpenRouter key par).
    aim_url: str = "https://openrouter.ai/api/v1"
    aim_model: str = "bytedance/ui-tars-1.5-7b"
    aim_timeout_secs: float = 20.0


# ── app ka config ────────────────────────────────────────────────────────────


@dataclass
class Brain:
    provider: str = "anthropic"  # anthropic | gemini | groq | openrouter
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
    groq: str = ""
    # OpenRouter dimagh (aur purana UI-TARS nishana) — duniya bhar ke models ek key par.
    openrouter: str = ""

    def for_brain(self, provider: str) -> str:
        return {"anthropic": self.anthropic, "gemini": self.gemini, "groq": self.groq, "openrouter": self.openrouter}.get(provider, "")


def openrouter_claude(model: str) -> bool:
    """OpenRouter par Claude ka model ("anthropic/claude-haiku-5.5")? Wohi agent chalata hai
    (kaam bhi karta hai) — OpenRouter ka Anthropic-jaisa raasta sirf Claude ke saath
    pakka chalta hai (openrouter.ai/docs, Claude Code integration). Baqi models sirf baat."""
    return model.lower().startswith("anthropic/")


@dataclass
class Cloud:
    """Stonic Cloud ka raasta. Sab kuch app deti hai — gateway se pooch kar
    (POST /v1/voice-beta/session), taake models ka faisla server ka rahe."""

    url: str = ""  # gateway, "https://…"
    token: str = ""  # device key ya login token — kisi provider ki key nahi
    brain_model: str = ""
    # Gateway dimagh kis se chalata hai: "anthropic" = Claude (kaam bhi karta hai,
    # `/v1/messages`), "openai" = purana raasta (sirf baat, `/v1/chat/completions`).
    brain_provider: str = "openai"
    reasoning: str = ""  # dimagh ki soch, provider ki zubaan mein ("none" / "low" / "medium")
    purpose: str = "voice_beta_brain"  # hisaab mein dimagh ka naam


@dataclass
class AgentTool:
    """Stonic ka apna tool (app ke andar chalta hai): naam, kaam, arguments ka naqsha."""

    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class Agent:
    """Agent ke liye app ki bheji hui cheezein. Sab ikhtiyari: na aayen to dimagh
    sirf SDK ke apne tools (PowerShell, files) ke saath chalta hai."""

    tools: list[AgentTool] = field(default_factory=list)
    memory: str = ""  # yaadash ka card, jaisa app ne banaya
    # Nayi yaadash: account ka folder. Ho to Claude Agent SDK ki APNI auto memory
    # isi par chalti hai (agent.py `autoMemoryDirectory`) aur `memory` mein Claude
    # Code ka "# Memory" matn hota hai — jaisa ka taisa prompt mein. Khali = purana card.
    memory_dir: str = ""
    user_name: str = ""
    # Isi chat ki pehle ki baat (Standard ya Beta ki), purani se nayi tak.
    history: list["HistoryTurn"] = field(default_factory=list)
    # Is chat ki notebook (SDK session ka UUID, app deti hai). Disk par ho to
    # dimagh wahin se resume karta hai — Claude Code jaisa (agent.py).
    session: str = ""
    # Awareness (app ki "aankhein" + dimagh): is lamhe ka haal, din bhar ki diary,
    # Stonic ki apni pichli baatein aur notes — wohi block jo Standard voice ke
    # prompt mein jata hai. Awareness band ho to khali (prompt pehle jaisa).
    awareness: str = ""
    # Raasta khulte hi salaam? False = baat-cheet Awareness ne kholi hai: Stonic
    # ki pehli baat wohi hogi jo wo kehna chahta tha (salaam + baat = do dafa bolna).
    greet: bool = True


@dataclass
class HistoryTurn:
    role: str  # "user" ya "assistant"
    text: str


@dataclass
class Skills:
    """User ki apni skills (app ka Skills card) — Claude Code ka official plugin raasta.

    App (electron/services/skills/library.js) ek plugin folder banati hai:
    `.claude-plugin/plugin.json` + `skills/` (user ke skills folder ka junction).
    SDK isay `plugins=[{"type": "local", …}]` se uthata hai aur `skills=[…]` sirf
    chalu skills ko model tak jane deta hai (agent.py).
    """

    plugin: str = ""  # plugin ka folder (poora raasta)
    plugin_name: str = ""  # plugin.json ka `name` — skill ka poora naam "<plugin_name>:<skill>"
    names: list[str] = field(default_factory=list)  # chalu skills (folder ke naam)
    # Skills ki files ka nishan (app banati hai): badle to Classic ka desk naya dimagh uthata hai.
    version: str = ""
    # User ka skills folder: user kahe to dimagh nayi skill yahin likhta hai (folder + SKILL.md —
    # Claude Code mein skill save karne ka official tareeqa). Skills card use foran dikhata hai.
    dir: str = ""

    @property
    def on(self) -> bool:
        return bool(self.plugin and self.plugin_name and self.names)

    @property
    def qualified(self) -> list[str]:
        """SDK ke `skills` option ke liye: "stonic:weekly-report" (Claude Code ka plugin naam)."""
        return [f"{self.plugin_name}:{name}" for name in self.names] if self.on else []


@dataclass
class AppConfig:
    """App ka bheja hua config, jaancha hua."""

    mode: str
    voice_engine: str
    brain: "Brain"
    character: "Character"
    keys: Keys
    cloud: Cloud
    agent: Agent = field(default_factory=Agent)
    screen_aim: str = "off"
    # User ki "Stay quiet" setting (app ki Stonic Voice Settings). Default band.
    stay_quiet: bool = False
    skills: Skills = field(default_factory=Skills)


@dataclass
class Settings:
    ears: Ears = field(default_factory=Ears)
    turn: Turn = field(default_factory=Turn)
    voice: Voice = field(default_factory=Voice)
    limits: BrainLimits = field(default_factory=BrainLimits)
    session: Session = field(default_factory=Session)
    agent_limits: AgentLimits = field(default_factory=AgentLimits)
    screen_limits: ScreenLimits = field(default_factory=ScreenLimits)
    floor_limits: FloorLimits = field(default_factory=FloorLimits)
    brain: Brain = field(default_factory=Brain)
    character: Character = field(default_factory=Character)
    keys: Keys = field(default_factory=Keys)
    mode: str = "custom"  # cloud | custom
    voice_engine: str = "live"  # live | tts — sirf mode = custom par
    cloud: Cloud = field(default_factory=Cloud)
    agent: Agent = field(default_factory=Agent)
    screen_aim: str = "off"  # off | claude | uitars
    # User ne Settings mein "Stay quiet" chalu kiya (24 Oct 2026: default BAND —
    # faisla kabhi ghalat hota tha aur Stonic us se ki gayi baat par bhi chup reh jata).
    stay_quiet: bool = False
    # Saanjha waqt (life.py) — yaadash ka folder ho to HAR kirdar ka ek jaisa (kab,
    # kitni baatein, kis ke saath). None = yaadash band aur aam kirdar (pehle jaisa).
    life: Any = None
    # User ki skills (dono voices ka Claude dimagh). Khali = koi skill nahi (pehle jaisa).
    skills: Skills = field(default_factory=Skills)

    @property
    def is_cloud(self) -> bool:
        return self.mode == "cloud"

    @property
    def uses_agent(self) -> bool:
        """Dimagh kaam bhi karega? Claude Code harness par: Claude (apni key, OpenRouter par
        Claude ka model, ya Cloud jab gateway Claude de) — ya muft Gemini key (7 Nov 2026):
        wohi harness, model Gemini, engine ke pul se (gemini_relay.py)."""
        if not self.agent_limits.enabled:
            return False
        if self.is_cloud:
            return self.cloud.brain_provider == "anthropic"
        if self.brain.provider == "openrouter":
            return openrouter_claude(self.brain.model)
        return self.brain.provider in ("anthropic", "gemini")

    @property
    def screen_pointing(self) -> str:
        """Screen par nishana kis shakal mein (screen.py): Claude tasveer ke pixel deta hai;
        Gemini "grid" — har tasveer par 0–1000 ka jaal (Gemini isi par sikhaya gaya hai,
        Google ke docs: jagah 0–1000 par). UI-TARS / off waise hi."""
        if self.screen_aim == "claude" and not self.is_cloud and self.brain.provider == "gemini":
            return "grid"
        return self.screen_aim

    @property
    def uses_screen(self) -> bool:
        """Dimagh ko screen ke haath milenge? Sirf agent ko (Claude — apni key par, ya
        Stonic Cloud par jab gateway Claude de), Windows par, aur jab user ne band na kiye hon."""
        return self.uses_agent and self.screen_aim != "off" and os.name == "nt"

    @property
    def uses_floor(self) -> bool:
        """Baat-cheet ka ehsaas (floor.py): Stonic samjhe ke baat us se hai ya kisi aur se,
        aur khamoshi mein khud pukare. Sirf agent (Claude) par — wahi `<quiet:…>` ka faisla
        likhta hai aur us ke lafz bolne se pehle roke ja sakte hain.

        Teen shartein: dimagh Claude ho, settings.toml `[floor] enabled` (hamari master
        chabi) aur user ki apni "Stay quiet" setting chalu ho."""
        return self.uses_agent and self.floor_limits.enabled and self.stay_quiet

    @property
    def voice_name(self) -> str:
        """Kirdar ki jins se awaaz: wohi jora jo app mein hai (Charon / Aoede)."""
        return self.voice.female_voice if self.character.gender == "female" else self.voice.male_voice

    @property
    def voice_model(self) -> str:
        """Wo model jo waqai bolega — log aur status ke liye."""
        if self.is_cloud:
            return "stonic-cloud"
        return self.voice.live_model if self.voice_engine == "live" else self.voice.model

    def public(self) -> dict[str, Any]:
        """Log aur status ke liye — is mein koi chabi nahi."""
        return {
            "mode": self.mode,
            "brain": {"provider": self.brain.provider, "model": self.brain.model, "thinking": self.brain.thinking},
            "character": {"id": self.character.id, "name": self.character.name, "gender": self.character.gender},
            "voice": {"engine": "cloud" if self.is_cloud else self.voice_engine, "model": self.voice_model, "voice": self.voice_name},
            "ears": {"model": self.ears.model},
            "keys": {name: bool(getattr(self.keys, name)) for name in ("gemini", "gemini_backup", "anthropic", "groq", "openrouter")},
            "cloud": bool(self.cloud.url and self.cloud.token),
            "agent": {"on": self.uses_agent, "tools": len(self.agent.tools), "memory_chars": len(self.agent.memory), "memory_dir": bool(self.agent.memory_dir), "history": len(self.agent.history), "session": bool(self.agent.session)},
            "screen": {"on": self.uses_screen, "aim": self.screen_aim},
            "floor": self.uses_floor,
            "life": self.life is not None,
            "skills": len(self.skills.names) if self.skills.on else 0,
        }


_FILE_SECTIONS = {
    "ears": "ears", "turn": "turn", "voice": "voice", "brain": "limits", "session": "session", "agent": "agent_limits", "screen": "screen_limits",
    "floor": "floor_limits",
}


def _file_settings() -> Settings:
    """settings.toml parho. Ghalat naam ki line par ruk jao — chup chaap default
    par chalna sab se bura hai: banda samjhe setting lagi hai aur lagi na ho."""
    settings = Settings()
    data = tomllib.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
    for section_name, values in data.items():
        attribute = _FILE_SECTIONS.get(section_name)
        if attribute is None:
            raise ConfigError("bad_settings_file", f"settings.toml: [{section_name}] koi section nahi hai")
        section = getattr(settings, attribute)
        known = {f.name for f in fields(section)}
        for key, value in values.items():
            if key not in known:
                raise ConfigError("bad_settings_file", f"settings.toml: [{section_name}] mein '{key}' koi setting nahi hai")
            setattr(section, key, value)
    if settings.voice.script not in _SCRIPT_RULES:
        raise ConfigError("bad_settings_file", f"settings.toml: voice.script '{settings.voice.script}' — roman ya devanagari likho")
    return settings


def _text(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


_GATEWAY_URL = re.compile(r"https?://[A-Za-z0-9.-]+(:\d+)?")


def _parse_cloud(raw: Any) -> Cloud:
    raw = raw if isinstance(raw, dict) else {}
    cloud = Cloud(
        url=_text(raw.get("url"), MAX_URL_CHARS).rstrip("/"),
        token=_text(raw.get("token"), MAX_TOKEN_CHARS),
        brain_model=_text(raw.get("brain_model"), MAX_MODEL_CHARS),
        brain_provider=_text(raw.get("brain_provider"), 20).lower() or Cloud.brain_provider,
        reasoning=_text(raw.get("reasoning"), 20).lower(),
        purpose=_text(raw.get("purpose"), 40) or Cloud.purpose,
    )
    if not _GATEWAY_URL.fullmatch(cloud.url):
        raise ConfigError("bad_config", "cloud.url: gateway ka pata chahiye")
    if not cloud.token:
        raise ConfigError("cloud_signin", "cloud.token nahi hai — Stonic mein sign in chahiye")
    if not cloud.brain_model or not _MODEL_ID.fullmatch(cloud.brain_model):
        raise ConfigError("bad_config", "cloud.brain_model: model ka naam chahiye")
    if cloud.brain_provider not in ("openai", "anthropic"):
        raise ConfigError("bad_config", f"cloud.brain_provider '{cloud.brain_provider}' — openai ya anthropic")
    return cloud


_SCHEMA_TYPES = {"object", "string", "number", "integer", "boolean", "array", "null"}


def _json_schema(node: Any) -> Any:
    """App ke tools Gemini ki likhai mein hain ("OBJECT", "STRING"); Claude ko JSON Schema chahiye."""
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, str):
            lowered = value.lower()
            out[key] = lowered if lowered in _SCHEMA_TYPES else value
        elif key == "properties" and isinstance(value, dict):
            out[key] = {str(name): _json_schema(schema) for name, schema in value.items()}
        else:
            out[key] = _json_schema(value)
    return out


def _parse_agent(raw: Any) -> Agent:
    raw = raw if isinstance(raw, dict) else {}
    tools: list[AgentTool] = []
    seen: set[str] = set()
    raw_tools = raw.get("tools") if isinstance(raw.get("tools"), list) else []
    if len(raw_tools) > MAX_TOOLS:
        raise ConfigError("bad_config", f"agent.tools: zyada se zyada {MAX_TOOLS}")
    for item in raw_tools:
        if not isinstance(item, dict):
            raise ConfigError("bad_config", "agent.tools: har tool ek object ho")
        name = _text(item.get("name"), 60)
        if not _TOOL_NAME.fullmatch(name) or name in seen:
            raise ConfigError("bad_config", f"agent.tools: naam '{name}' theek nahi ya dohraya gaya")
        description = _text(item.get("description"), MAX_TOOL_DESCRIPTION_CHARS)
        if not description:
            raise ConfigError("bad_config", f"agent.tools: '{name}' ki wazahat khali hai")
        schema = _json_schema(item.get("parameters") if isinstance(item.get("parameters"), dict) else {})
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        schema.setdefault("properties", {})
        seen.add(name)
        tools.append(AgentTool(name=name, description=description, schema=schema))
    return Agent(
        tools=tools,
        memory=_text(raw.get("memory"), MAX_MEMORY_CHARS),
        memory_dir=_parse_memory_dir(raw.get("memory_dir")),
        user_name=_text(raw.get("user_name"), MAX_NAME_CHARS),
        history=_parse_history(raw.get("history")),
        session=_parse_session(raw.get("session")),
        awareness=_text(raw.get("awareness"), MAX_AWARENESS_CHARS),
        greet=raw.get("greet") is not False,
    )


def _parse_memory_dir(raw: Any) -> str:
    """Yaadash ka folder — sirf poora (absolute) raasta jo disk par mojood folder ho.

    App ka main process ise apni FileMemoryService se pehle hi milata hai; yahan
    doosra pehra: kuch aur ho to khali (yaadash band, baat-cheet phir bhi chalti hai).
    Wapsi CLI ki shakl mein — aakhir mein separator.
    """
    value = str(raw or "").strip()
    if not value or len(value) > MAX_MEMORY_DIR_CHARS or "\x00" in value or not os.path.isabs(value):
        return ""
    folder = os.path.normpath(value)
    if not os.path.isdir(folder):
        return ""
    return folder.rstrip("\\/") + os.sep


def _parse_session(raw: Any) -> str:
    """Notebook ka id — sirf saaf UUID; kuch aur ho to khali (nayi notebook, baat-cheet phir bhi shuru)."""
    value = str(raw or "").strip().lower()
    return value if _SESSION_ID.fullmatch(value) else ""


def _parse_skills(raw: Any) -> Skills:
    """User ki skills — app ka plugin folder aur chalu skills ke naam.

    App ka main process plugin banata hai (electron/services/skills/library.js);
    yahan doosra pehra: folder poora raasta ho, mojood ho, us mein Claude Code ka
    plugin manifest aur `skills/` ho, aur naam Agent Skills ke qaide par hon.
    Kuch bhi ghalat ho to skills band (Skills()) — baat-cheet phir bhi chalti hai.
    """
    if not isinstance(raw, dict):
        return Skills()
    value = str(raw.get("plugin") or "").strip()
    if not value or len(value) > MAX_SKILLS_PLUGIN_CHARS or "\x00" in value or not os.path.isabs(value):
        return Skills()
    plugin = os.path.normpath(value)
    manifest = os.path.join(plugin, ".claude-plugin", "plugin.json")
    if not os.path.isdir(os.path.join(plugin, "skills")) or not os.path.isfile(manifest):
        return Skills()
    try:
        with open(manifest, encoding="utf-8") as handle:
            plugin_name = str(json.load(handle).get("name") or "")
    except (OSError, ValueError, AttributeError):
        return Skills()
    if not _SKILL_NAME.fullmatch(plugin_name) or len(plugin_name) > MAX_SKILL_NAME_CHARS:
        return Skills()
    names: list[str] = []
    for item in raw.get("names") if isinstance(raw.get("names"), list) else []:
        name = str(item or "")
        if _SKILL_NAME.fullmatch(name) and len(name) <= MAX_SKILL_NAME_CHARS and name not in names:
            names.append(name)
        if len(names) >= MAX_SKILLS:
            break
    version = str(raw.get("version") or "")
    folder = str(raw.get("dir") or "").strip()
    if not folder or len(folder) > MAX_SKILLS_PLUGIN_CHARS or "\x00" in folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        folder = ""
    return Skills(
        plugin=plugin, plugin_name=plugin_name, names=names, version=version if _SKILLS_VERSION.fullmatch(version) else "",
        dir=os.path.normpath(folder) if folder else "",
    )


def _parse_history(raw: Any) -> list[HistoryTurn]:
    """Chat ki pichli baat: sirf user / assistant ke jumle, taaza hissa, hadd ke andar.

    Ghalat shakal par ghalti nahi — history madad hai, zaroorat nahi: jo samajh
    na aaye wo chhor diya jata hai aur baat-cheet phir bhi shuru hoti hai.
    """
    items = raw[-MAX_HISTORY_TURNS:] if isinstance(raw, list) else []
    turns: list[HistoryTurn] = []
    for item in items:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        text = " ".join(str(item.get("text") or "").split())[:MAX_HISTORY_TURN_CHARS]
        if text:
            turns.append(HistoryTurn(role=item["role"], text=text))
    # Hadd se zyada ho to purane jumle pehle jate hain.
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
    raw_keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    raw_brain = payload.get("brain") if isinstance(payload.get("brain"), dict) else {}
    raw_character = payload.get("character") if isinstance(payload.get("character"), dict) else {}

    # `mode` na aaye (purani app) to wohi jo pehle tha: apni keys, asli TTS.
    mode = _text(payload.get("mode"), 20).lower() or "custom"
    if mode not in MODES:
        raise ConfigError("bad_config", f"mode '{mode}' — {', '.join(MODES)} mein se ek")
    voice_engine = _text(payload.get("voice"), 20).lower() or ("live" if "mode" in payload else "tts")
    if voice_engine not in VOICE_ENGINES:
        raise ConfigError("bad_config", f"voice '{voice_engine}' — {', '.join(VOICE_ENGINES)} mein se ek")

    keys = Keys(**{f.name: _text(raw_keys.get(f.name), MAX_KEY_CHARS) for f in fields(Keys)})

    thinking = _text(raw_brain.get("thinking"), 20).lower() or Brain.thinking
    if thinking not in THINKING_LEVELS:
        raise ConfigError("bad_config", f"brain.thinking '{thinking}' — {', '.join(THINKING_LEVELS)} mein se ek")

    if mode == "cloud":
        cloud = _parse_cloud(payload.get("cloud"))
        # Dimagh ka model server ka faisla hai; yahan sirf wohi naam rakha jata hai.
        brain = Brain(provider=CLOUD_PROVIDER, model=cloud.brain_model, thinking=thinking)
    else:
        cloud = Cloud()
        brain = Brain(
            provider=_text(raw_brain.get("provider"), 20).lower() or Brain.provider,
            model=_text(raw_brain.get("model"), MAX_MODEL_CHARS),
            thinking=thinking,
        )
        if brain.provider not in PROVIDERS:
            raise ConfigError("bad_config", f"brain.provider '{brain.provider}' — {', '.join(PROVIDERS)} mein se ek")
        if not brain.model or not _MODEL_ID.fullmatch(brain.model):
            raise ConfigError("bad_config", "brain.model: model ka naam chahiye")

    character = Character(
        id=_text(raw_character.get("id"), 64).lower(),
        name=_text(raw_character.get("name"), MAX_NAME_CHARS),
        gender=_text(raw_character.get("gender"), 10).lower() or Character.gender,
        prompt=_text(raw_character.get("prompt"), MAX_PROMPT_CHARS),
    )
    if character.gender not in GENDER_NAMES:
        raise ConfigError("bad_config", f"character.gender '{character.gender}' — male ya female")
    if not character.prompt:
        raise ConfigError("bad_config", "character.prompt khali hai")
    agent = _parse_agent(payload.get("agent"))
    # `screen` na aaye (purani app) to haath nahi: jo pehle tha wohi.
    raw_screen = payload.get("screen") if isinstance(payload.get("screen"), dict) else {}
    screen_aim = _text(raw_screen.get("aim"), 20).lower() or "off"
    if screen_aim not in SCREEN_AIMS:
        raise ConfigError("bad_config", f"screen.aim '{screen_aim}' — {', '.join(SCREEN_AIMS)} mein se ek")
    if mode == "cloud" and screen_aim == "uitars":
        # UI-TARS user ki apni OpenRouter key par chalta hai; Stonic Cloud par
        # koi key nahi hoti. Wahan nishana Claude khud lagata hai.
        screen_aim = "claude"
    return AppConfig(
        mode=mode, voice_engine=voice_engine, brain=brain, character=character, keys=keys, cloud=cloud, agent=agent, screen_aim=screen_aim,
        # Sirf saaf `true` par chalu — field na aaye (purani app) ya kuch aur ho to band.
        stay_quiet=payload.get("stay_quiet") is True,
        skills=_parse_skills(payload.get("skills")),
    )


_app_config: AppConfig | None = None


def _with_app_config(settings: Settings, app: AppConfig) -> Settings:
    settings.mode, settings.voice_engine = app.mode, app.voice_engine
    settings.brain, settings.character, settings.keys, settings.cloud = app.brain, app.character, app.keys, app.cloud
    settings.agent = app.agent
    settings.screen_aim = app.screen_aim
    settings.stay_quiet = app.stay_quiet
    settings.skills = app.skills
    return _with_life(settings)


def _with_life(settings: Settings) -> Settings:
    """Saanjha waqt (life.py) — HAR kirdar ka ek jaisa: asli haqaiq (kab, kitni baatein,
    kis kirdar ke saath). Har session taaza parha jata hai. Yaadash yahan nahi.

    Naam sirf us kirdar ka badalta hai jo naam khud chunta hai (Soulmate): app mein
    user ka diya hua, warna jo us ne khud chuna; dono na hon to khali (prompt kehta
    hai ke wo khud chunegi). Baqi kirdaron ka naam wohi jo app ne bheja.
    """
    character = settings.character
    try:
        life = life_module.for_session(
            CHARACTERS_DIR, character.id, character.name, settings.agent.memory_dir,
            agent_home() if not settings.agent.memory_dir else "",
        )
    except Exception as error:  # noqa: BLE001 — zindagi na parhi jaye to bhi baat-cheet chale (pehle jaisi)
        from loguru import logger

        logger.warning(f"Life: {character.id} ka saanjha waqt parha na ja saka — {type(error).__name__}: {error}")
        life = None
    settings.life = life
    if life is not None and life.traits.names_herself and life.name != character.name:
        # App ka Character object chhuye baghair (wo agli session ka bhi hai).
        settings.character = replace(character, name=life.name)
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
    """Kaun si chabi ke baghair ye session nahi chal sakti. Apni keys par sunna
    aur bolna hamesha Gemini key par hain, dimagh apne provider ki key par.
    Stonic Cloud par koi key nahi lagti (login parse_app_config dekh chuka)."""
    if settings.is_cloud:
        return []
    missing = [] if settings.keys.gemini else ["gemini"]
    if not settings.keys.for_brain(settings.brain.provider) and settings.brain.provider not in missing:
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
#
# `roman` / `devanagari` likhai ke qaide ki misaal hain. Lab mein mard sirf
# Jarvis tha aur us ki misaal "ji Sir, abhi karta hoon" thi — wo ab Jarvis ki
# apni file (characters/male_jarvis.toml) mein hai, taake "Sir" Bro ya Mentor
# ki zubaan par na charhe. Jarvis ka poora prompt lab jaisa hi banta hai.
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

# Connect hote hi Stonic ki pehli line. Ye user ke role mein jata hai kyunke
# baat-cheet user ke message se hi shuru ho sakti hai.
GREETING_CUE = "(The voice channel has just opened. Greet me in one short line.)"
# Purani chat mein wapsi: naya salaam nahi, baat wahin se.
RESUME_CUE = "(The voice channel has just opened again in our earlier conversation. Pick it back up in one short line — no fresh greeting.)"
# Har raasta khulne ka ishara isi se shuru hota hai (saanjhe waqt ke ishare
# bhi — life.py `opening_cue`). Ye user ki baat nahi: na "moment" ka note,
# na doosre jumle se jor (agent_service.py), na user ki baat ki ginti (agent.py).
OPENING_CUE_PREFIX = "(The voice channel has just opened"


def is_opening_cue(text: str) -> bool:
    return str(text or "").lstrip().startswith(OPENING_CUE_PREFIX)


def opening_cue(settings: Settings, resumed: bool = False) -> str:
    """Raasta khulte hi dimagh ko pehla ishara: nayi chat par salaam, purani par wahin se.

    `resumed`: dimagh ne is chat ki notebook khol li (agent.py) — pichli baat us ke paas hai.
    Saanjhe waqt (life.py) ke saath har kirdar pehli baat / thori der / dino baad ka farq jaanta hai.
    """
    if resumed or settings.agent.history:
        return RESUME_CUE
    if settings.life is not None:
        return settings.life.opening_cue()
    return GREETING_CUE


_EXTRA_KEYS = ("natural_voice", "examples", "script_example_roman", "script_example_devanagari")


def character_extras(character_id: str) -> dict[str, str]:
    """characters/<id>.toml: us kirdar ki ada ke do chhote hisse, agar file ho.

    Sirf wo kirdar jin ke saath Voice Lab mein naap hua. Baqi (aur user ke apne
    banaye hue) in ke baghair chalte hain — prompt.md un ke liye khud poora hai.
    """
    path = CHARACTERS_DIR / f"{character_id}.toml"
    if not _CHARACTER_ID.fullmatch(character_id) or not path.exists():
        return {key: "" for key in _EXTRA_KEYS}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {key: str(data.get(key, "")).strip() for key in _EXTRA_KEYS}


def system_prompt(settings: Settings) -> str:
    """prompt.md ka dhancha + chuna hua kirdar + likhai aur jins ke qaide."""
    voice = settings.voice
    gender = GENDERS[settings.character.gender]
    extras = character_extras(settings.character.id)
    script_example = extras[f"script_example_{voice.script}"] or gender[voice.script]
    template = PROMPT_FILE.read_text(encoding="utf-8")
    if settings.life is not None and settings.life.needs_name:
        # Naam abhi nahi — wo khud chunegi (life.py). "Stonic" naam us ke munh mein nahi.
        template = settings.life.unnamed_prompt(template)
    return (
        template
        .replace("{{NAME}}", settings.character.name or "Stonic")
        .replace("{{CHARACTER}}", settings.character.prompt)
        .replace("{{SCRIPT_RULE}}", _SCRIPT_RULES[voice.script].format(example=script_example))
        .replace("\n{{GENDER_RULE}}", f"\n- {gender['rule']}" if gender["rule"] else "")
        .replace("{{NATURAL_VOICE}}", f" — {extras['natural_voice']} —" if extras["natural_voice"] else "")
        .replace("\n\n{{PERFORM_EXAMPLES}}", f"\n\n{extras['examples']}" if extras["examples"] else "")
        .strip()
    )


def history_prompt(settings: Settings) -> str:
    """Isi chat ki pehle ki baat-cheet. Khali = nayi chat (prompt mein kuch nahi)."""
    turns = settings.agent.history
    if not turns:
        return ""
    who = settings.agent.user_name or "User"
    lines = [f"{who if turn.role == 'user' else 'You'}: {turn.text}" for turn in turns]
    return (
        "## Earlier in this conversation\n\n"
        "This chat was opened again, so the two of you have already been talking. These are the latest lines, oldest first. "
        "Carry on from where it left off as one continuous conversation — do not greet them again, and do not repeat or "
        "summarize it unless they ask.\n\n" + "\n".join(lines)
    )


def life_prompt(settings: Settings, *, agent: bool) -> str:
    """Saanjhe waqt ka "## Your time together" (life.py) — har kirdar ka ek jaisa. Yaadash band ho to khali."""
    return settings.life.prompt_section(agent=agent) if settings.life is not None else ""


def conversation_prompt(settings: Settings) -> str:
    """Sirf baat karne wale dimagh (Gemini, Groq, Cloud ka text) ka prompt: kirdar + awareness + pichli baat."""
    parts = (system_prompt(settings), life_prompt(settings, agent=False), settings.agent.awareness.strip(), history_prompt(settings))
    return "\n\n".join(part for part in parts if part)


# ── Agent ka prompt: kirdar + "tum kaam bhi karte ho" + is PC ke haqaiq + yaadash ──

AGENT_PROMPT_FILE = ROOT / "agent_prompt.md"

_KNOWN_FOLDERS = {
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}


class _Guid(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


def _known_folder(folder_id: str, fallback: str) -> str:
    """Windows se poochho folder asal mein kahan hai (Desktop aksar OneDrive ke andar hota hai)."""
    if os.name != "nt":
        return fallback
    try:
        guid = _Guid()
        ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid))
        pointer = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer)) != 0:
            return fallback
        try:
            return pointer.value or fallback
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pointer)
    except Exception:  # noqa: BLE001 — folder ka pata na mile to bhi dimagh chalta hai
        return fallback


def agent_workspace() -> str:
    """Dimagh ka kaam ka folder: user ka ghar ("Desktop par file banao" seedha chale)."""
    return os.path.expanduser("~")


def agent_home() -> str:
    """SDK ka apna ghar (CLAUDE_CONFIG_DIR) — app deti hai; na de to temp mein."""
    home = os.environ.get("STONIC_VOICE_AGENT_HOME", "").strip() or os.path.join(tempfile.gettempdir(), "stonic-voice-agent")
    os.makedirs(home, exist_ok=True)
    return home


def pc_facts(settings: Settings) -> str:
    home = agent_workspace()
    folders = {name: _known_folder(folder_id, os.path.join(home, name)) for name, folder_id in _KNOWN_FOLDERS.items()}
    account = os.environ.get("USERNAME") or os.path.basename(home)
    lines = [
        f"- When this conversation opened it was {time.strftime('%A, %d %B %Y, %I:%M %p')} on this PC (run a command when you need the exact time).",
        f'- Windows account "{account}"; home folder {home} — this is your working folder, so relative paths start there.',
        "- " + "; ".join(f"{name}: {path}" for name, path in folders.items()) + ".",
        f"- Computer name {os.environ.get('COMPUTERNAME', 'unknown')}; commands run in Windows PowerShell.",
    ]
    if settings.agent.user_name:
        lines.insert(0, f"- The person you are talking with is {settings.agent.user_name}.")
    return "\n".join(lines)


# Screen ke haath. Naam wohi jo screen.py ke tools ke hain (selftest dono ko milata hai).
SCREEN_TOOL_NAMES = (
    "screenshot", "zoom", "left_click", "double_click", "right_click", "triple_click",
    "mouse_move", "left_click_drag", "scroll", "type", "key", "wait",
)
_SCREEN_HANDS = (
    "- **Your screen hands** — {names} — let you see this screen and work the mouse and keyboard like a person sitting at this desk. "
    "Look with `screenshot`, then act: {how}"
)
# User ki skills (Claude Code ka Skill tool). Fehrist (naam + kab kaam aati hai) Claude Code
# khud har baat-cheet mein deta hai; yahan sirf ye ke kab aur kaise. Skill na ho to ye line nahi.
_SKILLS_VOICE = (
    "- **Skill** opens one of their own skills: a playbook they saved for how they want a job done. Their skills are listed for the Skill tool, "
    "each with when to use it. When a request matches one, or they name one, load it with the Skill tool before you act and then follow it — "
    "their way comes before your own habits. A skill can point to files in its folder; Read those only when a step needs them. The skill is for you: never read it out loud."
)
_SKILLS_WORK = (
    "- **Skill** opens one of their own skills: a playbook they saved for how they want a job done. Their skills are listed for the Skill tool, "
    "each with when to use it. When the task names a skill or matches one, load it with the Skill tool before you start and then follow it — "
    "their way comes before your own habits. A skill can point to files in its folder; Read those only when a step needs them."
)
# Skill save karna (Claude Code ka official tareeqa: folder + SKILL.md) — sirf jab user kahe.
_SKILLS_SAVE = (
    "- **Saving a skill**: when they ask you to keep a way of doing a job as a skill (\"isay skill bana do\", \"save this as a skill\"), write it to their "
    "skills folder `{dir}`: a new folder named like `weekly-report` (lowercase letters, numbers and hyphens) holding `SKILL.md`. SKILL.md starts with a "
    "`---` block giving `name` (the folder name) and `description` in double quotes (what it does and when to use it, in the words they would say), "
    "then the steps. To change a skill they name, edit its SKILL.md there. A saved skill shows in their Skills card at once and is used from the next conversation."
)


def skills_prompt(settings: Settings, *, voice: bool) -> str:
    """Prompt ka skills wala hissa: Skill tool (chalu skills hon) + save ka raasta (folder ka pata ho)."""
    skills = settings.skills
    parts = [(_SKILLS_VOICE if voice else _SKILLS_WORK)] if skills.on else []
    if skills.dir:
        parts.append(_SKILLS_SAVE.format(dir=skills.dir))
    return "\n".join(parts)
_SCREEN_HOW = {
    "claude": "every position you give is a pixel in the latest screenshot.",
    "grid": (
        "every position you give is [x, y] on a 0-1000 grid laid over the latest screenshot: [0, 0] is its top-left corner "
        "and [1000, 1000] its bottom-right, whatever the picture's size. Aim at the middle of the thing you click."
    ),
    "uitars": (
        "name the element in a full sentence — what it is, its label or icon, where it sits. A second pair of eyes finds that exact spot "
        "from your words alone, so say enough that only one thing on the screen fits."
    ),
}
_SCREEN_RULES = (
    "- A command is your faster and surer hand, so PowerShell does whatever a command can do, and the mouse does what lives inside a window: "
    "buttons, menus, forms, anything an app offers only on screen. When the path is clear, send several screen actions together "
    "(click the field, type, press Return); the screenshot that comes back shows the result, and you read it before the next step.\n"
    "- The Stonic window, where this conversation lives, often fills the first screenshot. When the task is in another app, go straight "
    "to it through the taskbar, Alt+Tab or the Start menu; that app comes to the front on its own. When the task is in Stonic itself, "
    "work in it like any other app.\n"
    "- Before you press send on a message, an email or a post, look at a fresh screenshot: the open chat or recipient must be exactly the person "
    "they named and the text must be what they asked for. Only then send it, as its own step — never in the same batch that picks the chat. "
    "If the app is still loading, wait for it; if more than one contact could match, ask them which one.\n"
    "- Passwords, card numbers and one-time codes are theirs to type: when a screen asks for one, tell them and wait.\n"
    "- When a screen tool says they took the mouse, the controls are theirs again: stop there, tell them where you are, and carry on when they ask."
)
# App ka apna screenshot (Electron se) haathon ke saath nahi diya jata: screen
# dekhne ka ek hi raasta ho, wohi jis ke pixel par click lagta hai.
_SCREEN_REPLACES = frozenset({"take_screenshot"})


# App ke purane yaadash tools (Memory Core / pehle wala bridge). Nayi yaadash
# par in mein se koi nahi aata — wahan SDK ki apni auto memory hai.
MEMORY_TOOL_NAMES = frozenset({"memory", "recall", "remember", "follow_up"})


def agent_tools(settings: Settings) -> list[AgentTool]:
    """App ke wo tools jo is baat-cheet mein dimagh ko milte hain."""
    if not settings.uses_screen:
        return list(settings.agent.tools)
    return [tool for tool in settings.agent.tools if tool.name not in _SCREEN_REPLACES]


# Baat-cheet ka ehsaas (floor.py) — "ye baat mujh se hai ya kisi aur se?" Dimagh
# ko kya milta hai (har jumle ka [moment] note, khamoshi ka (moment) moqa) aur wo
# kya likhta hai (`<quiet:…>`). Faisla dimagh ka hai, qaide nahi — is liye yahan
# sirf ishare hain ke insaan kaise pehchanta hai.
#
# Sab se bara khatra ulta hai: user aksar AKELA hota hai, aur zyada chup Stonic
# "sun kar nazar-andaz" lagta hai. Is liye "jab tak doosre ki koi nishani na ho,
# baat tumhari hai" saaf likha hai.
_FLOOR_RULES = """## Who is talking to you

Your microphone stays open while you two talk, so you hear everything said near this PC: their words to you, their talk with someone in the room or on a call, their thinking aloud, dictation, a video or a song. Like a person sitting beside them, you join in when the words are for you, and you notice when time passes without an answer.

- Everything you hear arrives with a [moment: …] line — a note for you, not their words: the time; how long since you last spoke and whether you asked them something; whether they started while you were still talking; whether the lines just before were for someone else; what is on their screen; and which other app is using the microphone (a call or a dictation app means they are talking there).
- The words are for you when they say your name ({names}), answer or follow what you just said, or ask you for something you can do. Most of the time they are alone with you, so a question or request you can help with is for you, even after a long quiet.
- The words are for someone else when they address another person (another name, "yaar, tum…", a question about that person's own life), speak on a call, talk about you to someone ("ye Stonic hai, sab kar leta hai"), think aloud, dictate, or when it is a video, the TV or a song — sentences that sound like a news report, an advert or a script come from a screen. While those signs are there, waiting is kinder than cutting in — they can always call you by name.
- A line that opens with someone else's name ("Ali, …", "Sara!") is for that person, even when it is about something you two were just doing.
- Once they are talking with someone else, the conversation stays with that person until they clearly turn back to you: your name, a reply to what you said, or a request only you would handle. Lines that simply carry their chat along ("haan, main bhi aaunga", "theek hai, kal baat karte hain") still belong to that person.
- Begin every reply to their words with your decision, which is never spoken: <for-me> followed by your answer when the words are for you; or one tag and nothing more when they are not — <quiet:aside> for talk with someone else, <quiet:self> for thinking aloud or dictation, <quiet:media> for a video, the TV or music. You still heard it, so you know what happened when they turn back to you.
- When they ask you to wait ("ek minute", "hold on"), reply <quiet:wait> and stay quiet until they speak to you again.
- In the middle of a job, words that are not for you get <quiet:aside> and you carry on with the job.
- When they turn back to you after talking with someone, carry on naturally, like a friend who waited. What you overheard stays between you: bring it up only when it clearly helps them, and only when it is just the two of you.
- A (moment: …) note with no words from them means time has passed: you asked something and got no answer, or your line was cut off. Do what a person would: one short, gentle nudge, or let it rest with <quiet:rest>. When they still do not answer, they are busy — wait for them to come to you."""


def floor_rules(settings: Settings) -> str:
    """Prompt ka "Who is talking to you" hissa. Khali = baat-cheet ka ehsaas band."""
    if not settings.uses_floor:
        return ""
    # Pehle wo naam jo user ne khud rakha (usi se pukaara jata hai), us ke baad
    # "Stonic" — purana naam bhi chalta rahe, warna jo aadat se "Stonic?" kehta
    # hai us ki awaaz par koi nahi uthta.
    names = []
    if settings.character.name and settings.character.name.lower() != "stonic":
        names.append(settings.character.name)
    names.append("Stonic")
    return _FLOOR_RULES.format(names=" or ".join(f'"{name}"' for name in names))


# Yaadash ki do aadatein — official "# Memory" ke UPAR hamari (Usman, 30 Oct 2026).
# Official matn sirf LIKHNE ke qaide deta hai. Asal sessions mein (a) yaad ke
# sawal par dimagh fehrist dekh kar hi "yaad nahi" keh deta tha — note fehrist mein
# sab se upar tha, file tab khuli jab user ne do dafa zor diya; (b) "yaad rahe"
# Notes app mein chala gaya aur agli baat-cheet mein yaadash mein "nahi mila".
_MEMORY_HABITS = """## Using your memory

- When they ask what you remember — "yaad hai?", "yaad karo", "pichli dafa", "mere baare mein kya jaante ho", a goal, a person, a plan or a promise from before — look first, then answer. The MEMORY.md lines are short pointers and the notes hold the detail, so Grep your memory folder with a few English words for the idea (the notes are written in English: "paison wala goal" → goal|money|freedom), Read the notes that match, and answer from them.{notes_recall}
- When they ask you to remember something — "yaad rakhna", "yaad rahe", "isko yaad kar lo", "remember this" — save it in your memory folder as a memory note, the way the Memory section describes.{notes_save}
- While you talk, whenever something comes up that is worth remembering — their name, a goal, a person in their life, a plan, a feeling about something, what they love or fear, how they like things done — save it as a memory note right then, the way the Memory section describes: answer them first, then save it at the end of that same reply, and the saved note completes your reply. Save as many times as the conversation calls for, each thing when it comes up, and keep the conversation flowing."""
_NOTES_RECALL = " When the folder has nothing on it, look in their notes too (`manage_notes`, list) before you tell them what you found."
_NOTES_SAVE = " Their notes (`manage_notes`) are for writing they keep and read themselves — a note, a list, a draft — when they ask for one there (\"note bana do\", \"notes mein likh do\")."


def memory_habits(settings: Settings) -> str:
    """Yaadash parhne / likhne ki aadat. Sirf nayi yaadash (folder) par; Notes ka zikr sirf jab wo tool mila ho."""
    if not (settings.agent.memory_dir and settings.agent.memory.strip()):
        return ""
    notes = any(tool.name == "manage_notes" for tool in agent_tools(settings))
    return _MEMORY_HABITS.format(notes_recall=_NOTES_RECALL if notes else "", notes_save=_NOTES_SAVE if notes else "")


def agent_system_prompt(settings: Settings, with_history: bool = True) -> str:
    """Bolne wala prompt (jaisa tha) + kaam karne wala hissa + baat-cheet ka ehsaas + yaadash.

    `with_history=False`: notebook resume ho rahi hai — pichli baat poori us mein
    hai (tools samet), prompt mein dobara likhne ki zaroorat nahi.
    """
    names = [f"`{tool.name}`" for tool in agent_tools(settings)]
    screen_hands = screen_rules = ""
    if settings.uses_screen:
        screen_hands = _SCREEN_HANDS.format(names=", ".join(f"`{name}`" for name in SCREEN_TOOL_NAMES), how=_SCREEN_HOW[settings.screen_pointing])
        screen_rules = _SCREEN_RULES
    # Gaane / videos ka zikr sirf jab tool mila ho — prompt kabhi aisa kaam na bataye jo hai hi nahi.
    player = (
        ", and songs and videos: one they name opens and plays in their own browser, "
        "and \"what's going on with someone\" opens that topic's latest videos (`play_media` with mode latest)"
        if any(tool.name == "play_media" for tool in agent_tools(settings)) else ""
    )
    # Nayi yaadash par yaad Stonic tool se nahi, SDK ki auto memory (# Memory) se hai.
    memory_tool = any(tool.name in MEMORY_TOOL_NAMES for tool in agent_tools(settings))
    own_tools = (
        f"- **Your Stonic tools** — {', '.join(names)} — are {'your memory of this person, ' if memory_tool else ''}fresh facts from the web, "
        f"their screen, their clipboard, their notes and tasks{player}. Each one says what it does."
        if names else ""
    )
    work = (
        AGENT_PROMPT_FILE.read_text(encoding="utf-8")
        .replace("{{SCREEN_HANDS}}", screen_hands)
        .replace("{{STONIC_TOOLS}}", own_tools)
        .replace("{{SKILLS}}", skills_prompt(settings, voice=True))
        .replace("{{SCREEN_RULES}}", screen_rules)
        .replace("{{PC_FACTS}}", pc_facts(settings))
        .strip()
    )
    work = re.sub(r"\n{3,}", "\n\n", work)
    parts = [system_prompt(settings), work]
    life = life_prompt(settings, agent=True)
    if life:
        # Kirdar ke foran baad nahi, kaam ke baad: "wo kaun hai" aur "kya kar sakta hai"
        # pehle, phir saanjha waqt (har session badalta hai).
        parts.append(life)
    floor = floor_rules(settings)
    if floor:
        parts.append(floor)
    if settings.agent.awareness.strip():
        parts.append(settings.agent.awareness.strip())
    if settings.agent.memory_dir and settings.agent.memory.strip():
        # Claude ki official auto memory: Claude Code ka "# Memory" hissa jaisa ka
        # taisa (SDK custom prompt par ye khud nahi bhejta). MEMORY.md SDK khud
        # pehle user message mein laata hai — yahan dobara nahi.
        parts.append(settings.agent.memory.strip())
        parts.append(memory_habits(settings))
    elif settings.agent.memory.strip():
        parts.append("## What you remember about them\n\n" + settings.agent.memory.strip())
    history = history_prompt(settings) if with_history else ""
    if history:
        parts.append(history)
    return "\n\n".join(parts)


# ── Classic ka kaam (work.py): wohi Claude Code dimagh, text mein ─────────────
#
# Classic mein baat Gemini Live karta hai; PC ka kaam `agent_task` se yahan aata
# hai. Dimagh wohi (agent.py), sirf prompt alag: kirdar aur awaaz Gemini ki hain,
# yahan sirf kaam aur aakhir mein chhoti report.

WORK_PROMPT_FILE = ROOT / "work_prompt.md"
_WORK_CHARACTER = {"id": "work", "name": "Stonic", "gender": "male", "prompt": "Stonic"}


def work_settings(payload: Any) -> Settings:
    """Classic ke kaam ki settings — app har kaam ke saath bhejti hai (raasta, chabi, model).

    Engine ki "app config" (New ki baat-cheet wali, `set_app_config`) ko NAHI chhoota:
    dono apne apne raaste par. Kirdar / yaadash / baat-cheet ka ehsaas yahan nahi.
    """
    if not isinstance(payload, dict):
        raise ConfigError("bad_config", "config: object chahiye")
    parsed = parse_app_config({**payload, "character": dict(_WORK_CHARACTER), "agent": None})
    settings = _file_settings()
    settings.mode, settings.voice_engine = parsed.mode, parsed.voice_engine
    settings.brain, settings.keys, settings.cloud = parsed.brain, parsed.keys, parsed.cloud
    settings.character = parsed.character
    settings.screen_aim = parsed.screen_aim
    settings.stay_quiet = False
    # User ki skills: Classic ka kaam bhi wohi skills chalata hai jo New.
    settings.skills = parsed.skills
    raw_agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
    settings.agent = Agent(user_name=_text(raw_agent.get("user_name"), MAX_NAME_CHARS))
    return settings


def work_missing(settings: Settings) -> str:
    """Kaam kyun nahi ho sakta (code) — '' = ho sakta hai. Kaam Claude Code harness karta hai:
    Claude par, ya muft Gemini key par (wohi harness, model Gemini — gemini_relay.py)."""
    if not settings.agent_limits.enabled:
        return "agent_off"
    if settings.is_cloud:
        return "" if settings.cloud.brain_provider == "anthropic" else "cloud_no_agent"
    provider = settings.brain.provider
    if provider not in ("anthropic", "openrouter", "gemini") or (provider == "openrouter" and not openrouter_claude(settings.brain.model)):
        return "brain_not_claude"
    return "" if settings.keys.for_brain(provider) else f"{provider}_key_missing"


def work_system_prompt(settings: Settings) -> str:
    """Classic ke kaam karne wale dimagh ka prompt: tools, kaam ka tareeqa, report, is PC ke haqaiq."""
    screen_hands = screen_rules = ""
    if settings.uses_screen:
        screen_hands = _SCREEN_HANDS.format(names=", ".join(f"`{name}`" for name in SCREEN_TOOL_NAMES), how=_SCREEN_HOW[settings.screen_pointing])
        screen_rules = _SCREEN_RULES
    text = (
        WORK_PROMPT_FILE.read_text(encoding="utf-8")
        .replace("{{SCREEN_HANDS}}", screen_hands)
        .replace("{{SKILLS}}", skills_prompt(settings, voice=False))
        .replace("{{SCREEN_RULES}}", screen_rules)
        .replace("{{PC_FACTS}}", pc_facts(settings))
        .strip()
    )
    return re.sub(r"\n{3,}", "\n\n", text)


def agent_thinking(model: str, level: str) -> dict[str, str] | None:
    """Agent SDK ke liye soch ki setting. None = model khud tay kare (adaptive).

    Sirf Haiku par soch band ho sakti hai, aur wahin sab se zyada farq parta hai
    (pehla lafz ~0.9 s). Baqi models par `effort` hi raftaar ka dasta hai.
    """
    return {"type": "disabled"} if level == "low" and "haiku" in model.lower() else None


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


def groq_reasoning(model: str, level: str) -> str | None:
    """Groq ka reasoning_effort. None = field bhejo hi mat (jo model sochta nahi wo is par error deta hai)."""
    name = model.lower()
    if "gpt-oss" in name or "qwen3.8" in name:
        return level
    if "qwen" in name:
        return "none" if level == "low" else "default"
    return None
