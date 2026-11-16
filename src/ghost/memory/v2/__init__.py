"""Memory v2 (Stonic port, phase 6) — behind ``[stonic].memory_v2``, off by default.

Two layers, ported from Stonic:

* **Memory Core** (``service.py`` and friends, from ``memory/*``): a local
  SQLite file with the verbatim conversation record, distilled memories
  (bitemporal, with evidence, entities and trust tiers), episodes, promises
  (intentions), the character's own record, an append-only audit trail,
  tombstones for "forget", and a durable job queue. Search is local and
  model-free (three FTS5 indexes over Urdu / Roman Urdu / Hindi / English
  normalisation); a Recall Agent answers by meaning with Ghost's own model;
  the digest distils conversations after they go quiet, and code — not the
  model — decides what is written.
* **File memory** (``files/``, from ``file-memory/*``): Claude's official
  file-based memory — one note per fact plus a ``MEMORY.md`` index, driven by
  Anthropic's memory tool commands and a Claude Code-style grep.

``bridge.py`` connects them to Ghost without changing ``ghost.memory``'s
interface: a ``FactStore`` backend that dual-writes the v1 JSONL, a one-time
forward migration with a backup, a rollback, chat turns from the event bus,
and awareness' memory card and promises.
"""

from __future__ import annotations

__all__: list = []
