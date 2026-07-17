-- 0002: Correction frequency gains 'off'; new orthogonal style axis (ADR-013).
-- SQLite cannot alter a CHECK constraint, so language_profiles is rebuilt.
-- Child tables (sessions, error_patterns, level_*) reference this table by
-- name, so the drop+rename is done with foreign keys off; references resolve
-- again once the new table takes the old name.

PRAGMA foreign_keys = OFF;

CREATE TABLE language_profiles_new (
    id INTEGER PRIMARY KEY,
    learner_id INTEGER NOT NULL REFERENCES learners(id),
    language TEXT NOT NULL,
    level_overall REAL,
    level_confidence REAL NOT NULL DEFAULT 0.0,
    correction_aggressiveness TEXT NOT NULL DEFAULT 'balanced'
        CHECK (correction_aggressiveness IN ('off', 'minimal', 'balanced', 'aggressive')),
    correction_style TEXT NOT NULL DEFAULT 'recast'
        CHECK (correction_style IN ('recast', 'recast_plus_note', 'explicit')),
    explanation_language TEXT NOT NULL DEFAULT 'adaptive'
        CHECK (explanation_language IN ('adaptive', 'native', 'target')),
    preferences TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (learner_id, language)
);

INSERT INTO language_profiles_new
    (id, learner_id, language, level_overall, level_confidence,
     correction_aggressiveness, explanation_language, preferences,
     created_at, updated_at)
SELECT id, learner_id, language, level_overall, level_confidence,
       correction_aggressiveness, explanation_language, preferences,
       created_at, updated_at
FROM language_profiles;

DROP TABLE language_profiles;
ALTER TABLE language_profiles_new RENAME TO language_profiles;

PRAGMA foreign_keys = ON;
