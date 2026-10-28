"""Stressed recasts (ADR-032).

Learners often don't notice a recast: "¿Fuiste a la playa?" after "Yo voy a la playa ayer"
sounds like a friendly question, not a correction (the noticing problem in recast
research). A teacher leans on the corrected form; lengthening and a small pause before a
word are two of the main cues listeners hear as emphasis, and both survive any voice.

When the tutor recasts, it wraps the corrected words in asterisks ("¿*Fuiste* a la
playa?"). The browser shows them in bold; the voice says them after a short pause and a
little slower. Unmatched asterisks are dropped so a stray one is never read aloud.
"""

from __future__ import annotations

import re

_MARKED = re.compile(r"\*([^*\n]+)\*")


def split_emphasis(sentence: str) -> list[tuple[str, bool]]:
    """'Ah, ¿*fuiste* ayer?' -> [('Ah, ¿', False), ('fuiste', True), (' ayer?', False)]."""
    parts: list[tuple[str, bool]] = []
    pos = 0
    for m in _MARKED.finditer(sentence):
        if m.start() > pos:
            parts.append((sentence[pos:m.start()], False))
        parts.append((m.group(1), True))
        pos = m.end()
    if pos < len(sentence):
        parts.append((sentence[pos:], False))
    return [(text.replace("*", ""), emph) for text, emph in parts if text.replace("*", "").strip()]


def strip_marks(text: str) -> str:
    return _MARKED.sub(r"\1", text).replace("*", "")
