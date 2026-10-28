-- 0006: the course layer (ADR-034) and the prompt-cache warm-up stage (ADR-033).
-- One transaction: executescript runs in autocommit, so without it a failure
-- halfway would leave a half-applied migration.
BEGIN;

-- turn_metrics gains a 'warmup' stage. SQLite can't alter a CHECK constraint,
-- so the table is rebuilt and its rows copied across unchanged.
CREATE TABLE turn_metrics_new (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_index INTEGER,
    language TEXT,
    stage TEXT NOT NULL CHECK (stage IN ('conversation', 'summary', 'sweep', 'warmup')),
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL,
    stt_s REAL,
    first_delta_s REAL,
    first_audio_s REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO turn_metrics_new SELECT * FROM turn_metrics;
DROP TABLE turn_metrics;
ALTER TABLE turn_metrics_new RENAME TO turn_metrics;
CREATE INDEX idx_turn_metrics_session ON turn_metrics(session_id);

-- Words being learned, per language profile, with Leitner spaced review.
-- A word enters when the tutor teaches it (note_teaching tool) or the learner
-- asks for it; it comes back for review in conversation, never as flashcards.
CREATE TABLE vocab_items (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES language_profiles(id),
    lemma TEXT NOT NULL,                     -- matching key: dictionary form(s), lowercase
    display TEXT NOT NULL,                   -- as taught: "la playa", "echar de menos"
    gloss TEXT,                              -- English, as the tutor gave it
    source TEXT NOT NULL CHECK (source IN ('taught', 'asked', 'syllabus', 'topic')),
    syllabus_id TEXT,                        -- curriculum item it belongs to, if any
    box INTEGER NOT NULL DEFAULT 0,          -- Leitner box 0..5 (0 = just introduced)
    due_at TEXT NOT NULL,
    times_heard INTEGER NOT NULL DEFAULT 0,  -- the tutor used it
    times_used INTEGER NOT NULL DEFAULT 0,   -- the learner used it
    times_wrong INTEGER NOT NULL DEFAULT 0,
    introduced_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT,
    UNIQUE (profile_id, lemma)
);
CREATE INDEX idx_vocab_due ON vocab_items(profile_id, due_at);

-- The evidence trail behind every box move (same philosophy as level_observations).
CREATE TABLE vocab_events (
    id INTEGER PRIMARY KEY,
    vocab_item_id INTEGER NOT NULL REFERENCES vocab_items(id),
    session_id INTEGER REFERENCES sessions(id),
    utterance_id INTEGER REFERENCES utterances(id),
    kind TEXT NOT NULL CHECK (kind IN ('introduced', 'heard', 'used', 'wrong')),
    box_after INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_vocab_events_item ON vocab_events(vocab_item_id);

-- Syllabus coverage per profile: introduced (the tutor taught or pointed it
-- out) -> practised (the learner used it correctly) -> solid (correct in
-- several separate sessions). item_id is an id from curriculum/<lang>.yaml.
CREATE TABLE syllabus_coverage (
    profile_id INTEGER NOT NULL REFERENCES language_profiles(id),
    item_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('introduced', 'practised', 'solid')),
    sessions_correct INTEGER NOT NULL DEFAULT 0,
    last_session_id INTEGER REFERENCES sessions(id),
    box INTEGER NOT NULL DEFAULT 0,
    due_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (profile_id, item_id)
);

-- Topic cards already told, so stories aren't repeated.
CREATE TABLE topics_told (
    profile_id INTEGER NOT NULL REFERENCES language_profiles(id),
    topic_id TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    told_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (profile_id, topic_id)
);

COMMIT;
