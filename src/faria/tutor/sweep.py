"""End-of-session transcript sweep (ADR-003, ADR-017).

A second, cheaper pass over the completed transcript that does two jobs the
live per-turn sidecar can't: (1) catch errors a real-time reviewer plausibly
missed, and (2) produce grounded LLM-judgment evidence for CEFR estimation.
Structured output, same style as summarizer.py (client.messages.parse).

Design notes:
  - The session is already over, so nothing found here can be corrected in
    conversation — missed errors are persisted with action="logged_silent"
    (there's no live moment left to intervene in) but still feed pattern
    recurrence, which is the whole point of catching them.
  - Errors are deduplicated against what the live sidecar already logged for
    the same learner turn, matched by exact feature_key string (see
    SessionRunner._apply_sweep()). This is best-effort: the sweep is a
    separate generation and may fingerprint the same conceptual error
    differently than the sidecar did (e.g. one call's
    "morphosyntax:conjugation:tener_present" vs. another's
    "morphosyntax:agreement:subject_verb_tener" for the same sentence),
    in which case both get logged as if distinct and occurrence_count is
    mildly inflated. Observed in live testing — not corrected for in this
    pass; a real semantic-equivalence check would need its own LLM call or a
    fuzzy match over feature_key, which felt like overkill for a v1.
  - The model can't reference utterance IDs (it never sees them), so the
    transcript is rendered with bracketed turn numbers on learner lines only,
    and the model is asked to cite the turn number it's reporting on.
  - level_observations are the sole input to CEFR estimation in this pass
    (levels.py's docstring also lists cheaper computed signals — lexical
    stats, spaCy parse depth — as a later, non-LLM addition; deliberately not
    built here, see ROADMAP Phase 1's "optional" line).
  - A companion session can touch several languages (ADR-014); this call is
    scoped to ONE target language at a time. SessionRunner._run_sweeps() calls
    it once per distinct non-native language the learner actually spoke,
    against the SAME full transcript each time (so the model keeps
    conversational context) but told to ignore every other language's turns.
"""

from __future__ import annotations

from typing import Callable, Literal

import anthropic
from pydantic import BaseModel, Field

from faria.config import ModelsConfig
from faria.domain.models import Utterance
from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity
from faria.tutor.prompts import language_name

_DimensionName = Literal[
    "lexical_range", "grammatical_accuracy", "syntactic_complexity", "comprehension"
]


class SweepErrorItem(BaseModel):
    turn_index: int = Field(
        description="The bracketed [N] learner turn number this error came from."
    )
    span_text: str
    category: ErrorCategory
    subcategory: ErrorSubcategory
    severity: Severity
    confidence: float = Field(description="0.0-1.0")
    feature_key: str = Field(
        description=(
            "Normalized 'category:subcategory:feature' fingerprint, matching the "
            "convention used elsewhere in this project."
        )
    )
    correction: str
    explanation: str
    interference_source: str | None = None


class SweepLevelObservation(BaseModel):
    dimension: _DimensionName
    value: float = Field(description="CEFR-scale estimate for this dimension: 1.0 (A1) to 6.0 (C2)")
    weight: float = Field(description="Evidence strength, roughly 0.5 (thin) to 2.0 (strong)")
    rationale: str = Field(description="One sentence, grounded in something specific from the transcript")


class SweepStructureUse(BaseModel):
    item_id: str = Field(description="The structure's id, exactly as listed.")
    correct_uses: int
    incorrect_uses: int


class SessionSweep(BaseModel):
    missed_errors: list[SweepErrorItem] = Field(
        description=(
            "Genuine errors NOT already visibly corrected by the tutor in its own "
            "replies. If the tutor already recast or named an error, do not "
            "re-report it here."
        )
    )
    level_observations: list[SweepLevelObservation] = Field(
        description="Omit a dimension entirely if the transcript gives too little evidence to judge it."
    )
    structure_uses: list[SweepStructureUse] = Field(
        default_factory=list,
        description=(
            "Only for the listed structures the learner actually attempted in their own "
            "turns (ADR-034); empty if none were listed or attempted."
        ),
    )


def _render_with_turn_indices(utterances: list[Utterance]) -> str:
    lines = []
    for u in utterances:
        if u.speaker == "learner":
            lines.append(f"[{u.turn_index}] LEARNER: {u.text}")
        else:
            lines.append(f"TUTOR: {u.text}")
    return "\n".join(lines)


def generate_sweep(
    utterances: list[Utterance],
    target_language: str,
    models: ModelsConfig,
    on_usage: Callable[[object], None] | None = None,
    structures: list[tuple[str, str]] | None = None,
) -> SessionSweep:
    """One structured-output pass over the transcript, scoped to one target
    language; validated by pydantic. Call once per target language a
    multilingual session touched (see module docstring).

    ``on_usage`` receives the raw response for token/cost accounting — a
    multilingual session runs this once per language, so sweeps are often the
    larger half of a session's bill."""
    client = anthropic.Anthropic()
    target = language_name(target_language)
    # Syllabus coverage (ADR-034): which tracked structures did the learner use, and how well?
    structure_job = ""
    if structures:
        listed = "; ".join(f"[{item_id}] {title}" for item_id, title in structures)
        structure_job = (
            " (3) For each of these structures that the learner attempted in their own "
            f"{target} turns, count their correct and incorrect uses (skip any they didn't "
            f"attempt; uses by the tutor don't count): {listed}."
        )
    response = client.messages.parse(
        model=models.summarizer,
        max_tokens=models.max_summary_tokens,
        system=(
            f"You are a careful {target} language assessor reviewing a completed "
            "tutoring transcript, not a participant in it. The transcript may "
            "include other languages the learner was practicing, or their "
            f"native language — ignore all of that; assess ONLY their {target} "
            f"production. Two jobs: (1) find {target} errors a live, "
            "conversationally-distracted reviewer would plausibly have missed — "
            "skip anything the tutor already visibly corrected in its own "
            f"replies; (2) rate the learner's demonstrated {target} level on "
            "each dimension you have real evidence for, citing something "
            "specific from the transcript. Do not guess a dimension you can't "
            "ground in the transcript — omit it instead."
            + structure_job
        ),
        messages=[
            {"role": "user", "content": _render_with_turn_indices(utterances)}
        ],
        output_format=SessionSweep,
    )
    if on_usage is not None:
        on_usage(response)
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError("Sweep returned unparseable output")
    return parsed
