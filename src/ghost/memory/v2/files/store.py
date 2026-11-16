"""Ghost's file memory — ONE folder, the way Claude's OFFICIAL memory works.

Ported from Stonic's ``file-memory/store.js``::

    <config>/memory_v2/notes/
        MEMORY.md          the index — ONE line per memory. The AI writes it
                           itself (like Claude Code); code only reads it, and
                           only creates it when it does not exist (backfill).
        <any name>.md      one memory = one file, frontmatter on top
                           (name / description / metadata.type).
        .migration.json    hidden, out of the AI's reach.

Who touches it: Anthropic's memory tool (``memory_20250818``) through
``command()``, a Claude Code-style ``grep()``, the Settings page and the
migration. Replies match the official handler (``official.py``).

Code's own three safeguards (the official docs' "security considerations"):

1. Paths: only inside /memories; "..", junctions/symlinks out, hidden files
   and Windows reserved names are refused.
2. Every write is whole or nothing (hidden temp file + rename), one at a time.
3. A file size cap (a too-big file is refused, never cut).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets as _rand
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ghost.memory.v2.files import official
from ghost.memory.v2.files.official import INDEX_FILE, VIRTUAL_ROOT
from ghost.memory.v2.files.official import memory_tool as T

logger = logging.getLogger(__name__)

LIMITS = {
    "file_chars": 60_000,
    "files": 5_000,
    "depth": 6,
    "segment_chars": 120,
    "name_chars": 120,
    "description_chars": 400,
    "grep_columns": 500,
}
_RESERVED_NAME = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)
_BAD_CHARS = re.compile(r'[<>:"|?*\\]')
COMMANDS = ("view", "create", "str_replace", "insert", "delete", "rename")


class MemoryError(Exception):  # noqa: A001 — Stonic's name; a message for the model
    pass


def one_line(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str("" if value is None else value)).strip()
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def to_lf(text: object) -> str:
    return re.sub(r"\r\n?", "\n", str("" if text is None else text))


def _is_control(c: int) -> bool:
    return c < 32 or 127 <= c <= 159


def _occurrences(hay: str, needle: str) -> List[int]:
    out: List[int] = []
    if not needle:
        return out
    i = hay.find(needle)
    while i != -1:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def _line_of(text: str, index: int) -> int:
    return text[:index].count("\n")


def _unquote(value: object) -> str:
    s = str("" if value is None else value).strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        try:
            return str(json.loads(s))
        except ValueError:
            pass
    return re.sub(r"""^(["'])(.*)\1$""", r"\2", s)


_FM = re.compile(r"^---[ \t]*\n([\s\S]*?)\n---[ \t]*(?:\n|$)")


def _frontmatter_lines(text: str) -> int:
    m = re.match(r"^---[ \t]*\n[\s\S]*?\n---[ \t]*(?:\n|$)", text)
    return m.group(0).count("\n") if m else 0


def parse_note(raw: object) -> Dict[str, Any]:
    """A note's frontmatter (top-level ``key: value`` and those inside ``metadata:``)."""
    text = to_lf(raw).lstrip("﻿")
    match = _FM.match(text)
    fields: Dict[str, str] = {}
    metadata: Dict[str, str] = {}
    body = text
    if match:
        body = text[match.end() :]
        in_meta = False
        for line in match.group(1).split("\n"):
            top = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", line)
            if top:
                in_meta = top.group(1) == "metadata" and top.group(2).strip() == ""
                if not in_meta:
                    fields[top.group(1).lower()] = _unquote(top.group(2))
                continue
            nested = (
                re.match(r"""^\s+["']?([\w-]+)["']?\s*:\s*(.*)$""", line)
                if in_meta
                else None
            )
            if nested:
                metadata[nested.group(1)] = _unquote(nested.group(2))
    description = fields.get("description") or ""
    if not description:
        description = next(
            (
                x
                for x in (
                    re.sub(r"^[#>*\-\s]+", "", ln).strip() for ln in body.split("\n")
                )
                if x
            ),
            "",
        )
    return {
        "name": one_line(fields.get("name", ""), LIMITS["name_chars"]),
        "description": one_line(description, LIMITS["description_chars"]),
        "type": one_line(metadata.get("type") or fields.get("type") or "", 40),
        "modified": metadata.get("modified", ""),
        "body": body,
        "has_header": bool(match and fields.get("description")),
        "fields": fields,
        "metadata": metadata,
    }


def write_atomic(file: Path, text: str) -> None:
    """Power cut or an antivirus grab — the file is still whole or old, never half."""
    tmp = file.parent / f".{file.name}.tmp-{os.getpid()}-{_rand.token_hex(4)}"
    tmp.write_text(text, encoding="utf-8", newline="")
    for attempt in range(5):
        try:
            os.replace(tmp, file)
            return
        except PermissionError:
            if attempt >= 4:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.03 * (attempt + 1))


