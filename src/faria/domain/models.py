"""Dataclasses mirroring DB rows, plus in-memory aggregates.

Kept deliberately dumb: fields match the SQL schema column-for-column (see
db/migrations/0001_initial.sql for column semantics). JSON columns surface as
parsed Python values. Behavior lives in taxonomy.py / levels.py / correction.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity


@dataclass(frozen=True)
class Learner:
    id: int
    name: str
    native_language: str
    other_languages: list[str]


@dataclass(frozen=True)
class LanguageProfile:
    id: int
    learner_id: int
    language: str
    level_overall: float | None
    level_confidence: float
    correction_aggressiveness: str  # off | minimal | balanced | aggressive
    correction_style: str           # recast | recast_plus_note | explicit
    explanation_language: str       # adaptive | native | target
    preferences: dict


@dataclass(frozen=True)
class LevelDimensionRow:
    profile_id: int
    dimension: str
    score: float
    confidence: float
    updated_at: str


@dataclass(frozen=True)
class Session:
    """A conversation with the learner (ADR-014). Language-agnostic: a session
    can move between English and any number of target languages — language is
    a property of each Utterance, not of the session."""

    id: int
    learner_id: int
    mode: str
    goal: str | None
    status: str


@dataclass(frozen=True)
class Utterance:
    id: int
    session_id: int
    turn_index: int
    speaker: str  # learner | tutor
    text: str
    language: str  # ISO 639-1/BCP-47 code the utterance was actually in (ADR-014)


@dataclass(frozen=True)
class ErrorPattern:
    id: int
    profile_id: int
    category: ErrorCategory
    subcategory: ErrorSubcategory | None
    feature_key: str
    description: str | None
    status: str
    occurrence_count: int


@dataclass(frozen=True)
class ErrorEvent:
    """One detected error occurrence, pre-persistence (ids assigned by the DB)."""

    utterance_id: int
    category: ErrorCategory
    subcategory: ErrorSubcategory | None
    severity: Severity
    confidence: float
    feature_key: str
    span_text: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    correction: str | None = None
    explanation: str | None = None
    interference_source: str | None = None
    action: str = "logged_silent"
    detector: str = "unknown"
    model_action: str | None = None  # the model's own "handled" self-report (ADR-005)


@dataclass(frozen=True)
class SummaryRow:
    session_id: int
    summary_text: str
    topics: list[str]
    vocab_exposed: list[str]
    error_digest: list[dict]
    level_evidence: list[dict]
    next_focus: list[str]
    model: str


@dataclass
class SessionContext:
    """Everything prompt assembly needs at session start.

    Built once by SessionRunner.start() from repository reads; passed to
    tutor/prompts.assemble_system_prompt().
    """

    learner: Learner
    profile: LanguageProfile
    dimensions: list[LevelDimensionRow] = field(default_factory=list)
    recent_summaries: list[SummaryRow] = field(default_factory=list)
    focus_patterns: list[ErrorPattern] = field(default_factory=list)
    # ALL known feature_keys for this profile, not just focus_patterns (which
    # only holds ones past the recurrence threshold) — surfaced to the
    # detector so it can reuse an existing fingerprint for a still-rare error
    # instead of minting a near-duplicate every time (see prompts.py).
    known_feature_keys: list[str] = field(default_factory=list)
    goal: str | None = None
