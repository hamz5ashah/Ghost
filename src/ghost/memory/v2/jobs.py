"""Background work queue — in the same database's ``jobs`` table (Stonic port).

Ported from Stonic's ``memory/jobs.js``. The digest runs 15 minutes after a
conversation ends; if the app closes, the PC sleeps or the power goes in that
time, an in-RAM queue loses the work forever. Here it is on disk and resumes.

Three safeguards:

* ``idempotency_key`` — the same work is never queued twice (and never re-run
  once done). The key includes the prompt version, so a new prompt can
  re-digest the same conversation.
* lease — whoever claims a job owns it for a while; if it crashes, the job is
  claimable again after the lease.
* attempts — a job that keeps failing goes ``dead`` instead of burning a
  model call at every launch.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence

from ghost.memory.v2.config import DIGEST, JOBS
from ghost.memory.v2.store import MemoryStore


class JobQueue:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.db = store.db

    def enqueue(
        self, kind: str, payload: Dict[str, Any], key: str, *, run_after: int = 0
    ) -> Dict[str, Any]:
        now = self.store.now()
        existing = self.store._one(
            "select id, state from jobs where idempotency_key = ?", (key,)
        )
        if existing:
            # Already queued and the new time is LATER (the talk resumed): wait longer.
            if existing["state"] == "queued" and run_after:
                self.store._run(
                    "update jobs set run_after = max(run_after, ?) where id = ?",
                    (run_after, existing["id"]),
                )
            return {
                "id": int(existing["id"]),
                "created": False,
                "state": existing["state"],
            }
        cur = self.store._run(
            "insert into jobs(kind, payload, idempotency_key, state, run_after, created_at) values (?, ?, ?, 'queued', ?, ?)",
            (kind, json.dumps(payload or {}, ensure_ascii=False), key, run_after, now),
        )
        return {"id": int(cur.lastrowid), "created": True, "state": "queued"}

    def claim(self, kinds: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
        """The next ready job (or one whose owner crashed)."""
        now = self.store.now()
        kinds = list(kinds or [])
        flt = f"and kind in ({','.join('?' * len(kinds))})" if kinds else ""
        with self.db.tx():
            row = self.store._one(
                "select * from jobs where ((state = 'queued' and run_after <= ?) or (state = 'running' and leased_until <= ?)) "
                f"{flt} order by run_after, id limit 1",
                (now, now, *kinds),
            )
            if not row:
                return None
            self.store._run(
                "update jobs set state = 'running', leased_until = ?, attempts = attempts + 1 where id = ?",
                (now + JOBS["lease_ms"], row["id"]),
            )
        try:
            payload = json.loads(row["payload"]) or {}
        except (ValueError, TypeError):
            payload = {}
        return {
            "id": int(row["id"]),
            "kind": row["kind"],
            "payload": payload,
            "attempts": int(row["attempts"]) + 1,
        }

    def complete(self, job_id: int, *, cost_usd: float = 0.0) -> None:
        self.store._run(
            "update jobs set state = 'done', finished_at = ?, cost_usd = cost_usd + ?, error = null, leased_until = 0 where id = ?",
            (self.store.now(), float(cost_usd or 0), job_id),
        )

    def fail(
        self,
        job_id: int,
        error: object,
        *,
        max_attempts: int = DIGEST["max_attempts"],
        cost_usd: float = 0.0,
        retry_in_ms: Optional[int] = None,
    ) -> str:
        """Failed: retry later (doubling the wait) while attempts remain, else ``dead``."""
        row = self.store._one("select attempts from jobs where id = ?", (job_id,))
        if not row:
            return "dead"
        attempts = int(row["attempts"])
        dead = attempts >= max_attempts
        wait = (
            retry_in_ms
            if retry_in_ms is not None
            else JOBS["retry_base_ms"] * (2 ** max(0, attempts - 1))
        )
        now = self.store.now()
        self.store._run(
            "update jobs set state = ?, run_after = ?, leased_until = 0, error = ?, cost_usd = cost_usd + ?, finished_at = ? where id = ?",
            (
                "dead" if dead else "queued",
                now + wait,
                str(error or "")[:500],
                float(cost_usd or 0),
                now if dead else None,
                job_id,
            ),
        )
        return "dead" if dead else "queued"

    def defer(self, job_id: int, wait_ms: int) -> None:
        """Back in the queue without counting an attempt (a cap was hit, or voice is busy)."""
        self.store._run(
            "update jobs set state = 'queued', run_after = ?, leased_until = 0, attempts = max(0, attempts - 1) where id = ?",
            (self.store.now() + wait_ms, job_id),
        )

    def spent_since(self, since: int) -> float:
        row = self.store._one(
            "select coalesce(sum(cost_usd), 0) as s from jobs where created_at >= ?",
            (since,),
        )
        return float(row["s"] or 0) if row else 0.0

    def ran_since(self, since: int, kind: str) -> int:
        row = self.store._one(
            "select count(*) as n from jobs where kind = ? and attempts > 0 and created_at >= ?",
            (kind, since),
        )
        return int(row["n"] or 0) if row else 0

    def counts(self) -> Dict[str, int]:
        out = {"queued": 0, "running": 0, "done": 0, "dead": 0}
        for r in self.store._all(
            "select state, count(*) as n from jobs group by state"
        ):
            out[r["state"]] = int(r["n"])
        return out

    def prune(self, older_than: int) -> int:
        """Drop old finished jobs — otherwise the table grows for years for nothing."""
        cur = self.store._run(
            "delete from jobs where state in ('done', 'dead') and finished_at is not null and finished_at < ?",
            (older_than,),
        )
        return int(cur.rowcount or 0)


__all__ = ["JobQueue"]
