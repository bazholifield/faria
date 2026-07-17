"""Typed data access. The only module that writes SQL (ADR-006).

Storage is mechanical and implemented; *deciding what to store* (detection,
policy, estimation) lives in domain/ and tutor/. Methods take/return the
dataclasses from domain/models.py.
"""

from __future__ import annotations

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
        self, learner_id: int, language: str, default_aggressiveness: str
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
                        (learner_id, language, correction_aggressiveness)
                    VALUES (?, ?, ?)
                    """,
                    (learner_id, language, default_aggressiveness),
                )
            row = self._conn.execute(
                "SELECT * FROM language_profiles WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._profile_from_row(row)

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> LanguageProfile:
        return LanguageProfile(
            id=row["id"],
            learner_id=row["learner_id"],
            language=row["language"],
            level_overall=row["level_overall"],
            level_confidence=row["level_confidence"],
            correction_aggressiveness=row["correction_aggressiveness"],
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

    def create_session(self, profile_id: int, mode: str = "text", goal: str | None = None) -> Session:
        with transaction(self._conn) as conn:
            cur = conn.execute(
                "INSERT INTO sessions (profile_id, mode, goal) VALUES (?, ?, ?)",
                (profile_id, mode, goal),
            )
        return Session(id=cur.lastrowid, profile_id=profile_id, mode=mode, goal=goal, status="active")

    def end_session(self, session_id: int, status: str = "completed") -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = datetime('now') WHERE id = ?",
                (status, session_id),
            )

    def add_utterance(
        self, session_id: int, turn_index: int, speaker: str, text: str, meta: dict | None = None
    ) -> Utterance:
        with transaction(self._conn) as conn:
            cur = conn.execute(
                """
                INSERT INTO utterances (session_id, turn_index, speaker, text, meta)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, turn_index, speaker, text, json.dumps(meta or {})),
            )
        return Utterance(
            id=cur.lastrowid, session_id=session_id, turn_index=turn_index, speaker=speaker, text=text
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
    ) -> ErrorPattern:
        """Find-or-create the pattern for feature_key and bump its recurrence count."""
        with transaction(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO error_patterns
                    (profile_id, category, subcategory, feature_key, description, occurrence_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT (profile_id, feature_key) DO UPDATE SET
                    occurrence_count = occurrence_count + 1,
                    last_seen_at = datetime('now')
                """,
                (profile_id, category.value, subcategory.value if subcategory else None,
                 feature_key, description),
            )
        row = self._conn.execute(
            "SELECT * FROM error_patterns WHERE profile_id = ? AND feature_key = ?",
            (profile_id, feature_key),
        ).fetchone()
        return self._pattern_from_row(row)

    def record_error_event(self, session_id: int, event: ErrorEvent, pattern_id: int | None) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                """
                INSERT INTO error_events
                    (session_id, utterance_id, pattern_id, span_start, span_end, span_text,
                     category, subcategory, severity, confidence, correction, explanation,
                     interference_source, action, detector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def recent_summaries(self, profile_id: int, limit: int) -> list[SummaryRow]:
        """Latest summaries for this profile, oldest-first for prompt injection."""
        rows = self._conn.execute(
            """
            SELECT ss.* FROM session_summaries ss
            JOIN sessions s ON s.id = ss.session_id
            WHERE s.profile_id = ?
            ORDER BY ss.created_at DESC, ss.id DESC
            LIMIT ?
            """,
            (profile_id, limit),
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
