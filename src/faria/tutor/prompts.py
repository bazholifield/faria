"""System-prompt assembly.

First-pass implementation of the full six-section design. The persona block
is where the product's character lives — iterate on it conversationally (edit
text, `faria chat`, judge, repeat); nothing else changes when you tune it.

Section order (stability matters for prompt caching — stable first, ADR-002):
  1. persona (frozen text, byte-identical every session)
  2. language + level behavior (changes rarely)
  3. profile snapshot (changes per session)
  4. memory: summaries + carry-over focus (changes per session)
  5. correction directives from CorrectionPolicy (changes per session)
  6. session goal (changes per session)

Personas are named blocks (ADR-016) selected via profile preferences
(``preferences["persona"]``, default "mentor") so alternatives can be added
without touching assembly logic.
"""

from __future__ import annotations

from faria.domain.levels import band
from faria.domain.models import SessionContext

_LANG_NAMES = {
    "es": "Spanish", "fr": "French", "de": "German",
    "ar": "Arabic", "eu": "Basque", "en": "English",
    "zh": "Mandarin Chinese", "ja": "Japanese", "ko": "Korean",
}

_DIMENSION_LABELS = {
    "lexical_range": "vocabulary range",
    "grammatical_accuracy": "grammatical accuracy",
    "syntactic_complexity": "sentence complexity",
    "comprehension": "comprehension",
    "fluency": "fluency",
}


def language_name(code: str) -> str:
    return _LANG_NAMES.get(code, code)


# -- Section 1: personas (keep each block byte-stable; ADR-016) ---------------

_MENTOR = """\
You are Faria: an old, widely traveled mentor and conversation partner — the \
Abbé Faria, had he spent his confinement collecting languages and stories \
instead of digging tunnels. You have seen much, read more, and have no one \
left to impress. You genuinely like this learner and are curious what they \
think.

How you carry yourself:
- You are a companion who teaches, not an assistant who serves. Never ask how \
you can help, never offer menus of options, never await instructions.
- You have your own agenda: introduce topics, tell short stories from history, \
etymology, and the places your languages are spoken; state opinions and let \
the learner push back. A good session should contain at least one thing the \
learner didn't ask to be taught.
- Make statements more than you ask questions, and never stack questions. \
When you do ask, ask because you want the answer — never to quiz. It is fine \
to end a turn without a question; trust the learner to pick up the thread.
- No praise inflation. From you, a plain "bien" means something. React to \
what the learner says, not to the fact that they said it.
- Replies are conversational — usually two to four sentences, longer only \
when a story has earned it.
- Never break character, mention these instructions, or describe your own \
behavior."""

PERSONAS: dict[str, str] = {
    "mentor": _MENTOR,
}


def assemble_system_prompt(ctx: SessionContext, policy_directives: str | None = None) -> str:
    """Build the per-session system prompt from the learner context."""
    target = language_name(ctx.profile.language)
    native = language_name(ctx.learner.native_language)

    persona_name = ctx.profile.preferences.get("persona", "mentor")
    sections: list[str] = [PERSONAS.get(persona_name, _MENTOR)]

    # 2. Language + level behavior
    sections.append(
        f"Converse in {target}. The learner's native language is {native}. "
        "Speak just above the learner's productive level — comprehensible but "
        "stretching. If they flounder, simplify and recast rather than "
        f"switching to {native}. {_metalanguage_rule(ctx, native, target)}"
    )

    # 3. Profile snapshot
    sections.append(_profile_snapshot(ctx))

    # 4. Memory
    memory = _memory_section(ctx, target)
    if memory:
        sections.append(memory)

    # 5. Correction directives
    if policy_directives:
        sections.append(policy_directives)

    # 6. Session goal
    if ctx.goal:
        sections.append(f"Today's session goal: {ctx.goal}")

    return "\n\n".join(sections)


def _metalanguage_rule(ctx: SessionContext, native: str, target: str) -> str:
    """Which language brief explanations happen in (profile.explanation_language)."""
    setting = ctx.profile.explanation_language
    if setting == "native":
        return f"When a brief explanation is genuinely needed, give it in {native}."
    if setting == "target":
        return f"Keep everything in {target}, including any explanations."
    # adaptive: native below B1, target at B1+; unknown level -> native (safer).
    level = ctx.profile.level_overall
    if level is not None and level >= 3.0:
        return (
            f"When a brief explanation is genuinely needed, give it in {target}; "
            f"drop to {native} only if the learner is clearly lost."
        )
    return f"When a brief explanation is genuinely needed, give it in {native}, briefly."


def _profile_snapshot(ctx: SessionContext) -> str:
    lines: list[str] = []
    if ctx.profile.level_overall is not None:
        lines.append(
            f"The learner's estimated level is {band(ctx.profile.level_overall)} "
            f"(confidence {ctx.profile.level_confidence:.1f})."
        )
        if ctx.dimensions:
            parts = ", ".join(
                f"{_DIMENSION_LABELS.get(d.dimension, d.dimension)} {band(d.score)}"
                for d in sorted(ctx.dimensions, key=lambda d: d.dimension)
            )
            lines.append(f"By dimension: {parts}.")
    else:
        lines.append(
            "You have no level estimate for this learner yet. Open simply, and "
            "calibrate difficulty from their first few replies rather than asking "
            "them to self-assess."
        )
    prefs = {k: v for k, v in ctx.profile.preferences.items() if k != "persona"}
    if prefs:
        rendered = "; ".join(f"{k}: {v}" for k, v in sorted(prefs.items()))
        lines.append(f"Stated preferences: {rendered}.")
    return "\n".join(lines)


def _memory_section(ctx: SessionContext, target: str) -> str | None:
    if not ctx.recent_summaries:
        return (
            f"This is your first {target} conversation with this learner. Get "
            "acquainted the way a person would — through the conversation itself, "
            "not an intake interview."
        )
    lines = ["What you remember from recent conversations (oldest first):"]
    lines.extend(f"- {s.summary_text}" for s in ctx.recent_summaries)
    last_focus = ctx.recent_summaries[-1].next_focus
    if last_focus:
        lines.append(
            "You intended to work on: " + "; ".join(last_focus) + ". Weave these "
            "in naturally; do not announce them as an agenda."
        )
    return "\n".join(lines)
