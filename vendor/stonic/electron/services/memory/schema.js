// Memory Core ka dhaancha (schema) aur us ki migrations.
//
// ════════════════════════════════════════════════════════════════════════════
//  SAB SE AHEM USOOL: MIGRATIONS SIRF JORTI HAIN, KABHI TORTI NAHI
// ════════════════════════════════════════════════════════════════════════════
//
//  Ye file user ki saalon ki yaadash ka dhaancha hai. App update hogi, ye file
//  wohi rahegi — is liye:
//
//   1. `MIGRATIONS` ki koi purani entry KABHI badli nahi jati. Naya kaam
//      hamesha list ke AAKHIR mein nayi entry ban kar aata hai.
//   2. Koi migration column ya table hatati ya naam nahi badalti. Sirf nayi
//      table, naya column (default ke saath), naya index.
//
//  Doosre usool ka faida: agar user kabhi PURANA version chala le (downgrade),
//  to purana code naye dhaanche par bhi chalta hai — us ke jaane hue columns
//  sab mojood hain. Isi liye `migrate()` naye `user_version` par rukta nahi.
//
//  Har migration ek transaction mein chalti hai: ya poori lagti hai ya bilkul
//  nahi. Migration se pehle `db.js` file ki ek copy bana leta hai.
//
// ── ASAL RECORD aur NICHOR ──────────────────────────────────────────────────
//
//  `turns` asal record hai: har jumla lafz-ba-lafz, kabhi nahi badalta (sirf
//  user ke "poori tarah mita do" par redact hota hai). Baqi sab — episodes,
//  memories, intentions — us ka nichor hain aur dobara ban sakte hain.
//
// ── TALASH KE TEEN INDEX ────────────────────────────────────────────────────
//
//  Har talash-wali table ke do column hain jo `normalize.js` bharta hai:
//    fold -> spelling ke farq hata kar ("jaana" = "jana")
//    skel -> sirf consonants, Latin mein ("jana" = "جانا" = "jn")
//  aur un par teen FTS5 index:
//    *_uni  (fold, poore lafz)      -> sab se bharose-mand
//    *_tri  (fold, 3-3 huroof)      -> lafz ke tukre, adhoori spelling
//    *_skel (skel, poore lafz)      -> doosri script ka pul; kamzor ishara
//
//  FTS index "external content" hain: asal text sirf ek jagah (table mein)
//  hai, index sirf ishare rakhta hai. Triggers dono ko saath rakhte hain.

/** FTS5 ke teeno index aur un ke triggers — ek table ke liye. */
function ftsBlock(table, { tri = true } = {}) {
  const parts = [
    `create virtual table ${table}_uni using fts5(fold, content='${table}', content_rowid='id', tokenize='unicode61 remove_diacritics 2');`,
    `create virtual table ${table}_skel using fts5(skel, content='${table}', content_rowid='id', tokenize='unicode61');`,
  ];
  if (tri) {
    parts.push(`create virtual table ${table}_tri using fts5(fold, content='${table}', content_rowid='id', tokenize='trigram');`);
  }
  const ins = (row) => [
    `insert into ${table}_uni(rowid, fold) values (${row}.id, ${row}.fold);`,
    `insert into ${table}_skel(rowid, skel) values (${row}.id, ${row}.skel);`,
    tri ? `insert into ${table}_tri(rowid, fold) values (${row}.id, ${row}.fold);` : '',
  ].join('\n      ');
  const del = (row) => [
    `insert into ${table}_uni(${table}_uni, rowid, fold) values ('delete', ${row}.id, ${row}.fold);`,
    `insert into ${table}_skel(${table}_skel, rowid, skel) values ('delete', ${row}.id, ${row}.skel);`,
    tri ? `insert into ${table}_tri(${table}_tri, rowid, fold) values ('delete', ${row}.id, ${row}.fold);` : '',
  ].join('\n      ');
  parts.push(`
    create trigger ${table}_fts_ai after insert on ${table} begin
      ${ins('new')}
    end;
    create trigger ${table}_fts_ad after delete on ${table} begin
      ${del('old')}
    end;
    create trigger ${table}_fts_au after update of fold, skel on ${table} begin
      ${del('old')}
      ${ins('new')}
    end;`);
  return parts.join('\n');
}

