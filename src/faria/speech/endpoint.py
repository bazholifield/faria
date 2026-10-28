"""Is the learner finished, or mid-thought? (ADR-030)

Judged on the early transcript of a pause (vad.Probe). Whisper punctuates, and learners
who are searching for a word usually trail off on a connector or a filler: "y…",
"porque…", "and, um…", "weil äh…". So:

- "hold": the transcript ends on a connector, article, filler or trailing comma/ellipsis,
  so wait longer;
- "end": it ends with . ? or ! on a real word and is at least two words long, so the
  turn can end early;
- None: no opinion (one word, no punctuation), so the usual timing decides.

Deliberately simple and conservative: a wrong "hold" costs a moment's wait, a wrong
"end" interrupts the learner, so "end" needs both punctuation and a non-connector word.
"""

from __future__ import annotations

import re

# Words a sentence rarely ends on: connectors, articles, prepositions, fillers. Words that
# can end one ("I think so", "Kommst du mit?", "Mach zu!", "Et alors ?") are left out.
CONTINUATIONS: dict[str, set[str]] = {
    "en": {"and", "or", "but", "because", "if", "when", "the", "a", "an", "of", "with", "um",
           "uh", "er", "erm", "my"},
    "es": {"y", "e", "o", "u", "pero", "porque", "que", "como", "cuando", "si", "entonces",
           "el", "la", "los", "las", "un", "una", "de", "del", "a", "al", "en", "con", "para", "por",
           "mi", "eh", "em", "mm"},
    "fr": {"et", "ou", "mais", "parce", "que", "qui", "quand", "si", "le", "la",
           "les", "un", "une", "des", "de", "du", "à", "au", "avec", "pour", "mon", "ma",
           "euh", "ben", "bah", "hum"},
    "de": {"und", "oder", "aber", "weil", "dass", "wenn", "der", "die", "den", "dem", "ein",
           "eine", "einen", "für", "von", "mein", "meine", "äh", "ähm", "öh"},
}
_WORDS = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)


def verdict(text: str, language: str) -> str | None:
    t = text.strip()
    if not t:
        return None
    words = _WORDS.findall(t.lower())
    if not words:
        return None
    if t.endswith(("…", "...", ",", "-", "–")):
        return "hold"
    if words[-1] in CONTINUATIONS.get(language, set()):
        return "hold"
    if t[-1] in ".?!" and len(words) >= 2:
        return "end"
    return None
