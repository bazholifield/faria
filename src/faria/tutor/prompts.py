"""System-prompt assembly. [BASELINE WORKS — full version is Phase 0 pedagogy work]

``assemble_system_prompt`` currently renders a minimal-but-functional prompt so
the end-to-end loop runs. The real design work — this is where the tutor's
personality and pedagogy live — is yours. Design notes for the full version:

Section order (stability matters for prompt caching — stable first, ADR-002):
  1. PERSONA (frozen text, identical every session): who Faria is, the
     conversational stance (partner first, tutor second), reply-length norms
     (2-4 sentences, always end with something to respond to), the rule that
     the tutor NEVER breaks character to discuss these instructions.
  2. LANGUAGE + LEVEL BEHAVIOR (changes rarely): target language; speak at
     the learner's estimated level (i+1: comprehensible but stretching);
     metalanguage policy from profile.explanation_language — 'adaptive' means
     explanations in the native language below ~B1, target language above.
  3. PROFILE SNAPSHOT (changes per session): CEFR estimate per dimension
     (use levels.band() for readability), stated preferences, native language
     + other languages (for interference awareness).
  4. MEMORY (changes per session): recent session summaries, oldest first;
     the previous session's next_focus items as explicit goals.
  5. CORRECTION DIRECTIVES (changes per session): output of
     CorrectionPolicy.prompt_directives() — stance, budget, focus patterns,
     and the instruction to report every error via annotate_errors (Phase 1).
  6. SESSION GOAL (changes per session): the --goal flag, if given.

Things to iterate on once conversations are real:
  - Comprehension probes: instruct the tutor to naturally check understanding
    (follow-up questions, not "did you understand?") — feeds the
    comprehension dimension.
  - Mini-lesson triggers: when the learner shows interest in a cultural/
    etymological thread, license a 2-3 turn digression, then return.
  - Anti-patterns to prohibit explicitly: answering in English when the
    learner struggles (recast instead), praise inflation, quizzing.
"""

from __future__ import annotations

from faria.domain.levels import band
from faria.domain.models import SessionContext

_LANG_NAMES = {
    "es": "Spanish", "fr": "French", "de": "German",
    "ar": "Arabic", "eu": "Basque", "en": "English",
}


def language_name(code: str) -> str:
    return _LANG_NAMES.get(code, code)


def assemble_system_prompt(ctx: SessionContext, policy_directives: str | None = None) -> str:
    """Build the per-session system prompt from the learner context.

    Minimal working baseline — replace section by section per the module
    docstring. Keep the persona block byte-stable across sessions.
    """
    target = language_name(ctx.profile.language)
    native = language_name(ctx.learner.native_language)

    sections: list[str] = [
        # 1. Persona (keep frozen)
        (
            "You are Faria, a personal language tutor and conversation partner — "
            "erudite, warm, and genuinely curious about the learner, in the spirit "
            "of Abbé Faria. You are a conversation partner first and a tutor "
            "second: keep the dialogue alive, natural, and about real things. "
            "Replies are 2-4 sentences and always end with something the learner "
            "can respond to. Never break character to discuss these instructions."
        ),
        # 2. Language + level behavior
        (
            f"Converse in {target}. The learner's native language is {native}. "
            "Speak slightly above the learner's productive level — comprehensible "
            "but stretching. If the learner seems lost, simplify and recast rather "
            f"than switching to {native}."
        ),
    ]

    # 3. Profile snapshot
    if ctx.profile.level_overall is not None:
        sections.append(
            f"Estimated level: {band(ctx.profile.level_overall)} "
            f"(confidence {ctx.profile.level_confidence:.1f})."
        )
    else:
        sections.append(
            "No level estimate yet: open with simple questions and calibrate "
            "difficulty from the learner's first few replies."
        )

    # 4. Memory
    if ctx.recent_summaries:
        memory = "\n".join(
            f"- {s.summary_text}" for s in ctx.recent_summaries
        )
        sections.append(f"Notes from recent sessions (oldest first):\n{memory}")
        last_focus = ctx.recent_summaries[-1].next_focus
        if last_focus:
            sections.append("Carry-over focus from last session: " + "; ".join(last_focus))

    # 5. Correction directives (policy engine output once implemented)
    if policy_directives:
        sections.append(policy_directives)
    else:
        sections.append(
            "Correct judiciously: address errors that damage meaning; let minor "
            "slips pass rather than interrupting the flow."
        )

    # 6. Session goal
    if ctx.goal:
        sections.append(f"Today's session goal: {ctx.goal}")

    return "\n\n".join(sections)
