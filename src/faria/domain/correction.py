"""Correction policy engine (DECISIONS.md ADR-005, ADR-013).

First-pass implementation written to the spec below — review and tune against
real sessions; the rules and constants are meant to be argued with.

Division of labor with the LLM
------------------------------
The LLM is good at *executing* a correction gracefully mid-conversation and bad
at *consistently deciding* when to correct across sessions. So:

1. PRE-TURN (this module -> prompt): ``prompt_directives()`` compiles the
   profile's aggressiveness, style, flow budget, and current focus patterns
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

Two axes, not one (ADR-013)
---------------------------
*Frequency* (off | minimal | balanced | aggressive) controls HOW OFTEN the
tutor intervenes; *style* (recast | recast_plus_note | explicit) controls HOW.
Default: balanced + recast — "Yo voy a la playa ayer" gets "Ah, ¿fuiste a la
playa ayer?", not a grammar note. Explicit correction is opt-in (style
setting) or reserved for BREAKDOWN. `off` means zero interventions of any
kind; logging continues silently.

Rules v1 (implemented below)
----------------------------
Priority order; first match wins:

  R0. category == ORTHOGRAPHY (spelling/diacritics/punctuation) ->
      LOGGED_SILENT, always, at every setting. The product is audio-first
      even when typed (ADR-013). Detector-side nuance: a load-bearing
      diacritic ("ayer hablo" for "habló") should reach us classified as
      morphosyntax:tense_aspect/conjugation, not orthography — if it didn't,
      that's a detector bug, not a reason to weaken this rule.
  R1. COMPREHENSION failure (category=comprehension) -> CLARIFIED, always,
      immediately — even at frequency `off`. Repairing understanding is
      conversation, not correction, and never counts against the budget.
  R2. severity >= 3 (DISRUPTIVE/BREAKDOWN) -> inline, in the profile's style:
      CORRECTED_RECAST by default at any level; CORRECTED_EXPLICIT only when
      style=explicit or severity == BREAKDOWN.
  R3. Pattern with occurrence_count >= recurrence_escalation_threshold ->
      inline (in the profile's style), and the pattern is on the prompt's
      focus list.
  R4. severity == 2 -> DEFERRED_RECAP if the recap queue has room
      (MAX_RECAP_QUEUE, so the recap stays digestible), else LOGGED_SILENT.
  R5. severity == 1 (slips) -> LOGGED_SILENT, always.

Budget (flow protection), applied to R2/R3 after the rule fires:
  - max_inline_per_turn (default 1): further inline candidates this turn are
    demoted to DEFERRED_RECAP.
  - max_inline_per_window over window_turns (default 2 per 5 turns).
  - Aggressiveness scales behavior: minimal -> only R1 + BREAKDOWN inline;
    balanced -> as written; aggressive -> caps doubled and R4-level (MINOR)
    errors become inline candidates when the budget allows.

Choices made in this first pass (revisit freely):
  - Confidence gate: detector confidence < MIN_INLINE_CONFIDENCE (0.6) never
    goes inline — an uncertain "error" interrupted for is worse than a real
    one let past. Demoted to recap/silent.
  - Harping guard: a feature_key corrected inline once in a session is silent
    for the rest of that session (not even recapped — it was already handled).
  - Style is applied uniformly at all levels; ``learner_level`` is accepted
    but unused, reserved for level-conditioned style tweaks.
  - recast_plus_note logs as CORRECTED_RECAST (the conversational move is a
    recast; the note is decoration). Adding a distinct action value would
    need a migration of the error_events CHECK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from faria.config import CorrectionConfig
from faria.domain.models import ErrorEvent, ErrorPattern
from faria.domain.taxonomy import ErrorCategory, Severity

#: Detector confidence below which an error may never be corrected inline.
MIN_INLINE_CONFIDENCE = 0.6

#: Cap on deferred corrections surfaced in the end-of-session recap.
MAX_RECAP_QUEUE = 5


class Aggressiveness(StrEnum):
    """HOW OFTEN the tutor intervenes. Persisted in language_profiles."""

    OFF = "off"            # zero interventions; logging continues silently
    MINIMAL = "minimal"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class CorrectionStyle(StrEnum):
    """HOW the tutor intervenes when it does. Persisted in language_profiles."""

    RECAST = "recast"                    # reformulate naturally, no meta-talk (default)
    RECAST_PLUS_NOTE = "recast_plus_note"  # recast + one short note
    EXPLICIT = "explicit"                # name the error, give the form


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
    inline_turns_in_window: list[int] = field(default_factory=list)  # one entry per inline correction
    corrected_feature_keys: set[str] = field(default_factory=set)    # harping guard
    recap_queue: list[ErrorEvent] = field(default_factory=list)


class CorrectionPolicy:
    """Deterministic correction decisions. See module docstring for the rules."""

    def __init__(
        self,
        config: CorrectionConfig,
        aggressiveness: Aggressiveness,
        style: CorrectionStyle = CorrectionStyle.RECAST,
    ) -> None:
        self.config = config
        self.aggressiveness = aggressiveness
        self.style = style

    # -- pre-turn: policy -> prompt ------------------------------------------

    def prompt_directives(
        self,
        focus_patterns: list[ErrorPattern],
        learner_level: float | None,
    ) -> str:
        """Compile correction directives for the system prompt."""
        if self.aggressiveness is Aggressiveness.OFF:
            return (
                "Correction policy: none. Do not correct the learner's language "
                "this session — no reformulation-as-correction, no notes, no "
                "accent or spelling fixes. Converse as you would with a fluent "
                "friend. If you genuinely cannot understand something, ask "
                "naturally for clarification; that is always allowed."
            )

        per_turn, per_window = self._effective_caps()
        lines = ["Correction policy for this session:"]
        lines.append("- Stance: " + self._stance_text())
        lines.append("- Style: " + self._style_text())
        lines.append(
            f"- Budget: at most {per_turn} correction(s) per learner turn, and at "
            f"most {per_window} across any {self.config.window_turns} turns. When "
            "over budget, let errors pass — conversational flow beats completeness."
        )
        lines.append(
            "- Never correct spelling, accents, or punctuation. Treat everything "
            "the learner writes as if it were speech."
        )
        lines.append(
            "- Let one-off slips pass silently: if the learner clearly knows the "
            "rule and just stumbled, it is not worth a correction."
        )
        lines.append(
            "- If you did not understand the learner, ask naturally for "
            "clarification. That is conversation repair, not correction, and is "
            "always allowed and does not count toward the budget."
        )
        if learner_level is not None and learner_level < 3.0:
            lines.append(
                "- This learner is below B1: prefer wordless corrections — pure "
                "recasts folded into your reply. Meta-talk costs more than it teaches."
            )
        if focus_patterns:
            lines.append(
                "- Recurring habits to watch for — correct these on sight, in your "
                "usual style (they still count toward the budget):"
            )
            for p in focus_patterns:
                label = p.description or p.feature_key
                lines.append(f"    - {label} (seen {p.occurrence_count}x)")
        return "\n".join(lines)

    def _stance_text(self) -> str:
        if self.aggressiveness is Aggressiveness.MINIMAL:
            return (
                "correct only when communication actually fails or the learner "
                "says something other than what they clearly meant. Otherwise let "
                "everything flow."
            )
        if self.aggressiveness is Aggressiveness.AGGRESSIVE:
            return (
                "correct most genuine errors, within the budget — the learner has "
                "asked for rigorous feedback. Still let pure slips pass."
            )
        return (
            "correct selectively: prioritize errors that distort meaning and the "
            "recurring habits listed below; let minor slips pass."
        )

    def _style_text(self) -> str:
        recast_example = (
            'fold the corrected form naturally into your reply, as a native '
            'speaker would ("Yo voy a la playa ayer" -> "Ah, ¿fuiste a la playa '
            'ayer?"). No meta-commentary, no "small correction:", no grammar labels.'
        )
        if self.style is CorrectionStyle.EXPLICIT:
            return (
                "name the error in one short phrase, give the correct form, then "
                "move on. One sentence of explanation at most."
            )
        if self.style is CorrectionStyle.RECAST_PLUS_NOTE:
            return (
                f"correct by RECASTING — {recast_example} You may add at most one "
                "very short note (a few words) when the error will clearly recur."
            )
        return (
            f"correct by RECASTING — {recast_example} Reserve an explicit "
            "correction for moments where communication has actually broken down."
        )

    # -- post-turn: sidecar -> decisions -------------------------------------

    def start_turn(self, state: TurnState) -> None:
        """Advance to the next turn: reset per-turn counters, slide the window."""
        state.turn_index += 1
        state.inline_used_this_turn = 0
        cutoff = state.turn_index - self.config.window_turns
        state.inline_turns_in_window = [t for t in state.inline_turns_in_window if t > cutoff]

    def decide(
        self,
        error: ErrorEvent,
        pattern: ErrorPattern | None,
        state: TurnState,
        learner_level: float | None,
    ) -> CorrectionAction:
        """Assign the final action tag for one reported error (rules v1).

        Pure given its arguments except for mutating ``state`` (budget
        counters, harping guard, recap queue). Call once per error, in the
        order the detector reported them. ``learner_level`` is reserved for
        level-conditioned style tweaks (unused in this pass).
        """
        # R0: audio-first — orthography is never a conversation topic.
        if error.category is ErrorCategory.ORTHOGRAPHY:
            return CorrectionAction.LOGGED_SILENT

        # R1: comprehension repair happens even at frequency `off`.
        if error.category is ErrorCategory.COMPREHENSION:
            return CorrectionAction.CLARIFIED

        if self.aggressiveness is Aggressiveness.OFF:
            return CorrectionAction.LOGGED_SILENT

        if (
            self._wants_inline(error, pattern)
            and error.confidence >= MIN_INLINE_CONFIDENCE
            and error.feature_key not in state.corrected_feature_keys
            and self._budget_allows(state)
        ):
            state.inline_used_this_turn += 1
            state.inline_turns_in_window.append(state.turn_index)
            state.corrected_feature_keys.add(error.feature_key)
            return self._inline_action(error)

        # Harping guard: corrected once this session -> silent thereafter
        # (not even recapped; it was already handled live).
        if error.feature_key in state.corrected_feature_keys:
            return CorrectionAction.LOGGED_SILENT

        # R4 + demoted inline candidates -> recap while there is room.
        if error.severity >= Severity.MINOR and len(state.recap_queue) < MAX_RECAP_QUEUE:
            state.recap_queue.append(error)
            return CorrectionAction.DEFERRED_RECAP

        # R5 and overflow.
        return CorrectionAction.LOGGED_SILENT

    # -- internals -----------------------------------------------------------

    def _wants_inline(self, error: ErrorEvent, pattern: ErrorPattern | None) -> bool:
        if self.aggressiveness is Aggressiveness.MINIMAL:
            return error.severity is Severity.BREAKDOWN
        if error.severity >= Severity.DISRUPTIVE:
            return True
        if (
            pattern is not None
            and pattern.occurrence_count >= self.config.recurrence_escalation_threshold
        ):
            return True
        if self.aggressiveness is Aggressiveness.AGGRESSIVE and error.severity is Severity.MINOR:
            return True
        return False

    def _budget_allows(self, state: TurnState) -> bool:
        per_turn, per_window = self._effective_caps()
        return (
            state.inline_used_this_turn < per_turn
            and len(state.inline_turns_in_window) < per_window
        )

    def _effective_caps(self) -> tuple[int, int]:
        per_turn = self.config.max_inline_per_turn
        per_window = self.config.max_inline_per_window
        if self.aggressiveness is Aggressiveness.AGGRESSIVE:
            return per_turn * 2, per_window * 2
        return per_turn, per_window

    def _inline_action(self, error: ErrorEvent) -> CorrectionAction:
        if self.style is CorrectionStyle.EXPLICIT or error.severity is Severity.BREAKDOWN:
            return CorrectionAction.CORRECTED_EXPLICIT
        return CorrectionAction.CORRECTED_RECAST
