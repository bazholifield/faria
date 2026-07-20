"""Session control tool (ADR-015): the conversation-as-UI mechanism.

Alongside the annotation sidecar, the model carries a second tool the learner
can invoke by just talking — "switch to German", "correct me less", "you can
test me sometimes" — and the tutor may invoke on its own initiative for a
surprise language pivot (ADR-016/ROADMAP north star). Every change goes
through this tool, so every change is logged and auditable: "the UI is the
conversation," not vibes the model might or might not act on consistently.

Two call sites process this tool's output:
  - SessionRunner._apply_state_change() applies the change (switches the
    active language context, updates a profile's correction settings, or
    updates the home profile's pivot-frequency preference) and rebuilds the
    system prompt so the change takes effect on the tutor's next reply.
  - Learner-requested changes always apply immediately. Tutor-initiated
    pivots (reason="tutor_pivot") are subject to a minimum-turn-gap cooldown
    so an overzealous model can't pivot every turn — see
    config.CorrectionConfig.min_turns_between_pivots.
"""

from __future__ import annotations

from dataclasses import dataclass

SESSION_CONTROL_TOOL: dict = {
    "name": "update_session_state",
    "description": (
        "Change how this session runs. Call this whenever the learner asks to "
        "switch which language you're speaking, change how much you correct "
        "them, change how you correct them (recasts vs. explicit), or give you "
        "standing permission to surprise-pivot them into another language to "
        "test them. Also call this yourself, unprompted, when you decide to "
        "pivot the learner into a different language as a surprise "
        "comprehension check (reason='tutor_pivot') — only do this if the "
        "system prompt says you have permission to. Always call the tool "
        "instead of just announcing a change in your reply; a change only "
        "takes effect once you call it, on your NEXT reply."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "switch_language",
                    "set_correction_frequency",
                    "set_correction_style",
                    "set_pivot_frequency",
                ],
                "description": "Which setting to change.",
            },
            "language": {
                "type": ["string", "null"],
                "description": (
                    "BCP-47/ISO 639-1 code for the new active language, e.g. "
                    "'en', 'de', 'fr', 'es', 'zh', 'ja', 'ko'. Required (non-null) "
                    "when action is switch_language, else null."
                ),
            },
            "correction_frequency": {
                # nullable enum: a bare `type: ["string","null"]` + `enum`
                # combo is rejected by the API's strict-schema validator, so
                # nullability is expressed via anyOf instead (also supported
                # under structured outputs; see shared/tool-use-concepts.md).
                "anyOf": [
                    {"type": "string", "enum": ["off", "minimal", "balanced", "aggressive"]},
                    {"type": "null"},
                ],
                "description": (
                    "New correction frequency. Required (non-null) when action "
                    "is set_correction_frequency, else null."
                ),
            },
            "correction_style": {
                "anyOf": [
                    {"type": "string", "enum": ["recast", "recast_plus_note", "explicit"]},
                    {"type": "null"},
                ],
                "description": (
                    "New correction style. Required (non-null) when action is "
                    "set_correction_style, else null."
                ),
            },
            "pivot_frequency": {
                "anyOf": [
                    {"type": "string", "enum": ["off", "occasional", "frequent"]},
                    {"type": "null"},
                ],
                "description": (
                    "New surprise-pivot frequency. Required (non-null) when "
                    "action is set_pivot_frequency, else null."
                ),
            },
            "reason": {
                "type": "string",
                "enum": ["learner_request", "tutor_pivot"],
                "description": (
                    "'learner_request' if the learner asked for this change; "
                    "'tutor_pivot' only for a switch_language call YOU initiated "
                    "as a surprise test."
                ),
            },
        },
        "required": [
            "action",
            "language",
            "correction_frequency",
            "correction_style",
            "pivot_frequency",
            "reason",
        ],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class SessionStateChange:
    action: str
    language: str | None
    correction_frequency: str | None
    correction_style: str | None
    pivot_frequency: str | None
    reason: str


def parse_control_call(tool_input: dict) -> SessionStateChange:
    return SessionStateChange(
        action=tool_input["action"],
        language=tool_input.get("language"),
        correction_frequency=tool_input.get("correction_frequency"),
        correction_style=tool_input.get("correction_style"),
        pivot_frequency=tool_input.get("pivot_frequency"),
        reason=tool_input.get("reason", "learner_request"),
    )
