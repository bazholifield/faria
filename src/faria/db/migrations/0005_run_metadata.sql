-- 0005: Run metadata — make every session reproducible and measurable.
--
-- Until now a session recorded what was SAID but nothing about what produced
-- it: which config, which persona text, how long each turn took, what it
-- cost. That makes "did that change help?" unanswerable, which is the whole
-- point of a tuning console. This migration adds the substrate.
--
-- Design mirrors the existing level_observations/level_dimensions split
-- (ADR-004): keep the raw evidence, not just the running figure. Here the
-- evidence is the exact prompt text and the per-turn metrics.

-- Identity of the run: what config and prompt text were in force.
-- config_snapshot is the FULL resolved Settings as JSON, not a diff against
-- defaults — a snapshot you need matching code to interpret is not a
-- snapshot. faria_version pins the code that assembled the prompt sections.
ALTER TABLE sessions ADD COLUMN config_snapshot TEXT;
ALTER TABLE sessions ADD COLUMN prompt_template_version TEXT;
ALTER TABLE sessions ADD COLUMN faria_version TEXT;

-- Content-addressed store of assembled system prompts. Keyed by hash, so the
-- same prompt across many sessions is stored once and identical runs are
-- trivially detectable.
CREATE TABLE prompt_versions (
    hash TEXT PRIMARY KEY,                           -- sha256 of text, truncated
    text TEXT NOT NULL,                              -- the exact system prompt as sent
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which prompts a session actually used, in order. A session rebuilds its
-- prompt on every language switch and settings change (ADR-015), so this is
-- one-to-many, not a column on sessions.
CREATE TABLE session_prompts (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,                            -- 0, 1, 2... in rebuild order
    language TEXT NOT NULL,                          -- active language for this prompt
    prompt_hash TEXT NOT NULL REFERENCES prompt_versions(hash),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, seq)
);

-- Per-call metrics. One row per API call, tagged by stage: the live
-- conversation turn, the end-of-session summary, or a per-language sweep —
-- all three spend tokens, so honest cost means capturing all three.
-- Timings are audio-only (the CLI has no STT/TTS stages) and arrive in a
-- second write from the websocket layer, so they stay nullable.
CREATE TABLE turn_metrics (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_index INTEGER,                              -- learner turn this belongs to; NULL for end-of-session work
    language TEXT,
    stage TEXT NOT NULL CHECK (stage IN ('conversation', 'summary', 'sweep')),
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL,                                   -- NULL when the model's price is unknown
    stt_s REAL,                                      -- utterance end -> transcript
    first_delta_s REAL,                              -- utterance end -> first token
    first_audio_s REAL,                              -- utterance end -> first audio out (the <2s budget, ADR-022)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_session_prompts_session ON session_prompts(session_id);
CREATE INDEX idx_turn_metrics_session ON turn_metrics(session_id);
