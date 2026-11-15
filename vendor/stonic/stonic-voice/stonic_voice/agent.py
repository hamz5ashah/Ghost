"""Stonic ka kaam karne wala dimagh — Claude Agent SDK (official Claude Code harness) par.

Dono voice assistants isi ek engine par chalte hain (5 Nov 2026):

    New      Pipecat ki awaaz wali pipeline ka dimagh (agent_service.py): jo awaaz
             baat kar rahi hai wohi PowerShell chala rahi hai.
    Classic  Gemini Live baat karta hai; PC ka har kaam `agent_task` se isi dimagh
             ko jata hai (work.py) — text mein, `voice=False`.

Dimagh Claude Code wala hi engine hai (Anthropic ki `claude-agent-sdk`, andar
bundled Claude Code CLI): wohi loop jo sochta hai, tool chalata hai, natija
dekhta hai, aur phir aage barhta hai. Hum loop, tools, context / compaction,
sessions ya permissions ka koi apna badal nahi banate — sirf SDK ke official
raaste (options, hooks, can_use_tool, in-process MCP tools, stream events) se
jorte hain.

    user ka jumla ──> AgentBrain.say() ──> Claude (SDK ka apna process)
                                             │  lafz ba lafz jawab ──> awaaz
                                             │  tool call ──> pehra (safety.py) ──> chalta hai
                                             └─ natija ──> agla qadam … ──> "ho gaya"

Is file mein do cheezein hain:

    AgentBrain              SDK ke saath baat (Pipecat se azaad — akele test hota hai)
    ClaudeAgentLLMService   Pipecat ki pipeline mein us ki jagah (patla sa jor)

Teen baatein jo naap kar tay huin (12 Oct 2026, Haiku 5.5):

  - Pehla lafz ~0.9 s mein aata hai, aur tool se PEHLE model khud ek line bolta
    hai ("Checking the clock now") — wo line tool ke chalne ke dauran sunai deti hai.
  - Kaam ke dauran user kuch poochhe to wo jumla SDK ko usi waqt de diya jata
    hai; model usay agle qadam par dekhta hai, jawab deta hai, aur kaam jari
    rakhta hai. Is ke liye kaam rokna nahi parta.
  - Sirf baat (koi tool nahi) ke beech user bol pare to jawab wahin kaat diya
    jata hai (`interrupt`) — warna purana jawab poora likha jata aur naya der se aata.

Tools teen tarah ke hain:

    SDK ke apne      PowerShell, Read, Write, Edit, Glob, Grep — isi process mein chalte hain
    Stonic ke apne   recall / remember / web_search … — app ke andar chalte hain
                     (wohi code jo asal voice assistant ka hai); yahan se sirf
                     hukm jata hai (bridge.py) aur natija wapas aata hai
    Screen ke haath  screenshot, left_click, type, key … — yahin chalte hain
                     (screen.py): dimagh khud screen dekhta aur mouse / keyboard
                     chalata hai. User ne Settings mein band kiye hon to nahi milte.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from stonic_voice import safety
from stonic_voice.config import is_opening_cue
from stonic_voice.floor import SpeakTagScrubber, open_reply, settle_reply
from stonic_voice.screen import PREFIX as SCREEN_PREFIX
from stonic_voice.screen import SERVER as SCREEN_SERVER
from stonic_voice.screen import ScreenTools, label_for

# SDK ka process is engine ka mahaul (environment) wirse mein leta hai. User ki
# machine par us ke apne Claude Code ki settings ho sakti hain (kisi aur server
# ka pata, koi aur key, Bedrock/Vertex ka raasta) — wo Stonic ke dimagh ko ghalat
# jagah bhej deti. Engine ko in mein se kisi ki zaroorat nahi; jo chahiye wo
# `AgentConfig.env` mein saaf likha jata hai.
for _name in list(os.environ):
    if _name.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE")):
        os.environ.pop(_name, None)

# SDK har connect par pehle `claude -v` alag process chala kar version jaanchta
# hai — phir asli process. Hamara CLI SDK ke andar hi bundled hai (SDK pehle wohi
# chunta hai, `_find_bundled_cli`), is liye ye jaanch hamesha paas hoti hai aur
# sirf ek izafi 245 MB exe ka uthna hai: thande / antivirus wale PC par seconds.
# SDK ka apna sarkari switch; sirf engine ke apne process mein (upar wali safai ke baad).
os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

MCP_SERVER = "stonic"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"
# Naam khud chunne wale kirdar (life.py, Soulmate) ka apna tool: chuna hua naam mehfooz karna.
LIFE_SERVER = "life"
LIFE_PREFIX = f"mcp__{LIFE_SERVER}__"
# Itna bol kar (poora jawab) yaadash mein note likha → us ke baad ka text nahi bola jata
# (`_hush_after`). Chhoti line ("Noted, Sir…", ~40 huroof) ke baad asal jawab bola jata hai.
MEMORY_HUSH_CHARS = 80

# Lamba kaam chup-chaap (Usman, 4 Nov 2026): kai qadam wale kaam mein Stonic har qadam
# par bolti thi ("ye kar rahi hoon… ye nahi ho pa raha…") — har likha lafz awaaz banta
# hai. Ab: shuru ki ek line (SPEAK_FIRST), phir user ke aakhri paigham ke baad itne kaam
# ke qadam ho jayen to har paigham ka text roka jata hai. Us ke baad tool aaye = beech ki
# commentary (bola nahi jata, sirf chat mein "soch" ki tarah); paigham bina tool ke khatam =
# nateeja ya sawal (bola jata hai). User beech mein bole to ginti phir zero — jawab foran.
QUIET_AFTER_STEPS = 2

# App ke purane yaadash tools (nayi yaadash band ho to): ye baat ka hissa hain, kaam ke qadam nahi.
_APP_MEMORY_TOOLS = frozenset({"memory", "recall", "remember", "follow_up"})
# File tools jo yaadash ke folder par chalein to yaadash ka kaam hain (SDK ki auto memory).
_MEMORY_FILE_TOOLS = frozenset({"Read", "Write", "Edit", "Glob", "Grep"})

# SDK ek paigham (CLI ki ek JSON line) itne bytes tak hi parhta hai; zyada par
# poora dimagh toot jata hai (CLIJSONDecodeError). SDK ka default 1 MB hai —
# Read se PDF/tasveer ka natija base64 mein us line mein DO dafa aata hai (model
# ka tool_result + `tool_use_result`), is liye ~0.4 MB ki PDF hi had paar kar
# deti thi (31 Oct 2026: 556 KB PDF → har dafa "Stonic Voice could not start").
# Claude Code ka Read khud PDF 20 MB tak inline bhejta hai: 20 MB → base64 ~27 MB,
# do dafa ~54 MB. Ye had sirf chhat hai — memory utni hi lagti hai jitna paigham.
MAX_MESSAGE_BYTES = 96 * 1024 * 1024

# Baat-cheet ki lambai — Claude Code ki official auto-compact, us ki APNI had par:
# had ke qareeb CLI khud purani baat ki summary bana kar usi chat mein aage chalta
# hai. Hum koi window nahi dete (Usman ka faisla, 31 Oct 2026: "jo Claude ka
# official hai wohi"). Naapa (CLI 2.1.294, `get_context_usage`): 5.5 models
# (Haiku / Sonnet / Opus) 10 lakh window → compact 9,67,000 par; 4.5 models aur
# jo naam CLI na pehchane (jaise OpenRouter ka "anthropic/claude-haiku-5.5") 2 lakh
# → 1,67,000. Docs: code.claude.com/docs/en/model-config ("about 967K tokens by default").
# CLI window ke aakhir mein itni jagah summary aur agle jawab ke liye chhorta hai.
COMPACT_RESERVE_TOKENS = 33_000
# Ring ki had baat-cheet ke shuru mein CLI se ek dafa poochhi jati hai; itni der intezar.
COMPACT_POINT_WAIT_SECS = 10.0


def compact_point(model: str) -> tuple[int, int]:
    """(window, compact ki had) — jab tak CLI khud na bataye (naapa hua andaza)."""
    name = model.lower()
    # Gemini (muft key, gemini_relay.py): CLI is naam ko nahi pehchanta — anjaan naam = 2 lakh.
    small = "4-5" in name or "4.5" in name or ("/" in name and "haiku" in name) or name.startswith("gemini")
    window = 200_000 if small else 1_000_000
    return window, window - COMPACT_RESERVE_TOKENS
# Purani notebook kholte waqt us ki aakhri lambai: file ke aakhir se itna parhte hain.
NOTEBOOK_TAIL_BYTES = 2 * 1024 * 1024


def context_tokens(usage: Any, *, with_output: bool = True) -> int:
    """Ek model call ka poora context (tokens): naya + cache se parha + cache mein likha + jawab.

    Claude Code bhi apni had isi ginti par naapta hai. `/context` (`get_context_usage`)
    har dafa 5 `count_tokens` calls karta hai (naapa) — is liye har jawab par nahi,
    sirf shuru mein ek dafa (compact ki had jaanne ke liye).
    `with_output=False`: stream ka shuru — wahan jawab ka 1 token pehle se likha hota hai.
    """
    if not isinstance(usage, dict):
        return 0
    total = 0
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens") + (("output_tokens",) if with_output else ())
    for key in keys:
        try:
            total += max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total


def notebook_context(home: str, session_id: str) -> int:
    """Purani notebook ki aakhri lambai: aakhri jawab ka `usage`. 0 = pata nahi.

    Aakhri cheez compact ho (us ke baad koi jawab nahi) to bhi 0 — nayi lambai agle
    jawab ke saath aati hai. Sirf file ka aakhri hissa parha jata hai (bari PDF wali
    notebook 10 MB+ hoti hai).
    """
    if not home or not session_id:
        return 0
    try:
        paths = [path for path in (Path(home) / "projects").glob(f"*/{session_id}.jsonl") if path.is_file()]
    except OSError:
        return 0
    for path in paths:
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - NOTEBOOK_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # aadhi kati pehli line
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                return 0
            message = entry.get("message")
            if entry.get("type") != "assistant" or entry.get("isSidechain") or not isinstance(message, dict):
                continue
            used = context_tokens(message.get("usage"))
            if used:
                return used
    return 0


_COMPACT_MARK = re.compile(rb'"subtype"\s*:\s*"compact_boundary"')


def notebook_summarized(home: str, session_id: str) -> bool:
    """Is chat mein kabhi compact hua? (app ka ring phir hamesha dikhta hai)."""
    if not home or not session_id:
        return False
    try:
        paths = [path for path in (Path(home) / "projects").glob(f"*/{session_id}.jsonl") if path.is_file()]
        return any(_COMPACT_MARK.search(path.read_bytes()) for path in paths)
    except OSError:
        return False

# Interrupt ke baad us turn ke baqi paigham nazar-andaz hote hain jab tak us ka
# "khatam" na aaye. Wo kisi wajah se na aaye to itni der baad pehra khud hat
# jata hai — warna agla jawab bhi nigal liya jata.
DROP_FAILSAFE_SECS = 12.0

# "Pehle bolo, phir karo" — pehra, sirf hidayat nahi.
#
# Naapa hua (13 Oct 2026, Haiku 5.5, 8 hukm ek hi baat-cheet mein): prompt ki
# hidayat par model pehle do hukmon par tool se pehle bola, phir paanch dafa
# chup-chaap tool chalata raha — user ne 4+ second khamoshi suni. Har jumle ke
# saath yaad-dihani bhejne se bhi farq nahi para (2/8).
#
# Is liye ye code mein pakka hai: user ke jumle ke baad model ne abhi kuch bola
# na ho aur wo seedha tool bulaye, to wo call EK dafa lauta di jati hai is
# jawab ke saath. Model line bolta hai aur wohi call dobara karta hai. Qeemat:
# us ek dafa ~1 second; faida: user ko kabhi lambi khamoshi nahi milti, aur
# model usi baat-cheet mein ye aadat pakar leta hai.
SPEAK_FIRST = (
    "NOT RUN YET: they are listening and have heard nothing since they spoke. Say one short line out loud "
    "about what you are doing, then call this tool again with the same input."
)

# Screen ke kaam ke beech user bol para (agent_service.py `_on_context`): kaam wahin
# roka gaya aur us ka jumla naye jawab ke taur par diya gaya. Ye line dimagh ko
# batati hai ke wo kahan khara hai.
#
# Pehle ye jumla SDK ki "qatar" se jata tha (chalte jawab ke agle tool par).
# Naapa (13 Oct 2026, Haiku 5.5, screen ka kaam): user ne pehli tasveer par kaha
# "nahi, naam ye rakho" — dimagh ne purana naam hi likha aur Apply bhi daba diya
# ("as you first asked"), aur naye jumle ka zikr sirf aakhir mein kiya. Wajah do:
# qatar wala jumla "pehle chalta kaam poora karo" ke saath pohnchta hai, aur ek
# jawab ke saare kaam (click, type, Apply) us se pehle chal chuke hote hain.
CUT_IN = (
    "[They said this while you were in the middle of the job, so the job is paused right where it was. Follow what they say now: "
    "if they changed what they want, carry on from where things stand with the new instruction; if they only asked something, "
    "answer in a sentence and pick the job back up. Look at the screen again before your next action.]"
)
# "Ruk jao" ne screen ka kaam roka. Naapa: 1040 harf ki tehreer 72 par ruki, aur
# dimagh ne kaha "kuch nahi likha gaya" — use roke gaye kaam ka natija milta hi
# nahi. Ye line use sach batane deti hai.
STOPPED_MID = "[The job stopped at that moment. The action that was running may be partly done — for example, some of the text may already be typed.]"

# Shor (khansi, kursi, "hmm") ne Stonic ka jawab beech mein kaata aur us mein koi
# lafz nahi tha (agent_service.py `_answer_unheard`): user ki baat ka jawab us
# tak pohncha hi nahi. Naya jumla nahi — dimagh ke paas wo baat pehle se hai.
# Asal log (21 Oct 2026): "Hello" ke baad dimagh soch raha tha, shor aaya, jawab
# kata — aur Stonic tab tak chup raha jab tak user ne dobara nahi poochha.
UNHEARD_RETRY = "[A short noise cut in before your reply reached them — no new words. They are still waiting: reply to their last message now.]"

# Peeche chalta command khatam (SDK ke `TERMINAL_TASK_STATUSES`).
_JOB_OVER = frozenset({"completed", "failed", "stopped", "killed"})
# "Ruk jao" aaya aur peeche kaam chal rahe hain: faisla dimagh ka ke user ka
# matlab wo bhi hain ya sirf saamne wala kaam.
JOBS_RUNNING = "[Still running in the background: {jobs}. If they mean one of these, stop it with TaskStop; otherwise leave it running.]"

# Notebook (SDK ka session) ki file disk par na mile to SDK yehi kehta hai —
# process band hota hai (naapa, 14 Oct 2026, CLI 2.1.294). Is liye resume se
# pehle file dekhi jati hai (`saved_session`), aur phir bhi aaye to nayi notebook.
RESUME_MISSING = "No conversation found with session ID"


def saved_session(home: str, session_id: str) -> bool:
    """Is chat ki notebook disk par hai? (`CLAUDE_CONFIG_DIR/projects/<folder>/<id>.jsonl`)

    Folder ka naam SDK kaam ke folder (cwd) se banata hai; har folder dekh liya
    jata hai, taake us ke naam ke qaide par hum na tikein. Khali file = nahi.
    """
    if not home or not session_id:
        return False
    projects = Path(home) / "projects"
    try:
        return any(path.is_file() and path.stat().st_size > 0 for path in projects.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return False

# Baat-cheet ka ehsaas (floor.py): dimagh ne tay kiya ke suni hui baat us se nahi
# thi (`<quiet:aside>`). Us jawab mein koi tool nahi chalta — kamre mein kisi ka
# "ye files delete kar do yaar" Stonic ka hukm nahi. Kaam ke beech aisi baat aaye
# to kaam jari rehta hai (`mid_job`); ye pehra sirf naye, khali turn par hai.
QUIET_TOOLS = "NOT RUN: you decided these words were not meant for you, so nothing runs. End your turn now without saying anything."
# Moqa (Floor / Awareness) aaya aur dimagh abhi uth raha hai: itni der intezar.
CUE_READY_WAIT_SECS = 15.0
# Khamoshi ka moqa (floor.py `due`): sirf bolne ya chup rehne ka faisla, kaam nahi.
CUE_TOOLS = "NOT RUN: this moment is only for a short spoken line or <quiet:rest>. Nothing runs until they speak to you."

Listener = Callable[[str, dict[str, Any]], Awaitable[None]]
AppCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class AppTool:
    """Stonic ka apna tool, jaisa app ne bataya (naam, kaam, arguments ka naqsha)."""

    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class AgentConfig:
    model: str
    system_prompt: str
    env: dict[str, str]
    cwd: str
    effort: str = "low"
    thinking: dict[str, Any] | None = None
    builtin_tools: list[str] = field(default_factory=lambda: ["PowerShell", "Read", "Write", "Edit", "Glob", "Grep", "TaskStop"])
    app_tools: list[AppTool] = field(default_factory=list)
    max_turns: int = 40
    # Baat-cheet ka ehsaas (floor.py): dimagh `<quiet:…>` likh kar chup reh sakta hai.
    floor: bool = False
    # Beta ki "notebook" (SDK ka session, Claude Code jaisa): app har chat ko ek
    # UUID deta hai. Disk par ho to `resume` — poori pichli baat, tools samet;
    # na ho to isi naam se nayi. Khali = SDK khud naam rakhe.
    session_id: str = ""
    resume: bool = False
    # Resume nakaam ho (notebook kharab / gayab) to nayi notebook isi prompt se —
    # jis mein chat ki pichli baat likhi hai. None = `system_prompt` hi.
    fresh_system_prompt: str | None = None
    # Nayi yaadash: account ka folder. Ho to Claude Code / Agent SDK ki APNI auto
    # memory isi par chalti hai (`autoMemoryDirectory`): MEMORY.md SDK khud pehle
    # user message mein laata hai, model Read / Write / Edit / Grep se likhta
    # parhta hai, aur CLI note par `modified` ki mohar khud lagata hai. Khali =
    # auto memory band (pehle jaisa).
    memory_dir: str = ""
    # Saanjha waqt (life.Life): baat-cheet ka hisaab (code likhta hai) aur naam
    # khud chunne wale kirdar ka tool. Yaadash ki likhai yahan nahi — wo dimagh
    # baat ke dauran khud karta hai (chat mein nazar aati hai). None = yaadash band.
    life: Any = None
    # Dimagh ka text seedha awaaz banta hai (New). False = Classic ka kaam (work.py):
    # text ek report hai jo Gemini Live user ko sunata hai — "pehle bolo" aur "lamba
    # kaam chup" ke awaaz wale pehre yahan nahi lagte.
    voice: bool = True
    # Notebook disk par likhi jaye (SDK ka session, resume ke liye). False = sirf is
    # process ki yaad (Claude Code ka `--no-session-persistence`): Classic ka kaam.
    persist: bool = True
    # User ki skills (app ka Skills card) — Claude Code ka official raasta: plugin folder
    # (`plugins`) + sirf chalu skills (`skills`, "stonic:weekly-report"). Khali = koi skill nahi.
    skills_plugin: str = ""
    skills: list[str] = field(default_factory=list)


# ── SDK ke process ka mahaul ─────────────────────────────────────────────────


def base_env(home: str) -> dict[str, str]:
    """Har raaste par ek jaisa hissa.

    `home` SDK ka apna ghar hai (CLAUDE_CONFIG_DIR): user ke apne Claude Code
    (~/.claude) se bilkul alag — na us ka login yahan chalta hai, na us ki
    settings, na us ki yaad.
    """
    return {
        "CLAUDE_CONFIG_DIR": home,
        # Default band: auto memory sirf tab jab app yaadash ka folder de
        # (`memory_env` — ClaudeAgentBrain._options ise "0" kar deta hai).
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # Update ki jaanch, telemetry aur error report: Stonic ke andar in ka koi kaam nahi.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        # Windows par Git Bash ho ya na ho, hukm PowerShell mein chalein.
        "CLAUDE_CODE_USE_POWERSHELL_TOOL": "1",
        # Hamare apne tools thore hain: un ka naqsha shuru se saamne ho. (Default
        # mein SDK unhein "talash" ke peeche rakhta hai — har pehli call par ek
        # qadam zyada, aur awaaz mein wo qadam sunai deta hai.)
        "ENABLE_TOOL_SEARCH": "false",
        # SDK ka sarkari label (User-Agent): request kis app se aayi.
        "CLAUDE_AGENT_SDK_CLIENT_APP": "stonic-ai",
    }


def memory_options(memory_dir: str) -> tuple[dict[str, str], str | None]:
    """Claude Code ki auto memory hamare folder par: (env ka izafa, `settings` JSON).

    Docs (code.claude.com/docs/en/memory): `autoMemoryDirectory` kisi bhi settings
    scope se — `--settings` bhi — parha jata hai; `CLAUDE_CODE_DISABLE_AUTO_MEMORY=0`
    auto memory ko zabardasti ON rakhta hai. Lab mein naapa (CLI 2.1.294, custom
    prompt ke saath): MEMORY.md pehle user message mein aati hai. Folder na ho = band.
    """
    if not memory_dir:
        return {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}, None
    return {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}, json.dumps({"autoMemoryDirectory": memory_dir})


SKILL_TOOL = "Skill"
# Skills chalu hon to Claude Code ki apni settings (`--settings`, flag settings — `setting_sources=[]`
# ke bawajood parhi jati hain). Naapa (6 Nov 2026, CLI 2.1.318):
#   disableBundledSkills        Claude Code ki apni skills (code-review, loop, update-config …) fehrist
#                               se gayab — model ko sirf user ki skills dikhti hain.
#   disableSkillShellExecution  skill ke andar likha "!`command`" chalta NAHI ("[shell command execution
#                               disabled by policy]"). Warna skill kholte hi command chal jata — pehre
#                               (PreToolUse) se guzre baghair.
SKILL_SETTINGS: dict[str, Any] = {"disableBundledSkills": True, "disableSkillShellExecution": True}


def skill_options(plugin: str, skills: list[str]) -> dict[str, Any]:
    """User ki skills ke liye SDK ke official options: plugin folder + sirf chalu skills.

    Docs (code.claude.com/docs/en/agent-sdk/skills): `skills` = model ko kaun si skills
    dikhen aur Skill tool kaun si khole (baqi "not in this session's skills allowlist");
    hamari apni `tools` list ho to us mein "Skill" khud dalna parta hai. `setting_sources=[]`
    par skills sirf plugin se aati hain. Khali = kuch nahi (pehle jaisa).
    """
    if not plugin or not skills:
        return {}
    return {"plugins": [{"type": "local", "path": plugin}], "skills": list(skills)}


def skill_label(args: dict[str, Any]) -> str:
    """Screen par: "Using your weekly-report skill" (plugin ka naam "stonic:" nahi dikhta)."""
    name = str(args.get("skill") or "").strip().lstrip("/")
    short = name.split(":", 1)[1] if ":" in name else name
    return f"Using your {_clip(short, 60)} skill" if short else "Opening a skill"


def in_memory_dir(path: Any, memory_dir: str) -> bool:
    """Ye raasta yaadash ke folder ke andar hai? (Windows: chhote bare huroof barabar.)"""
    if not memory_dir or not path:
        return False
    try:
        target = os.path.normcase(os.path.abspath(str(path)))
        root = os.path.normcase(os.path.abspath(memory_dir)).rstrip("\\/")
    except (TypeError, ValueError):
        return False
    return target == root or target.startswith(root + os.sep)


def key_env(api_key: str) -> dict[str, str]:
    """Custom API: user ki apni Anthropic key, seedha Anthropic par."""
    return {"ANTHROPIC_API_KEY": api_key}


# OpenRouter ka Anthropic-jaisa raasta (openrouter.ai/docs, "Claude Code integration").
OPENROUTER_ANTHROPIC_URL = "https://openrouter.ai/api"


def openrouter_env(api_key: str, model: str) -> dict[str, str]:
    """Custom API, OpenRouter par Claude: request OpenRouter ke Anthropic-jaise raste se.

    OpenRouter ki hidayat: key `ANTHROPIC_AUTH_TOKEN` mein, aur `ANTHROPIC_API_KEY`
    saaf khali (warna SDK Anthropic ki key dhoondta hai). SDK ke andar ke chhote
    kaam (haiku / sonnet / opus ke naam se) bhi isi model par — OpenRouter ke naam
    "anthropic/…" hain, Anthropic ke apne naam wahan bharose se nahi chalte.
    """
    return {
        "ANTHROPIC_BASE_URL": OPENROUTER_ANTHROPIC_URL,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
    }


def gemini_env(relay_url: str, ticket: str, model: str) -> dict[str, str]:
    """Custom API, muft Gemini key par: request isi engine ke pul (gemini_relay.py) se Google tak.

    CLI ke paas Gemini key NAHI — sirf pul ka ticket (`ANTHROPIC_AUTH_TOKEN`). SDK ke
    andar ke chhote kaam (haiku / sonnet / opus ke naam se) bhi isi model par. Beta
    fields band (Gemini un ka koi kaam nahi karta), aur "zaroori nahi" wali model
    calls bhi band — muft key par har request din ki hadd mein se kat-ti hai.
    """
    return {
        "ANTHROPIC_BASE_URL": relay_url,
        "ANTHROPIC_AUTH_TOKEN": ticket,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
        # Pul isi PC par hai: user ki machine ka proxy (agar ho) beech mein na aaye.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def cloud_env(gateway_url: str, token: str, purpose: str) -> dict[str, str]:
    """Stonic Cloud: Claude ki request hamare gateway se guzarti hai (`/v1/messages`).

    Key gateway ke paas hai; yahan sirf user ka Stonic token jata hai. `purpose`
    se hisaab mein ye kharcha Beta ke dimagh ke naam likha jata hai.
    """
    return {
        "ANTHROPIC_BASE_URL": gateway_url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_CUSTOM_HEADERS": f"x-stonic-purpose: {purpose}",
    }


# ── Tool ka chhota sa naam, screen ke liye ───────────────────────────────────

_LABELS = {
    "recall": "Checking memory",
    "remember": "Saving to memory",
    "follow_up": "Noting a follow-up",
    "web_search": "Searching the web",
    "take_screenshot": "Looking at the screen",
    "look_at_camera": "Looking through the camera",
    "clipboard_read": "Reading the clipboard",
    "clipboard_write": "Copying to the clipboard",
    "send_notification": "Sending a notification",
    "manage_notes": "Working with notes",
    "manage_tasks": "Working with tasks",
    "play_media": "Finding the video",
    "media_control": "Controlling the music",
}
_BUILTIN_LABELS = {"TaskStop": "Stopping a background job"}


_FILE_VERBS = {"Read": "Reading", "Write": "Writing", "Edit": "Editing"}


def _clip(text: Any, limit: int = 90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe_tool(name: str, args: dict[str, Any], memory_dir: str = "") -> str:
    """"Abhi kya ho raha hai" — ek chhoti line. Model PowerShell ke saath khud wazahat likhta hai."""
    if name in safety.SHELL_TOOLS:
        return _clip(args.get("description") or args.get("command") or "Running a command")
    if name == SKILL_TOOL:
        return skill_label(args)
    # SDK ki auto memory: yaadash ke folder par file tools = yaadash ka kaam.
    if name in _FILE_VERBS or name in ("Glob", "Grep"):
        target = args.get("file_path") or args.get("path") or ""
        if in_memory_dir(target, memory_dir):
            return "Saving to memory" if name in ("Write", "Edit") else "Checking memory"
    if name in _FILE_VERBS:
        target = os.path.basename(str(args.get("file_path") or "")) or "a file"
        return f"{_FILE_VERBS[name]} {_clip(target, 60)}"
    if name in ("Glob", "Grep"):
        return f"Searching files for {_clip(args.get('pattern') or '', 50)}".rstrip()
    if name.startswith(SCREEN_PREFIX):
        return label_for(name, args)
    if name.startswith(LIFE_PREFIX):
        return "Choosing a name"
    if name in _BUILTIN_LABELS:
        return _BUILTIN_LABELS[name]
    if name.startswith(MCP_PREFIX):
        short = name[len(MCP_PREFIX):]
        if short == "memory":
            # Nayi yaadash ka akela tool: hukm se pata chalta hai parh rahi hai ya likh rahi hai.
            return "Checking memory" if str(args.get("command") or "").lower() == "view" else "Saving to memory"
        return _LABELS.get(short, short.replace("_", " ").capitalize())
    return name


# ── App ki chat ke liye: tool ne kya liya, kya diya ──────────────────────────
#
# App ka voice panel har qadam dikhata hai (Input / Output khol kar). Wahan sirf
# parhne ke liye — is liye lambi cheezein kaat di jati hain (poori file ki
# tehreer, bara natija) taake data channel aur chat ki file na phoolein.

TOOL_ARG_CHARS = 1500
TOOL_OUTPUT_CHARS = 6000
_ARG_ITEMS = 40
_ARG_DEPTH = 4


def tool_args(value: Any, depth: int = 0) -> Any:
    """Tool ke arguments, JSON ke qabil aur chhote."""
    if isinstance(value, str):
        return value if len(value) <= TOOL_ARG_CHARS else value[:TOOL_ARG_CHARS] + f"… [{len(value) - TOOL_ARG_CHARS} more characters]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _ARG_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {str(key): tool_args(item, depth + 1) for key, item in list(value.items())[:_ARG_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [tool_args(item, depth + 1) for item in list(value)[:_ARG_ITEMS]]
    return _clip(value, 200)


def tool_output(content: Any) -> str:
    """SDK ka tool natija (text ya blocks) — parhne wala text. Tasveer ki jagah sirf us ka zikr."""
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", "")
            if kind == "text":
                parts.append(str(block.get("text") if isinstance(block, dict) else getattr(block, "text", "")))
            elif kind == "image":
                parts.append("[image]")
        text = "\n".join(part for part in parts if part)
    else:
        text = str(content)
    text = text.strip()
    return text if len(text) <= TOOL_OUTPUT_CHARS else text[:TOOL_OUTPUT_CHARS] + f"\n… [{len(text) - TOOL_OUTPUT_CHARS} more characters]"


# ── SDK ki ghalti ko wo lafz dena jo errors.py pehchanta hai ─────────────────

_ERROR_HINTS = {
    "authentication_failed": "authentication_error invalid x-api-key",
    "billing_error": "credit balance is too low",
    "rate_limit": "rate_limit",
    "server_error": "overloaded",
    "invalid_request": "invalid_request",
}


class AgentBrain:
    """Ek baat-cheet ka dimagh. `start()` → `say()` jitni dafa chaho → `stop()`."""

    def __init__(self, config: AgentConfig, *, listener: Listener, call_app: AppCall | None = None, screen: ScreenTools | None = None) -> None:
        self._config = config
        self._listener = listener
        self._call_app = call_app
        # Screen ke haath (screen.py). None = is baat-cheet mein nahi (user ne band kiye, ya Windows nahi).
        self._screen = screen
        if screen is not None:
            screen.bind(self)
        self._client: ClaudeSDKClient | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closed = False
        self._started_at = 0.0
        # Notebook: koi jawab poora hua (ab resume ki nakaami ka sawal nahi), aur aakhri paigham.
        self._settled = False
        self._last_query = ""

        # Turn ka haal.
        self._working = False      # model kuch kar raha hai (jawab likh raha hai ya tool chala raha hai)
        self._used_tools = False   # is turn mein koi tool chala (yani "kaam", sirf baat nahi)
        self._reply_open = False   # awaaz ke liye ek jawab khula hai
        self._cut = False          # user ne is paigham ko kaat diya: us ka baqi text nahi bola jayega
        self._user_speaking = False
        self._unheard = False      # model ne kuch likha jo user ne suna nahi
        self._drop_until = 0.0     # interrupt ke baad: is waqt tak (ya "khatam" tak) sab nazar-andaz
        self._error_told = False   # is turn ki ghalti bata di gayi (ek hi dafa batani hai)
        # "Pehle bolo, phir karo" (SPEAK_FIRST).
        self._spoke = False        # user ke aakhri jumle ke baad model ne kuch bola
        self._message_seq = 0      # model ke paighamon ki ginti
        self._nudged_seq = -1      # jis paigham ke tools "pehle bolo" keh kar lautaye gaye
        self._tools: dict[str, str] = {}
        self._tool_started: dict[str, float] = {}   # tool id -> kab chala (app ki chat mein waqt)
        # Chalte paigham ke tools, tarteeb se (screen.py: "ye is jawab ka aakhri kaam hai?").
        self._message_tools: list[tuple[str, str]] = []
        self._message_done = False
        self._current_tool: str | None = None
        self._last_tool = ""       # is jawab mein aakhri chala hua tool (naam)
        # Peeche chalte command (SDK ka `run_in_background`).
        self._background_calls: set[str] = set()   # wo tool calls jo peeche gaye
        self._jobs: dict[str, str] = {}            # task id -> ek line ka bayan
        self._notice_due = False   # ek kaam khatam hua: session ab KHUD ek jawab shuru karega
        # Baat-cheet ki lambai (app ka ring): Claude ki apni token ginti, har model call ke saath.
        self._ctx_call = 0         # chalte paigham ka input (prompt + pichli baat)
        self._ctx_used = 0         # aakhri call ka poora context (input + jawab); 0 = pata nahi
        self._compacting = False   # CLI is waqt purani baat ki summary bana raha hai
        self._summarized = False   # is chat mein kabhi compact hua (ring phir hamesha dikhta hai)
        # CLI ki apni had (shuru mein poochhi jati hai); tab tak naapa hua andaza.
        self._ctx_window, self._ctx_limit = compact_point(config.model)
        self._point_task: asyncio.Task[None] | None = None
        # Baat-cheet ka ehsaas (floor.py) — "ye baat mujh se hai?" Har paigham ka
        # shuru parh kar: `<quiet…` = kuch nahi bola jata.
        self._gate = "open"        # "pending" (shuru dekh rahe hain) | "open" (bolo) | "quiet" (sab nigal lo)
        self._gate_buf = ""
        # Jawab ke beech / aakhir mein bhatka `<for-me>` / `</for-me>` — na chat mein, na awaaz mein.
        self._tags = SpeakTagScrubber()
        self._awaiting = False     # user ke jumle / moqe ka faisla abhi baqi
        self._may_continue = False  # wo jumla kaam ke beech aaya: chup ke baad bhi kaam jari
        self._quiet_turn = False   # is turn ka faisla "chup": koi tool nahi, koi lafz nahi
        self._cue_turn = False     # ye turn khamoshi ka moqa hai (floor.py `due`), user ka jumla nahi
        self._held: str | None = None  # jumla jis ka faisla baqi: ijazat aur screen ka taala tab, jab baat Stonic se nikle
        # Saanjha waqt (life.py): user waqai bola? (salaam ke ishare nahi gine jate)
        self._user_lines = 0
        self._first_line_at = 0.0
        self._talk_recorded = False
        # Naam bol kar `choose_my_name` chala, ya poora jawab de kar yaadash mein note
        # likha: is jawab ka baqi text nahi bola jata. Naapa (30 Oct 2026, Haiku 5.5):
        # tool ke baad har dafa wohi baat dobara jur jati thi ("Batao, woh kaisi hai?" do dafa).
        self._hush_rest = False
        self._turn_spoken = 0      # is turn mein ab tak kitne huroof bole ja chuke
        # Lamba kaam chup-chaap (QUIET_AFTER_STEPS): aakhri paigham ke baad kitne kaam ke qadam
        # chale, aur is paigham ka roka hua text (None = seedha bolo).
        self._job_steps = 0
        self._held_words: list[str] | None = None
        self._message_thought = False  # is paigham mein soch aayi (chat mein commentary alag para)
        self._stop_reason = ""         # chalte paigham ka khatma (message_delta): "tool_use" = aage kaam
        # Tool ka block jis ke arguments abhi aa rahe hain (yaadash ka kaam hai ya nahi — raasta dekh kar).
        self._pending_block: tuple[int, str, list[str]] | None = None

    # ── Zindagi ─────────────────────────────────────────────────────────────

    @property
    def working(self) -> bool:
        return self._working

    @property
    def closed(self) -> bool:
        """`stop()` ho chuka — ye dimagh ab kisi kaam ka nahi."""
        return self._closed

    async def wait_ready(self) -> None:
        """`start()` poora hone tak ruko (utha, nakaam hua, ya band kiya gaya — teeno par khulta hai)."""
        await self._ready.wait()

    @property
    def doing_work(self) -> bool:
        """Kaam chal raha hai (tool chal chuka), sirf baat nahi."""
        return self._working and self._used_tools

    @property
    def jobs(self) -> list[str]:
        """Peeche chalte kaam (un ka bayan). Khali = koi nahi."""
        return list(self._jobs.values())

    @property
    def on_screen(self) -> bool:
        """Is waqt screen ka kaam chal raha hai (aakhri chala hua tool mouse / keyboard / tasveer ka tha)?"""
        return self.doing_work and self._last_tool.startswith(SCREEN_PREFIX)

    @property
    def resuming(self) -> bool:
        """Is chat ki purani notebook khol rahe hain (pichli baat dimagh ke paas hai)."""
        return self._config.resume

    def _options(self) -> ClaudeAgentOptions:
        config = self._config
        # Claude ki official auto memory, hamare folder par (folder na ho = band).
        memory_env, memory_settings = memory_options(config.memory_dir)
        kwargs: dict[str, Any] = dict(
            model=config.model,
            # `snapshot: False`: notebook prompt ko "pakar" kar nahi rakhti — resume par
            # bhi har dafa yehi taaza prompt jata hai (aaj ki yaadash, waqt, settings).
            # Default (True) mein CLI pehli dafa wala prompt hamesha bhejta hai —
            # naapa, 14 Oct 2026, CLI 2.1.294. Pichli baat notebook se aati hai.
            system_prompt={"type": "custom", "prompt": config.system_prompt, "snapshot": False},
            tools=list(config.builtin_tools),
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            # Pehra fail-closed: hook gire (exception / ghalat jawab) to tool NAHI chalta —
            # SDK 0.2.177+ / CLI 2.1.317+ ka official `on_failure="block"`.
            hooks={"PreToolUse": [HookMatcher(hooks=[self._pre_tool_use], on_failure="block")]},
            # User ki apni Claude Code settings / CLAUDE.md is dimagh mein nahi aatin.
            setting_sources=[],
            include_partial_messages=True,
            cwd=config.cwd,
            effort=config.effort,
            max_turns=config.max_turns,
            env={**config.env, **memory_env},
            # Baat-cheet SDK ki apni notebook mein (CLAUDE_CONFIG_DIR/projects,
            # sirf is PC par) — Claude Code jaisi: chat dobara khule to wahin se.
            stderr=lambda line: logger.debug(f"claude: {line.rstrip()[:400]}"),
            # Bari file (PDF / tasveer) ka natija ek lambi line hai — 1 MB default par dimagh tootta tha.
            max_buffer_size=MAX_MESSAGE_BYTES,
        )
        if not config.persist:
            # Classic ka kaam: baat-cheet sirf is process ki yaad mein, disk par kuch nahi.
            kwargs["extra_args"] = {"no-session-persistence": None}
        elif config.session_id:
            kwargs["resume" if config.resume else "session_id"] = config.session_id
        if config.thinking is not None:
            kwargs["thinking"] = config.thinking
        flag_settings: dict[str, Any] = json.loads(memory_settings) if memory_settings else {}
        skills = skill_options(config.skills_plugin, config.skills)
        if skills:
            # User ki skills: Claude Code ka Skill tool + plugin folder + sirf chalu skills.
            kwargs.update(skills)
            if SKILL_TOOL not in kwargs["tools"]:
                kwargs["tools"] = [*kwargs["tools"], SKILL_TOOL]
            flag_settings.update(SKILL_SETTINGS)
        if flag_settings:
            # `--settings`: Claude Code ise "flag settings" ki tarah parhta hai —
            # `setting_sources=[]` ke bawajood (user ki apni settings phir bhi nahi aatin).
            kwargs["settings"] = json.dumps(flag_settings)
        servers: dict[str, Any] = {}
        if config.app_tools and self._call_app is not None:
            servers[MCP_SERVER] = create_sdk_mcp_server(name=MCP_SERVER, version="1.0.0", tools=[self._bridge_tool(spec) for spec in config.app_tools])
        if self._screen is not None:
            servers[SCREEN_SERVER] = create_sdk_mcp_server(name=SCREEN_SERVER, version="1.0.0", tools=self._screen.sdk_tools())
        if config.life is not None and config.life.needs_name:
            # Naam abhi nahi: pehli baat mein wo khud chunegi aur yahan se mehfooz karegi.
            servers[LIFE_SERVER] = create_sdk_mcp_server(name=LIFE_SERVER, version="1.0.0", tools=[self._name_tool()])
        if servers:
            kwargs["mcp_servers"] = servers
        return ClaudeAgentOptions(**kwargs)

    # ── Chalte jawab ka haal (screen.py ke liye) ────────────────────────────

    def current_tool_id(self) -> str | None:
        """Wo tool jo abhi chal raha hai (pehre se guzar chuka)."""
        return self._current_tool

    def message_seq(self) -> int:
        return self._message_seq

    def later_tool_names(self, tool_id: str | None) -> list[str] | None:
        """Isi paigham mein is tool ke BAAD aane wale tools. None = abhi pata nahi (paigham aa raha hai)."""
        ids = [item[0] for item in self._message_tools]
        if not tool_id or tool_id not in ids:
            return None
        later = [name for _id, name in self._message_tools[ids.index(tool_id) + 1:]]
        # SDK tool ko us waqt bhi chala deta hai jab jawab abhi aa raha ho. Agla
        # tool nazar aa chuka ho to jawab maloom hai; warna jawab poora hone tak "pata nahi".
        return later if later or self._message_done else None

    async def start(self) -> None:
        """SDK ka process uthao (~0.5–1.5 s). Is ke baad `say()` foran chalta hai."""
        if self._closed:
            # Uthne se pehle hi band kar diya gaya (pehle se utha dimagh jo kaam na aaya): process hi mat uthao.
            self._ready.set()
            return
        started = time.perf_counter()
        try:
            client = ClaudeSDKClient(options=self._options())
            await client.connect()
        except Exception as error:  # noqa: BLE001 — jo bhi ho, user ko saaf wajah dikhni chahiye
            # Notebook disk se gayab (dekhne aur kholne ke beech mitayi gayi): nayi notebook.
            if self._resume_lost(str(error)) and await self._start_fresh(str(error)):
                self._ready.set()
                return
            logger.error(f"Agent ka dimagh uth na saka: {type(error).__name__}: {error}")
            self._ready.set()
            await self._emit("error", {"detail": f"agent_start_failed: {type(error).__name__}: {error}", "fatal": True})
            return
        if self._closed:
            await self._disconnect(client)
            return
        self._client = client
        self._reader = asyncio.create_task(self._read(client), name="stonic-agent-reader")
        self._ready.set()
        logger.info(
            f"Agent ka dimagh tayyar — {self._config.model}, {round((time.perf_counter() - started) * 1000)} ms"
            + (f", notebook {'resume' if self._config.resume else 'nayi'}" if self._config.session_id else "")
        )
        await self._tell_session()
        self._point_task = asyncio.create_task(self._learn_compact_point(client), name="stonic-agent-compact-point")
        if self._config.resume:
            # Purani lambi chat khuli: ring pehle jawab se pehle hi sahi dikhe.
            home = str(self._config.env.get("CLAUDE_CONFIG_DIR") or "")
            self._ctx_used = await asyncio.to_thread(notebook_context, home, self._config.session_id)
            self._summarized = await asyncio.to_thread(notebook_summarized, home, self._config.session_id)
            if self._ctx_used or self._summarized:
                await self._tell_context()
        life = self._config.life
        if life is not None and life.name_from_facts:
            # Us ne naam pehle chuna tha magar app ke paas nahi (data mita / naya PC): app ko dobara.
            await self._emit("life", {"character": life.character_id, "name": life.name})

    # ── Notebook (SDK ka session) ───────────────────────────────────────────

    async def _tell_session(self) -> None:
        """App ko batao is chat ki notebook kaunsi hai — wo chat ke saath yaad rakhti hai."""
        if self._config.session_id:
            await self._emit("session", {"id": self._config.session_id, "resumed": self._config.resume})

    async def _query(self, client: ClaudeSDKClient, text: str) -> None:
        # Aakhri paigham yaad: resume nakaam ho to nayi notebook par yehi dobara jata hai.
        self._last_query = text
        await client.query(text)

    def _resume_lost(self, detail: str) -> bool:
        """Purani notebook khul na saki (mitayi gayi / kharab) — aur abhi tak koi jawab nahi bana."""
        return self._config.resume and not self._settled and not self._closed and RESUME_MISSING in detail

    async def _start_fresh(self, detail: str) -> bool:
        """Resume nakaam: nayi notebook (chat ki pichli baat prompt mein), aur wohi paigham dobara.

        False = nayi bhi na uth saki — bulane wala asal ghalti batata hai.
        """
        config = self._config
        logger.warning(f"Agent: notebook {config.session_id} resume nahi hui ({detail[:160]}) — nayi notebook se")
        config.resume = False
        config.session_id = str(uuid.uuid4())
        self._ctx_call = self._ctx_used = 0  # nayi notebook: purani lambai ab us ki nahi
        self._summarized = False
        if config.fresh_system_prompt:
            config.system_prompt = config.fresh_system_prompt
        old, self._client = self._client, None
        if old is not None:
            await self._disconnect(old)
        try:
            client = ClaudeSDKClient(options=self._options())
            await client.connect()
        except Exception as error:  # noqa: BLE001
            logger.error(f"Agent: nayi notebook bhi na khul saki: {type(error).__name__}: {error}")
            return False
        if self._closed:
            await self._disconnect(client)
            return True
        self._client = client
        self._reader = asyncio.create_task(self._read(client), name="stonic-agent-reader")
        await self._tell_session()
        if self._last_query:
            self._working = True
            try:
                await client.query(self._last_query)
            except Exception as error:  # noqa: BLE001
                logger.error(f"Agent: nayi notebook par paigham na gaya: {error}")
                return False
        return True

    async def stop(self, *, touch_hands: bool = True) -> None:
        """Dimagh band. `touch_hands=False`: screen ke haath (sab baat-cheeton ke
        shared) ka chalta kaam mat roko — us dimagh ke liye jis ne kabhi screen
        chalayi hi nahi (pehle se utha dimagh jo istemal na hua)."""
        self._closed = True
        self._ready.set()
        reader, self._reader = self._reader, None
        client, self._client = self._client, None
        if self._screen is not None:
            await self._screen.close(cancel_hands=touch_hands)
        if reader is not None:
            reader.cancel()
        self._record_talk()
        if client is not None:
            # Call band = process band. Koi chhupi baari nahi: yaadash jo likhni thi wo
            # dimagh baat ke dauran likh chuka (chat mein nazar aayi).
            await self._disconnect(client)

    # ── Saanjha waqt: baat-cheet ka hisaab (code, AI nahi) ──────────────────

    def _record_talk(self) -> None:
        """Asli baat-cheet (user bola) ka hisaab .talks.json mein — EK dafa. Agli baat-cheet
        ka "aakhri baat kab, kis ke saath" isi se. Koi yaadash nahi likhi jati."""
        life = self._config.life
        if life is None or self._talk_recorded or self._user_lines == 0:
            return
        self._talk_recorded = True
        life.record_talk(self._first_line_at, time.time())

    def _name_tool(self) -> Any:
        """`choose_my_name`: us ka khud chuna hua naam (ya user ka diya hua) — hamesha ke liye us ka."""
        life = self._config.life

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            name = life.set_name((args or {}).get("name"))
            if not name:
                return {
                    "content": [{"type": "text", "text": "That name did not save — use a real first name of 2 to 24 letters. Choose again."}],
                    "is_error": True,
                }
            await self._emit("life", {"character": life.character_id, "name": name})
            # Wo naam bol chuki hoti hai (pehle bolo, phir tool): baat dohrana awaaz mein ajeeb lagta hai.
            # Pehle kuch na bola ho (tool pehle chala) to jawab khula rehta hai.
            self._hush_rest = self._spoke
            return {"content": [{"type": "text", "text": (
                f"Saved: your name is {name} now, and it stays yours. They already heard what you said, so end your turn here — "
                "no need to repeat yourself or mention the saving."
            )}]}

        return tool(
            "choose_my_name",
            "Keep the name you chose for yourself — or the one they gave you — so it stays yours from now on. Call it quietly in the "
            "same reply where you say the name, without waiting for their reaction. It is not a task, so it needs no announcement.",
            {"type": "object", "properties": {"name": {"type": "string", "description": 'Only the name, e.g. "Zoya".'}}, "required": ["name"]},
        )(handler)


    @staticmethod
    async def _disconnect(client: ClaudeSDKClient) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception as error:  # noqa: BLE001
            logger.debug(f"Agent ka process band karte hue: {error}")

    # ── User ki taraf se ────────────────────────────────────────────────────

    async def say(self, text: str, *, note: str = "", mid_job: bool = False) -> None:
        """User ka mukammal jumla. Kaam chal raha ho to model usay agle qadam par dekhta hai.

        `note`: is jumle ne screen ka chalta kaam roka hai (bulane wala pehle
        `interrupt()` kar chuka) — dimagh ko bataya jata hai ke wo kahan khara
        hai (CUT_IN ya STOPPED_MID). Baat-cheet ka ehsaas chalu ho to us ka
        "moment" note bhi isi mein aata hai.

        `mid_job`: jumla kaam ke beech aaya. Dimagh use "kisi aur se" samjhe to
        kaam jari rehta hai (sirf wo jumla nazar-andaz).
        """
        text = str(text or "").strip()
        if not text:
            return
        await self._ready.wait()
        client = self._client
        if client is None or self._closed:
            return
        if not is_opening_cue(text):
            # User waqai bola (raasta khulne ka ishara nahi): ye asli baat-cheet hai.
            self._user_lines += 1
            self._first_line_at = self._first_line_at or time.time()
        query = text
        if self._unheard:
            # Model ki yaad mein wo line "kahi ja chuki" hai; user ne suni nahi.
            query = f"{query}\n\n[They were speaking over you, so they did not hear your last line.]"
            self._unheard = False
        if note:
            query = f"{query}\n\n{note}"
        await self._send_turn(client, query, line=text, mid_job=mid_job)

    async def retry(self, line: str) -> bool:
        """User ki aakhri baat (`line`) ka jawab us tak nahi pohncha — shor ne kaata, koi lafz nahi.

        Dimagh ke paas wo baat pehle se hai (notebook mein); sirf UNHEARD_RETRY
        jata hai. `line` ijazat aur screen ke taale ke liye — wohi jo `say` mein.
        False = abhi nahi (dimagh kaam mein hai ya band).
        """
        line = str(line or "").strip()
        client = self._client
        if not line or client is None or self._closed or self._working:
            return False
        self._unheard = False  # note khud batata hai ke jawab nahi pohncha
        await self._send_turn(client, UNHEARD_RETRY, line=line, mid_job=False)
        return True

    async def _send_turn(self, client: ClaudeSDKClient, query: str, *, line: str, mid_job: bool) -> None:
        """User ki taraf se ek turn dimagh ko (`say` / `retry`). `line` = user ke apne lafz."""
        if self._config.floor:
            # Ye baat Stonic se hai ya kisi aur se — dimagh ka faisla abhi aana
            # hai. Ijazat ("haan, kar do") aur screen ka taala tab, jab baat Stonic
            # se nikle: kamre mein kisi ka "haan" ya "chalo" Stonic ki ijazat nahi.
            self._awaiting = True
            self._may_continue = mid_job
            self._quiet_turn = False
            self._cue_turn = False
            self._held = line
        elif self._screen is not None:
            self._screen.user_spoke(line)
        self._spoke = False
        self._hush_rest = False
        self._turn_spoken = 0
        self._nudged_seq = -1
        # Naya jumla (kaam ke beech bhi): agla jawab usi ka hai, foran bola jata hai.
        self._job_steps = 0
        self._working = True
        try:
            await self._query(client, query)
        except Exception as error:  # noqa: BLE001
            self._working = False
            await self._emit("error", {"detail": f"agent_send_failed: {type(error).__name__}: {error}", "fatal": True})

    async def cue(self, text: str) -> bool:
        """Khamoshi mein Stonic ka apna moqa (floor.py `due`): user ne kuch nahi kaha.

        Dimagh khud tay karta hai — ek chhoti pukaar, ya `<quiet:rest>`. Is turn
        mein koi tool nahi chalta (CUE_TOOLS), aur na ijazat milti hai: user bola
        hi nahi. False = abhi moqa nahi (dimagh kaam mein hai ya band).

        Dimagh abhi uth raha ho to us ka intezar (CUE_READY_WAIT_SECS): Awareness
        baat-cheet khud khol kar foran bolti hai — judna ~0.1 s, dimagh ka uthna
        0.5–1.5 s. Intezar na ho to wo pehli baat chup chaap gir jati.
        """
        text = str(text or "").strip()
        if not text:
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CUE_READY_WAIT_SECS)
        except asyncio.TimeoutError:
            logger.warning("Agent: moqa aaya magar dimagh waqt par tayyar na hua — chhor diya")
            return False
        client = self._client
        if client is None or self._closed or self._working:
            return False
        self._awaiting = True
        self._may_continue = False
        self._quiet_turn = False
        self._cue_turn = True
        self._held = None
        # "Pehle bolo" ka pehra yahan nahi: is turn mein koi tool chalta hi nahi.
        self._spoke = True
        self._hush_rest = False
        self._turn_spoken = 0
        self._nudged_seq = -1
        self._job_steps = 0
        self._working = True
        try:
            await self._query(client, text)
        except Exception as error:  # noqa: BLE001
            self._working = False
            await self._emit("error", {"detail": f"agent_send_failed: {type(error).__name__}: {error}", "fatal": True})
            return False
        return True

    def user_speaking(self, speaking: bool) -> None:
        """User bol raha ho to model ki nayi line us ke upar nahi boli jati — aur screen ke haath thehar jate hain."""
        self._user_speaking = speaking
        if self._screen is not None:
            self._screen.user_speaking(speaking)

    async def barge_in(self) -> None:
        """User bol para. Sirf baat ho rahi thi to jawab wahin kaat do; kaam chal raha ho to chalne do."""
        if self._reply_open:
            self._cut = True
            self._unheard = True
            await self._close_reply()
        if self._working and not self._used_tools:
            await self.interrupt()

    async def interrupt(self) -> None:
        """Jo ho raha hai roko (Stop button, "ruk jao", ya baat ke beech user ka bol parna)."""
        client = self._client
        if client is None or not self._working:
            return
        if self._screen is not None:
            self._screen.stop()  # chalta click / tehreer wahin ruke, jawab ka intezar nahi
        self._drop_until = time.monotonic() + DROP_FAILSAFE_SECS
        await self._close_reply()
        try:
            await client.interrupt()
        except Exception as error:  # noqa: BLE001
            logger.debug(f"Agent interrupt: {error}")

    # ── Pehra: har tool call yahan se guzarti hai ───────────────────────────

    async def _tool_running(self, tool_id: str | None, name: str, args: dict[str, Any]) -> None:
        """Pehra guzar gaya: ye tool ab chalega. Screen ko batao."""
        self._used_tools = True
        self._current_tool = tool_id
        self._last_tool = name
        label = describe_tool(name, args, self._config.memory_dir)
        key = tool_id or f"{name}-{len(self._tools)}"
        self._tools[key] = label
        self._tool_started[key] = time.monotonic()
        if not self._talk_tool(name, args):
            self._job_steps += 1
        self._hush_after(name, args)
        await self._emit("tool_start", {"id": key, "tool": name, "label": label, "args": tool_args(args)})

    def _talk_tool(self, name: str, args: dict[str, Any] | None) -> bool:
        """Baat ka hissa, kaam ka qadam nahi: yaadash aur kirdar ke tool ("pehle jawab, phir note")."""
        if name.startswith(LIFE_PREFIX):
            return True
        if name.startswith(MCP_PREFIX) and name[len(MCP_PREFIX):] in _APP_MEMORY_TOOLS:
            return True
        if name in _MEMORY_FILE_TOOLS and args:
            return in_memory_dir(args.get("file_path") or args.get("path") or "", self._config.memory_dir)
        return False

    def _hush_after(self, name: str, args: dict[str, Any]) -> None:
        """Poora jawab de kar yaadash mein note: is ke baad ka text aksar wohi jawab dobara hota hai.

        Sirf tab jab note likhne se PEHLE asli jawab bola ja chuka ho (`MEMORY_HUSH_CHARS`) —
        "Noted, Sir, abhi likhta hoon" jaisi chhoti line ke baad asal jawab tool ke baad aata
        hai, wo bola jata hai. Yaadash ke ilawa koi aur tool chale to baad ka text phir khula.
        """
        target = args.get("file_path") or args.get("path") or ""
        if name in _FILE_VERBS or name in ("Glob", "Grep"):
            if in_memory_dir(target, self._config.memory_dir):
                if name in ("Write", "Edit") and self._turn_spoken >= MEMORY_HUSH_CHARS:
                    self._hush_rest = True
                return
        if not name.startswith(LIFE_PREFIX):
            self._hush_rest = False

    async def _decide(self, name: str, args: dict[str, Any], tool_id: str | None = None) -> tuple[bool, str]:
        allowed, message = await self._review(name, args)
        if allowed:
            await self._tool_running(tool_id, name, args)
        return allowed, message

    async def _review(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        if self._quiet_turn:
            logger.info(f"Agent: {name} rad — dimagh ne baat ko apni nahi mana (chup)")
            return False, QUIET_TOOLS
        if self._cue_turn:
            logger.info(f"Agent: {name} rad — khamoshi ka moqa sirf bolne ke liye hai")
            return False, CUE_TOOLS
        verdict = safety.review_tool(name, args, memory_dir=self._config.memory_dir)
        if verdict.action == "allow":
            # Chalne wala tool, magar model ne abhi tak kuch bola nahi: pehle bole.
            # Sirf usi paigham ke tools lautaye jate hain (ek saath kai hon to sab);
            # agle paigham par tool har haal mein chalta hai — chakkar nahi banta.
            # Naam ka tool kaam nahi, chupke ki likhai hai: us ke liye "pehle bolo" nahi.
            # Classic (`voice=False`): text awaaz nahi banta — ye pehra wahan nahi.
            if (
                self._config.voice and not self._spoke and not name.startswith(LIFE_PREFIX)
                and self._nudged_seq in (-1, self._message_seq)
            ):
                self._nudged_seq = self._message_seq
                logger.info(f"Agent: {name} lautaya — pehle bolna hai")
                return False, SPEAK_FIRST
            return True, ""
        if verdict.action == "redo":
            # User ko beech mein nahi laana: sirf shakl ghalat hai (safety.review_memory_delete).
            logger.info(f"Agent: {name} lautaya — shakl badalni hai")
            return False, f"NOT RUN YET: {verdict.reason}"
        # "Pehle poochho" nahi (3 Nov 2026): saaf hukm foran chalta hai. Sirf "kabhi nahi" ki fehrist.
        logger.warning(f"Agent: {name} rad — {verdict.reason}")
        return False, (
            f"REFUSED: {verdict.reason}. Stonic never does this from a voice request. "
            "Tell them plainly that you will not do it and why; offer a safer way if there is one."
        )

    async def _pre_tool_use(self, input_data: Any, tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        allowed, message = await self._decide(
            str(input_data.get("tool_name") or ""), dict(input_data.get("tool_input") or {}),
            tool_use_id or str(input_data.get("tool_use_id") or "") or None,
        )
        output: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": "allow" if allowed else "deny"}
        if not allowed:
            output["permissionDecisionReason"] = message
            return {"hookSpecificOutput": output}
        name, args = str(input_data.get("tool_name") or ""), dict(input_data.get("tool_input") or {})
        call_id = tool_use_id or str(input_data.get("tool_use_id") or "")
        if name in safety.SHELL_TOOLS:
            if not args.get("run_in_background") and safety.runs_long(str(args.get("command") or "")):
                # Dimagh ne lamba command saamne chalaya: us ke natije tak wo na
                # sunta, na bolta. SDK ka apna raasta: peeche bhej do (safety.py).
                logger.info("Agent: lamba command — peeche bheja ja raha hai")
                output["updatedInput"] = {**args, "run_in_background": True}
                args = output["updatedInput"]
            if args.get("run_in_background") and call_id:
                self._background_calls.add(call_id)
        return {"hookSpecificOutput": output}

    async def _can_use_tool(self, name: str, args: dict[str, Any], context: Any) -> Any:
        # Yahan sirf wo call aati hai jis par upar wala hook faisla na de saka
        # (hook gir gaya ya der kar gaya). Wohi pehra dobara — khula kabhi nahi chhorna.
        allowed, message = await self._decide(name, dict(args or {}), getattr(context, "tool_use_id", None))
        return PermissionResultAllow() if allowed else PermissionResultDeny(message=message)

    # ── Stonic ke apne tools (app ke andar chalte hain) ─────────────────────

    def _bridge_tool(self, spec: AppTool) -> Any:
        call_app = self._call_app
        assert call_app is not None

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                result = await call_app(spec.name, dict(args or {}))
            except Exception as error:  # noqa: BLE001
                logger.warning(f"Agent: app ka tool {spec.name} gir gaya — {error}")
                result = {"ok": False, "text": f"{spec.name} did not work: {error}"}
            content: list[dict[str, Any]] = []
            image = result.get("image")
            if isinstance(image, dict) and image.get("data"):
                content.append({"type": "image", "data": str(image["data"]), "mimeType": str(image.get("mimeType") or "image/jpeg")})
            content.append({"type": "text", "text": str(result.get("text") or ("Done." if result.get("ok") else "It did not work."))})
            return {"content": content, "is_error": not result.get("ok", False)}

        return tool(spec.name, spec.description, spec.schema)(handler)

    # ── SDK se aane wale paigham ────────────────────────────────────────────

    async def _emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        try:
            await self._listener(event, data or {})
        except Exception as error:  # noqa: BLE001 — sunne wale ki ghalti dimagh ko na roke
            logger.warning(f"Agent ka paigham '{event}' pohncha nahi: {error}")

    async def _close_reply(self) -> None:
        if self._reply_open:
            self._reply_open = False
            await self._emit("reply_end")

    def _dropping(self) -> bool:
        if not self._drop_until:
            return False
        if time.monotonic() > self._drop_until:
            self._drop_until = 0.0
            return False
        return True

    async def _read(self, client: ClaudeSDKClient) -> None:
        try:
            async for message in client.receive_messages():
                if client is not self._client:
                    return  # notebook badal chuki (_start_fresh): is process ki baat ab nahi suni jati
                await self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            if client is not self._client:
                return  # purani notebook ka process (nayi se badla ja chuka) — us ki ghalti ab kisi ki nahi
            if self._resume_lost(str(error)) and await self._start_fresh(str(error)):
                return
            if not self._closed:
                logger.error(f"Agent ka process toot gaya: {type(error).__name__}: {error}")
                self._working = False
                await self._close_reply()
                await self._emit("error", {"detail": f"agent_exited: {type(error).__name__}: {error}", "fatal": True})

    async def _handle(self, message: Any) -> None:
        if isinstance(message, ResultMessage):
            await self._on_result(message)
            return
        if isinstance(message, TaskStartedMessage):
            await self._on_job_started(message)
            return
        if isinstance(message, (TaskNotificationMessage, TaskUpdatedMessage)):
            await self._on_job_update(message)
            return
        if isinstance(message, SystemMessage):
            await self._on_system(message)
            return
        if getattr(message, "parent_tool_use_id", None):
            return  # kisi andar ke kaam ka paigham — bola nahi jata
        if isinstance(message, StreamEvent):
            await self._on_stream(message.event)
        elif isinstance(message, AssistantMessage):
            await self._on_assistant(message)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock) and self._tools.pop(block.tool_use_id, None) is not None:
                    started = self._tool_started.pop(block.tool_use_id, None)
                    await self._emit("tool_end", {
                        "id": block.tool_use_id,
                        "ok": not block.is_error,
                        "output": tool_output(block.content),
                        "ms": round((time.monotonic() - started) * 1000) if started is not None else None,
                    })

    # ── Peeche chalte command ───────────────────────────────────────────────
    #
    # SDK lambe saamne wale command ko bhi "task" ginta hai; yahan sirf wo gine
    # jate hain jo waqai peeche gaye (`_background_calls`).

    async def _on_job_started(self, message: TaskStartedMessage) -> None:
        call_id = message.tool_use_id or ""
        if call_id not in self._background_calls:
            return
        self._background_calls.discard(call_id)
        self._jobs[message.task_id] = _clip(message.description or "A command", 80)
        logger.info(f"Agent: peeche kaam shuru ({len(self._jobs)} chal rahe hain)")
        await self._emit("job_start", {"id": message.task_id, "label": self._jobs[message.task_id], "running": len(self._jobs)})

    async def _on_job_update(self, message: TaskNotificationMessage | TaskUpdatedMessage) -> None:
        if isinstance(message, TaskNotificationMessage):
            status = str(message.status or "")
        else:
            status = str((message.patch or {}).get("status") or message.status or "")
        if status not in _JOB_OVER or message.task_id not in self._jobs:
            return
        label = self._jobs.pop(message.task_id)
        # Khatam (ya nakaam) hone par session khud ek jawab shuru karta hai:
        # dimagh user ko bataata hai. Roke gaye kaam par nahi.
        self._notice_due = status in ("completed", "failed")
        logger.info(f"Agent: peeche wala kaam {status} ({len(self._jobs)} baqi)")
        await self._emit("job_end", {"id": message.task_id, "label": label, "status": status, "running": len(self._jobs)})

    # ── Baat-cheet ki lambai (app ka ring) aur compact ──────────────────────

    async def _learn_compact_point(self, client: ClaudeSDKClient) -> None:
        """CLI se us ki apni compact ki had (model + mahaul ke hisaab se) — ek dafa, peeche.

        `get_context_usage` lambai gin-ne ke liye `count_tokens` bhi bulata hai; Stonic
        Cloud ka gateway us par 404 deta hai — had phir bhi milti hai (naapa, ~0.1 s).
        """
        try:
            usage = await asyncio.wait_for(client.get_context_usage(), timeout=COMPACT_POINT_WAIT_SECS)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — na mile to naapa hua andaza hi sahi
            logger.debug(f"Agent: compact ki had CLI se na mili ({type(error).__name__}: {error}) — andaza {self._ctx_limit}")
            return
        try:
            limit = int(usage.get("autoCompactThreshold") or 0)
            window = int(usage.get("rawMaxTokens") or 0)
        except (TypeError, ValueError, AttributeError):
            return
        if limit <= 0 or window <= 0 or client is not self._client:
            return
        logger.info(f"Agent: compact ki had {limit} tokens (window {window}, CLI ki apni)")
        if (window, limit) == (self._ctx_window, self._ctx_limit):
            return
        self._ctx_window, self._ctx_limit = window, limit
        if self._ctx_used or self._summarized:
            await self._tell_context()

    async def _tell_context(self, *, compacted: bool = False) -> None:
        """App ko: chat kitni lambi hai aur compact kahan hota hai (CLI ki apni had)."""
        payload: dict[str, Any] = {
            "used": self._ctx_used,
            "limit": self._ctx_limit,
            "window": self._ctx_window,
            "compacting": self._compacting,
            "summarized": self._summarized,
        }
        if compacted:
            payload["compacted"] = True
        await self._emit("context", payload)

    def _note_usage(self, kind: str, event: dict[str, Any]) -> None:
        """Har model call ki token ginti (stream ke shuru aur aakhir mein) — muft, koi izafi call nahi."""
        if kind == "message_start":
            self._ctx_call = context_tokens((event.get("message") or {}).get("usage"), with_output=False)
            if self._ctx_call:
                self._ctx_used = self._ctx_call
            return
        try:
            out = max(0, int((event.get("usage") or {}).get("output_tokens") or 0))
        except (TypeError, ValueError):
            out = 0
        if self._ctx_call:
            self._ctx_used = self._ctx_call + out

    async def _on_system(self, message: SystemMessage) -> None:
        """CLI ke haal ke paigham: sirf compact wale kaam ke hain (baqi chup chaap chhor diye jate hain)."""
        data = message.data or {}
        if message.subtype == "status":
            compacting = data.get("status") == "compacting"
            if data.get("compact_error"):
                logger.warning(f"Agent: compact nakaam — {str(data.get('compact_error'))[:200]}")
            if compacting == self._compacting:
                return
            self._compacting = compacting
            if compacting:
                logger.info(f"Agent: baat-cheet lambi ho gayi (~{self._ctx_used} tokens) — purani baat ki summary ban rahi hai")
            await self._tell_context()
        elif message.subtype == "compact_boundary":
            meta = data.get("compact_metadata") or {}
            logger.info(f"Agent: baat-cheet compact hui ({meta.get('trigger', '?')}) — pehle {meta.get('pre_tokens', '?')} tokens")
            self._compacting = False
            self._summarized = True
            self._ctx_call = self._ctx_used = 0  # nayi lambai agle jawab ke saath aati hai
            await self._tell_context(compacted=True)

    async def _on_stream(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in ("message_start", "message_delta"):
            self._note_usage(kind, event)
        if kind == "message_start":
            if not self._working and self._notice_due:
                # Ye jawab user ke jumle ka nahi: peeche wale kaam ki khabar hai,
                # jo session ne khud shuru ki. Koi intezar mein nahi baitha, is
                # liye "pehle bolo" ka pehra yahan nahi lagta — aur khabar foran boli jati hai.
                self._notice_due = False
                self._spoke = True
                self._job_steps = 0
            self._working = True
            self._cut = False
            self._message_seq += 1
            self._message_tools = []
            self._message_done = False
            # Baat-cheet ka ehsaas: har paigham ka shuru dekha jata hai (`<quiet…` = chup).
            # Is turn ka faisla pehle hi "chup" ho chuka ho to baqi paigham bhi chup.
            # "Stay quiet" band ho tab bhi shuru dekha jata hai: purani chat (jab ye chalu
            # tha) ki notebook mein `<for-me>` / `<quiet:…>` ki aadat hoti hai — tag kabhi
            # awaaz tak na jaye. Bina tag ka jawab pehle harf par hi khul jata hai (koi der nahi).
            self._gate = "quiet" if self._quiet_turn else "pending"
            self._gate_buf = ""
            self._tags.flush()
            # Kaam chal raha hai (QUIET_AFTER_STEPS): is paigham ka text pehle roka jata hai.
            self._held_words = [] if self._config.voice and self._job_steps >= QUIET_AFTER_STEPS else None
            self._message_thought = False
            self._stop_reason = ""
            self._pending_block = None
            return
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._message_tools.append((str(block.get("id") or ""), str(block.get("name") or "")))
        elif kind == "message_delta":
            self._stop_reason = str((event.get("delta") or {}).get("stop_reason") or "")
        elif kind == "message_stop":
            self._message_done = True
        if self._dropping():
            return
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                pending = self._pending_block
                if pending is not None and pending[0] == event.get("index"):
                    pending[2].append(str(delta.get("partial_json") or ""))
                return
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                # Model ki soch: boli nahi jati, sirf app ki chat mein dikhti hai.
                self._message_thought = True
                await self._emit("thinking", {"text": delta["thinking"]})
                return
            if delta.get("type") != "text_delta" or not delta.get("text"):
                return
            text = delta["text"]
            if self._gate == "quiet":
                return  # dimagh ne "chup" kaha: is paigham ka koi lafz bola nahi jata
            if self._gate == "pending":
                # Jawab ka shuru dimagh ka faisla hai (`<for-me>` / `<quiet:…>`) — wo
                # tag band hone tak roka jata hai, phir hata diya jata hai. Bina tag
                # ke jawab ka pehla harf hi bata deta hai — koi der nahi.
                self._gate_buf += text
                verdict, why, text = open_reply(self._gate_buf)
                if verdict == "wait":
                    return
                self._gate_buf = ""
                if verdict == "quiet":
                    await self._floor_decided("quiet", why)
                    return
                await self._floor_decided("speak")
            await self._speak(text)
        elif kind == "content_block_start":
            # Tool ka block shuru = bolne wali line poori ho gayi: awaaz ko abhi de do,
            # taake wo tool ke chalne ke dauran sunai de.
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                await self._settle_gate(at_end=False)
                self._tags.flush()
                name = str(block.get("name") or "")
                if self._held_words and name in _MEMORY_FILE_TOOLS and self._config.memory_dir:
                    # Lamba kaam, aur ye file tool yaadash ka ho sakta hai: faisla raasta aane par
                    # (content_block_stop) — yaadash ka note jawab ke BAAD aata hai, us se pehle ka text jawab hai.
                    self._pending_block = (event.get("index"), name, [])
                else:
                    # Lamba kaam: tool se pehle ka roka hua text beech ki commentary hai — bola nahi jata.
                    await self._settle_held(spoken=self._talk_tool(name, None))
                await self._close_reply()
        elif kind == "content_block_stop":
            pending = self._pending_block
            if pending is not None and pending[0] == event.get("index"):
                self._pending_block = None
                try:
                    args = json.loads("".join(pending[2]) or "{}")
                except ValueError:
                    args = {}
                await self._settle_held(spoken=self._talk_tool(pending[1], args if isinstance(args, dict) else {}))
                await self._close_reply()
        elif kind == "message_stop":
            await self._settle_gate(at_end=True)
            self._tags.flush()  # aakhir ka adhoora `</for-me` kabhi nahi bola jata
            # Lamba kaam: paigham bina naye tool ke khatam = nateeja ya sawal, ab bolo.
            await self._settle_held(spoken=self._stop_reason != "tool_use")
            await self._close_reply()

    async def _speak(self, text: str) -> None:
        text = self._tags.feed(text)
        if not text:
            return  # sirf `<for-me>` aaya, lafz abhi nahi — "pehle bolo" ka pehra abhi bhi khula
        if self._hush_rest:
            return  # naam bol chuki / jawab de kar note likh chuki — baqi (dohraya hua) hissa nahi
        self._spoke = True
        if self._held_words is not None:
            self._held_words.append(text)  # lamba kaam: faisla tool / paigham ke khatam hone par
            return
        await self._voice(text)

    async def _settle_held(self, *, spoken: bool) -> None:
        """Lamba kaam: is paigham ka roka hua text. `spoken` = nateeja / sawal (bolo); warna beech ki commentary."""
        held = self._held_words
        if not held:
            return
        self._held_words = []
        text = "".join(held)
        if spoken:
            await self._voice(text)
        elif text.strip():
            logger.info(f"Agent: kaam ke beech ki line chup — {_clip(text)}")
            # Chat mein dikhti hai (soch ki tarah), awaaz tak nahi jati.
            await self._emit("thinking", {"text": ("\n\n" if self._message_thought else "") + text.strip()})
            self._message_thought = True

    async def _voice(self, text: str) -> None:
        """Text awaaz ko (aur app ki chat mein jawab ki tarah)."""
        self._turn_spoken += len(text)
        if self._cut or self._user_speaking:
            self._unheard = True
            return
        if not self._reply_open:
            self._reply_open = True
            await self._emit("reply_start")
        await self._emit("text", {"text": text})

    # ── Baat-cheet ka ehsaas: dimagh ka faisla (floor.py) ───────────────────

    async def _settle_gate(self, *, at_end: bool) -> None:
        """Paigham ka text khatam (tool shuru hua ya paigham band) aur faisla abhi baqi."""
        if self._gate != "pending":
            return
        buffered, self._gate_buf = self._gate_buf, ""
        # Na koi lafz na koi kaam = chup; kata hua `<quiet` = chup; adhoora tag kabhi bola nahi jata.
        verdict, why, text = settle_reply(buffered, at_end=at_end)
        await self._floor_decided(verdict, why)
        if verdict == "speak":
            await self._speak(text)

    async def _floor_decided(self, kind: str, why: str = "") -> None:
        """`kind` "speak" ya "quiet". Intezar wale jumle / moqe ka faisla app tak (agent_service → floor.py)."""
        self._gate = "quiet" if kind == "quiet" else "open"
        awaiting, self._awaiting = self._awaiting, False
        if not awaiting:
            if kind == "quiet":
                self._spoke = True
            return
        held, self._held = self._held, None
        cue = self._cue_turn
        if kind == "quiet":
            # Soch samajh kar chup — "pehle bolo" ka pehra is par nahi lagta.
            self._spoke = True
            if not self._may_continue:
                self._quiet_turn = True
            logger.info(f"Agent: chup — {'moqa chhor diya' if cue else 'ye baat Stonic se nahi thi'} ({why})")
        elif held is not None and self._screen is not None:
            # Baat Stonic se thi: ab ye user ki apni baat hai — screen ka taala (user ke
            # bolne tak) wahin se khulta hai.
            self._screen.user_spoke(held)
        await self._emit("floor", {"decision": kind, "why": why, "cue": cue})

    async def _on_assistant(self, message: AssistantMessage) -> None:
        if message.error:
            text = " ".join(getattr(block, "text", "") for block in message.content if getattr(block, "text", ""))
            await self._close_reply()
            self._error_told = True
            await self._emit("error", {"detail": f"{_ERROR_HINTS.get(message.error, message.error)} — {_clip(text, 300)}", "kind": message.error})
            return

    async def _on_result(self, message: ResultMessage) -> None:
        if message.is_error:
            detail = "; ".join(message.errors or []) or str(message.result or "")
            if self._resume_lost(detail) and await self._start_fresh(detail):
                return
        self._settled = True
        sid = str(getattr(message, "session_id", "") or "")
        if sid and self._config.session_id and sid != self._config.session_id:
            # SDK ne notebook ka naam badla (fork): app ko naya naam.
            self._config.session_id = sid
            await self._tell_session()
        aborted = str(getattr(message, "terminal_reason", "") or "").startswith("aborted") or bool(self._drop_until)
        self._drop_until = 0.0
        self._working = False
        self._used_tools = False
        self._tools.clear()
        self._tool_started.clear()
        self._current_tool = None
        self._last_tool = ""
        if self._screen is not None:
            self._screen.turn_ended(aborted)
        await self._close_reply()
        if self._cue_turn and self._awaiting:
            # Moqe ka turn bina faisle ke khatam (roka gaya / ghalti): moqa band, taake agla aa sake.
            await self._floor_decided("quiet", "other")
        self._quiet_turn = False
        self._cue_turn = False
        self._hush_rest = False
        self._job_steps = 0
        self._held_words = None
        self._pending_block = None
        self._gate = "open"
        self._gate_buf = ""
        usage = message.usage or {}
        logger.info(
            f"Agent: turn khatam — {message.subtype}, {message.num_turns} qadam, {message.duration_ms} ms, "
            f"in {usage.get('input_tokens')} + cache {usage.get('cache_read_input_tokens')} / out {usage.get('output_tokens')}, "
            f"${message.total_cost_usd}"
        )
        told, self._error_told = self._error_told, False
        if message.is_error and not aborted and not told:
            detail = "; ".join(message.errors or []) or str(message.result or message.subtype)
            status = getattr(message, "api_error_status", None)
            await self._emit("error", {"detail": f"{detail} (status {status})" if status else detail, "subtype": message.subtype})
        if self._compacting:
            self._compacting = False  # turn khatam: compact ka "chal raha hai" atka na rahe (ghalti / roka gaya)
            await self._tell_context()
        elif self._ctx_used:
            await self._tell_context()
        await self._emit("turn_end", {"aborted": aborted, "steps": message.num_turns, "cost_usd": message.total_cost_usd})
