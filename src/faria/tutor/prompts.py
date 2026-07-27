"""System-prompt assembly.

First-pass implementation of the full section design. The persona block is
where the product's character lives — iterate on it conversationally (edit
text, `faria chat`, judge, repeat); nothing else changes when you tune it.

Section order (stability matters for prompt caching — stable first, ADR-002):
  1. persona (frozen text, byte-identical every session)
  2. language + level behavior (changes rarely; home-base wording when the
     active language IS the learner's native language — ADR-014/019)
  3. profile snapshot (changes per session)
  4. memory: summaries + carry-over focus (changes per session)
  5. correction directives from CorrectionPolicy (changes per session)
  6. session control: always-on instructions for learner-requested language/
     correction changes, plus a conditional surprise-pivot section when the
     learner has opted in (ADR-015)
  7. session goal (changes per session)

Personas are named blocks (ADR-016) selected via profile preferences
(``preferences["persona"]``, default "mentor") so alternatives can be added
without touching assembly logic. Rebuilt in full on every language switch
(SessionRunner._rebuild_prompt) — mid-session changes already invalidate the
prompt cache for the rest of that session, which is an accepted tradeoff at
personal scale (ADR-015).
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


def assemble_system_prompt(
    ctx: SessionContext,
    policy_directives: str | None = None,
    pivot_frequency: str = "off",
    other_known_languages: list[str] | None = None,
    audio: bool = False,
) -> str:
    """Build the system prompt for the currently active language.

    ``pivot_frequency`` and ``other_known_languages`` are session-wide
    concepts (not properties of any one language's profile), computed by
    SessionRunner from the home profile's preferences and the learner's
    profile history respectively — see ADR-019.
    """
    is_home_base = ctx.profile.language == ctx.learner.native_language
    target = language_name(ctx.profile.language)
    native = language_name(ctx.learner.native_language)

    persona_name = ctx.profile.preferences.get("persona", "mentor")
    sections: list[str] = [PERSONAS.get(persona_name, _MENTOR)]

    # 2. Language + level behavior
    sections.append(_language_section(ctx, is_home_base, target, native))

    # 3. Profile snapshot
    sections.append(_profile_snapshot(ctx))

    # 4. Memory
    memory = _memory_section(ctx, target, is_home_base)
    if memory:
        sections.append(memory)

    # 5. Correction directives (home base has nothing to correct)
    if policy_directives and not is_home_base:
        sections.append(policy_directives)
        known_keys = _known_keys_section(ctx)
        if known_keys:
            sections.append(known_keys)

    # 6. Session control — always-on learner-driven changes, plus an opt-in
    # surprise-pivot addendum.
    sections.append(_CONTROL_INSTRUCTIONS)
    if pivot_frequency != "off" and other_known_languages:
        sections.append(_pivot_section(pivot_frequency, other_known_languages))

    # 7. Audio-session ground rules (ADR-023): the transcript is fallible.
    if audio:
        sections.append(_AUDIO_INSTRUCTIONS)

    # 8. Session goal
    if ctx.goal:
        sections.append(f"Today's session goal: {ctx.goal}")

    return "\n\n".join(sections)


_AUDIO_INSTRUCTIONS = (
    "This is a SPOKEN conversation: the learner talks, and you read a "
    "speech-recognition transcript — which can be wrong. A learner turn may "
    "end with a bracketed [transcription note: ...] listing words the "
    "recognizer was unsure of; that note is machine metadata, NOT the "
    "learner's words — never respond to it directly and never report errors "
    "inside it. Ground rules that follow from fallible transcripts: (1) never "
    "correct or annotate a flagged word — the learner may not have said it; "
    "(2) if a phrase seems absurd or out of character for this learner, "
    "suspect the transcript first: ask naturally what they meant, and offer "
    "the natural way to say what they were probably going for; (3) ignore "
    "anything that is obviously transcription noise rather than speech. Also: "
    "your replies are spoken aloud — no lists, no headings, no formatting, "
    "nothing you wouldn't say with your voice."
)


def _language_section(ctx: SessionContext, is_home_base: bool, target: str, native: str) -> str:
    if is_home_base:
        return (
            f"You're currently speaking {native} — this is home base: ambient "
            "conversation, companionship, and teaching about whatever the "
            "learner is doing or curious about. They may ask you to switch into "
            "a language they're practicing at any point; you may also teach "
            "them a word or phrase in another language in passing without "
            "fully switching."
        )
    return (
        f"Converse in {target}. The learner's native language is {native}. "
        "Speak just above the learner's productive level — comprehensible but "
        "stretching. If they flounder, simplify and recast rather than "
        f"switching to {native}. {_metalanguage_rule(ctx, native, target)}"
    )


_CONTROL_INSTRUCTIONS = (
    "The learner can reshape this session just by asking, at any point: switch "
    "which language you're speaking (e.g. 'let's do German', 'back to "
    "English'), change how much you correct them, change how you correct them "
    "(recasts vs. explicit), or give you standing permission to occasionally "
    "test them with a surprise switch. When they ask for any of these, call "
    "update_session_state to make the change — don't just say you will and "
    "then keep behaving the old way. A language switch takes effect on your "
    "NEXT reply, in the new language. Whenever you call this tool, ALSO write "
    "a normal conversational reply in the same message, in the language you "
    "were just using — answer whatever they asked, acknowledge the change if "
    "it's natural to. Never respond with only a tool call and no words."
)


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
    # pivot_frequency lives here structurally (ADR-019) but has its own prompt
    # section — don't double-render it as a generic "stated preference".
    hidden_keys = {"persona", "pivot_frequency"}
    prefs = {k: v for k, v in ctx.profile.preferences.items() if k not in hidden_keys}
    if prefs:
        rendered = "; ".join(f"{k}: {v}" for k, v in sorted(prefs.items()))
        lines.append(f"Stated preferences: {rendered}.")
    return "\n".join(lines)


def _memory_section(ctx: SessionContext, target: str, is_home_base: bool) -> str | None:
    if not ctx.recent_summaries:
        if is_home_base:
            return None  # nothing distinctive to say about a first home-base chat
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


def _known_keys_section(ctx: SessionContext) -> str | None:
    """Surfaces every feature_key already on record for this profile — not
    just the ones that have crossed the recurrence threshold — so the
    annotate_errors detector can reuse a key on the SECOND occurrence of a
    still-rare error instead of minting a near-duplicate fingerprint each
    time (observed live: the same "tengo vs. tiene" mistake logged under
    three different feature_keys across three calls before this section
    existed)."""
    if not ctx.known_feature_keys:
        return None
    keys = ", ".join(ctx.known_feature_keys)
    return (
        "When you call annotate_errors, check this list of the learner's "
        "already-logged error fingerprints FIRST. If an error is the same "
        "underlying mistake as one already here, reuse that exact feature_key "
        f"string verbatim — do not invent a new one for it: {keys}"
    )


def _pivot_section(frequency: str, other_languages: list[str]) -> str:
    langs = ", ".join(language_name(code) for code in other_languages)
    cadence = {
        "occasional": "rarely — no more than once every several exchanges, and only when it fits naturally",
        "frequent": "somewhat often, whenever a natural moment presents itself",
    }.get(frequency, "rarely")
    return (
        f"With the learner's standing permission, you may {cadence} pivot the "
        f"conversation into one of their other languages ({langs}) to test "
        "them, unannounced — call update_session_state(action='switch_language', "
        "language=<code>, reason='tutor_pivot'). This is a comprehension check, "
        "not a trick: stay on the same topic through the switch. If they seem "
        "genuinely lost or ask to switch back, do so immediately, the same way. "
        "Never pivot mid-sentence, and never two turns in a row."
    )
