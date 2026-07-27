"""Per-run language identification for TTS routing (ADR-024, ADR-026).

Problem observed live: in English mode the tutor drops in a foreign phrase —
exactly what a language companion should do — and a single-language voice
either mangles the phrase or reads the whole sentence wrong. Fix: split each
sentence into contiguous same-language RUNS and synthesize each with its own
voice. "You could say bonjour mon ami" becomes [en]"You could say "
[fr]"bonjour mon ami" — two voices, one sentence.

Detector: **lingua**, not py3langid. The lightweight py3langid classified
"Allons manger" and "bonjour mon ami" as English and "qué" (Spanish) as
French — its short-text accuracy is too poor for this, and the whole feature
lives or dies on that accuracy. lingua is purpose-built for short and mixed
text, constrained to the languages we have voices for, and its
``detect_multiple_languages_of`` returns the code-switch segmentation directly
with character offsets.

Safety bias (unchanged goal): a false switch (base text in the wrong voice) is
a real glitch; a missed foreign phrase (read in the base voice) is just the
status quo. So a detected foreign segment shorter than MIN_FOREIGN_CHARS of
actual letters reverts to the base language, and any doubt falls back to base.
Trade-off accepted (ADR-024): Piper voices are different speakers per language,
so the speaker changes mid-sentence; a single code-switching cloned voice is
the Phase 4 answer.
"""

from __future__ import annotations

MIN_CHARS = 12          # whole strings shorter than this: skip detection
MIN_FOREIGN_CHARS = 5   # a foreign run needs at least this many letters to switch

# Our language codes -> lingua Language enum names. Only codes lingua supports
# and we might voice; unknowns are simply not routed.
_LINGUA_NAMES = {
    "en": "ENGLISH", "es": "SPANISH", "fr": "FRENCH", "de": "GERMAN",
    "ar": "ARABIC", "eu": "BASQUE", "zh": "CHINESE", "ja": "JAPANESE", "ko": "KOREAN",
}


def _alpha_len(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


class SentenceLanguageRouter:
    def __init__(self, languages: list[str]) -> None:
        """``languages``: the closed set to detect among (the languages we
        actually have voices for)."""
        from lingua import Language, LanguageDetectorBuilder

        self._codes = [l for l in languages if l in _LINGUA_NAMES]
        self._to_code: dict = {}
        lingua_langs = []
        for code in self._codes:
            lang = getattr(Language, _LINGUA_NAMES[code])
            lingua_langs.append(lang)
            self._to_code[lang] = code
        # Need >=2 languages for detection to be meaningful.
        self._detector = (
            LanguageDetectorBuilder.from_languages(*lingua_langs).build()
            if len(lingua_langs) >= 2
            else None
        )

    def detect(self, sentence: str, default: str) -> str:
        """Single dominant language for a whole string; ``default`` on doubt."""
        text = sentence.strip()
        if self._detector is None or len(text) < MIN_CHARS:
            return default
        lang = self._detector.detect_language_of(text)
        return self._to_code.get(lang, default) if lang is not None else default

    def split_runs(self, sentence: str, default: str) -> list[tuple[str, str]]:
        """Split ``sentence`` into (text, language) runs for per-run TTS. Always
        returns at least one run whose texts concatenate back to the original
        exactly; the common (monolingual) case returns a single run."""
        if self._detector is None or len(sentence) < MIN_CHARS:
            return [(sentence, default)]

        results = self._detector.detect_multiple_languages_of(sentence)
        if not results:
            return [(sentence, default)]

        runs: list[tuple[str, str]] = []
        cursor = 0
        for res in results:
            if res.start_index > cursor:  # gap (defensive): assign to base
                runs.append((sentence[cursor:res.start_index], default))
            seg = sentence[res.start_index:res.end_index]
            lang = self._to_code.get(res.language, default)
            if lang != default and _alpha_len(seg) < MIN_FOREIGN_CHARS:
                lang = default  # too little foreign text to justify a voice switch
            runs.append((seg, lang))
            cursor = res.end_index
        if cursor < len(sentence):
            runs.append((sentence[cursor:], default))

        return self._coalesce(runs)

    @staticmethod
    def _coalesce(runs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        merged: list[tuple[str, str]] = []
        for text, lang in runs:
            if merged and merged[-1][1] == lang:
                merged[-1] = (merged[-1][0] + text, lang)
            else:
                merged.append((text, lang))
        return merged
