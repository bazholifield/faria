-- 0001: Full Phase 0/1 schema.
-- Conventions: timestamps are UTC ISO-8601 TEXT; JSON columns are TEXT holding
-- JSON (SQLite json functions work on them); error taxonomy values are enforced
-- in Python (domain/taxonomy.py), not CHECKs, so the taxonomy can evolve
-- without a migration (ADR-006/ADR-009).

-- Who is learning. Single row today; multi-user-shaped on purpose (ADR-012).
CREATE TABLE learners (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    native_language TEXT NOT NULL DEFAULT 'en',      -- L1, for interference attribution
    other_languages TEXT NOT NULL DEFAULT '[]',      -- JSON [lang codes]; L3 interference sources
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per (learner, target language). The hub everything hangs off.
CREATE TABLE language_profiles (
    id INTEGER PRIMARY KEY,
    learner_id INTEGER NOT NULL REFERENCES learners(id),
    language TEXT NOT NULL,                          -- ISO 639-1/BCP-47: es, fr, de, ar, eu
    level_overall REAL,                              -- continuous 1.0=A1 .. 6.0=C2; NULL = no estimate yet
    level_confidence REAL NOT NULL DEFAULT 0.0,      -- 0..1
    correction_aggressiveness TEXT NOT NULL DEFAULT 'balanced'
        CHECK (correction_aggressiveness IN ('minimal', 'balanced', 'aggressive')),
    explanation_language TEXT NOT NULL DEFAULT 'adaptive'
        CHECK (explanation_language IN ('adaptive', 'native', 'target')),
    preferences TEXT NOT NULL DEFAULT '{}',          -- JSON: stated preferences, free-form
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (learner_id, language)
);

-- Current per-dimension CEFR estimates (ADR-004). A *view over evidence*:
-- level_observations is the ground truth; these rows are the running estimate.
CREATE TABLE level_dimensions (
    profile_id INTEGER NOT NULL REFERENCES language_profiles(id),
    dimension TEXT NOT NULL CHECK (dimension IN
        ('lexical_range', 'grammatical_accuracy', 'syntactic_complexity',
         'comprehension', 'fluency')),
    score REAL NOT NULL,                             -- same 1..6 scale as level_overall
    confidence REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (profile_id, dimension)
);

-- Raw evidence feeding the estimator. Never updated, only appended; keeps the
-- estimate recomputable when the update rule in domain/levels.py changes.
CREATE TABLE level_observations (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES language_profiles(id),
    session_id INTEGER REFERENCES sessions(id),
    dimension TEXT NOT NULL,
    value REAL NOT NULL,                             -- observed level on the 1..6 scale
    weight REAL NOT NULL DEFAULT 1.0,                -- evidence strength (sample size, source trust)
    source TEXT NOT NULL,                            -- 'llm_judgment' | 'error_rate' | 'lexical_stats' | 'comprehension_check' | ...
    detail TEXT,                                     -- JSON: raw numbers behind the observation
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES language_profiles(id),
    mode TEXT NOT NULL DEFAULT 'text' CHECK (mode IN ('text', 'audio')),
    goal TEXT,                                       -- optional session goal, echoed into the prompt
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'abandoned')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT
);

-- Every turn, both speakers. Error spans index into learner utterances.
CREATE TABLE utterances (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_index INTEGER NOT NULL,
    speaker TEXT NOT NULL CHECK (speaker IN ('learner', 'tutor')),
    text TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',                 -- JSON: Phase 2 adds stt_confidence, audio duration
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, turn_index)
);

-- Recurrence layer (ADR-009): events with the same normalized feature_key
-- link to one pattern. feature_key convention: 'category:subcategory:feature',
-- e.g. 'morphosyntax:agreement:gender_noun_adj', 'lexis:false_friend:actualmente'.
CREATE TABLE error_patterns (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES language_profiles(id),
    category TEXT NOT NULL,
    subcategory TEXT,
    feature_key TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'improving', 'resolved', 'fossilization_risk')),
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (profile_id, feature_key)
);

-- One detected error occurrence. Severity anchors live in domain/taxonomy.py:
-- 1 slip / 2 minor / 3 disruptive / 4 breakdown. Fossilization is pattern-level
-- status, not a severity (ADR-009).
CREATE TABLE error_events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    utterance_id INTEGER NOT NULL REFERENCES utterances(id),
    pattern_id INTEGER REFERENCES error_patterns(id),
    span_start INTEGER,                              -- char offsets into utterances.text; NULL if not localizable
    span_end INTEGER,
    span_text TEXT,
    category TEXT NOT NULL,
    subcategory TEXT,
    severity INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 4),
    confidence REAL NOT NULL DEFAULT 1.0,            -- detector's own confidence 0..1
    correction TEXT,                                 -- target-like form
    explanation TEXT,                                -- short why, learner-facing
    interference_source TEXT,                        -- lang code suspected as source (false friend / calque)
    action TEXT NOT NULL DEFAULT 'logged_silent'
        CHECK (action IN ('corrected_explicit', 'corrected_recast', 'deferred_recap',
                          'logged_silent', 'clarified')),
    detector TEXT NOT NULL,                          -- provenance: 'sidecar:<model>' | 'sweep:<model>' (ADR-003)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- LLM-written structured summary per session; last N are injected into the
-- next session's prompt (ADR-007).
CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
    summary_text TEXT NOT NULL,                      -- prose, written to be read by the next prompt
    topics TEXT NOT NULL DEFAULT '[]',               -- JSON [str]
    vocab_exposed TEXT NOT NULL DEFAULT '[]',        -- JSON [str]; gets its own table in Phase 4 (SRS)
    error_digest TEXT NOT NULL DEFAULT '[]',         -- JSON: notable errors as summarized by the model
    level_evidence TEXT NOT NULL DEFAULT '[]',       -- JSON: per-dimension impressions
    next_focus TEXT NOT NULL DEFAULT '[]',           -- JSON [str]: what the next session should target
    model TEXT NOT NULL,                             -- which model wrote it
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_sessions_profile ON sessions(profile_id);
CREATE INDEX idx_utterances_session ON utterances(session_id);
CREATE INDEX idx_error_events_session ON error_events(session_id);
CREATE INDEX idx_error_events_pattern ON error_events(pattern_id);
CREATE INDEX idx_error_patterns_profile ON error_patterns(profile_id);
CREATE INDEX idx_level_observations_profile ON level_observations(profile_id);
