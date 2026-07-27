"""Speech-to-text interface with per-language routing (ADR-010). Phase 2.

The registry exists because one backend won't fit all languages: faster-whisper
is the default, but Whisper's Basque (eu) support is weak — route `eu` to a
different implementation (HiTZ/Aholab fine-tune, or an API) without touching
the realtime loop.

Word-level confidence matters beyond transcription quality: low-confidence
spans on otherwise-fluent speech are the signal for `phonology:
pronunciation_suspected` error events (logged at low confidence, never
corrected inline off STT evidence alone).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faria.config import AudioConfig


@dataclass(frozen=True)
class WordSpan:
    word: str
    start_s: float
    end_s: float
    confidence: float


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    confidence: float                       # overall, 0..1
    words: list[WordSpan] = field(default_factory=list)
    # Whisper's own speechiness signals, for hallucination filtering (ADR-025).
    # Defaults are the "definitely fine" values so non-whisper backends and
    # test fakes pass the gate without setting them.
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


def is_probable_speech(result: TranscriptionResult, config: "AudioConfig") -> bool:
    """Does this transcript look like real speech, or whisper hallucinating on
    silence/noise (ADR-025)? Combines length, whisper's own no_speech/logprob
    signals, mean word confidence, and a non-linguistic-junk filter. Kept pure
    and testable; the WS loop calls it to decide whether a turn happened."""
    text = result.text.strip()
    if len(text) < 2:
        return False
    # Pure punctuation / music glyphs / bracketed stage directions ("[Music]",
    # "(wind)") are whisper silence-fillers, never a learner turn. Real words
    # are left to the probabilistic gates below (blocklisting real words like
    # "gracias" would drop legitimate short answers).
    if not any(ch.isalpha() for ch in text):
        return False
    if text[0] in "[(" and text[-1] in ")]":
        return False
    if result.no_speech_prob > config.stt_no_speech_max:
        return False
    if result.avg_logprob < config.stt_logprob_min:
        return False
    if result.words and result.confidence < config.stt_min_confidence:
        return False
    return True


class Transcriber(ABC):
    """One STT backend. Implementations: FasterWhisperTranscriber, ..."""

    @abstractmethod
    def transcribe(
        self, audio_pcm: bytes, sample_rate: int, language: str | None
    ) -> TranscriptionResult:
        """Transcribe a complete utterance (VAD segmentation happens upstream).

        ``language=None`` asks the backend to auto-detect — the right call for
        companion sessions, where the learner may answer in a different
        language than the active one ("switch back to English please" spoken
        while Spanish is active must not be force-decoded as Spanish).
        TranscriptionResult.language reports what was actually detected/used.
        """


class TranscriberRegistry:
    """Routes language -> backend; falls back to the default."""

    def __init__(self, default: Transcriber) -> None:
        self._default = default
        self._overrides: dict[str, Transcriber] = {}

    def register(self, language: str, transcriber: Transcriber) -> None:
        self._overrides[language] = transcriber

    def for_language(self, language: str) -> Transcriber:
        return self._overrides.get(language, self._default)
