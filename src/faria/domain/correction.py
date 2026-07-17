"""Correction policy engine (DECISIONS.md ADR-005). [STUB — yours to implement]

Division of labor with the LLM
------------------------------
The LLM is good at *executing* a correction gracefully mid-conversation and bad
at *consistently deciding* when to correct across sessions. So:

1. PRE-TURN (this module -> prompt): ``prompt_directives()`` compiles the
   profile's aggressiveness, the flow budget, and the current focus patterns
   into explicit instructions injected into the system prompt. The LLM makes
   the in-flight micro-decision (correct now / recast / let it go) *within*
   those constraints, and reports each error + what it did via the annotation
   sidecar (tutor/schema.py).

2. POST-TURN (sidecar -> this module): ``decide()`` runs over each reported
   error and deterministically assigns the final ``action`` tag that gets
   persisted, maintains the deferred-recap queue, and updates the turn budget.
   Where the model's self-report and the policy disagree (it corrected
   something the budget didn't allow), the event is logged with what actually
   happened — disagreement is data, not something to hide.

Rules v1 (proposed starting point — tune against real sessions)
---------------------------------------------------------------
Priority order; first match wins:

  R1. COMPREHENSION failure (category=comprehension) -> CLARIFIED, always,
      immediately. Repairing understanding is not "correction" and never
      counts against the budget.
  R2. severity >= 3 (DISRUPTIVE/BREAKDOWN) -> inline. Below ~B1 prefer
      CORRECTED_RECAST (reformulate naturally); B1+ CORRECTED_EXPLICIT with a
      one-line explanation.
  R3. Pattern with occurrence_count >= recurrence_escalation_threshold ->
      inline (explicit), and pattern goes on the focus list next session.
  R4. severity == 2 -> DEFERRED_RECAP if the recap queue has room (cap ~5 per
      session so the recap stays digestible), else LOGGED_SILENT.
  R5. severity == 1 (slips) -> LOGGED_SILENT, always. Correcting slips is
      noise; the learner knows the rule.

Budget (flow protection), applied to R2/R3 after the rule fires:
  - max_inline_per_turn (default 1): further inline candidates this turn are
    demoted to DEFERRED_RECAP.
  - max_inline_per_window over window_turns (default 2 per 5 turns): protects
    against a bad patch destroying conversational flow.
  - Aggressiveness scales the caps: minimal -> only R1 + BREAKDOWN inline;
    balanced -> defaults; aggressive -> caps doubled and R4 becomes inline
    when the budget allows.

Open design questions to resolve while implementing:
  - Should the same feature_key corrected inline this session be demoted to
    silent for the rest of the session (avoid harping)? Probably yes.
  - Does severity get weighted by detector confidence (< 0.6 never inline)?
  - Recast vs explicit threshold: fixed at B1 or per-profile preference?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from faria.config import CorrectionConfig
from faria.domain.models import ErrorEvent, ErrorPattern


class Aggressiveness(StrEnum):
    MINIMAL = "minimal"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class CorrectionAction(StrEnum):
    """Persisted in error_events.action — keep in sync with the SQL CHECK."""

    CORRECTED_EXPLICIT = "corrected_explicit"  # named the error, gave the form
    CORRECTED_RECAST = "corrected_recast"      # reformulated correctly in the reply, no meta-talk
    DEFERRED_RECAP = "deferred_recap"          # queued for the end-of-session recap
    LOGGED_SILENT = "logged_silent"            # recorded, never surfaced this session
    CLARIFIED = "clarified"                    # comprehension repair, not a correction


@dataclass
class TurnState:
    """Rolling budget state across one session, updated per turn."""

    turn_index: int = 0
    inline_used_this_turn: int = 0
    inline_turns_in_window: list[int] = field(default_factory=list)
    recap_queue: list[ErrorEvent] = field(default_factory=list)


class CorrectionPolicy:
    """Deterministic correction decisions. See module docstring for rules v1."""

    def __init__(self, config: CorrectionConfig, aggressiveness: Aggressiveness) -> None:
        self.config = config
        self.aggressiveness = aggressiveness

    def prompt_directives(
        self,
        focus_patterns: list[ErrorPattern],
        learner_level: float | None,
    ) -> str:
        """Compile correction directives for the system prompt.

        Should produce a short, imperative block covering: (a) the
        aggressiveness stance in plain language, (b) the inline budget as an
        explicit numeric constraint, (c) the focus patterns to watch for and
        correct on sight, (d) recast-vs-explicit guidance for the learner's
        level, (e) the instruction to report EVERY detected error via the
        annotation tool regardless of whether it was corrected (coverage in
        the log, selectivity in the conversation).
        """
        raise NotImplementedError  # Phase 0: yours

    def decide(
        self,
        error: ErrorEvent,
        pattern: ErrorPattern | None,
        state: TurnState,
        learner_level: float | None,
    ) -> CorrectionAction:
        """Assign the final action tag for one reported error (rules v1).

        Must be pure given its arguments except for mutating ``state``
        (budget counters, recap queue). Called once per error, in the order
        the detector reported them.
        """
        raise NotImplementedError  # Phase 0: yours

    def start_turn(self, state: TurnState) -> None:
        """Reset per-turn counters and slide the window. Trivial, implement with decide()."""
        raise NotImplementedError  # Phase 0: yours
