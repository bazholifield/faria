"""OpenAI TTS implementation of the Synthesizer interface (ADR-010).

The pluggable API option promised in the roadmap: set audio.tts_default =
"openai" and put OPENAI_API_KEY in .env. Raw httpx rather than the openai SDK
— one endpoint doesn't justify a dependency.

UNTESTED LIVE: written to the documented API but never run against it (no
OPENAI_API_KEY on this machine). If you enable it and something's off, the
response format is the first thing to check.

Why it exists despite Piper being free: OpenAI voices handle code-switching
(a German aside mid-English-sentence) far better than a single-language Piper
voice, and cover Basque, which Piper doesn't. Per-utterance cost is real,
though — this stays opt-in.
"""

from __future__ import annotations

import os
from typing import Iterator

import httpx

from faria.config import AudioConfig
from faria.speech.tts import AudioChunk, Synthesizer

_OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
_PCM_SAMPLE_RATE = 24000  # response_format="pcm" is 24 kHz mono s16le per the docs


class OpenAISynthesizer(Synthesizer):
    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "audio.tts_default is 'openai' but OPENAI_API_KEY is not set in .env"
            )
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
        )

    def voices(self, language: str) -> list[str]:
        # OpenAI voices are language-agnostic; one list for all languages.
        return ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]

    def synthesize(
        self, text: str, language: str, voice: str | None = None
    ) -> Iterator[AudioChunk]:
        with self._client.stream(
            "POST",
            _OPENAI_TTS_URL,
            json={
                "model": self._config.openai_tts_model,
                "voice": voice or self._config.openai_tts_voice,
                "input": text,
                "response_format": "pcm",
            },
        ) as response:
            response.raise_for_status()
            for raw in response.iter_bytes(chunk_size=8192):
                if raw:
                    yield AudioChunk(pcm=raw, sample_rate=_PCM_SAMPLE_RATE, is_final=False)
        yield AudioChunk(pcm=b"", sample_rate=_PCM_SAMPLE_RATE, is_final=True)
