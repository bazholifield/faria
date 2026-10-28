"""The note_teaching tool (ADR-034): the mentor records what it just taught.

A third one-shot side channel next to annotate_errors and update_session_state, and
for the same reason: the model knows what it introduced; regex over its reply doesn't.
Words recorded here enter spaced review (course/review.py); syllabus ids move to
"introduced" in coverage; a topic id is marked as told so stories aren't repeated.
Only carried in target-language turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TEACHING_TOOL: dict = {
    "name": "note_teaching",
    "description": (
        "Record what you just taught, so it can come back for review later. Call it in "
        "the same message as your reply whenever you (a) introduce a word or expression "
        "the learner probably didn't know, or give them one they asked for; (b) point "
        "out or explain one of the structures listed under 'Ideas' (use its id); (c) "
        "tell one of the stories listed there (use its id). Don't record words the "
        "learner already used correctly themselves, and don't call it when you taught "
        "nothing new."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "words": {
                "type": "array",
                "description": "New words or expressions taught this turn (usually none or one).",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "As taught, in the target language, e.g. 'echar de menos'."},
                        "gloss": {"type": "string", "description": "Short English meaning."},
                        "asked": {"type": "boolean", "description": "True if the learner asked for it."},
                    },
                    "required": ["text", "gloss", "asked"],
                    "additionalProperties": False,
                },
            },
            "syllabus_items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ids (from the Ideas section) of structures you introduced or explained this turn.",
            },
            "topic": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Id of the story or topic card you told this turn, else null.",
            },
        },
        "required": ["words", "syllabus_items", "topic"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class TaughtWord:
    text: str
    gloss: str
    asked: bool


@dataclass(frozen=True)
class Teaching:
    words: list[TaughtWord] = field(default_factory=list)
    syllabus_items: list[str] = field(default_factory=list)
    topic: str | None = None


def parse_teaching(tool_input: dict) -> Teaching:
    return Teaching(
        words=[TaughtWord(w.get("text", "").strip(), w.get("gloss", "").strip(), bool(w.get("asked")))
               for w in tool_input.get("words") or [] if w.get("text", "").strip()],
        syllabus_items=[i for i in tool_input.get("syllabus_items") or [] if i],
        topic=tool_input.get("topic") or None,
    )
