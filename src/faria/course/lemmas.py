"""Recognising a word in any of its forms (ADR-034): "fui" is a use of "ir".

Uses simplemma (a small dictionary-based lemmatizer) when it's installed; without it,
matching falls back to exact word forms, which still catches nouns and fixed
expressions but misses most verb forms. Install it into the venv to enable it:
    .venv/bin/pip install simplemma

A word is stored under a key: its tokens, lemmatized, with a leading article dropped
("la playa" -> "playa"), so "playas" or "en la playa" both match. Multi-word
expressions match as a contiguous run ("echar de menos" in "te echo mucho de menos"
does NOT match: the run is broken; "te echo de menos" does). German separable verbs
("aufstehen" vs "ich stehe auf") are a known miss.
"""

from __future__ import annotations

import re

try:
    import simplemma
except ImportError:  # optional: exact-form matching until it's installed
    simplemma = None

_TOKENS = re.compile(r"[^\W\d_]+", re.UNICODE)
_ARTICLES = {
    "es": {"el", "la", "los", "las", "un", "una", "unos", "unas", "lo"},
    "fr": {"le", "la", "les", "l", "un", "une", "des", "du"},
    "de": {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer"},
    "en": {"the", "a", "an", "to"},
}


def available() -> bool:
    return simplemma is not None


def _lemma(token: str, language: str) -> str:
    token = token.lower()
    if simplemma is None:
        return token
    try:
        return simplemma.lemmatize(token, lang=language).lower()
    except ValueError:  # language simplemma doesn't cover
        return token


def lemmas(text: str, language: str) -> list[str]:
    """The text's tokens in order, lemmatized."""
    return [_lemma(t, language) for t in _TOKENS.findall(text)]


def key(text: str, language: str) -> str:
    raw = [t.lower() for t in _TOKENS.findall(text)]
    toks = lemmas(text, language)
    # judged on the raw word: a lemmatizer may turn "la" into "el"
    if len(raw) > 1 and raw[0] in _ARTICLES.get(language, set()):
        toks = toks[1:]
    return " ".join(toks)


def contains(sequence: list[str], word_key: str) -> bool:
    target = word_key.split()
    if not target:
        return False
    n = len(target)
    return any(sequence[i:i + n] == target for i in range(len(sequence) - n + 1))
