"""Hifazat ka pehra: Stonic jo kaam PC par khud karta hai, us se pehle ek nazar.

Beta ka dimagh (agent.py) PowerShell chalata hai aur files likhta hai. Har tool
call chalne se PEHLE yahan se guzarti hai aur teen mein se ek faisla hota hai:

    allow     chalao — delete, install, shutdown, settings, bhejna, sab. Hukm
              saaf ho to dimagh foran karta hai, koi "pehle poochho" nahi
              (3 Nov 2026, Usman ka faisla: Claude jaisa — sirf baat ghair-wazeh
              ho to dimagh khud poochhta hai; ye prompt ka kaam hai, pehre ka nahi).
    redo      abhi nahi, user ko beech mein laye baghair: model ko sahi shakl
              batao (yaadash ka mitana jo Claude Code ka apna pehra rok deta)
    deny      kabhi nahi, user haan bhi kahe — chhoti fehrist: disk format /
              boot, backups mitana, poora C: / Windows / Program Files / user ka
              folder mitana, passwords / keys / .env parhna, Windows ki files
              likhna, aur internet se utha kar (ya chhupa kar) code chalana.
              Ye ghalat sune lafz aur web page / file ke andar chhupe hukm
              (prompt injection) ke khilaf aakhri deewar hai.

Faisle ke saath jane wala `reason` angrezi mein hai: wo model ko jata hai, jo
usay user ki zubaan mein bolta hai.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

IS_WINDOWS = os.name == "nt"

SHELL_TOOLS = frozenset({"PowerShell", "Bash"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READ_TOOLS = frozenset({"Read", "Glob", "Grep"})


@dataclass(frozen=True)
class Verdict:
    action: str  # allow | redo | deny
    reason: str = ""


ALLOW = Verdict("allow")


def _deny(why: str) -> Verdict:
    return Verdict("deny", why)


# ── PowerShell ───────────────────────────────────────────────────────────────

# Hukm jumle ke shuru mein hota hai, ya pipe / semicolon / bracket ke baad.
# Is ke baghair "model" ke andar ka "del" bhi pakra jata.
_AT_COMMAND = r"(?:^|[;|&({\n]\s*|\b(?:cmd(?:\.exe)?\s*/c|powershell(?:\.exe)?|pwsh(?:\.exe)?)\s+(?:-\w+\s+)*[\"']?\s*)"

_DELETE = re.compile(
    _AT_COMMAND + r"(remove-item|rm|ri|del|erase|rd|rmdir|clear-content|clear-recyclebin|remove-itemproperty)(?![\w-])"
    r"|\[system\.io\.(?:file|directory)\]::delete|\.delete\(\s*\)|\bfsutil\s+file\s+setzerodata\b",
    re.IGNORECASE,
)

# Disk aur boot ko chherne wali cheezein: awaaz ke ek jumle par kabhi nahi.
_NEVER = [
    (re.compile(r"\b(format-volume|clear-disk|remove-partition|initialize-disk|diskpart|bcdedit|bootrec)\b", re.I),
     "it would wipe or repartition a disk or change how Windows boots"),
    (re.compile(_AT_COMMAND + r"format(?:\.com)?\s+[a-z]:", re.I), "it would format a drive"),
    (re.compile(r"\bvssadmin\b.*\bdelete\b|\bcipher\b.*?/w\b|\bwbadmin\b.*\bdelete\b", re.I),
     "it would destroy backups or wipe free space"),
    (re.compile(r"\b(mimikatz|lsass|procdump)\b|\breg(?:\.exe)?\s+save\s+hklm\\(sam|security|system)\b", re.I),
     "it reads Windows login secrets"),
    (re.compile(r"login data|\\cookies\b|web data|\\\.ssh\\id_|wallet\.dat|key4\.db|logins\.json", re.I),
     "it reads saved passwords, cookies or private keys"),
    # Internet se utha kar (ya base64 mein chhupa kar) code chalana: `irm … | iex` ka
    # khatra wohi hai jo kisi anjaan file ko chalane ka — aur web page ka chhupa hukm
    # sab se pehle yehi maangta hai. Install ke liye winget hai (wo chalta hai).
    (re.compile(
        r"\b(iex|invoke-expression)\b|-e(?:nc|ncodedcommand)?\s+[a-z0-9+/=]{20,}|\bdownloadstring\b|\bdownloadfile\b[^\n;]*\.(ps1|bat|cmd|exe)\b"
        r"|\b(irm|iwr|invoke-restmethod|invoke-webrequest|curl(?:\.exe)?|wget)\b[^\n;]*\|\s*&?\s*(powershell|pwsh|cmd|bash|sh)(?:\.exe)?\b",
        re.I,
     ), "it runs code downloaded from the internet or hidden in an encoded command"),
]

_ENV_ROOTS = {
    "systemdrive": lambda: os.environ.get("SystemDrive", "C:"),
    "systemroot": lambda: os.environ.get("SystemRoot", r"C:\Windows"),
    "windir": lambda: os.environ.get("windir", r"C:\Windows"),
    "programfiles": lambda: os.environ.get("ProgramFiles", r"C:\Program Files"),
    "programfiles(x86)": lambda: os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "programdata": lambda: os.environ.get("ProgramData", r"C:\ProgramData"),
    "userprofile": lambda: os.path.expanduser("~"),
    "appdata": lambda: os.environ.get("APPDATA", ""),
    "localappdata": lambda: os.environ.get("LOCALAPPDATA", ""),
}


def _slashed(value: str) -> str:
    """Mac/Linux: `$HOME` ki jagah aane wala raasta bhi ulti slash mein (warna `/users/x` != `\\users\\x`)."""
    return value if IS_WINDOWS else value.replace("/", "\\")


def _expand_env(text: str) -> str:
    """`$env:APPDATA` jaise hisse asal raaste mein (text pehle se chhote huroof aur ulti slash mein ho)."""
    return re.sub(r"\$env:([a-z()0-9_]+)|\$\{env:([a-z()0-9_]+)\}", lambda m: _slashed((_ENV_ROOTS.get(m.group(1) or m.group(2), lambda: m.group(0))() or m.group(0)).lower()), text)


def _norm(path: str) -> str:
    """Raasta muqable ke liye: chhote huroof, ulti slash, aakhir ki slash aur `\\*` ke baghair."""
    text = _expand_env(str(path or "").strip().strip("\"'").replace("/", "\\").lower())
    text = re.sub(r"^(\$home|~)(?=\\|$)", lambda _m: _slashed(os.path.expanduser("~").lower()), text)
    text = re.sub(r"(\\\*)+$", "", text)
    return text.rstrip("\\")


def _roots() -> set[str]:
    """Wo folders jo poore ke poore kabhi nahi mit-te: drive ki jar, Windows, Program Files, Users, user ka apna ghar."""
    home = os.path.expanduser("~")
    drive = os.environ.get("SystemDrive", "C:")
    raw = [
        drive, os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramData", r"C:\ProgramData"),
        os.path.dirname(home), home, os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", ""),
        *(os.path.join(home, name) for name in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music", "OneDrive")),
    ]
    return {_norm(item) for item in raw if item}


_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_DRIVE_ROOT = re.compile(r"[a-z]:")


def _wipes_a_root(command: str) -> bool:
    """Delete ka nishana koi poora bunyadi folder hai? ("Remove-Item C:\\Users\\me\\Desktop -Recurse")"""
    roots = _roots()
    for statement in re.split(r"[;\n|]", command):
        if not _DELETE.search(statement):
            continue
        for token in _TOKEN.findall(statement):
            target = _norm(token)
            if target in roots or _DRIVE_ROOT.fullmatch(target):
                return True
    return False


# ── Apni yaadash: mitana bhi bina poochhe ────────────────────────────────────
#
# Claude Code apni memory ke notes khud likhta, badalta aur mitata hai — kisi se
# poochhe baghair. Yaadash ke folder mein likhai (review_file) pehle se bina
# poochhe thi; yahan mitana bhi. Shart: command SIRF mitane ka ho, har nishana
# poore raaste se likha ho (ya `$src = '…'` jaise saade naam se) aur folder ke
# andar ho. Poori yaadash ka folder, ya chhupi cheezein (muntaqli ka backup /
# nishan) KABHI nahi (backup mitana "kabhi nahi" fehrist mein hai).
#
# Claude Code (CLI) ka APNA pehra lambi script (mitana + fehrist badalna + baqi
# kaam ek saath, loop, Join-Path) mein raasta nahi parh pata aur "system path"
# keh kar rok deta hai — bypass mode mein bhi (lab, 30 Oct 2026, CLI 2.1.294,
# Khushbo ke asal commands). Chhota, alag delete command hamesha chalta hai. Is
# liye aisi shakl par yahan "redo": model ko foran sahi shakl batao — na user se
# ijazat, na CLI ki rukawat.

_DELETE_CMDLETS = frozenset({"remove-item", "rm", "ri", "del", "erase", "rd", "rmdir"})
_DELETE_FLAGS = frozenset({"-literalpath", "-lp", "-path", "-force", "-recurse", "-r", "-confirm:$false"})
_VALUE_FLAGS = frozenset({"-erroraction", "-ea"})
_ASSIGN = re.compile(r"^\$(\w+)\s*=\s*(\"[^\"]*\"|'[^']*')$")
_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+(?::\w+)?)")
_ARG = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s,]+")
_WILDCARD = re.compile(r"[*?\[]")


def _statements(text: str) -> list[str]:
    """Command ke jumle: nayi line ya `;` par (quote ke andar wale `;` nahi). Khali / comment chhoot jate hain."""
    out: list[str] = []
    current: list[str] = []
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in ";\n\r":
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    out.append("".join(current))
    return [s.strip() for s in out if s.strip() and not s.strip().startswith("#")]


def _resolve(token: str, values: Mapping[str, str]) -> str | None:
    """Ek argument ka asal matn. `'…'` jaisa ka taisa; `"…"` / khula: jaane pehchane `$naam` bhar kar. Na bhar sake = None."""
    if token.startswith("'"):
        return token[1:-1]
    body = token[1:-1] if token.startswith('"') else token
    missing = False

    def fill(match: re.Match[str]) -> str:
        nonlocal missing
        name = (match.group(1) or match.group(2)).lower()
        if name in values:
            return values[name]
        missing = True
        return match.group(0)

    filled = _VAR.sub(fill, body)
    return None if missing else filled


def _delete_targets(args: str, values: Mapping[str, str]) -> list[str] | None:
    """`Remove-Item` ke nishane. None = koi nishana ya flag aisa jo parha nahi ja sakta."""
    tokens = _ARG.findall(args)
    targets: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        low = token.lower()
        if low in _DELETE_FLAGS:
            continue
        if low in _VALUE_FLAGS:
            skip = True
            continue
        if low.startswith("-"):
            return None
        # `(Join-Path …)` / `(Get-Item …)`: chalne par hi pata chalta hai kya mitega.
        if not token.startswith(("'", '"')) and ("(" in token or ")" in token):
            return None
        value = _resolve(token, values)
        if value is None or _WILDCARD.search(value):
            return None
        targets.append(value)
    return targets or None


# Poori yaadash ya us ka backup / muntaqli ka nishan: kabhi nahi.
MEMORY_BACKUP = "it would delete Stonic's whole memory folder or its backup"


def _memory_redo(memory_dir: str) -> Verdict:
    example = ",".join(f"'{os.path.join(memory_dir, name)}'" for name in ("old-note.md", "other-note.md"))
    return Verdict("redo", (
        "this delete is in Stonic's memory folder, but in a shape that cannot be checked — and Claude Code itself "
        "blocks long delete scripts as a \"system path\". Run the delete again as its own short command that names "
        f"every file by its full path, for example: Remove-Item -LiteralPath {example} -Force — no loops, Join-Path, "
        "wildcards or other steps in it. Write or change notes and MEMORY.md with the Write and Edit tools. Deleting "
        "memory files needs no permission from them, so do not ask."
    ))


def review_memory_delete(command: str, memory_dir: str) -> Verdict | None:
    """Yaadash ke folder mein mitana. ALLOW = bina poochhe; redo = shakl badlo; None = ye yaadash ka mitana nahi (aam qaida)."""
    text = str(command or "")
    if not memory_dir or not _DELETE.search(text):
        return None
    root = _norm(os.path.abspath(memory_dir))
    if root not in _expand_env(text.replace("/", "\\").lower()):
        return None
    values: dict[str, str] = {}
    targets: list[str] = []
    readable = True
    for statement in _statements(text):
        assign = _ASSIGN.match(statement)
        if assign:
            value = _resolve(assign.group(2), values)
            if value is None:
                readable = False
            else:
                values[assign.group(1).lower()] = value
            continue
        words = statement.split(None, 1)
        if words[0].lower() not in _DELETE_CMDLETS:
            readable = False
            continue
        found = _delete_targets(words[1] if len(words) > 1 else "", values)
        if found is None:
            readable = False
        else:
            targets.extend(found)
    for target in targets:
        # `..` pehle hal: "yaadash\..\x.md" asal mein yaadash se bahar hai (chhupi cheez nahi).
        path = _norm(os.path.normpath(target))
        if not os.path.isabs(target) or not _inside(path, root):
            return None
        if path == root or any(part.startswith(".") for part in path[len(root):].split("\\") if part):
            return _deny(MEMORY_BACKUP)
    if not readable or not targets:
        return _memory_redo(memory_dir)
    return ALLOW


def review_powershell(command: str, memory_dir: str = "") -> Verdict:
    text = str(command or "")
    for pattern, why in _NEVER:
        if pattern.search(text):
            return _deny(why)
    if _wipes_a_root(text):
        return _deny("it would delete an entire core folder of this PC (a drive, Windows, Program Files or the whole user folder)")
    memory = review_memory_delete(text, memory_dir)
    if memory is not None:
        return memory
    return ALLOW


# ── Files ────────────────────────────────────────────────────────────────────

_SECRET_FILE = re.compile(
    r"\\login data(-journal)?$|\\cookies(-journal)?$|\\web data$|\\\.ssh\\id_[^\\]*$|\\wallet\.dat$|\\key4\.db$|\\logins\.json$"
    r"|\\voice_beta\.json$|\\\.env(\.[^\\]*)?$|\\credentials(\.json)?$|\.(pem|pfx|p12|kdbx)$",
    re.I,
)


# Mac: wo folder jo macOS ya installed programs ke hain. (/var nahi: temp
# folder /var/folders mein hota hai aur wahan likhna aam kaam hai.)
_MAC_SYSTEM_DIRS = ("/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/opt")
# Mac ka Keychain: passwords ka khazana, `.keychain-db` files mein.
_MAC_SECRET_FILE = re.compile(r"\\library\\keychains\\|\.keychain(-db)?$", re.I)


def _system_dirs() -> list[str]:
    if not IS_WINDOWS:
        return [_norm(item) for item in _MAC_SYSTEM_DIRS]
    raw = [os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", r"C:\Program Files"),
           os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    return [_norm(item) for item in raw if item]


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "\\")


def review_file(tool_name: str, tool_input: Mapping[str, Any], memory_dir: str = "") -> Verdict:
    raw = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
    path = _norm(os.path.abspath(str(raw))) if raw else ""
    if not path:
        return ALLOW
    writing = tool_name in WRITE_TOOLS
    if _SECRET_FILE.search(path) or (not IS_WINDOWS and _MAC_SECRET_FILE.search(path)):
        if writing or tool_name == "Read":
            return _deny("that file holds passwords, keys or other secrets")
    if not writing:
        return ALLOW
    if any(_inside(path, folder) for folder in _system_dirs()):
        if not IS_WINDOWS:
            return _deny("that file belongs to macOS or an installed program")
        return _deny("that file belongs to Windows or an installed program")
    # Apni yaadash (Claude Code ki auto memory ka folder): yaad likhna / badalna
    # model ka apna kaam hai — Claude Code bhi is folder ki likhai par nahi poochhta.
    # Chhupi cheezein (muntaqli ka backup / nishan) kabhi nahi.
    memory_root = _norm(os.path.abspath(memory_dir)) if memory_dir else ""
    if memory_root and _inside(path, memory_root):
        if path == memory_root or any(part.startswith(".") for part in path[len(memory_root):].split("\\") if part):
            return _deny(MEMORY_BACKUP)
    return ALLOW


# ── Lambe command: peeche chalen ─────────────────────────────────────────────
#
# Dimagh command chala kar us ke natije ka intezar karta hai — aur us dauran na
# kuch sunta hai, na bolta hai (Claude Code aise hi chalta hai). Lambe kaam ke
# liye Claude Code ka apna raasta hai: `run_in_background` — command peeche chalta
# hai, dimagh azaad rehta hai, aur khatam hone par CLI use khud khabar deta hai.
#
# Ye faisla dimagh ka hai (prompt use batata hai). Naapa (13 Oct 2026, Haiku
# 5.5): 4 mein se 1 dafa us ne 15 second ka command saamne hi chala diya. Is
# liye jo command apni shakal se hi lambe hain (install, file ka download, poore
# folder ki copy, archive…) un par ye faisla yahan pakka hota hai: agent.py ka
# PreToolUse hook unhein `updatedInput` se peeche bhej deta hai chahe dimagh bhool
# jaye. Ghalat pakar ka nuqsan sirf itna ke ek chhota command peeche se chalta hai.

_LONG_RUNNING = re.compile(
    r"\b(winget|choco|scoop)(?:\.exe)?\s+(install|upgrade|update|uninstall|import)\b|\bmsiexec\b"
    r"|\b(pip|pip3|npm|pnpm|yarn)(?:\.exe|\.cmd)?\s+(install|i|ci|add|update)\b|\bpython(?:\.exe)?\s+-m\s+pip\s+install\b"
    r"|\binstall-(module|package|windowsupdate)\b|\bget-windowsupdate\b"
    r"|\bgit(?:\.exe)?\s+clone\b|\bstart-bitstransfer\b"
    r"|\b(invoke-webrequest|iwr|curl(?:\.exe)?|wget)\b[^\n;|]*\s(-outfile|-o|--output|--remote-name)\b"
    r"|\brobocopy\b|\bxcopy\b|\b(copy-item|move-item|cp|mv|copy|move)\b[^\n;|]*\s-recurse\b"
    r"|\b(compress|expand)-archive\b|\b7z(?:\.exe)?\s+[ax]\b|\btar(?:\.exe)?\s+-?[cxz]"
    r"|\bffmpeg\b|\bwait-process\b|\bstart-process\b[^\n;|]*\s-wait\b"
    r"|\b(sfc|dism|chkdsk|defrag)(?:\.exe)?\s|\brepair-volume\b",
    re.I,
)
_SLEEP = re.compile(r"\bstart-sleep\b(?:\s+-s(?:econds)?)?\s+(\d+)(?![\d.]*\s*-m)|\bsleep\s+(\d+)\b|\btimeout(?:\.exe)?\s+/t\s+(\d+)", re.I)
LONG_SLEEP_SECS = 5


def runs_long(command: str) -> bool:
    """Ye command apni shakal se hi lamba hai (install, download, bari copy, lamba intezar)?"""
    text = str(command or "")
    if _LONG_RUNNING.search(text):
        return True
    return any(int(next(group for group in match.groups() if group)) >= LONG_SLEEP_SECS for match in _SLEEP.finditer(text))


# ── Screen ke haath (mouse / keyboard) ───────────────────────────────────────
#
# Click / tehreer par koi pehra nahi: Send, Pay, Delete — saaf hukm par foran
# (3 Nov 2026). Bharosa prompt ke qaide (passwords / card number user khud
# likhta hai) aur user ke Esc / mouse par hai.

SCREEN_PREFIX = "mcp__computer__"


# ── Darwaza ──────────────────────────────────────────────────────────────────


def review_tool(tool_name: str, tool_input: Mapping[str, Any] | None, memory_dir: str = "") -> Verdict:
    """Ek tool call ka faisla. Jo tool yahan pehchana na jaye (Stonic ke apne tools) wo chalta hai.

    `memory_dir` = yaadash ka folder (Claude Code ki auto memory): wahan notes
    likhna / badalna (review_file) aur mitana (review_memory_delete) bina poochhe.
    """
    tool_input = tool_input or {}
    if tool_name in SHELL_TOOLS:
        return review_powershell(str(tool_input.get("command") or ""), memory_dir)
    if tool_name in WRITE_TOOLS or tool_name in READ_TOOLS:
        return review_file(tool_name, tool_input, memory_dir)
    return ALLOW
