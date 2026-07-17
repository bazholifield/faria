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
    @abstractmethod
    def synthesize(self, text: str, language: str, voice: str | None = None) -> Iterator[AudioChunk]:
        """Yield audio for one sentence/fragment as chunks become available."""

    @abstractmethod
    def voices(self, language: str) -> list[str]:
        """Available voice identifiers for a language."""
