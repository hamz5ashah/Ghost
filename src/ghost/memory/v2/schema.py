"""Memory Core's schema and its migrations (Stonic port, phase 6).

Ported from Stonic's ``memory/schema.js``; the V1 DDL is Stonic's, so a
Stonic ``memory.db`` opens here unchanged.

THE RULE: migrations only ADD, never break.

1. An entry in ``MIGRATIONS`` is never edited. New work is a new entry at the end.
2. No migration drops or renames a column or table — only new tables, new
   columns (with defaults) and new indexes.

So an older Ghost can still open a newer file (every column it knows is
there) — that is the rollback path for the schema. Each migration runs in one
transaction, and ``db.open_memory_db`` copies the file before migrating.

``turns`` is the record: every sentence verbatim, never changed (only redacted
on a full "forget"). Everything else — episodes, memories, intentions — is
distilled from it and can be rebuilt.

Search uses two normalised columns (``normalize.py``) and three FTS5 indexes:
``*_uni`` (fold, whole words — most reliable), ``*_tri`` (fold, trigrams —
fragments and partial spellings), ``*_skel`` (consonant skeleton — the bridge
across scripts, a weak signal). The indexes are external-content: the text
lives once, in the table; triggers keep them in step.
"""

from __future__ import annotations

import sqlite3
from typing import Dict

