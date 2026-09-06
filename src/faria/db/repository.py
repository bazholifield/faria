"""Typed data access. The only module that writes SQL (ADR-006).

Storage is mechanical and implemented; *deciding what to store* (detection,
policy, estimation) lives in domain/ and tutor/. Methods take/return the
dataclasses from domain/models.py.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from faria.db.connection import transaction
from faria.domain.models import (
    ErrorEvent,
    ErrorPattern,
    Learner,
    LanguageProfile,
    LevelDimensionRow,
    Session,
    SummaryRow,
    TokenUsage,
    Utterance,
)
from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- learner / profile ---------------------------------------------------

    def get_or_create_learner(self, name: str, native_language: str = "en") -> Learner:
        row = self._conn.execute("SELECT * FROM learners LIMIT 1").fetchone()
        if row is None:
            with transaction(self._conn) as conn:
                cur = conn.execute(
                    "INSERT INTO learners (name, native_language) VALUES (?, ?)",
                    (name, native_language),
                )
            row = self._conn.execute(
                "SELECT * FROM learners WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return Learner(
            id=row["id"],
            name=row["name"],
            native_language=row["native_language"],
            other_languages=json.loads(row["other_languages"]),
        )

    def get_or_create_profile(
        self,
        learner_id: int,
        language: str,
        default_aggressiveness: str,
        default_style: str = "recast",
    ) -> LanguageProfile:
        row = self._conn.execute(
            "SELECT * FROM language_profiles WHERE learner_id = ? AND language = ?",
            (learner_id, language),
        ).fetchone()
        if row is None:
            with transaction(self._conn) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO language_profiles
                        (learner_id, language, correction_aggressiveness, correction_style)
                    VALUES (?, ?, ?, ?)
                    """,
                    (learner_id, language, default_aggressiveness, default_style),
                )
            row = self._conn.execute(
                "SELECT * FROM language_profiles WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._profile_from_row(row)

    def update_correction_settings(
        self, profile_id: int, aggressiveness: str | None, style: str | None
    ) -> None:
        """Apply a learner-driven change from the session control tool (ADR-015).

        Either argument may be None to leave that setting untouched; both
        None is a no-op.
        """
        if aggressiveness is None and style is None:
            return
        sets, params = [], []
        if aggressiveness is not None:
            sets.append("correction_aggressiveness = ?")
            params.append(aggressiveness)
        if style is not None:
            sets.append("correction_style = ?")
            params.append(style)
        params.append(profile_id)
        with transaction(self._conn) as conn:
            conn.execute(
                f"UPDATE language_profiles SET {', '.join(sets)}, updated_at = datetime('now') "
                "WHERE id = ?",
                params,
            )

    def update_preferences(self, profile_id: int, preferences: dict) -> None:
        """Replace the profile's preferences JSON wholesale (caller merges first)."""
        with transaction(self._conn) as conn:
            conn.execute(
                "UPDATE language_profiles SET preferences = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(preferences, ensure_ascii=False), profile_id),
            )

    def known_target_languages(self, learner_id: int, exclude_language: str) -> list[str]:
        """Languages this learner has an existing profile for, other than
        ``exclude_language`` (normally the native/home language). Only languages
        with real history are eligible for tutor-initiated pivots (ADR-015) —
        pivoting into a language with zero context isn't a comprehension test,
        it's just noise."""
        rows = self._conn.execute(
            "SELECT language FROM language_profiles WHERE learner_id = ? AND language != ? "
            "ORDER BY updated_at DESC",
            (learner_id, exclude_language),
        ).fetchall()
        return [r["language"] for r in rows]

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> LanguageProfile:
        return LanguageProfile(
            id=row["id"],
            learner_id=row["learner_id"],
            language=row["language"],
            level_overall=row["level_overall"],
            level_confidence=row["level_confidence"],
            correction_aggressiveness=row["correction_aggressiveness"],
            correction_style=row["correction_style"],
            explanation_language=row["explanation_language"],
            preferences=json.loads(row["preferences"]),
        )

    # -- levels --------------------------------------------------------------

    def get_dimensions(self, profile_id: int) -> list[LevelDimensionRow]:
        rows = self._conn.execute(
            "SELECT * FROM level_dimensions WHERE profile_id = ?", (profile_id,)
        ).fetchall()
        return [
            LevelDimensionRow(
                profile_id=r["profile_id"],
                dimension=r["dimension"],
                score=r["score"],
                confidence=r["confidence"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def record_observation(
        self,
        profile_id: int,
        session_id: int | None,
        dimension: str,
        value: float,
        weight: float,
        source: str,
        detail: dict | None = None,
    ) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO level_observations
                    (profile_id, session_id, dimension, value, weight, source, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    session_id,
                    dimension,
                    value,
                    weight,
                    source,
                    json.dumps(detail) if detail is not None else None,
                ),
            )

    def upsert_dimension(
        self, profile_id: int, dimension: str, score: float, confidence: float
    ) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO level_dimensions (profile_id, dimension, score, confidence)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (profile_id, dimension) DO UPDATE SET
                    score = excluded.score,
                    confidence = excluded.confidence,
                    updated_at = datetime('now')
                """,
                (profile_id, dimension, score, confidence),
            )

    def set_overall_level(self, profile_id: int, score: float, confidence: float) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                """
                UPDATE language_profiles
                SET level_overall = ?, level_confidence = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (score, confidence, profile_id),
            )

    # -- sessions / utterances ----------------------------------------------
    # Sessions are learner-scoped, not language-scoped (ADR-014): a single
    # session can move between English and any number of target languages.
    # Language lives on each Utterance instead.

    def create_session(self, learner_id: int, mode: str = "text", goal: str | None = None) -> Session:
        with transaction(self._conn) as conn:
            cur = conn.execute(
                "INSERT INTO sessions (learner_id, mode, goal) VALUES (?, ?, ?)",
                (learner_id, mode, goal),
            )
        return Session(id=cur.lastrowid, learner_id=learner_id, mode=mode, goal=goal, status="active")

    def end_session(self, session_id: int, status: str = "completed") -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = datetime('now') WHERE id = ?",
                (status, session_id),
            )

    def add_utterance(
        self,
        session_id: int,
        turn_index: int,
        speaker: str,
        text: str,
        language: str,
        meta: dict | None = None,
    ) -> Utterance:
        with transaction(self._conn) as conn:
            cur = conn.execute(
                """
                INSERT INTO utterances (session_id, turn_index, speaker, text, language, meta)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, turn_index, speaker, text, language, json.dumps(meta or {})),
            )
        return Utterance(
            id=cur.lastrowid,
            session_id=session_id,
            turn_index=turn_index,
            speaker=speaker,
            text=text,
            language=language,
        )

    def session_utterances(self, session_id: int) -> list[Utterance]:
        rows = self._conn.execute(
            "SELECT * FROM utterances WHERE session_id = ? ORDER BY turn_index", (session_id,)
        ).fetchall()
        return [
            Utterance(
                id=r["id"],
                session_id=r["session_id"],
                turn_index=r["turn_index"],
                speaker=r["speaker"],
                text=r["text"],
                language=r["language"],
            )
            for r in rows
        ]

    # -- errors --------------------------------------------------------------

    def upsert_pattern(
        self,
        profile_id: int,
        category: ErrorCategory,
        subcategory: ErrorSubcategory | None,
        feature_key: str,
        description: str | None,
        fossilization_threshold: int = 6,
    ) -> ErrorPattern:
        """Find-or-create the pattern for feature_key and bump its recurrence count.

        Any new occurrence resets status to 'active' (still a live problem)
        unless the bumped count crosses `fossilization_threshold`, in which
        case it's marked 'fossilization_risk'. Downgrading a quiet pattern to
        'improving'/'resolved' happens separately in decay_pattern_statuses(),
        called at the start of each session so a fresh session sees an
        up-to-date focus list (ADR-018).
        """
        with transaction(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO error_patterns
                    (profile_id, category, subcategory, feature_key, description, occurrence_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT (profile_id, feature_key) DO UPDATE SET
                    occurrence_count = occurrence_count + 1,
                    last_seen_at = datetime('now'),
                    status = CASE
                        WHEN occurrence_count + 1 >= ? THEN 'fossilization_risk'
                        ELSE 'active'
                    END
                """,
                (profile_id, category.value, subcategory.value if subcategory else None,
                 feature_key, description, fossilization_threshold),
            )
        row = self._conn.execute(
            "SELECT * FROM error_patterns WHERE profile_id = ? AND feature_key = ?",
            (profile_id, feature_key),
        ).fetchone()
        return self._pattern_from_row(row)

    def decay_pattern_statuses(
        self, profile_id: int, improving_after_days: int, resolved_after_days: int
    ) -> None:
        """Downgrade patterns that have gone quiet (ADR-018): active or
        fossilization_risk -> improving after `improving_after_days` without
        recurrence, -> resolved after `resolved_after_days`. A pattern that
        recurs again is bumped back to 'active' by upsert_pattern, so this is
        a one-way decay unless real evidence of continuing struggle shows up.
        Call at session start, before focus_patterns(), so the focus list
        reflects current reality.
        """
        with transaction(self._conn) as conn:
            conn.execute(
                """
                UPDATE error_patterns
                SET status = CASE
                    WHEN julianday('now') - julianday(last_seen_at) >= ? THEN 'resolved'
                    WHEN julianday('now') - julianday(last_seen_at) >= ? THEN 'improving'
                    ELSE status
                END
                WHERE profile_id = ? AND status IN ('active', 'fossilization_risk', 'improving')
                """,
                (resolved_after_days, improving_after_days, profile_id),
            )

    def list_patterns(self, profile_id: int, limit: int = 5) -> list[ErrorPattern]:
        """All patterns for this profile, most-recurrent first, any status —
        for inspection (`faria patterns`). Contrast with focus_patterns(),
        which filters by status specifically for prompt injection."""
        rows = self._conn.execute(
            """
            SELECT * FROM error_patterns
            WHERE profile_id = ?
            ORDER BY occurrence_count DESC, last_seen_at DESC
            LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()
        return [self._pattern_from_row(r) for r in rows]

    def pattern_examples(self, pattern_id: int, limit: int = 2) -> list[tuple[str, str | None]]:
        """A few (span_text, correction) examples for a pattern, newest first."""
        rows = self._conn.execute(
            """
            SELECT span_text, correction FROM error_events
            WHERE pattern_id = ? AND span_text IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (pattern_id, limit),
        ).fetchall()
        return [(r["span_text"], r["correction"]) for r in rows]

    def known_feature_keys(self, profile_id: int, limit: int = 30) -> list[str]:
        """All feature_keys logged for this profile, most-recently-seen first.

        Deliberately not filtered by status/threshold like focus_patterns()
        — a pattern only gets ONE chance to be recognized as recurring the
        second time it happens, and that requires the detector to already
        know the key exists the first time (see prompts.py's known-keys
        section)."""
        rows = self._conn.execute(
            "SELECT feature_key FROM error_patterns WHERE profile_id = ? "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (profile_id, limit),
        ).fetchall()
        return [r["feature_key"] for r in rows]

    def pattern_feature_keys_for_utterance(self, utterance_id: int) -> set[str]:
        """feature_keys already logged for this utterance — lets the
        end-of-session sweep skip errors the live sidecar already caught."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT ep.feature_key FROM error_events ee
            JOIN error_patterns ep ON ep.id = ee.pattern_id
            WHERE ee.utterance_id = ?
            """,
            (utterance_id,),
        ).fetchall()
        return {r["feature_key"] for r in rows}

    def record_error_event(self, session_id: int, event: ErrorEvent, pattern_id: int | None) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO error_events
                    (session_id, utterance_id, pattern_id, span_start, span_end, span_text,
                     category, subcategory, severity, confidence, correction, explanation,
                     interference_source, action, detector, model_action)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    event.utterance_id,
                    pattern_id,
                    event.span_start,
                    event.span_end,
                    event.span_text,
                    event.category.value,
                    event.subcategory.value if event.subcategory else None,
                    int(event.severity),
                    event.confidence,
                    event.correction,
                    event.explanation,
                    event.interference_source,
                    event.action,
                    event.detector,
                    event.model_action,
                ),
            )

    def focus_patterns(self, profile_id: int, min_occurrences: int, limit: int = 5) -> list[ErrorPattern]:
        """Most-recurrent unresolved patterns, for the prompt's focus list."""
        rows = self._conn.execute(
            """
            SELECT * FROM error_patterns
            WHERE profile_id = ? AND status IN ('active', 'fossilization_risk')
              AND occurrence_count >= ?
            ORDER BY occurrence_count DESC, last_seen_at DESC
            LIMIT ?
            """,
            (profile_id, min_occurrences, limit),
        ).fetchall()
        return [self._pattern_from_row(r) for r in rows]

    @staticmethod
    def _pattern_from_row(row: sqlite3.Row) -> ErrorPattern:
        return ErrorPattern(
            id=row["id"],
            profile_id=row["profile_id"],
            category=ErrorCategory(row["category"]),
            subcategory=ErrorSubcategory(row["subcategory"]) if row["subcategory"] else None,
            feature_key=row["feature_key"],
            description=row["description"],
            status=row["status"],
            occurrence_count=row["occurrence_count"],
        )

    # -- run metadata (migration 0005) ---------------------------------------
    # What produced a session, kept alongside what was said in it: the config
    # and prompt text in force, and per-call latency and token cost. Without
    # these, comparing two runs of a tuned prompt is guesswork.

    def stamp_session_run(
        self,
        session_id: int,
        config_snapshot: dict,
        prompt_template_version: str,
        faria_version: str,
    ) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET config_snapshot = ?, prompt_template_version = ?, faria_version = ?
                WHERE id = ?
                """,
                (
                    json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True),
                    prompt_template_version,
                    faria_version,
                    session_id,
                ),
            )

    def record_session_prompt(self, session_id: int, language: str, text: str) -> str:
        """Store one assembled system prompt and link it to the session.

        Content-addressed: the same prompt text across many sessions is stored
        once. Returns the hash. Called on every prompt rebuild, so a session
        that switches languages three times has four rows.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO prompt_versions (hash, text) VALUES (?, ?)",
                (digest, text),
            )
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS next FROM session_prompts WHERE session_id = ?",
                (session_id,),
            ).fetchone()["next"]
            conn.execute(
                """
                INSERT INTO session_prompts (session_id, seq, language, prompt_hash)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, seq, language, digest),
            )
        return digest

    def get_prompt(self, prompt_hash: str) -> str | None:
        row = self._conn.execute(
            "SELECT text FROM prompt_versions WHERE hash = ?", (prompt_hash,)
        ).fetchone()
        return row["text"] if row else None

    def record_turn_metrics(
        self,
        session_id: int,
        stage: str,
        turn_index: int | None = None,
        language: str | None = None,
        model: str | None = None,
        usage: TokenUsage | None = None,
        cost_usd: float | None = None,
    ) -> int:
        """One API call's tokens and cost. Returns the row id so the audio
        layer can attach its timings once the turn finishes."""
        usage = usage or TokenUsage()
        with transaction(self._conn) as conn:
            cur = conn.execute(
                """
                INSERT INTO turn_metrics
                    (session_id, turn_index, language, stage, model, input_tokens,
                     output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_index,
                    language,
                    stage,
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    cost_usd,
                ),
            )
        return cur.lastrowid

    def attach_turn_timings(
        self,
        metric_id: int,
        stt_s: float | None = None,
        first_delta_s: float | None = None,
        first_audio_s: float | None = None,
    ) -> None:
        """Second write for an audio turn: the stage timings ws.py measures.

        Split from record_turn_metrics because the token counts are known when
        the model call ends, while first_audio_s isn't known until TTS has
        flushed — and a text session never has either.
        """
        with transaction(self._conn) as conn:
            conn.execute(
                """
                UPDATE turn_metrics
                SET stt_s = ?, first_delta_s = ?, first_audio_s = ?
                WHERE id = ?
                """,
                (stt_s, first_delta_s, first_audio_s, metric_id),
            )

    # -- summaries -----------------------------------------------------------

    def store_summary(self, summary: SummaryRow) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO session_summaries
                    (session_id, summary_text, topics, vocab_exposed, error_digest,
                     level_evidence, next_focus, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.session_id,
                    summary.summary_text,
                    json.dumps(summary.topics, ensure_ascii=False),
                    json.dumps(summary.vocab_exposed, ensure_ascii=False),
                    json.dumps(summary.error_digest, ensure_ascii=False),
                    json.dumps(summary.level_evidence, ensure_ascii=False),
                    json.dumps(summary.next_focus, ensure_ascii=False),
                    summary.model,
                ),
            )

    def recent_summaries(self, language: str, limit: int) -> list[SummaryRow]:
        """Latest summaries for sessions where this language appeared, oldest
        first for prompt injection. Language-scoped rather than profile-scoped
        (ADR-014): a session is no longer bound to one language, so "recent
        Spanish sessions" means "sessions with at least one Spanish
        utterance", not "sessions of the Spanish profile"."""
        rows = self._conn.execute(
            """
            SELECT * FROM session_summaries
            WHERE session_id IN (SELECT DISTINCT session_id FROM utterances WHERE language = ?)
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (language, limit),
        ).fetchall()
        summaries = [
            SummaryRow(
                session_id=r["session_id"],
                summary_text=r["summary_text"],
                topics=json.loads(r["topics"]),
                vocab_exposed=json.loads(r["vocab_exposed"]),
                error_digest=json.loads(r["error_digest"]),
                level_evidence=json.loads(r["level_evidence"]),
                next_focus=json.loads(r["next_focus"]),
                model=r["model"],
            )
            for r in rows
        ]
        return list(reversed(summaries))
