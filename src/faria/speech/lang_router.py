"""Per-sentence language identification for TTS routing (ADR-024).

Problem observed live: in English mode the tutor drops in a Spanish phrase —
exactly what a language companion should do — and the English Piper voice
reads it with ludicrous phonemes. Fix: identify each sentence's language just
before synthesis and route it to that language's voice.

py3langid with a CONSTRAINED language set: unconstrained short-text LID is
noisy, but "is this sentence English, Spanish, French, German or Arabic?" is
easy. Guards: short fragments and low-confidence calls fall back to the
active language, so a borderline sentence can at worst be read by the voice
that would have read it anyway. Mixed sentences ("the word 'sobremesa'
means...") still read in the dominant language's voice — per-word switching
is a non-goal; a code-switching-capable API voice (Phase 4) is the real
answer there.
"""

from __future__ import annotations

MIN_CHARS = 12          # shorter fragments: not enough signal, use the default
MIN_CONFIDENCE = 0.85   # below this, trust the active language over the detector


class SentenceLanguageRouter:
    def __init__(self, languages: list[str]) -> None:
        """``languages``: the closed set to choose among (the languages we
        actually have voices for)."""
        from py3langid.langid import MODEL_FILE, LanguageIdentifier

        self._ident = LanguageIdentifier.from_pickled_model(MODEL_FILE, norm_probs=True)
        self._languages = [l for l in languages if l]
        if self._languages:
            self._ident.set_languages(self._languages)

    def detect(self, sentence: str, default: str) -> str:
        """Language to synthesize ``sentence`` in; ``default`` on any doubt."""
        text = sentence.strip()
        if len(text) < MIN_CHARS or not self._languages:
            return default
        lang, prob = self._ident.classify(text)
        if lang != default and float(prob) >= MIN_CONFIDENCE:
            return lang
        return default