V1 = """
  -- Small settings and markers (pause, last backup, imported or not).
  create table meta (
    key text primary key,
    value text not null
  );

  -- One conversation. id = '<channel>:<outside id>'.
  create table sessions (
    id text primary key,
    channel text not null,
    character_id text,
    title text,
    started_at integer not null,
    last_turn_at integer not null,
    turn_count integer not null default 0,
    -- turns.id up to which the digest has run; later turns are "raw".
    digested_upto integer not null default 0,
    digest_attempts integer not null default 0,
    -- 1 = outside content (web/document/email) was read in this session.
    external_content integer not null default 0,
    ended_at integer
  );
  create index sessions_last on sessions(last_turn_at);

  -- THE RECORD. Verbatim. Never updated (except redaction).
  create table turns (
    id integer primary key,
    session_id text not null references sessions(id),
    ts integer not null,
    speaker text not null,
    channel text not null,
    character_id text,
    text text not null,
    fold text not null,
    skel text not null,
    script text,
    -- Outside message id: the same turn twice is written once.
    source_ref text,
    flags integer not null default 0,
    created_at integer not null
  );
  create unique index turns_source on turns(channel, source_ref) where source_ref is not null;
  create index turns_session on turns(session_id, id);
  create index turns_ts on turns(ts);
  create virtual table turns_uni using fts5(fold, content='turns', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
create virtual table turns_skel using fts5(skel, content='turns', content_rowid='id', tokenize='unicode61');
create virtual table turns_tri using fts5(fold, content='turns', content_rowid='id', tokenize='trigram');

    create trigger turns_fts_ai after insert on turns begin
      insert into turns_uni(rowid, fold) values (new.id, new.fold);
      insert into turns_skel(rowid, skel) values (new.id, new.skel);
      insert into turns_tri(rowid, fold) values (new.id, new.fold);
    end;
    create trigger turns_fts_ad after delete on turns begin
      insert into turns_uni(turns_uni, rowid, fold) values ('delete', old.id, old.fold);
      insert into turns_skel(turns_skel, rowid, skel) values ('delete', old.id, old.skel);
      insert into turns_tri(turns_tri, rowid, fold) values ('delete', old.id, old.fold);
    end;
    create trigger turns_fts_au after update of fold, skel on turns begin
      insert into turns_uni(turns_uni, rowid, fold) values ('delete', old.id, old.fold);
      insert into turns_skel(turns_skel, rowid, skel) values ('delete', old.id, old.skel);
      insert into turns_tri(turns_tri, rowid, fold) values ('delete', old.id, old.fold);
      insert into turns_uni(rowid, fold) values (new.id, new.fold);
      insert into turns_skel(rowid, skel) values (new.id, new.skel);
      insert into turns_tri(rowid, fold) values (new.id, new.fold);
    end;

  -- "What happened": a stretch of conversation on one topic, with a short summary.
  create table episodes (
    id integer primary key,
    uid text not null unique,
    session_id text not null references sessions(id),
    channel text not null,
    character_id text,
    first_turn integer not null,
    last_turn integer not null,
    start_ts integer not null,
    end_ts integer not null,
    title text not null,
    summary_en text not null default '',
    fold text not null,
    skel text not null,
    mood text,
    salience real not null default 0,
    digest_version text,
    created_at integer not null
  );
  create index episodes_session on episodes(session_id);
  create index episodes_time on episodes(start_ts);
  create virtual table episodes_uni using fts5(fold, content='episodes', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
create virtual table episodes_skel using fts5(skel, content='episodes', content_rowid='id', tokenize='unicode61');

    create trigger episodes_fts_ai after insert on episodes begin
      insert into episodes_uni(rowid, fold) values (new.id, new.fold);
      insert into episodes_skel(rowid, skel) values (new.id, new.skel);

    end;
    create trigger episodes_fts_ad after delete on episodes begin
      insert into episodes_uni(episodes_uni, rowid, fold) values ('delete', old.id, old.fold);
      insert into episodes_skel(episodes_skel, rowid, skel) values ('delete', old.id, old.skel);

    end;
    create trigger episodes_fts_au after update of fold, skel on episodes begin
      insert into episodes_uni(episodes_uni, rowid, fold) values ('delete', old.id, old.fold);
      insert into episodes_skel(episodes_skel, rowid, skel) values ('delete', old.id, old.skel);

      insert into episodes_uni(rowid, fold) values (new.id, new.fold);
      insert into episodes_skel(rowid, skel) values (new.id, new.skel);

    end;

  -- "What is true": facts, preferences, relationships. Superseded, never erased.
  create table memories (
    id integer primary key,
    uid text not null unique,
    type text not null,
    text_original text not null,
    text_en text not null default '',
    fold text not null,
    skel text not null,
    origin text not null,
    -- 1 = the user said it · 2 = inferred from the user · 3 = outside content
    trust_tier integer not null default 1,
    confidence real not null default 1.0,
    evidence_count integer not null default 1,
    session_count integer not null default 1,
    -- 'user' = all characters · 'character:<id>' = that character only
    scope text not null default 'user',
    sensitivity text not null default 'normal',
    status text not null default 'active',
    pinned integer not null default 0,
    -- JSON array: (folded) words that bring this memory up when heard.
    triggers text not null default '[]',
    -- When it actually happened / will happen (event time) ...
    event_start integer,
    event_end integer,
    -- ... and when it was said and recorded (bitemporal).
    mentioned_at integer not null,
    recorded_at integer not null,
    valid_from integer,
    valid_to integer,
    superseded_by integer references memories(id),
    sal_explicit real not null default 0,
    sal_future real not null default 0,
    sal_emotion real not null default 0,
    sal_surprise real not null default 0,
    -- Grows only when the memory was actually useful, not merely retrieved.
    useful_count integer not null default 0,
    last_used_at integer,
    content_hash text not null,
    source_session text,
    created_by text,
    prompt_version text,
    updated_at integer not null,
    deleted integer not null default 0
  );
  create index memories_status on memories(status, scope);
  create index memories_pinned on memories(pinned) where pinned = 1;
  create index memories_hash on memories(content_hash);
  create index memories_type on memories(type, status);
  create virtual table memories_uni using fts5(fold, content='memories', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
create virtual table memories_skel using fts5(skel, content='memories', content_rowid='id', tokenize='unicode61');
create virtual table memories_tri using fts5(fold, content='memories', content_rowid='id', tokenize='trigram');

    create trigger memories_fts_ai after insert on memories begin
      insert into memories_uni(rowid, fold) values (new.id, new.fold);
      insert into memories_skel(rowid, skel) values (new.id, new.skel);
      insert into memories_tri(rowid, fold) values (new.id, new.fold);
    end;
    create trigger memories_fts_ad after delete on memories begin
      insert into memories_uni(memories_uni, rowid, fold) values ('delete', old.id, old.fold);
      insert into memories_skel(memories_skel, rowid, skel) values ('delete', old.id, old.skel);
      insert into memories_tri(memories_tri, rowid, fold) values ('delete', old.id, old.fold);
    end;
    create trigger memories_fts_au after update of fold, skel on memories begin
      insert into memories_uni(memories_uni, rowid, fold) values ('delete', old.id, old.fold);
      insert into memories_skel(memories_skel, rowid, skel) values ('delete', old.id, old.skel);
      insert into memories_tri(memories_tri, rowid, fold) values ('delete', old.id, old.fold);
      insert into memories_uni(rowid, fold) values (new.id, new.fold);
      insert into memories_skel(rowid, skel) values (new.id, new.skel);
      insert into memories_tri(rowid, fold) values (new.id, new.fold);
    end;

  -- Each memory's evidence: which sentence of which turn it came from.
  create table memory_evidence (
    id integer primary key,
    memory_id integer not null references memories(id) on delete cascade,
    turn_id integer references turns(id) on delete set null,
    session_id text,
    quote text not null default '',
    created_at integer not null
  );
  create index evidence_memory on memory_evidence(memory_id);
  create index evidence_turn on memory_evidence(turn_id);

  -- People, places, projects — and their other names (Ammi = Mother = امی).
  create table entities (
    id integer primary key,
    uid text not null unique,
    canonical_name text not null,
    type text not null default 'other',
    created_at integer not null
  );
  create table entity_aliases (
    entity_id integer not null references entities(id) on delete cascade,
    alias_fold text not null,
    alias_skel text not null default '',
    primary key (entity_id, alias_fold)
  );
  create index alias_fold on entity_aliases(alias_fold);
  create index alias_skel on entity_aliases(alias_skel);
  create table memory_entities (
    memory_id integer not null references memories(id) on delete cascade,
    entity_id integer not null references entities(id) on delete cascade,
    primary key (memory_id, entity_id)
  );
  create index memory_entities_entity on memory_entities(entity_id);

  -- "What to do next": promises and follow-ups. Code checks the cue.
  create table intentions (
    id integer primary key,
    uid text not null unique,
    what text not null,
    what_en text not null default '',
    who_for text,
    cue_type text not null default 'time',
    cue_text text,
    due_ts integer,
    expiry_ts integer,
    source_turn integer references turns(id) on delete set null,
    source_session text,
    quote text not null default '',
    confidence real not null default 1.0,
    status text not null default 'open',
    created_by text,
    character_id text,
    scope text not null default 'user',
    created_at integer not null,
    resolved_at integer,
    last_surfaced_at integer,
    surfaced_count integer not null default 0,
    content_hash text not null default ''
  );
  create index intentions_open on intentions(status, due_ts);

  -- The character's raw record: what it thought, said, held back, and
  -- how it was answered. Append-only. Never becomes a user "fact".
  create table character_events (
    id integer primary key,
    ts integer not null,
    character_id text not null,
    kind text not null,
    text text not null default '',
    meta text not null default '{}',
    screen_derived integer not null default 0,
    session_id text,
    source_ref text
  );
  create index character_events_char on character_events(character_id, ts);
  create unique index character_events_source on character_events(character_id, source_ref) where source_ref is not null;

  -- Every change, append-only. Anything can be undone from here.
  create table memory_events (
    id integer primary key,
    ts integer not null,
    actor text not null,
    op text not null,
    target_kind text not null,
    target_id text,
    before text,
    after text,
    cause text
  );
  create index memory_events_target on memory_events(target_kind, target_id);

  -- Which memory was retrieved when, and whether it helped.
  create table access_log (
    id integer primary key,
    memory_id integer not null references memories(id) on delete cascade,
    ts integer not null,
    mind text not null,
    useful integer not null default 0
  );
  create index access_memory on access_log(memory_id, ts);

  -- After "forget" the thing must not come back: its hash stays here and
  -- the next digest cannot learn it again.
  create table tombstones (
    content_hash text primary key,
    created_at integer not null,
    note text
  );

  -- Background work queue. Resumes where it left off after a restart.
  create table jobs (
    id integer primary key,
    kind text not null,
    payload text not null default '{}',
    idempotency_key text not null unique,
    state text not null default 'queued',
    attempts integer not null default 0,
    run_after integer not null default 0,
    leased_until integer not null default 0,
    cost_usd real not null default 0,
    error text,
    created_at integer not null,
    finished_at integer
  );
  create index jobs_ready on jobs(state, run_after);
"""

# In order; index 0 = version 1. Never edit an old entry — append.
MIGRATIONS = [V1]
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(db: sqlite3.Connection) -> Dict[str, int]:
    """Bring the database to the current version. Returns {from, to, applied}."""
    start = int(db.execute("pragma user_version").fetchone()[0] or 0)
    applied = 0
    for version in range(start, len(MIGRATIONS)):
        # The DDL and user_version in ONE transaction: a half migration leaves
        # no trace and simply runs again next time.
        db.execute("begin immediate")
        try:
            for statement in _statements(MIGRATIONS[version]):
                db.execute(statement)
            db.execute(f"pragma user_version = {version + 1}")
            db.execute("commit")
        except BaseException:
            db.execute("rollback")
            raise
        applied += 1
    end = int(db.execute("pragma user_version").fetchone()[0] or 0)
    return {"from": start, "to": end, "applied": applied}


def _statements(sql: str):
    """Split a script into statements (triggers contain ';' inside begin…end)."""
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                yield stmt
            buf = ""
    if buf.strip():
        yield buf.strip()


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "V1", "migrate"]
