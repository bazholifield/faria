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

from faria.domain.taxonomy import (
    SEVERITY_ANCHORS,
    ErrorCategory,
    ErrorSubcategory,
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
                                "Reuse a key from the learner's known patterns when one fits."
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
