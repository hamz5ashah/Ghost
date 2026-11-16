"""Memory Core's file: opening, keeping it safe, backups (Stonic port, phase 6).

Ported from Stonic's ``memory/db.js``.

Layout (inside Ghost's config dir, outside the install folder, so updates and
reinstalls never touch it)::

    <config>/memory_v2/memory.db          (+ -wal, -shm)
    <config>/memory_v2/backups/           daily / weekly / monthly copies

The file is NOT encrypted (Stonic's 4 Oct 2026 decision, kept): a key tied to
the OS would lock the memory away forever after a reinstall, and "still
remembers in ten years" matters more. Passwords, OTPs and card numbers are
removed by ``secrets.py`` before anything is written.

Python's built-in ``sqlite3`` is the driver: no native module to build or load.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ghost.memory.v2.config import BACKUP, DAY, FILES
from ghost.memory.v2.schema import MIGRATIONS, migrate
from ghost.memory.v2.timefmt import now_ms


def db_path(base_dir: Path) -> Path:
    return Path(base_dir) / FILES["db_name"]


def backup_dir(base_dir: Path) -> Path:
    return Path(base_dir) / FILES["backup_dir_name"]


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d-%H%M%S")


class MemoryDb:
    """A sqlite3 connection shared across threads behind one re-entrant lock.

    ``with db.tx():`` runs a block in one IMMEDIATE transaction (nested blocks
    join the outer one), like better-sqlite3's ``db.transaction``.
    """

    def __init__(self, conn: sqlite3.Connection, file: Path) -> None:
        self.conn = conn
        self.file = file
        self.lock = threading.RLock()
        self._depth = 0
        self.open = True

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def all(self, sql: str, params: tuple | dict = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def tx(self) -> "_Tx":
        return _Tx(self)


class _Tx:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def __enter__(self) -> MemoryDb:
        self.db.lock.acquire()
        if self.db._depth == 0:
            self.db.conn.execute("begin immediate")
        self.db._depth += 1
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        self.db._depth -= 1
        try:
            if self.db._depth == 0:
                self.db.conn.execute("rollback" if exc_type else "commit")
        finally:
            self.db.lock.release()


@dataclass
class Opened:
    db: MemoryDb
    file: Path
    migrated: Dict[str, int]
    pre_migration_backup: Optional[Path]


def open_memory_db(base_dir: Path, *, now: Optional[int] = None) -> Opened:
    """Open the database and bring its schema up to date. Raises on failure (the service catches)."""
    now = now or now_ms()
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    backup_dir(base_dir).mkdir(parents=True, exist_ok=True)
    file = db_path(base_dir)
    existed = file.exists()
    conn = sqlite3.connect(
        str(file), isolation_level=None, check_same_thread=False, timeout=5.0
    )
    conn.row_factory = sqlite3.Row
    # WAL: readers never block the writer, and the file survives a crash.
    conn.execute("pragma journal_mode = WAL")
    # NORMAL with WAL is crash-safe (the last few writes may be lost on power
    # loss, the file is never corrupted) and much faster than FULL.
    conn.execute("pragma synchronous = NORMAL")
    conn.execute("pragma foreign_keys = ON")
    conn.execute("pragma busy_timeout = 5000")
    # A copy BEFORE the schema changes: migrations are transactional, this is
    # for the case where the new version itself is wrong.
    pre = None
    current = int(conn.execute("pragma user_version").fetchone()[0] or 0)
    if existed and 0 < current < len(MIGRATIONS):
        pre = backup_dir(base_dir) / f"pre-migrate-v{current}-{_stamp(now)}.db"
        conn.execute("vacuum into ?", (str(pre),))
    migrated = migrate(conn)
    return Opened(MemoryDb(conn, file), file, migrated, pre)


def quick_check(db: MemoryDb) -> bool:
    try:
        row = db.one("pragma quick_check")
        return bool(row) and row[0] == "ok"
    except sqlite3.Error:
        return False


def close_memory_db(db: Optional[MemoryDb]) -> None:
    if not db or not db.open:
        return
    db.open = False
    try:
        # Fold the WAL into the main file so the closed file stands alone.
        db.execute("pragma wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


# -- backups -------------------------------------------------------------------
#
# Copying the .db without its -wal can lose data, so backups use SQLite's own
# online backup, which produces one complete file from the live database.

_BACKUP_NAME = re.compile(r"^memory-(\d{8})-(\d{6})\.db$")


@dataclass
class Backup:
    name: str
    file: Path
    at: int
    size: int


def list_backups(base_dir: Path) -> List[Backup]:
    folder = backup_dir(base_dir)
    try:
        names = [p.name for p in folder.iterdir()]
    except OSError:
        return []
    out: List[Backup] = []
    for name in names:
        m = _BACKUP_NAME.match(name)
        if not m:
            continue
        at = int(
            datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
            * 1000
        )
        file = folder / name
        try:
            size = file.stat().st_size
        except OSError:
            size = 0
        out.append(Backup(name, file, at, size))
    return sorted(out, key=lambda b: -b.at)


def backup_now(
    db: MemoryDb,
    base_dir: Path,
    now: Optional[int] = None,
    mirror_dir: Optional[Path] = None,
) -> dict:
    now = now or now_ms()
    folder = backup_dir(base_dir)
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f"memory-{_stamp(now)}.db"
    if file.exists():
        file.unlink()
    target = sqlite3.connect(str(file))
    try:
        with db.lock:
            db.conn.backup(target, pages=256)
    finally:
        target.close()
    size = file.stat().st_size
    pruned = prune_backups(base_dir)
    mirrored = mirror_backup(file, mirror_dir) if mirror_dir else None
    return {
        "file": str(file),
        "size": size,
        "pruned": pruned,
        "mirrored": str(mirrored) if mirrored else None,
    }


def mirror_backup(file: Path, mirror_dir: Path, keep: int = 2) -> Optional[Path]:
    """A copy somewhere else; only the newest ``keep`` stay there. Failure is not fatal."""
    import shutil

    try:
        mirror_dir = Path(mirror_dir)
        mirror_dir.mkdir(parents=True, exist_ok=True)
        dest = mirror_dir / Path(file).name
        shutil.copyfile(file, dest)
        old = sorted(
            (p.name for p in mirror_dir.iterdir() if _BACKUP_NAME.match(p.name)),
            reverse=True,
        )[keep:]
        for name in old:
            try:
                (mirror_dir / name).unlink()
            except OSError:
                pass
        return dest
    except OSError:
        return None


def prune_backups(base_dir: Path) -> List[str]:
    """Keep one per day for N days, then one per week, then one per month. The newest always stays."""
    backups = list_backups(base_dir)
    if len(backups) <= 1:
        return []
    keep = {backups[0].name}

    def pick(key_of, count: int) -> None:
        seen: set = set()
        for b in backups:
            k = key_of(b.at)
            if k in seen:
                continue
            seen.add(k)
            keep.add(b.name)
            if len(seen) >= count:
                break

    def day_key(t: int) -> str:
        return datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d")

    def month_key(t: int) -> str:
        return datetime.fromtimestamp(t / 1000).strftime("%Y-%m")

    pick(day_key, BACKUP["keep_daily"])
    pick(lambda t: t // (7 * DAY), BACKUP["keep_weekly"])
    pick(month_key, BACKUP["keep_monthly"])
    pruned = []
    for b in backups:
        if b.name in keep:
            continue
        # pre-migrate copies have another name and are never pruned on purpose.
        try:
            b.file.unlink()
            pruned.append(b.name)
        except OSError:
            pass
    return pruned


def verify_backup(file: Path) -> dict:
    """Actually open a backup read-only: "a backup exists" and "a backup opens" differ."""
    try:
        conn = sqlite3.connect(f"file:{Path(file).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    try:
        healthy = conn.execute("pragma quick_check").fetchone()[0] == "ok"
        version = int(conn.execute("pragma user_version").fetchone()[0] or 0)
        turns = conn.execute("select count(*) from turns").fetchone()[0]
        memories = conn.execute("select count(*) from memories").fetchone()[0]
        return {"ok": healthy, "turns": turns, "memories": memories, "version": version}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def backup_due(base_dir: Path, now: Optional[int] = None) -> bool:
    now = now or now_ms()
    latest = next(iter(list_backups(base_dir)), None)
    return latest is None or now - latest.at >= BACKUP["min_interval_ms"]


__all__ = [
    "Backup",
    "MemoryDb",
    "Opened",
    "backup_dir",
    "backup_due",
    "backup_now",
    "close_memory_db",
    "db_path",
    "list_backups",
    "mirror_backup",
    "open_memory_db",
    "prune_backups",
    "quick_check",
    "verify_backup",
]
