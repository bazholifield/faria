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
class TokenUsage:
    """The four token counts one API call reports (turn_metrics columns).

    Built from an SDK usage object via ``from_usage``. Every field defaults to
    zero and reads defensively — a scripted fake brain in tests reports no
    usage at all, and a missing count should cost nothing, not raise.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: object | None) -> "TokenUsage":
        if usage is None:
            return cls()
        return cls(
            input_tokens=_int_attr(usage, "input_tokens"),
            output_tokens=_int_attr(usage, "output_tokens"),
            cache_read_tokens=_int_attr(usage, "cache_read_input_tokens"),
            cache_write_tokens=_int_attr(usage, "cache_creation_input_tokens"),
        )

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


def _int_attr(obj: object, name: str) -> int:
    value = getattr(obj, name, 0)
    return int(value) if isinstance(value, (int, float)) else 0


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
class CourseNotes:
    """What the mentor may draw on this session (course/planner.py). Suggestions only."""

    variety: str | None = None
    due_words: list[tuple[str, str | None]] = field(default_factory=list)   # (as taught, gloss)
    new_items: list[dict] = field(default_factory=list)       # syllabus items worth introducing
    revisit_items: list[dict] = field(default_factory=list)   # practised, due for another look
    topics: list[dict] = field(default_factory=list)          # topic cards not yet told
    placement: list[tuple[str, list[str]]] | None = None      # (level, probe titles) when level unknown
    tracked_items: list[tuple[str, str]] = field(default_factory=list)  # (id, title) for the sweep


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
    # The course layer's suggestions for this language (ADR-034); None = off.
    course: "CourseNotes | None" = None
