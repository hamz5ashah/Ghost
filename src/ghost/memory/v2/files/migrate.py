"""Older memory into the notes folder — ONCE (Stonic port, phase 6).

Ported from Stonic's ``file-memory/migrate.js``. Sources, read only:

1. Memory Core     ``<config>/memory_v2/memory.db`` — active memories (one note
                   each), open promises, weekly conversation-summary notes
2. Ghost's files   ``MEMORY.md`` / ``USER.md`` in Ghost's config dir (the
                   prompt-builder memory files; entries split on "§" like
                   Stonic's legacy notes, otherwise on bullets / paragraphs)
3. Ghost v1 facts  ``memory_facts.jsonl`` (trusted / auto facts only)

Three promises:

1. ORIGINALS UNTOUCHED. Everything is copied to ``.old-memory-backup/<time>/``
   first; the database is read from a temp copy of that copy.
2. NOTHING LOST. Every active memory becomes its own note — nothing merged or
   shortened. Raw turns stay in the backed-up database.
3. RE-RUNS DON'T DUPLICATE. Names are deterministic, so an interrupted
   migration rewrites the same files. When done, ``.migration.json`` — and it
   never runs again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from ghost.memory.v2.files.store import FileMemory, parse_note

logger = logging.getLogger(__name__)

MIGRATION_VERSION = 1
MARKER_FILE = ".migration.json"
BACKUP_DIR = ".old-memory-backup"
ORIGIN = "old-ghost-memory"
MAX_ATTEMPTS = 3
PART_CHARS = 18_000
DESCRIPTION_CHARS = 240
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def _clean(v: object) -> str:
    return re.sub(r"\s+", " ", str("" if v is None else v)).strip()


def _one_line(v: object, limit: int = DESCRIPTION_CHARS) -> str:
    t = _clean(v)
    return t if len(t) <= limit else f"{t[: limit - 1].rstrip()}…"


def _date_of(ms: float) -> str:
    if not ms or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _date_time_of(ms: float) -> str:
    if not ms or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _same_key(text: object) -> str:
    s = unicodedata.normalize("NFKC", str("" if text is None else text)).lower()
    return " ".join(
        "".join(ch if unicodedata.category(ch)[0] in "LN" else " " for ch in s).split()
    )


def _short_hash(value: object) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:6]


_STOP = set(
    "a an the and or but of to in on for at by with from as into is are was were be been has have had do does did will would "
    "should that this these those it its he she they them his her their him user users wants want said says told tells likes "
    "which who when also very ne ki ka ke hai hain ko se aur bhi ye woh wo".split()
)
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])$")


def slug_from(text: object) -> str:
    """A short name from English / Roman text ("ali-bought-new-motorcycle"); '' from Urdu script."""
    ascii_text = "".join(
        c
        for c in unicodedata.normalize("NFKD", str(text or ""))
        if not unicodedata.combining(c)
    ).lower()
    words = [
        w for w in re.findall(r"[a-z0-9]+", ascii_text) if len(w) > 1 and w not in _STOP
    ]
    slug = ""
    for w in words:
        nxt = f"{slug}-{w}" if slug else w
        if len(nxt) > 48:
            break
        slug = nxt
        if len(slug.split("-")) >= 6:
            break
    if len(slug) < 4:
        return ""
    return f"{slug}-note" if _RESERVED.match(slug) else slug


_PREFIX_BY_TYPE = {
    "identity": "about-you", "preference": "likes", "instruction": "how-to-talk",
    "project": "project", "opinion": "opinion", "relationship": "people",
}  # fmt: skip
_OFFICIAL_TYPE = {
    "identity": "user", "preference": "user", "opinion": "user", "relationship": "user",
    "instruction": "feedback", "project": "project",
}  # fmt: skip


def character_label(ident: object) -> str:
    """ "female_companion" -> "Companion"."""
    raw = re.sub(r"^(female|male)_", "", str(ident or "")).replace("_", " ").strip()
    if not raw:
        return ""
    if raw == "custom":
        return "a custom character"
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), raw)


def _unique_rel(base: str, taken: Set[str]) -> str:
    stem = base or "note"
    rel = f"{stem}.md"
    i = 2
    while rel.lower() in taken:
        rel = f"{stem}-{i}.md"
        i += 1
    taken.add(rel.lower())
    return rel


def _header(name: str, description: str, kind: str = "user") -> str:
    return "\n".join(
        ["---", f"name: {name}", f"description: {json.dumps(_one_line(description), ensure_ascii=False)}", "metadata:",
         f"  type: {kind}", f"  origin: {ORIGIN}", "---", ""]
    )  # fmt: skip


# -- reading sources -------------------------------------------------------------


def read_core_db(db_file: Path) -> Dict[str, List[Dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{Path(db_file).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:

        def has(table: str) -> bool:
            return (
                conn.execute(
                    "select name from sqlite_master where type = 'table' and name = ?",
                    (table,),
                ).fetchone()
                is not None
            )

        def rows(sql: str) -> List[Dict[str, Any]]:
            return [dict(r) for r in conn.execute(sql).fetchall()]

        return {
            "memories": rows(
                "select uid, type, text_original, text_en, pinned, scope, mentioned_at, recorded_at, updated_at "
                "from memories where deleted = 0 and status = 'active' order by id"
            ) if has("memories") else [],
            "intentions": rows(
                "select uid, what, what_en, due_ts, character_id, created_at from intentions where status = 'open' order by id"
            ) if has("intentions") else [],
            "episodes": rows(
                "select uid, title, summary_en, character_id, start_ts from episodes order by start_ts, id"
            ) if has("episodes") else [],
        }  # fmt: skip
    finally:
        conn.close()


def parse_entries(text: object) -> List[Dict[str, str]]:
    """ "§"-separated notes (Stonic's legacy format), else bullets / paragraphs (Ghost's MEMORY.md)."""
    raw = str(text or "")
    if "§" in raw:
        pieces = re.split(r"\n?§\n?", raw)
    else:
        body = re.sub(r"^---[ \t]*\n[\s\S]*?\n---[ \t]*\n", "", raw)
        pieces = []
        for block in re.split(r"\n\s*\n", body):
            lines = [
                ln
                for ln in block.split("\n")
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
            if lines and all(re.match(r"^\s*[-*]\s+", ln) for ln in lines):
                pieces += lines
            elif lines:
                pieces.append(" ".join(lines))
    out = []
    for piece in pieces:
        t = re.sub(r"^[-*]\s+", "", piece.strip())
        noted = ""
        d = re.match(r"^\[(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4})\]\s*", t)
        if d:
            noted = d.group(1)
            t = t[d.end() :]
        c = re.match(r"^\[([A-Za-z][A-Za-z/ _-]{1,30})\]\s*", t)
        if c:
            t = t[c.end() :]
        t = _clean(t)
        if t and not re.match(
            r"^(stonic|ghost) memory system initiali[sz]ed\.?$", t, re.I
        ):
            out.append({"text": t, "noted": noted})
    return out


def _read_text(file: Optional[Path]) -> str:
    try:
        return (
            Path(file).read_text(encoding="utf-8")
            if file and Path(file).is_file()
            else ""
        )
    except (OSError, UnicodeDecodeError):
        return ""


def read_extras(sources: Dict[str, Optional[Path]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    def add(entries: Iterable[Dict[str, str]], label: str) -> None:
        out.extend({**e, "source": label} for e in entries)

    if sources.get("user_md"):
        add(parse_entries(_read_text(sources["user_md"])), "Ghost's notes about you")
    if sources.get("memory_md"):
        add(parse_entries(_read_text(sources["memory_md"])), "Ghost's memory file")
    facts = sources.get("facts_jsonl")
    if facts:
        entries = []
        for line in _read_text(facts).splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("trust") == "untrusted":
                continue
            text = _clean(row.get("text"))
            if text:
                entries.append(
                    {
                        "text": text,
                        "noted": _date_of((row.get("created_at") or 0) * 1000),
                    }
                )
        add(entries, "Ghost's saved facts")
    return out


# -- the plan ------------------------------------------------------------------------


def plan_migration(
    legacy: Dict[str, List[Dict[str, Any]]],
    extras: List[Dict[str, str]],
    taken: Set[str],
    now: float,
) -> Dict[str, Any]:
    """The whole plan — no file is written, so it is easy to test."""
    notes: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    counts = {
        "memories": 0,
        "follow_ups": 0,
        "conversation_notes": 0,
        "extras": 0,
        "duplicates_skipped": 0,
    }

    # 1. Active memories — one note each. Important ones (pin, identity,
    #    instruction) get today's time so they sit at the top of the index.
    for row in legacy.get("memories") or []:
        original = _clean(row.get("text_original"))
        english = _clean(row.get("text_en"))
        main = english or original
        if not main:
            continue
        key = _same_key(main)
        if key in seen:
            counts["duplicates_skipped"] += 1
            continue
        seen.add(key)
        if original:
            seen.add(_same_key(original))
        base = (
            slug_from(english)
            or slug_from(original)
            or f"{_PREFIX_BY_TYPE.get(row.get('type') or '', 'note')}-{_short_hash(row.get('uid') or main)}"
        )
        rel = _unique_rel(base, taken)
        meta = [f"Kind: {row.get('type') or 'note'}"]
        if row.get("pinned"):
            meta.append("marked important")
        scope = row.get("scope")
        if isinstance(scope, str) and scope.startswith("character:"):
            meta.append(
                f"told to {character_label(scope[len('character:') :]) or 'one character'}"
            )
        noted = _date_of(float(row.get("mentioned_at") or row.get("recorded_at") or 0))
        lines = [
            _header(rel[:-3], main, _OFFICIAL_TYPE.get(row.get("type") or "", "user")),
            original or english,
        ]
        if english and original and english != original:
            lines += ["", f"In English: {english}"]
        lines += [
            "",
            f"From Ghost's earlier memory{f' — first noted {noted}' if noted else ''}. {' · '.join(meta)}.",
        ]
        important = bool(row.get("pinned")) or row.get("type") in (
            "identity",
            "instruction",
        )
        when = float(row.get("updated_at") or row.get("recorded_at") or now)
        notes.append(
            {
                "rel": rel,
                "text": "\n".join(lines),
                "mtime_ms": now if important else min(when, now),
            }
        )
        counts["memories"] += 1

    # 2. Open promises — one note each, with the time.
    for row in legacy.get("intentions") or []:
        what = _clean(row.get("what_en")) or _clean(row.get("what"))
        if not what:
            continue
        due = _date_time_of(float(row.get("due_ts") or 0))
        base = f"follow-up-{slug_from(_clean(row.get('what_en'))) or slug_from(_clean(row.get('what'))) or _short_hash(row.get('uid') or what)}"
        rel = _unique_rel(base[:60].rstrip("-"), taken)
        lines = [
            _header(
                rel[:-3],
                f"Follow-up: {what}{f' — due {due}' if due else ''}",
                "project",
            ),
            _clean(row.get("what")) or what,
        ]
        if (
            _clean(row.get("what_en"))
            and _clean(row.get("what"))
            and _clean(row.get("what_en")) != _clean(row.get("what"))
        ):
            lines += ["", f"In English: {_clean(row.get('what_en'))}"]
        by = character_label(row.get("character_id"))
        lines += [
            "",
            f"Status: open. {f'Due: {due}.' if due else 'No time was set.'}{f' Promised by {by}.' if by else ''} "
            f"Noted {_date_of(float(row.get('created_at') or 0)) or 'earlier'}.",
        ]
        notes.append({"rel": rel, "text": "\n".join(lines), "mtime_ms": now})
        counts["follow_ups"] += 1

    # 3. Past conversation summaries — one note per week (split when big).
    weeks: Dict[str, Dict[str, Any]] = {}
    for row in legacy.get("episodes") or []:
        start = float(row.get("start_ts") or 0)
        if start <= 0:
            continue
        d = datetime.fromtimestamp(start / 1000)
        monday = datetime(d.year, d.month, d.day) - timedelta(days=d.weekday())
        key = monday.strftime("%Y-%m-%d")
        weeks.setdefault(key, {"monday": monday, "rows": []})["rows"].append(row)
    for key in sorted(weeks):
        week = weeks[key]
        mon = week["monday"]
        sun = mon + timedelta(days=6)
        rng = (
            f"{mon.day}–{sun.day} {_MONTHS[sun.month - 1]} {sun.year}"
            if mon.month == sun.month
            else f"{mon.day} {_MONTHS[mon.month - 1]} – {sun.day} {_MONTHS[sun.month - 1]} {sun.year}"
        )
        parts: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        size = 0
        for row in week["rows"]:
            who = character_label(row.get("character_id"))
            block = (
                f"## {_date_time_of(float(row['start_ts']))} — {_clean(row.get('title')) or 'Conversation'}{f' (with {who})' if who else ''}\n"
                f"{_clean(row.get('summary_en')) or '(no summary)'}\n"
            )
            if size + len(block) > PART_CHARS and current:
                parts.append(current)
                current = []
                size = 0
            current.append({"block": block, "row": row})
            size += len(block)
        if current:
            parts.append(current)
        for i, part in enumerate(parts):
            titles = [t for t in (_clean(p["row"].get("title")) for p in part) if t]
            label = f" (part {i + 1} of {len(parts)})" if len(parts) > 1 else ""
            description = (
                f"Summaries of past conversations, {rng}{label}: {'; '.join(titles)}"
            )
            rel = _unique_rel(
                f"past-conversations-{key}{f'-part-{i + 1}' if len(parts) > 1 else ''}",
                taken,
            )
            last_ts = max(float(p["row"].get("start_ts") or 0) for p in part)
            text = _header(rel[:-3], description, "project") + "\n".join(
                p["block"] for p in part
            )
            notes.append(
                {"rel": rel, "text": text, "mtime_ms": min(last_ts or now, now)}
            )
            counts["conversation_notes"] += 1

    # 4. Ghost's own files and v1 facts — whatever Memory Core does not already have.
    for entry in extras or []:
        key = _same_key(entry["text"])
        if not key or key in seen:
            counts["duplicates_skipped"] += 1
            continue
        seen.add(key)
        rel = _unique_rel(
            slug_from(entry["text"]) or f"note-{_short_hash(entry['text'])}", taken
        )
        text = "\n".join(
            [
                _header(rel[:-3], entry["text"]),
                entry["text"],
                "",
                "From "
                + entry["source"]
                + (f" (noted {entry['noted']})" if entry.get("noted") else "")
                + ".",
            ]
        )
        notes.append({"rel": rel, "text": text, "mtime_ms": now - 1000})
        counts["extras"] += 1
    return {"notes": notes, "counts": counts}


# -- backup and run -------------------------------------------------------------------------


def _copy_if_exists(src: Optional[Path], dst: Path) -> bool:
    try:
        if src and Path(src).is_file():
            shutil.copyfile(src, dst)
            return True
    except OSError:
        pass
    return False


def make_backup(
    notes_dir: Path, sources: Dict[str, Optional[Path]], now: float
) -> Dict[str, Any]:
    folder = (
        Path(notes_dir)
        / BACKUP_DIR
        / datetime.fromtimestamp(now / 1000).strftime("%Y%m%d-%H%M%S")
    )
    folder.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    core = sources.get("core_db")
    if core and _copy_if_exists(core, folder / "memory.db"):
        copied.append("memory.db")
        for suffix in ("-wal", "-shm"):
            _copy_if_exists(Path(f"{core}{suffix}"), folder / f"memory.db{suffix}")
    for key, name in (
        ("memory_md", "ghost-MEMORY.md"),
        ("user_md", "ghost-USER.md"),
        ("facts_jsonl", "memory_facts.jsonl"),
    ):
        if _copy_if_exists(sources.get(key), folder / name):
            copied.append(name)
    (folder / "README.txt").write_text(
        "\r\n".join(
            [
                f"Copy of Ghost's earlier memory, made on {_date_time_of(now)} when memory moved to this folder.",
                "Nothing here is used by the notes folder; it is kept only as a safety copy.",
                "",
                'memory.db holds every earlier conversation word for word (table "turns").',
                f"Copied: {', '.join(copied) if copied else 'nothing — there was no earlier memory'}.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"dir": folder, "copied": copied}


def _read_core_from_backup(backup: Path) -> Dict[str, List[Dict[str, Any]]]:
    source = Path(backup) / "memory.db"
    if not source.exists():
        return {"memories": [], "intentions": [], "episodes": []}
    work = Path(tempfile.mkdtemp(prefix="ghost-memory-move-"))
    try:
        db = work / "memory.db"
        shutil.copyfile(source, db)
        for suffix in ("-wal", "-shm"):
            _copy_if_exists(Path(f"{source}{suffix}"), Path(f"{db}{suffix}"))
        return read_core_db(db)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _read_marker(file: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_marker(file: Path, data: Dict[str, Any]) -> None:
    try:
        Path(file).write_text(
            json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def run_migration(
    store: FileMemory,
    sources: Dict[str, Optional[Path]],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    import time

    now = now or time.time() * 1000
    marker_file = store.dir / MARKER_FILE
    previous = _read_marker(marker_file)
    if previous and previous.get("done"):
        return {"ok": True, "skipped": True, "marker": previous}
    attempts = int((previous or {}).get("attempts") or 0) + 1
    backup = (previous or {}).get("backup") or ""
    copied = (previous or {}).get("copied") or []
    if not backup or not Path(backup).exists():
        made = make_backup(store.dir, sources, now)
        backup, copied = str(made["dir"]), made["copied"]
    legacy: Dict[str, List[Dict[str, Any]]] = {
        "memories": [],
        "intentions": [],
        "episodes": [],
    }
    legacy_error = ""
    try:
        legacy = _read_core_from_backup(Path(backup))
    except (OSError, sqlite3.Error) as exc:
        legacy_error = str(exc)[:200]
        if attempts < MAX_ATTEMPTS:
            _write_marker(
                marker_file,
                {
                    "version": MIGRATION_VERSION,
                    "done": False,
                    "attempts": attempts,
                    "backup": backup,
                    "copied": copied,
                    "error": legacy_error,
                },
            )
            logger.warning(
                "[FileMemory] older database did not open (%s) — retry next time (%d/%d)",
                legacy_error,
                attempts,
                MAX_ATTEMPTS,
            )
            return {"ok": False, "retry": True, "error": legacy_error}
        logger.error(
            "[FileMemory] older database did not open %d times — other sources moved, the database is in the backup",
            MAX_ATTEMPTS,
        )
    taken: Set[str] = set()
    for note in store.scan():
        origin = ""
        try:
            parsed = parse_note(
                store.dir.joinpath(*note["rel"].split("/")).read_text(encoding="utf-8")
            )
            origin = (
                parsed["metadata"].get("origin") or parsed["fields"].get("origin") or ""
            )
        except (OSError, UnicodeDecodeError):
            pass
        if origin != ORIGIN:
            taken.add(note["rel"].lower())
    plan = plan_migration(legacy, read_extras(sources), taken, now)
    result = (
        store.bulk_write(plan["notes"])
        if plan["notes"]
        else {"written": 0, "failed": []}
    )
    marker = {
        "version": MIGRATION_VERSION, "done": True, "at": datetime.fromtimestamp(now / 1000).isoformat(), "attempts": attempts,
        "backup": backup, "copied": copied,
        "counts": {**plan["counts"], "notes_written": result["written"], "notes_failed": len(result["failed"])},
    }  # fmt: skip
    if legacy_error:
        marker["legacy_error"] = legacy_error
    if result["failed"]:
        marker["failed"] = result["failed"][:20]
    _write_marker(marker_file, marker)
    logger.info(
        "[FileMemory] migration done: %d notes (%s)", result["written"], plan["counts"]
    )
    return {"ok": True, "marker": marker}


__all__ = [
    "BACKUP_DIR",
    "MARKER_FILE",
    "ORIGIN",
    "make_backup",
    "parse_entries",
    "plan_migration",
    "read_core_db",
    "read_extras",
    "run_migration",
    "slug_from",
]
