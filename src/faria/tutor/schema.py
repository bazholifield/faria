"""Tool schema for the error-annotation sidecar (ADR-003).

The conversation call carries this tool. The system prompt instructs the model:
after composing its conversational reply, call ``annotate_errors`` exactly once
per learner turn, reporting EVERY error detected in the learner's last
utterance — including ones it chose not to correct (selectivity belongs in the
conversation, coverage belongs in the log). The reply text streams as normal
text blocks; the annotations arrive as a tool_use block on the same response,
so the log and the conversation can never disagree about what happened.

The enum values and severity anchors are generated from domain/taxonomy.py —
single source of truth for human and model raters.

Strict mode guarantees the input validates against this schema exactly
(no minimum/maximum constraints allowed under strict — confidence bounds are
by-description only, which is fine).
"""

from __future__ import annotations

from faria.domain.models import ErrorEvent
from faria.domain.taxonomy import (
    SEVERITY_ANCHORS,
    ErrorCategory,
    ErrorSubcategory,
    Severity,
    render_for_prompt,
)

_SEVERITY_TEXT = "\n".join(SEVERITY_ANCHORS.values())

ERROR_ANNOTATION_TOOL: dict = {
    "name": "annotate_errors",
    "description": (
        "Report every error detected in the learner's most recent utterance, "
        "whether or not you addressed it in your reply. Call exactly once per "
        "learner turn, after composing your conversational reply. If the "
        "utterance is error-free, call with an empty errors array.\n\n"
        + render_for_prompt()
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "span_text": {
                            "type": "string",
                            "description": "Exact substring of the learner utterance containing the error.",
                        },
                        "category": {
                            "type": "string",
                            "enum": [c.value for c in ErrorCategory],
                        },
                        "subcategory": {
                            "type": "string",
                            "enum": [s.value for s in ErrorSubcategory],
                        },
                        "severity": {
                            "type": "integer",
                            "enum": [1, 2, 3, 4],
                            "description": _SEVERITY_TEXT,
                        },
                        "confidence": {
                            "type": "number",
                            "description": "Your confidence this is a real error, 0.0-1.0.",
                        },
                        "feature_key": {
                            "type": "string",
                            "description": (
                                "Normalized fingerprint 'category:subcategory:feature' in "
                                "lowercase snake_case, e.g. "
                                "'morphosyntax:agreement:gender_noun_adj' or "
                                "'lexis:false_friend:actualmente' (lemma for lexical items). "
                                "The system prompt lists this learner's already-known "
                                "feature_keys for this language — reuse one verbatim if the "
                                "error is the same underlying mistake, rather than minting a "
                                "near-duplicate."
                            ),
                        },
                        "correction": {
                            "type": "string",
                            "description": "The target-like form.",
                        },
                        "explanation": {
                            "type": "string",
                            "description": "One short learner-facing sentence on why.",
                        },
                        "interference_source": {
                            "type": ["string", "null"],
                            "description": (
                                "Language code suspected as the source of interference "
                                "(false friend / calque), else null."
                            ),
                        },
                        "handled": {
                            "type": "string",
                            "enum": ["corrected_explicit", "corrected_recast", "clarified", "not_addressed"],
                            "description": "What YOU did about this error in your reply.",
                        },
                    },
                    "required": [
                        "span_text",
                        "category",
                        "subcategory",
                        "severity",
                        "confidence",
                        "feature_key",
                        "correction",
                        "explanation",
                        "interference_source",
                        "handled",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["errors"],
        "additionalProperties": False,
    },
}


def parse_annotated_errors(
    tool_input: dict, utterance_id: int, detector: str
) -> list[ErrorEvent]:
    """Convert one ``annotate_errors`` tool call's input into ErrorEvents.

    ``action`` is left at the ErrorEvent default (logged_silent) — the caller
    runs each event through CorrectionPolicy.decide() to get the real value.
    ``model_action`` preserves the model's own "handled" self-report, kept
    separately so it can be compared against the policy's decision (ADR-005):
    the model may claim it corrected something the budget wouldn't have
    allowed, and that disagreement is worth keeping, not silently resolving.
    """
    events = []
    for raw in tool_input.get("errors", []):
        events.append(
            ErrorEvent(
                utterance_id=utterance_id,
                category=ErrorCategory(raw["category"]),
                subcategory=ErrorSubcategory(raw["subcategory"]),
                severity=Severity(raw["severity"]),
                confidence=raw["confidence"],
                feature_key=raw["feature_key"],
                span_text=raw.get("span_text"),
                correction=raw.get("correction"),
                explanation=raw.get("explanation"),
                interference_source=raw.get("interference_source") or None,
                model_action=raw.get("handled"),
                detector=detector,
            )
        )
    return events