def glob_to_regex(glob: str) -> "re.Pattern[str]":
    """Simple glob (``*``, ``?``, ``{a,b}``, ``**``). Without "/" it matches the file name."""
    out = ""
    i = 0
    while i < len(glob):
        ch = glob[i]
        if ch == "*":
            if i + 1 < len(glob) and glob[i + 1] == "*":
                out += ".*"
                i += 1
                if i + 1 < len(glob) and glob[i + 1] == "/":
                    i += 1
            else:
                out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "{":
            end = glob.find("}", i)
            if end == -1:
                out += "\\{"
                i += 1
                continue
            alts = [
                re.escape(p).replace("\\*", "[^/]*")
                for p in glob[i + 1 : end].split(",")
            ]
            out += "(?:" + "|".join(alts) + ")"
            i = end
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(f"^{out}$", re.I)


class FileMemory:
    def __init__(
        self, folder: Path, *, now: Optional[Callable[[], float]] = None
    ) -> None:
        self.dir = Path(folder).resolve()
        self.now = now or (lambda: time.time() * 1000)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._real_root: Optional[str] = None
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def on_change(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, event: Dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                logger.debug("file memory listener failed", exc_info=True)

    def ensure(self) -> "FileMemory":
        """Create the folder (first time) and sweep old half temp files. MEMORY.md is NOT created."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self._real_root = os.path.realpath(self.dir)
        cutoff = self.now() - 60_000
        try:
            for p in self.dir.iterdir():
                if ".tmp-" in p.name:
                    try:
                        if p.stat().st_mtime * 1000 < cutoff:
                            p.unlink()
                    except OSError:
                        pass
        except OSError:
            pass
        return self

    @property
    def index_path(self) -> Path:
        return self.dir / INDEX_FILE

    # -- paths ---------------------------------------------------------------------

    def resolve(self, value: object) -> Dict[str, Any]:
        """The model's path ("/memories/ammi.md") -> the real place inside the folder."""
        from urllib.parse import unquote

        raw = str("" if value is None else value).strip()
        try:
            decoded = unquote(raw, errors="strict")
        except UnicodeDecodeError:
            decoded = raw
        unified = decoded.replace("\\", "/")
        if not unified.startswith(VIRTUAL_ROOT) or (
            len(unified) > len(VIRTUAL_ROOT) and unified[len(VIRTUAL_ROOT)] != "/"
        ):
            raise MemoryError(T.must_start_with_root(raw))
        parts = [p for p in unified[len(VIRTUAL_ROOT) :].split("/") if p]
        if len(parts) > LIMITS["depth"]:
            raise MemoryError(
                f"Path {raw} is nested too deep (at most {LIMITS['depth']} levels inside {VIRTUAL_ROOT})."
            )
        for seg in parts:
            if seg in (".", ".."):
                raise MemoryError(T.escapes(raw))
            bad = (
                seg.startswith(".")
                or _BAD_CHARS.search(seg)
                or seg.endswith((".", " "))
                or _RESERVED_NAME.match(seg)
                or len(seg) > LIMITS["segment_chars"]
                or any(_is_control(ord(c)) for c in seg)
            )
            if bad:
                raise MemoryError(
                    f"Path {raw} is not a usable memory file name. Use letters, numbers and dashes, like {VIRTUAL_ROOT}/sister-ayesha.md."
                )
        rel = "/".join(parts)
        absolute = self.dir.joinpath(*parts) if parts else self.dir
        self._assert_real_inside(absolute, raw)
        return {
            "rel": rel,
            "abs": absolute,
            "is_root": not parts,
            "is_index": len(parts) == 1 and parts[0].lower() == INDEX_FILE.lower(),
            "virtual": f"{VIRTUAL_ROOT}/{rel}" if rel else VIRTUAL_ROOT,
        }

    def _assert_real_inside(self, absolute: Path, raw: str) -> None:
        """A junction/symlink inside the folder that leads out is refused too."""
        if not self._real_root:
            self.ensure()
        probe = absolute
        while not probe.exists():
            if probe.parent == probe:
                return
            probe = probe.parent
        real = os.path.realpath(probe)
        root = self._real_root or ""
        if os.name == "nt":
            real, root = real.lower(), root.lower()
        if real != root and not real.startswith(root + os.sep):
            raise MemoryError(T.escapes(raw))

    # -- the notes list ------------------------------------------------------------------

    def scan(self) -> List[Dict[str, Any]]:
        """All notes (except MEMORY.md), newest first. Only changed files are re-read."""
        if not self._real_root:
            self.ensure()
        rels: List[str] = []
        self._walk(self.dir, "", 0, rels, lambda name: name.lower().endswith(".md"))
        seen = set()
        notes = []
        for rel in rels:
            if "/" not in rel and rel.lower() == INDEX_FILE.lower():
                continue
            seen.add(rel)
            path = self.dir.joinpath(*rel.split("/"))
            try:
                st = path.stat()
            except OSError:
                continue
            meta = self._cache.get(rel)
            if (
                not meta
                or meta["mtime_ms"] != st.st_mtime * 1000
                or meta["size"] != st.st_size
            ):
                try:
                    raw = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                p = parse_note(raw)
                meta = {
                    "mtime_ms": st.st_mtime * 1000, "size": st.st_size, "name": p["name"],
                    "description": p["description"], "type": p["type"], "has_header": p["has_header"],
                }  # fmt: skip
                self._cache[rel] = meta
            notes.append({"rel": rel, "path": f"{VIRTUAL_ROOT}/{rel}", **meta})
        for rel in [r for r in self._cache if r not in seen]:
            del self._cache[rel]
        notes.sort(key=lambda n: (-n["mtime_ms"], n["rel"]))
        return notes

    def _walk(
        self,
        folder: Path,
        prefix: str,
        depth: int,
        out: List[str],
        accept: Callable[[str], bool] = lambda _n: True,
    ) -> None:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.is_symlink() or e.name == "node_modules":
                continue
            rel = f"{prefix}/{e.name}" if prefix else e.name
            if e.is_dir():
                if depth + 1 < LIMITS["depth"]:
                    self._walk(Path(e.path), rel, depth + 1, out, accept)
                continue
            if e.is_file() and accept(e.name):
                out.append(rel)

    # -- the index (MEMORY.md) ------------------------------------------------------------------

    def read_index(self) -> str:
        try:
            return self.index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def ensure_index(self) -> bool:
        """Like the CLI: no MEMORY.md but notes exist -> build the index (backfill)."""
        if self.index_path.exists():
            return False
        notes = self.scan()
        if not notes:
            return False
        content = official.backfill_index(
            [{"rel": n["rel"], "description": n["description"]} for n in notes]
        )["content"]
        if not content:
            return False
        try:
            with open(self.index_path, "x", encoding="utf-8", newline="") as fh:
                fh.write(content)
        except FileExistsError:
            return False
        except OSError as exc:
            logger.warning("[FileMemory] MEMORY.md not created: %s", exc)
            return False
        self._emit({"op": "index", "path": f"{VIRTUAL_ROOT}/{INDEX_FILE}"})
        return True

    def prompt_index(self) -> str:
        return official.load_index(self.read_index())["content"]

    @staticmethod
    def _line_links(line: str, rel: str) -> bool:
        if not rel:
            return False
        links = [
            re.sub(r"^memories/", "", re.sub(r"^\.?/", "", m.group(1)), flags=re.I)
            for m in re.finditer(r"\]\(\s*<?([^)>]+?)>?\s*\)", line)
        ]
        return any(link.lower() == rel.lower() for link in links)

    def _index_mentions(self, index_text: str, rel: str) -> bool:
        return any(
            self._line_links(line, rel) for line in str(index_text or "").split("\n")
        )

    def add_pointers(self, notes: List[Dict[str, Any]]) -> int:
        """Our layers (migration, Settings) add these notes' lines to the end of the index."""
        current = self.read_index()
        lines: List[str] = []
        for note in notes or []:
            if self._index_mentions(current, note["rel"]) or any(
                f"]({note['rel']})" in x for x in lines
            ):
                continue
            line = official.backfill_line(note["rel"], note.get("description", ""))
            if line:
                lines.append(line)
        if not lines:
            return 0
        base = current + "\n" if current and not current.endswith("\n") else current
        write_atomic(self.index_path, base + "\n".join(lines) + "\n")
        return len(lines)

    def remove_pointers(self, rel: str) -> int:
        current = self.read_index()
        if not current:
            return 0
        kept = []
        removed = 0
        for line in current.split("\n"):
            if self._line_links(line, rel):
                removed += 1
                continue
            kept.append(line)
        if removed:
            write_atomic(self.index_path, "\n".join(kept))
        return removed

    # -- the memory tool ----------------------------------------------------------------------

    def command(self, args: object, *, session_id: str = "") -> Dict[str, Any]:
        """Anthropic's memory tool (view / create / str_replace / insert / delete / rename)."""
        a = args if isinstance(args, dict) else {}
        cmd = str(a.get("command") or "").strip()
        if cmd not in COMMANDS:
            return {"ok": False, "text": T.unknown_command(cmd or "(none)")}
        with self._lock:
            try:
                return getattr(self, f"_{cmd}")(a, session_id)
            except MemoryError as exc:
                return {"ok": False, "text": str(exc)}
            except OSError as exc:
                logger.error("[FileMemory] %s failed: %s", cmd, exc)
                return {
                    "ok": False,
                    "text": f"Error: the memory could not do that right now ({type(exc).__name__}). Try again in a moment.",
                }

    @staticmethod
    def _stat(path: Path) -> Optional[os.stat_result]:
        try:
            return path.stat()
        except OSError:
            return None

    def _view(self, a: Dict[str, Any], _sid: str) -> Dict[str, Any]:
        import stat as st_mod

        target = self.resolve(a.get("path"))
        st = self._stat(target["abs"])
        if not st:
            raise MemoryError(T.not_found(target["virtual"]))
        if st_mod.S_ISDIR(st.st_mode):
            lines = [f"{official.format_list_size(st.st_size)}\t{target['virtual']}"]

            def collect(folder: Path, prefix: str, depth: int) -> None:
                if depth > 2:
                    return
                try:
                    entries = sorted(os.scandir(folder), key=lambda e: e.name)
                except OSError:
                    return
                for e in entries:
                    if (
                        e.name.startswith(".")
                        or e.name == "node_modules"
                        or e.is_symlink()
                    ):
                        continue
                    rel = f"{prefix}/{e.name}" if prefix else e.name
                    s = self._stat(Path(e.path))
                    if not s:
                        continue
                    if e.is_dir():
                        lines.append(
                            f"{official.format_list_size(s.st_size)}\t{target['virtual']}/{rel}/"
                        )
                        if depth < 2:
                            collect(Path(e.path), rel, depth + 1)
                    elif e.is_file():
                        lines.append(
                            f"{official.format_list_size(s.st_size)}\t{target['virtual']}/{rel}"
                        )

            collect(target["abs"], "", 1)
            return {
                "ok": True,
                "text": T.dir_header(target["virtual"]) + "\n" + "\n".join(lines),
            }
        content = target["abs"].read_text(encoding="utf-8")
        lines = content.split("\n")
        if len(lines) > 1 and lines[-1] == "":
            lines.pop()
        if len(lines) > 999_999:
            raise MemoryError(T.too_many_lines(target["virtual"]))
        shown = lines
        start = 1
        rng = a.get("view_range")
        if isinstance(rng, list) and len(rng) == 2:
            try:
                lo, hi = int(rng[0]), int(rng[1])
                frm = max(1, lo) - 1
                shown = lines[frm:] if hi == -1 else lines[frm:hi]
                start = frm + 1
            except (TypeError, ValueError):
                pass
        body = ""
        cut = 0
        for i, line in enumerate(shown):
            numbered = official.numbered_line(i + start, line)
            if len(body) + len(numbered) + 1 > T.view_chars and body:
                cut = i + start
                break
            body += ("\n" if body else "") + numbered
        if cut:
            body += f"\n[Truncated at line {cut - 1} of {len(lines)} (16,000 characters) — use view_range to read the rest.]"
        age = (
            ""
            if target["is_index"]
            else official.age_note(st.st_mtime * 1000, self.now())
        )
        return {"ok": True, "text": f"{age}{T.file_header(target['virtual'])}\n{body}"}

    def _file_target(self, path_arg: object) -> Dict[str, Any]:
        target = self.resolve(path_arg)
        if target["is_root"]:
            raise MemoryError(T.file_missing(target["virtual"]))
        return target

    @staticmethod
    def _check_size(text: str, virtual: str) -> None:
        if len(text) > LIMITS["file_chars"]:
            raise MemoryError(
                f"Error: {virtual} would be {len(text)} characters; the limit is {LIMITS['file_chars']}. Keep each memory to one "
                "fact and move detail into separate files."
            )

    def _prepare(self, target: Dict[str, Any], text: str, session_id: str) -> str:
        return (
            text
            if target["is_index"]
            else official.stamp_note(text, session_id=session_id, now=self.now())
        )

    def _with_index_notice(self, target: Dict[str, Any], text: str) -> str:
        if not target["is_index"]:
            return text
        notice = official.index_write_notice(self.read_index())
        return f"{text}\n\n{notice['text']}" if notice else text

    def _done(self, target: Dict[str, Any], op: str, text: str) -> Dict[str, Any]:
        self._emit({"op": op, "path": target["virtual"]})
        return {
            "ok": True,
            "text": self._with_index_notice(target, text),
            "changed": target["virtual"],
        }

    def _create(self, a: Dict[str, Any], sid: str) -> Dict[str, Any]:
        target = self._file_target(a.get("path"))
        if target["abs"].exists():
            raise MemoryError(T.exists(target["virtual"]))
        text = a["file_text"] if isinstance(a.get("file_text"), str) else ""
        self._check_size(text, target["virtual"])
        if len(self.scan()) >= LIMITS["files"]:
            raise MemoryError(
                "Error: the memory directory is full. Merge or delete files that are no longer relevant first."
            )
        target["abs"].parent.mkdir(parents=True, exist_ok=True)
        write_atomic(target["abs"], self._prepare(target, text, sid))
        return self._done(target, "create", T.created(target["virtual"]))

    def _read_existing(
        self, target: Dict[str, Any], missing: Callable[[str], str]
    ) -> str:
        st = self._stat(target["abs"])
        if not st or not target["abs"].is_file():
            raise MemoryError(missing(target["virtual"]))
        return target["abs"].read_text(encoding="utf-8")

    def _str_replace(self, a: Dict[str, Any], sid: str) -> Dict[str, Any]:
        target = self._file_target(a.get("path"))
        content = self._read_existing(target, T.file_missing)
        old = a["old_str"] if isinstance(a.get("old_str"), str) else ""
        new = a["new_str"] if isinstance(a.get("new_str"), str) else ""
        hits = _occurrences(content, old)
        if not hits:
            raise MemoryError(T.no_match(old, target["virtual"]))
        if len(hits) > 1:
            raise MemoryError(
                T.many_matches(old, [_line_of(content, i) + 1 for i in hits])
            )
        nxt = content[: hits[0]] + new + content[hits[0] + len(old) :]
        self._check_size(nxt, target["virtual"])
        written = self._prepare(target, nxt, sid)
        write_atomic(target["abs"], written)
        changed = _line_of(nxt, hits[0])
        if changed >= _frontmatter_lines(nxt):
            changed += _frontmatter_lines(written) - _frontmatter_lines(nxt)
        final = written.split("\n")
        frm = max(0, changed - 2)
        to = min(len(final), changed + 3)
        snippet = [
            official.numbered_line(n, final[n - 1]) for n in range(frm + 1, to + 1)
        ]
        return self._done(
            target, "update", f"{T.edited} {T.snippet_header}\n" + "\n".join(snippet)
        )

    def _insert(self, a: Dict[str, Any], sid: str) -> Dict[str, Any]:
        target = self._file_target(a.get("path"))
        content = self._read_existing(target, T.path_missing)
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        raw_at = a.get("insert_line")
        try:
            at = int(raw_at)  # type: ignore[arg-type]
            if isinstance(raw_at, float) and raw_at != at:
                raise ValueError
        except (TypeError, ValueError):
            raise MemoryError(T.bad_insert_line(raw_at, len(lines))) from None
        if at < 0 or at > len(lines):
            raise MemoryError(T.bad_insert_line(raw_at, len(lines)))
        insert_text = a["insert_text"] if isinstance(a.get("insert_text"), str) else ""
        lines.insert(at, insert_text.rstrip("\n"))
        nxt = "\n".join(lines)
        if not nxt.endswith("\n"):
            nxt += "\n"
        self._check_size(nxt, target["virtual"])
        write_atomic(target["abs"], self._prepare(target, nxt, sid))
        return self._done(target, "update", T.inserted(target["virtual"]))

    def _delete(self, a: Dict[str, Any], _sid: str) -> Dict[str, Any]:
        target = self.resolve(a.get("path"))
        if target["is_root"]:
            raise MemoryError(T.cannot_delete_root)
        if not self._stat(target["abs"]):
            raise MemoryError(T.path_missing(target["virtual"]))
        if target["abs"].is_dir():
            shutil.rmtree(target["abs"], ignore_errors=True)
        else:
            target["abs"].unlink()
        self._emit({"op": "delete", "path": target["virtual"]})
        return {
            "ok": True,
            "text": T.deleted(target["virtual"]),
            "changed": target["virtual"],
        }

    def _rename(self, a: Dict[str, Any], _sid: str) -> Dict[str, Any]:
        frm = self.resolve(a.get("old_path"))
        to = self.resolve(a.get("new_path"))
        if frm["is_root"]:
            raise MemoryError(T.cannot_rename_root)
        if to["is_root"]:
            raise MemoryError(T.destination_exists(to["virtual"]))
        if not self._stat(frm["abs"]):
            raise MemoryError(T.path_missing(frm["virtual"]))
        if self._stat(to["abs"]):
            raise MemoryError(T.destination_exists(to["virtual"]))
        to["abs"].parent.mkdir(parents=True, exist_ok=True)
        os.rename(frm["abs"], to["abs"])
        self._emit({"op": "rename", "path": to["virtual"], "from": frm["virtual"]})
        return {
            "ok": True,
            "text": self._with_index_notice(
                to, T.renamed(frm["virtual"], to["virtual"])
            ),
            "changed": to["virtual"],
        }

    # -- grep -----------------------------------------------------------------------------------

    def grep(self, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            return {"ok": True, "text": self._grep(args or {})}
        except MemoryError as exc:
            return {"ok": False, "text": str(exc)}
        except OSError:
            return {
                "ok": False,
                "text": "Error: the memory search did not work right now. Try again in a moment.",
            }

    def _grep(self, a: Dict[str, Any]) -> str:
        source = a["pattern"] if isinstance(a.get("pattern"), str) else ""
        if not source:
            raise MemoryError("Error: pattern is required.")
        flags = 0
        if source.startswith("(?i)"):
            source = source[4:]
            flags |= re.I
        if a.get("case_insensitive") is True or a.get("-i") is True:
            flags |= re.I
        try:
            regex = re.compile(source, flags)
        except re.error as exc:
            raise MemoryError(
                f"Error: invalid regular expression: {one_line(str(exc), 200)}"
            ) from None
        mode = (
            a.get("output_mode")
            if a.get("output_mode") in ("content", "count", "files_with_matches")
            else "files_with_matches"
        )
        try:
            limit = (
                official.grep_result.default_limit
                if a.get("head_limit") is None
                else int(a["head_limit"])
            )
            if limit < 0:
                limit = official.grep_result.default_limit
        except (TypeError, ValueError):
            limit = official.grep_result.default_limit
        try:
            offset = max(0, int(a.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            context = max(0, min(20, int(a.get("context", a.get("-C", 0)) or 0)))
        except (TypeError, ValueError):
            context = 0

        base = self.resolve(a.get("path") or VIRTUAL_ROOT)
        st = self._stat(base["abs"])
        if not st:
            raise MemoryError(f"Path does not exist: {base['virtual']}.")
        rels: List[str] = []
        if base["abs"].is_dir():
            self._walk(
                base["abs"],
                base["rel"],
                len(base["rel"].split("/")) if base["rel"] else 0,
                rels,
            )
        else:
            rels = [base["rel"]]
        glob = a.get("glob")
        if isinstance(glob, str) and glob.strip():
            patterns = [
                glob_to_regex(g)
                for part in glob.split()
                for g in ([part] if "{" in part else part.split(","))
                if g
            ]
            rels = [
                r
                for r in rels
                if any(p.match(r) or p.match(r.split("/")[-1]) for p in patterns)
            ]

        files = []
        for rel in rels:
            path = self.dir.joinpath(*rel.split("/"))
            try:
                text = to_lf(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            lines = text.split("\n")
            if len(lines) > 1 and lines[-1] == "":
                lines.pop()
            matched = [i for i, line in enumerate(lines) if regex.search(line)]
            if not matched:
                continue
            mtime = self._stat(path)
            files.append(
                {
                    "rel": rel,
                    "virtual": f"{VIRTUAL_ROOT}/{rel}",
                    "lines": lines,
                    "matched": matched,
                    "mtime": mtime.st_mtime if mtime else 0,
                }
            )
        files.sort(key=lambda f: (-f["mtime"], f["rel"]))

        def page(items: List[str]):
            sliced = items[offset:] if limit == 0 else items[offset : offset + limit]
            limited = limit != 0 and len(items) - offset > limit
            note = ", ".join(
                x
                for x in (
                    f"limit: {limit}" if limited else "",
                    f"offset: {offset}" if offset else "",
                )
                if x
            )
            return sliced, note

        g = official.grep_result
        if mode == "files_with_matches":
            if not files:
                return g.no_files
            sliced, note = page([f["virtual"] for f in files])
            return (
                g.files(
                    sliced,
                    f"[Showing results with pagination = {note}]" if note else "",
                )
                if sliced
                else g.no_files
            )
        if mode == "count":
            if not files:
                return g.no_matches
            rows = [f"{f['virtual']}:{len(f['matched'])}" for f in files]
            sliced, note = page(rows)
            return g.count(
                "\n".join(sliced),
                sum(len(f["matched"]) for f in files),
                len(files),
                note,
            )
        out: List[str] = []
        for f in files:
            show = set()
            for i in f["matched"]:
                show.update(
                    range(
                        max(0, i - context), min(len(f["lines"]) - 1, i + context) + 1
                    )
                )
            prev = -2
            for j in sorted(show):
                if context > 0 and prev >= 0 and j > prev + 1:
                    out.append("--")
                line = f["lines"][j]
                hit = j in f["matched"]
                shown = (
                    f"[Omitted long line with {1 if hit else 0} matches]"
                    if len(line) > LIMITS["grep_columns"]
                    else line
                )
                sep = ":" if hit else "-"
                out.append(f"{f['virtual']}{sep}{j + 1}{sep}{shown}")
                prev = j
        if not out:
            return g.no_matches
        sliced, note = page(out)
        return g.content("\n".join(sliced) or g.no_matches, note)

    # -- the Settings page and migration ----------------------------------------------------------

    def bulk_write(self, notes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Many notes at once (migration only); afterwards the index once."""
        with self._lock:
            written = 0
            failed = []
            done = []
            for note in notes:
                try:
                    target = self._file_target(f"{VIRTUAL_ROOT}/{note['rel']}")
                    if target["is_index"]:
                        raise MemoryError("MEMORY.md is the index")
                    text = to_lf(note["text"])
                    self._check_size(text, target["virtual"])
                    target["abs"].parent.mkdir(parents=True, exist_ok=True)
                    write_atomic(target["abs"], text)
                    mt = note.get("mtime_ms")
                    if isinstance(mt, (int, float)) and mt > 0:
                        try:
                            os.utime(target["abs"], (mt / 1000, mt / 1000))
                        except OSError:
                            pass
                    written += 1
                    done.append(
                        {
                            "rel": target["rel"],
                            "description": parse_note(text)["description"],
                        }
                    )
                except (MemoryError, OSError) as exc:
                    failed.append({"rel": note.get("rel"), "error": str(exc)})
            if written:
                try:
                    if not self.ensure_index():
                        self.add_pointers(done)
                except OSError as exc:
                    logger.warning(
                        "[FileMemory] index not updated after migration: %s", exc
                    )
                self._emit({"op": "import", "count": written})
            return {"written": written, "failed": failed}

    def list(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": n["path"], "name": n["name"] or re.sub(r"\.md$", "", n["rel"], flags=re.I),
                "description": n["description"], "type": n["type"], "updated_at": n["mtime_ms"], "size": n["size"],
            }
            for n in self.scan()
        ]  # fmt: skip

    def read(self, path_arg: str) -> Dict[str, Any]:
        try:
            target = self._file_target(path_arg)
            if not target["abs"].is_file():
                return {"ok": False, "text": T.not_found(target["virtual"])}
            return {"ok": True, "text": target["abs"].read_text(encoding="utf-8")}
        except (MemoryError, OSError) as exc:
            return {"ok": False, "text": str(exc)}

    def write_note(
        self, path_arg: str, text: str, *, session_id: str = ""
    ) -> Dict[str, Any]:
        with self._lock:
            try:
                target = self._file_target(path_arg)
                body = to_lf(text)
                self._check_size(body, target["virtual"])
                existed = target["abs"].exists()
                target["abs"].parent.mkdir(parents=True, exist_ok=True)
                write_atomic(target["abs"], self._prepare(target, body, session_id))
                if not existed and not target["is_index"]:
                    self.add_pointers(
                        [
                            {
                                "rel": target["rel"],
                                "description": parse_note(body)["description"],
                            }
                        ]
                    )
                self._emit(
                    {"op": "update" if existed else "create", "path": target["virtual"]}
                )
                return {
                    "ok": True,
                    "text": f"Saved {target['virtual']}.",
                    "changed": target["virtual"],
                }
            except (MemoryError, OSError) as exc:
                return {"ok": False, "text": str(exc)}

    def delete_note(self, path_arg: str) -> Dict[str, Any]:
        with self._lock:
            try:
                target = self._file_target(path_arg)
                if target["is_index"]:
                    return {
                        "ok": False,
                        "text": "The index cannot be deleted from here.",
                    }
                if not target["abs"].is_file():
                    return {"ok": False, "text": T.path_missing(target["virtual"])}
                target["abs"].unlink()
                self.remove_pointers(target["rel"])
                self._emit({"op": "delete", "path": target["virtual"]})
                return {
                    "ok": True,
                    "text": T.deleted(target["virtual"]),
                    "changed": target["virtual"],
                }
            except (MemoryError, OSError) as exc:
                return {"ok": False, "text": str(exc)}

    def stats(self) -> Dict[str, int]:
        notes = self.scan()
        return {"notes": len(notes), "bytes": sum(n["size"] for n in notes)}


__all__ = [
    "COMMANDS",
    "LIMITS",
    "FileMemory",
    "MemoryError",
    "glob_to_regex",
    "parse_note",
    "write_atomic",
]
