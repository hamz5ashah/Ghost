"""Sleep: looking after memory while the PC is idle (Stonic port, phase 6).

Ported from Stonic's ``memory/consolidate.js``. Sleep is CODE ONLY — no AI.
Stonic's research found the most dangerous thing is letting an LLM "clean up"
a whole memory: in one test a model shrank 18,000 tokens of memory to 122 in a
single step. So no model rewrites anything here.

Every task is a rule, and each is harmless (nothing is erased):

* promises past their expiry -> ``expired``
* guesses with evidence from enough separate sessions -> active
* backups (and now and then, opening one to check it)
* index optimisation, pruning old finished jobs
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from ghost.memory.v2.config import DAY, DIGEST
from ghost.memory.v2.db import backup_due, list_backups, quick_check, verify_backup
from ghost.memory.v2.jobs import JobQueue
from ghost.memory.v2.store import MemoryStore

_FTS_TABLES = (
    "turns_uni", "turns_tri", "turns_skel", "memories_uni", "memories_tri", "memories_skel", "episodes_uni", "episodes_skel",
)  # fmt: skip


def run_maintenance(
    store: MemoryStore,
    jobs: Optional[JobQueue],
    base_dir: Optional[Path],
    now: Optional[int] = None,
) -> Dict[str, Any]:
    now = now or store.now()
    out: Dict[str, Any] = {
        "expired": 0,
        "promoted": 0,
        "backup_wanted": False,
        "backup_check": None,
        "pruned_jobs": 0,
        "healthy": True,
    }
    out["expired"] = store.expire_intentions(now)
    ready = store._all(
        "select id from memories where deleted = 0 and status = 'candidate' and session_count >= ?",
        (DIGEST["promote_after_sessions"],),
    )
    for row in ready:
        store._run(
            "update memories set status = 'active', confidence = max(confidence, 0.9), updated_at = ? where id = ?",
            (now, row["id"]),
        )
        store.audit(
            "system",
            "promote",
            "memory",
            row["id"],
            {"status": "candidate"},
            {"status": "active"},
            "maintenance",
        )
        out["promoted"] += 1
    out["healthy"] = quick_check(store.db)
    # A damaged file is never backed up — the rotation would replace good
    # backups with bad ones. The service makes the backup when asked here.
    out["backup_wanted"] = bool(
        out["healthy"] and base_dir and backup_due(base_dir, now)
    )
    # Once a month, really open the newest backup.
    last_check = int(store.get_meta("last_backup_check_at", "0") or 0)
    if base_dir and now - last_check >= 30 * DAY:
        latest = next(iter(list_backups(base_dir)), None)
        if latest:
            out["backup_check"] = verify_backup(latest.file)
            store.set_meta("last_backup_check_at", now)
            store.set_meta(
                "last_backup_check_ok", "1" if out["backup_check"].get("ok") else "0"
            )
    out["pruned_jobs"] = jobs.prune(now - 45 * DAY) if jobs else 0
    try:
        for t in _FTS_TABLES:
            store.db.execute(f"insert into {t}({t}) values ('optimize')")
        store.db.execute("pragma wal_checkpoint(PASSIVE)")
    except sqlite3.Error:
        pass  # next time
    store.set_meta("last_maintenance_at", now)
    return out


__all__ = ["run_maintenance"]
