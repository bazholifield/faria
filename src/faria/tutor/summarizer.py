"""End-of-session structured summary via ``client.messages.parse`` (ADR-007).

The call itself is implemented (mechanical: transcript in, validated
SessionSummary out). What the rest of the system *does* with the summary —
updating patterns, levels, and focus — is the learner-model work in domain/
and session/lifecycle.py. The end-of-session sweep (ADR-003, missed errors +
level observations) is a separate call in tutor/sweep.py, which shares
render_transcript() with this module.

One summary per SESSION regardless of how many languages it touched (ADR-014)
— it's a narrative of the whole conversation, not a per-language artifact —
so this module takes the raw utterances and derives which languages were
spoken from them, rather than depending on any one language's profile.
"""

from __future__ import annotations

from typing import Callable

import anthropic
from pydantic import BaseModel, Field

from faria.config import ModelsConfig
from faria.domain.models import SummaryRow, Utterance
from faria.tutor.prompts import language_name


class ErrorDigestItem(BaseModel):
    category: str
    description: str = Field(description="What the learner gets wrong, in one phrase")
    example: str = Field(description="A quoted example from this session")
    count: int


class LevelEvidenceItem(BaseModel):
    dimension: str = Field(
        description="One of: lexical_range, grammatical_accuracy, syntactic_complexity, comprehension"
    )
    assessment: str = Field(description="One-sentence impression grounded in the transcript")


class SessionSummary(BaseModel):
    """Structured output schema for the summary call."""

    summary_text: str = Field(
        description=(
            "3-5 sentences addressed to the tutor of the NEXT session: what was "
            "discussed, how the learner did, anything promised or left open."
        )
    )
    topics: list[str]
    vocab_exposed: list[str] = Field(
        description="Notable words/expressions the learner encountered or produced for the first time"
    )
    notable_errors: list[ErrorDigestItem]
    level_evidence: list[LevelEvidenceItem]
    next_focus: list[str] = Field(
        description="1-3 concrete things the next session should target"
    )


def render_transcript(utterances: list[Utterance]) -> str:
    return "\n".join(f"{u.speaker.upper()}: {u.text}" for u in utterances)


def generate_summary(
    utterances: list[Utterance],
    models: ModelsConfig,
    on_usage: Callable[[object], None] | None = None,
) -> SessionSummary:
    """One structured-output call over the transcript; validated by pydantic.

    ``on_usage`` receives the raw response so the caller can record tokens and
    cost (migration 0005). A callback rather than an extra return value: the
    summary is what this function is *for*, and every existing caller keeps
    working unchanged.
    """
    client = anthropic.Anthropic()
    languages = sorted({u.language for u in utterances})
    lang_list = ", ".join(language_name(code) for code in languages)
    response = client.messages.parse(
        model=models.summarizer,
        max_tokens=models.max_summary_tokens,
        system=(
            "You are the session archivist for a personal multilingual tutoring "
            f"companion. This session's languages: {lang_list}. Summarize the "
            "session below for whichever tutoring context picks it up next — "
            "note which language(s) were used for what if more than one came "
            "up. Be concrete and evidence-based: quote the learner where "
            "relevant, never flatter, and only report errors actually present "
            "in the transcript."
        ),
        messages=[{"role": "user", "content": render_transcript(utterances)}],
        output_format=SessionSummary,
    )
    if on_usage is not None:
        on_usage(response)
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError("Summarizer returned unparseable output")
    return parsed


def to_summary_row(summary: SessionSummary, session_id: int, model: str) -> SummaryRow:
    return SummaryRow(
        session_id=session_id,
        summary_text=summary.summary_text,
        topics=summary.topics,
        vocab_exposed=summary.vocab_exposed,
        error_digest=[e.model_dump() for e in summary.notable_errors],
        level_evidence=[e.model_dump() for e in summary.level_evidence],
        next_focus=summary.next_focus,
        model=model,
    )
