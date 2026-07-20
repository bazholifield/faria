-- 0004: Sessions become learner-scoped; language becomes a per-utterance
-- property (ADR-014). This is the schema change the companion vision needs:
-- one session can move between English and any number of target languages,
-- with evidence routed to the right per-language profile by looking at what
-- language a given utterance was actually in.
--
-- Backfill: every existing utterance inherits the language of the session's
-- original (single, pre-1.5) profile -- exactly correct for all prior data,
-- since before this migration a session could only ever be in one language.

PRAGMA foreign_keys = OFF;

CREATE TABLE sessions_new (
    id INTEGER PRIMARY KEY,
    learner_id INTEGER NOT NULL REFERENCES learners(id),
    mode TEXT NOT NULL DEFAULT 'text' CHECK (mode IN ('text', 'audio')),
    goal TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'abandoned')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT
);

INSERT INTO sessions_new (id, learner_id, mode, goal, status, started_at, ended_at)
SELECT s.id, lp.learner_id, s.mode, s.goal, s.status, s.started_at, s.ended_at
FROM sessions s
JOIN language_profiles lp ON lp.id = s.profile_id;

-- utterances: language becomes a per-row property. Backfilled here (while
-- the old sessions.profile_id is still readable) from the session's original
-- single-language profile.
ALTER TABLE utterances ADD COLUMN language TEXT NOT NULL DEFAULT 'und';

UPDATE utterances
SET language = (
    SELECT lp.language
    FROM sessions s JOIN language_profiles lp ON lp.id = s.profile_id
    WHERE s.id = utterances.session_id
);

DROP TABLE sessions;
ALTER TABLE sessions_new RENAME TO sessions;

PRAGMA foreign_keys = ON;