const V1 = `
  -- Chhoti settings aur nishan (pause, aakhri backup, import ho chuka ya nahi).
  create table meta (
    key text primary key,
    value text not null
  );

  -- Ek baat-cheet. id = '<channel>:<bahar ki id>' (voice ka chatId, Hermes ka session_id).
  create table sessions (
    id text primary key,
    channel text not null,
    character_id text,
    title text,
    started_at integer not null,
    last_turn_at integer not null,
    turn_count integer not null default 0,
    -- turns.id jahan tak nichor nikal chuka. Is se aage ke turns "kache" hain.
    digested_upto integer not null default 0,
    digest_attempts integer not null default 0,
    -- 1 = is session mein bahar ka content (web/document/email) parha gaya.
    external_content integer not null default 0,
    ended_at integer
  );
  create index sessions_last on sessions(last_turn_at);

  -- ASAL RECORD. Lafz-ba-lafz. Kabhi update nahi hota (siwaye redact ke).
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
    -- Bahar ki message id. Wohi turn do dafa aaye to dobara nahi likha jata.
    source_ref text,
    flags integer not null default 0,
    created_at integer not null
  );
  create unique index turns_source on turns(channel, source_ref) where source_ref is not null;
  create index turns_session on turns(session_id, id);
  create index turns_ts on turns(ts);
  ${ftsBlock('turns')}

  -- "Kya hua tha": ek mauzoo par baat-cheet ka tukra, chhote khulase ke saath.
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
  ${ftsBlock('episodes', { tri: false })}

  -- "Kya sach hai": facts, pasand, rishte. Mit-ta nahi, mansookh hota hai.
  create table memories (
    id integer primary key,
    uid text not null unique,
    type text not null,
    text_original text not null,
    text_en text not null default '',
    fold text not null,
    skel text not null,
    origin text not null,
    -- 1 = user ne khud kaha · 2 = user ki baaton se andaza · 3 = bahar ka content
    trust_tier integer not null default 1,
    confidence real not null default 1.0,
    evidence_count integer not null default 1,
    session_count integer not null default 1,
    -- 'user' = sab characters ke liye · 'character:<id>' = sirf us character ke liye
    scope text not null default 'user',
    sensitivity text not null default 'normal',
    status text not null default 'active',
    pinned integer not null default 0,
    -- JSON array: wo lafz (fold shuda) jin ke sunte hi ye yaad saamne aaye.
    triggers text not null default '[]',
    -- Baat asal mein kab hui / hogi (event time) ...
    event_start integer,
    event_end integer,
    -- ... aur kab kahi gayi, kab likhi gayi (do-waqt wala model).
    mentioned_at integer not null,
    recorded_at integer not null,
    valid_from integer,
    valid_to integer,
    superseded_by integer references memories(id),
    sal_explicit real not null default 0,
    sal_future real not null default 0,
    sal_emotion real not null default 0,
    sal_surprise real not null default 0,
    -- Sirf tab barhta hai jab yaad waqai kaam aaye, sirf nikalne par nahi.
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
  ${ftsBlock('memories')}

  -- Har yaad ka saboot: kis turn ke kis jumle se bani.
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

  -- Log, jagahein, projects — aur un ke doosre naam (Ammi = Mother = امی).
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

  -- "Aage kya karna hai": waade aur follow-ups. Cue code check karta hai.
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

  -- Character ki taraf ka kacha record: us ne kya socha, kaha, chup raha, aur
  -- us par kya jawab aaya. Sirf append. Ye kabhi user ka "fact" nahi banta.
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

  -- Har tabdeeli ka hisaab. Sirf append. Is se har cheez wapas ki ja sakti hai.
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

  -- Kaun si yaad kab nikli, aur kaam aayi ya nahi.
  create table access_log (
    id integer primary key,
    memory_id integer not null references memories(id) on delete cascade,
    ts integer not null,
    mind text not null,
    useful integer not null default 0
  );
  create index access_memory on access_log(memory_id, ts);

  -- "Bhool jao" ke baad baat wapas na aaye: us ka hash yahan rehta hai aur
  -- agla nichor usay dobara nahi seekh sakta.
  create table tombstones (
    content_hash text primary key,
    created_at integer not null,
    note text
  );

  -- Background kaam ki qataar. Restart ke baad wahin se uthta hai.
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
`;

/**
 * Tarteeb-war migrations. index 0 = version 1.
 * ⚠ Upar ka usool: purani entry kabhi mat badlo, sirf aakhir mein nayi joro.
 */
const MIGRATIONS = [V1];

const SCHEMA_VERSION = MIGRATIONS.length;

/**
 * Database ko maujooda version tak laata hai.
 *
 * @returns {{ from: number, to: number, applied: number }}
 */
function migrate(db) {
  const from = Number(db.pragma('user_version', { simple: true })) || 0;
  let applied = 0;
  for (let v = from; v < MIGRATIONS.length; v += 1) {
    const sql = MIGRATIONS[v];
    // exec + user_version ek hi transaction mein: adhoori migration ka koi
    // nishan nahi rehta, agli dafa wahin se dobara shuru hoti hai.
    db.transaction(() => {
      db.exec(sql);
      db.pragma(`user_version = ${v + 1}`);
    })();
    applied += 1;
  }
  const to = Number(db.pragma('user_version', { simple: true })) || 0;
  return { from, to, applied };
}

module.exports = { MIGRATIONS, SCHEMA_VERSION, migrate };
