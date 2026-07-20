"""SessionRunner: orchestrates one tutoring session end to end.

Transport-independent on purpose (ADR-001): the Phase 0 CLI drives this
directly; the Phase 2 WebSocket handler will drive the same object with audio
transcripts. Nothing here knows about terminals or sockets.

Lifecycle:
  start()  — load learner + profile + memory, assemble the system prompt,
             open a session row.
  turn()   — persist the learner utterance, stream the tutor reply (yielding
             text deltas), persist the reply, then process the annotation
             sidecar's tool_use block: each reported error goes through
             CorrectionPolicy.decide() and is persisted with pattern linking.
  end()    — generate + store the structured summary, close the session row.
             Still Phase 1 TODO: an end-of-session transcript sweep for
             missed errors + level observations -> levels.update_dimension()
             -> repo.upsert_dimension()/set_overall_level(). The recap queue
             built during turn() is already available via the recap_queue
             property for the caller to surface at end of session.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterator

from anthropic.types import Message

from faria.config import Settings
from faria.db.repository import Repository
from faria.domain.correction import (
    Aggressiveness,
    CorrectionPolicy,
    CorrectionStyle,
    TurnState,
)
from faria.domain.models import ErrorEvent, SessionContext
from faria.tutor.brain import TutorBrain
from faria.tutor.prompts import assemble_system_prompt
from faria.tutor.schema import ERROR_ANNOTATION_TOOL, parse_annotated_errors
from faria.tutor.summarizer import SessionSummary, generate_summary, to_summary_row


class SessionRunner:
    def __init__(self, repo: Repository, brain: TutorBrain, settings: Settings) -> None:
        self._repo = repo
        self._brain = brain
        self._settings = settings
        self._ctx: SessionContext | None = None
        self._session_id: int | None = None
        self._system: str = ""
        self._history: list[dict] = []
        self._turn_index: int = 0
        self._policy: CorrectionPolicy | None = None
        self._turn_state = TurnState()

    @property
    def context(self) -> SessionContext:
        assert self._ctx is not None, "call start() first"
        return self._ctx

    @property
    def recap_queue(self) -> list[ErrorEvent]:
        """Errors deferred this session (CorrectionAction.DEFERRED_RECAP), for /end."""
        return list(self._turn_state.recap_queue)

    def start(self, language: str, learner_name: str, goal: str | None = None) -> SessionContext:
        learner = self._repo.get_or_create_learner(learner_name)
        profile = self._repo.get_or_create_profile(
            learner.id,
            language,
            self._settings.correction.default_aggressiveness,
            self._settings.correction.default_style,
        )
        ctx = SessionContext(
            learner=learner,
            profile=profile,
            dimensions=self._repo.get_dimensions(profile.id),
            recent_summaries=self._repo.recent_summaries(
                profile.id, self._settings.memory.recent_summaries_in_prompt
            ),
            focus_patterns=self._repo.focus_patterns(
                profile.id, self._settings.correction.recurrence_escalation_threshold
            ),
            goal=goal,
        )
        session = self._repo.create_session(profile.id, mode="text", goal=goal)
        self._ctx = ctx
        self._session_id = session.id
        self._policy = CorrectionPolicy(
            self._settings.correction,
            Aggressiveness(profile.correction_aggressiveness),
            CorrectionStyle(profile.correction_style),
        )
        self._system = assemble_system_prompt(
            ctx, self._policy.prompt_directives(ctx.focus_patterns, profile.level_overall)
        )
        return ctx

    def turn(self, user_text: str) -> Iterator[str]:
        """One conversation turn: yields the tutor reply as text deltas."""
        assert (
            self._session_id is not None
            and self._policy is not None
            and self._ctx is not None
        ), "call start() first"
        self._policy.start_turn(self._turn_state)  # budget window tracks turns from day one
        learner_utt = self._repo.add_utterance(
            self._session_id, self._turn_index, "learner", user_text
        )
        self._turn_index += 1
        self._history.append({"role": "user", "content": user_text})

        with self._brain.stream_reply(
            self._system, self._history, tools=[ERROR_ANNOTATION_TOOL]
        ) as stream:
            for delta in stream.text_stream:
                yield delta
            final = stream.get_final_message()

        reply_text = "".join(
            block.text for block in final.content if block.type == "text"
        )
        self._repo.add_utterance(self._session_id, self._turn_index, "tutor", reply_text)
        self._turn_index += 1
        # Only the plain text goes into history — dropping the tool_use block
        # means we never send a matching tool_result back; the annotation
        # call is a one-shot sidecar, not a round trip (ADR-003).
        self._history.append({"role": "assistant", "content": reply_text})

        self._process_annotations(final, learner_utt.id)

    def _process_annotations(self, final: Message, learner_utterance_id: int) -> None:
        assert self._ctx is not None and self._policy is not None and self._session_id is not None
        detector = f"sidecar:{final.model}"
        for block in final.content:
            if block.type != "tool_use" or block.name != "annotate_errors":
                continue
            for raw_event in parse_annotated_errors(block.input, learner_utterance_id, detector):
                pattern = self._repo.upsert_pattern(
                    self._ctx.profile.id,
                    raw_event.category,
                    raw_event.subcategory,
                    raw_event.feature_key,
                    raw_event.explanation,
                )
                action = self._policy.decide(
                    raw_event, pattern, self._turn_state, self._ctx.profile.level_overall
                )
                self._repo.record_error_event(
                    self._session_id, replace(raw_event, action=action), pattern.id
                )

    def end(self) -> SessionSummary | None:
        """Close the session; returns the summary (None if nothing was said)."""
        assert self._session_id is not None and self._ctx is not None
        utterances = self._repo.session_utterances(self._session_id)
        if not utterances:
            self._repo.end_session(self._session_id, status="abandoned")
            return None

        summary = generate_summary(utterances, self._ctx, self._settings.models)
        self._repo.store_summary(
            to_summary_row(summary, self._session_id, self._settings.models.summarizer)
        )
        self._repo.end_session(self._session_id, status="completed")
        # Phase 1: transcript sweep -> error events; level observations ->
        # levels.update_dimension() -> repo.upsert_dimension()/set_overall_level().
        return summary

    def abandon(self) -> None:
        """Close without a summary (user quit or crash cleanup)."""
        if self._session_id is not None:
            self._repo.end_session(self._session_id, status="abandoned")
