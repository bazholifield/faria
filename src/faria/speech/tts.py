"""Text-to-speech interface (ADR-010). Phase 2.

Streaming-first contract: ``synthesize`` yields audio chunks so playback can
start while Claude is still generating — the session layer feeds it complete
sentences as they stream in, not the full reply. Implementations planned:
PiperSynthesizer (local, free, default), an API option (e.g. OpenAI TTS), and
later XTTS/ElevenLabs for custom cloned voices — which is why ``voice`` is an
opaque string the implementation interprets.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class AudioChunk:
    pcm: bytes
    sample_rate: int
    is_final: bool = False


class Synthesizer(ABC):
    #: True if one voice already handles mixed-language text well (an API voice),
    #: so the server should NOT split a sentence into per-language runs for it
    #: (ADR-026). False for single-language local voices like Piper.
    handles_code_switching: bool = False

    @abstractmethod
    def synthesize(self, text: str, language: str, voice: str | None = None) -> Iterator[AudioChunk]:
        """Yield audio for one sentence/fragment as chunks become available."""

    @abstractmethod
    def voices(self, language: str) -> list[str]:
        """Available voice identifiers for a language."""

    def preload(self, language: str) -> None:
        """Optional warm-up (download/load the language's voice ahead of the
        first sentence). No-op by default; local backends override."""
