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


class Transcriber(ABC):
    """One STT backend. Implementations: FasterWhisperTranscriber (Phase 2), ..."""

    @abstractmethod
    def transcribe(self, audio_pcm: bytes, sample_rate: int, language: str) -> TranscriptionResult:
        """Transcribe a complete utterance (VAD segmentation happens upstream)."""


class TranscriberRegistry:
    """Routes language -> backend; falls back to the default."""

    def __init__(self, default: Transcriber) -> None:
        self._default = default
        self._overrides: dict[str, Transcriber] = {}

    def register(self, language: str, transcriber: Transcriber) -> None:
        self._overrides[language] = transcriber

    def for_language(self, language: str) -> Transcriber:
        return self._overrides.get(language, self._default)
