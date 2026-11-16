"""Every Memory Core number in one place (Stonic port, phase 6).

Ported from Stonic's ``memory/config.js``. The design's *shape* came from
research; its *numbers* (tokens, weights, days) are starting guesses tuned on a
golden set. Kept in one place so tuning never means opening ten files.

The schema version lives in ``schema.py`` (``len(MIGRATIONS)``), never here.
"""

from __future__ import annotations

MIN = 60 * 1000
HOUR = 60 * MIN
DAY = 24 * HOUR

FILES = {"dir_name": "memory_v2", "db_name": "memory.db", "backup_dir_name": "backups"}

# -- write limits --------------------------------------------------------------
LIMITS = {
    # One turn this long at most; longer is cut and flagged.
    "turn_max_chars": 8000,
    # A safety fuse, not a style limit: one memory is one topic. Over the
    # limit the write is REFUSED with a clear reason, never silently cut.
    "memory_max_chars": 2000,
    # `remember` per session (voice start to stop) — a loop fuse.
    "remember_per_session": 30,
    "follow_ups_per_session": 8,
    "digest_memories_per_session": 8,
    "digest_intentions_per_session": 5,
    # Pins bypass search and go into every session, so they have hard caps.
    "pinned_max": 20,
    "pinned_max_chars": 1400,
    "trigger_words_per_memory": 8,
}

# -- context budgets (characters; ~4 chars = 1 token) --------------------------
# Pinned memories are OUTSIDE these budgets: every mind gets all of them.
# "hermes" is Stonic's historical name for the full, character-free view
# (digest and "forget" searches use it); kept so old rows stay readable.
BUDGETS = {
    "voice": {"profile": 700, "intentions": 300, "last_time": 400},
    "hermes": {"profile": 2000, "intentions": 800, "last_time": 900},
    "awareness": {"profile": 400, "intentions": 300, "last_time": 200},
    "recall_chars": {"voice": 900, "hermes": 2400, "awareness": 500},
}

# -- search ----------------------------------------------------------------------
SEARCH = {
    "per_channel": 30,
    "rrf_k": 60,
    # Memories above raw turns; lexical above skeleton (a weak cross-script bridge).
    "weights": {
        "memUni": 1.0,
        "memTri": 0.8,
        "memSkel": 0.45,
        "memEntity": 1.1,
        "memTrigger": 2.0,
        "turnUni": 0.8,
        "turnTri": 0.6,
        "turnSkel": 0.35,
        "episodeUni": 0.7,
        "episodeSkel": 0.3,
    },  # fmt: skip
    # Final = R x (1 + a*A + s*S) x C — relevance always dominates.
    "activation_weight": 0.25,
    "salience_weight": 0.25,
    "activation_decay": 0.5,
    "salience": {
        "explicit": 0.30,
        "future": 0.25,
        "emotion": 0.20,
        "surprise": 0.15,
        "useful": 0.10,
    },
    # FOUND / WEAK / NONE by (idf-weighted) coverage of the query's words.
    "found_coverage": 0.6,
    "weak_coverage": 0.34,
    "absent_weight": 0.3,
    "rare_share": 0.02,
    "idf_min_docs": 40,
    "user_turn_boost": 1.15,
    "coverage_floor": 0.6,
    # Speaking unasked (awareness) stays quiet below this.
    "proactive_coverage": 0.75,
    "default_k": 6,
    "turn_window": 1,
}

# -- recall agent ----------------------------------------------------------------
AGENT = {
    "recent_messages": 15,
    "recent_message_max_chars": 600,
    "question_max_chars": 1200,
    "keywords_max_chars": 200,
    "facts_max_chars": 120000,
    "episodes_max_chars": 24000,
    "turns_max_chars": 40000,
    "turn_max_chars": 500,
    "follow_ups_max": 40,
    "older_hits": 6,
    "answer_max_chars": 1100,
    "max_output_tokens": 500,
    "timeout_ms": 8000,
    "asks_per_session": 40,
    "pending_ttl_ms": 5 * MIN,
}

# -- digest (after-conversation distillation) --------------------------------------
DIGEST = {
    # Wait this long after a session goes quiet — no digest of a half conversation.
    "quiet_ms": 15 * MIN,
    "min_user_turns": 2,
    "max_chars_per_call": 14000,
    "max_attempts": 3,
    "prompt_version": "d2",
    # An inferred memory becomes active after evidence from this many sessions.
    "promote_after_sessions": 2,
    "candidate_stale_days": 90,
    # Stonic capped spend per user; Ghost uses the user's own key, so this is a
    # politeness cap on background calls.
    "monthly_cap_usd": 1.0,
    "daily_call_cap": 60,
    "fallback_cost_usd": 0.002,
}

# -- intentions (promises / follow-ups) --------------------------------------------
INTENTIONS = {
    "expiry_after_due_ms": 14 * DAY,
    "expiry_undated_ms": 30 * DAY,
    # When two promises are "the same thing" (content-word overlap 0..1).
    "same_overlap": 0.6,
    "same_overlap_with_cue": 0.34,
}

JOBS = {"lease_ms": 5 * MIN, "retry_base_ms": 2 * MIN, "tick_ms": 60 * 1000}

BACKUP = {
    "first_check_ms": 10 * MIN,
    "min_interval_ms": 20 * HOUR,
    "keep_daily": 7,
    "keep_weekly": 4,
    "keep_monthly": 12,
}

MEMORY_TYPES = (
    "identity", "preference", "relationship", "project", "routine",
    "event", "opinion", "instruction", "other",
)  # fmt: skip
# Only one value is true at a time (city, job, taste): a new one supersedes.
SINGLE_VALUED_TYPES = frozenset(
    {"identity", "preference", "routine", "opinion", "instruction"}
)
MEMORY_STATUSES = ("candidate", "active", "superseded", "retracted")
ORIGINS = ("stated", "inferred", "assistant_tool", "manual", "imported", "agent_action")
# Ghost adds "chat" (text chat) and "ghost_v1" (migrated JSONL facts).
CHANNELS = ("voice", "hermes", "awareness", "remote", "chat")
SPEAKERS = ("user", "assistant", "awareness")
MINDS = ("voice", "hermes", "awareness")

TURN_FLAGS = {
    # Shown to the model AS a memory — never becomes a memory again.
    "recalled": 1,
    # A web page / document / email was read this session: only things the
    # user said themselves are learned.
    "external": 2,
    "redacted": 4,
    # Automated record (archive prompt, memory note) — never a digest source.
    "automated": 8,
    "truncated": 16,
    # The user said "forget" a memory made from this turn: the turn stays for
    # the audit trail, but search, the state card and the digest skip it.
    "forgotten": 32,
}

AUTOMATED_PREFIXES = (
    "[Automated record",
    "[Memory note via Stonic",
    "[Memory request via Stonic",
    "[Memory note via Ghost",
    "[MEMORY NOTE]",
    "[SYSTEM_MEMORY_INJECTION]",
    "[GHOST_AWARENESS]",
)
