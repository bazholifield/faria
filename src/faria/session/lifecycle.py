"""SessionRunner: orchestrates one tutoring session end to end.

Transport-independent on purpose (ADR-001): the Phase 0 CLI drives this
directly; the Phase 2 WebSocket handler will drive the same object with audio
transcripts. Nothing here knows about terminals or sockets.

Lifecycle:
  start()  — load learner + profile + memory, assemble the system prompt,
             open a session row.
  turn()   — persist the learner utterance, stream the tutor reply (yielding
             text deltas), persist the reply.
             Phase 1 additions here: pass ERROR_ANNOTATION_TOOL to the brain,
             pull the annotate_errors tool_use block off the final message,
             run each error through CorrectionPolicy.decide(), upsert patterns
             and record events.
  end()    — generate + store the structured summary, close the session row.
             Phase 1 additions: run the transcript sweep, record level
             observations, update dimension estimates, surface the recap
             queue to the caller.
"""

from __future__ import annotations

from typing import Iterator

from faria.config import Settings
from faria.db.repository import Repository
from faria.domain.models import SessionContext
from faria.tutor.brain import TutorBrain
from faria.tutor.prompts import assemble_system_prompt
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

    @property
    def context(self) -> SessionContext:
        assert self._ctx is not None, "call start() first"
        return self._ctx

    def start(self, language: str, learner_name: str, goal: str | None = None) -> SessionContext:
        learner = self._repo.get_or_create_learner(learner_name)
        profile = self._repo.get_or_create_profile(
            learner.id, language, self._settings.correction.default_aggressiveness
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
        # Phase 0 (yours): pass CorrectionPolicy.prompt_directives(...) here.
        self._system = assemble_system_prompt(ctx)
        return ctx

    def turn(self, user_text: str) -> Iterator[str]:
        """One conversation turn: yields the tutor reply as text deltas."""
        assert self._session_id is not None, "call start() first"
        self._repo.add_utterance(self._session_id, self._turn_index, "learner", user_text)
        self._turn_index += 1
        self._history.append({"role": "user", "content": user_text})

        with self._brain.stream_reply(self._system, self._history) as stream:
            for delta in stream.text_stream:
                yield delta
            final = stream.get_final_message()

        reply_text = "".join(
            block.text for block in final.content if block.type == "text"
        )
        self._repo.add_utterance(self._session_id, self._turn_index, "tutor", reply_text)
        self._turn_index += 1
        self._history.append({"role": "assistant", "content": reply_text})
        # Phase 1: extract annotate_errors tool_use from final.content,
        # run CorrectionPolicy.decide() per error, persist events + patterns.

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
