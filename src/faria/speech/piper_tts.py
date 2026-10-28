"""Piper implementation of the Synthesizer interface (ADR-010) — local, free.

Voices auto-download into audio.voices_dir on first use (~60 MB each) and are
cached in-process after first load. Each Piper voice has its own native sample
rate (typically 22050 Hz); AudioChunk carries the rate so the browser can
resample at playback — nothing server-side ever resamples TTS audio.

Known v1 limitation (note for Phase 4 voice work): the tutor sometimes mixes
languages inside one reply — a German aside while speaking English, exactly
what the companion is *for* — but a single Piper voice phonemizes everything
with one language's rules, so foreign words come out with an accent at best.
Per-sentence language detection + voice switching, or an API voice that
handles code-switching natively, is the eventual fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from faria.config import AudioConfig
from faria.speech.tts import AudioChunk, Synthesizer

if TYPE_CHECKING:
    from piper import PiperVoice


class PiperSynthesizer(Synthesizer):
    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        self._voices: dict[str, "PiperVoice"] = {}  # voice_id -> loaded model

    def voices(self, language: str) -> list[str]:
        voice_id = self._config.piper_voices.get(language)
        return [voice_id] if voice_id else []

    def preload(self, language: str) -> None:
        """Download+load the language's voice ahead of first use, so a
        mid-reply language switch (ADR-024) doesn't stall on a model load."""
        if voice_id := self._config.piper_voices.get(language):
            self._load(voice_id)

    def synthesize(
        self, text: str, language: str, voice: str | None = None, pace: float = 1.0
    ) -> Iterator[AudioChunk]:
        from piper import SynthesisConfig

        voice_id = voice or self._config.piper_voices.get(language)
        if voice_id is None:
            raise ValueError(
                f"No Piper voice configured for language '{language}' "
                "(see [audio.piper_voices] in config; note there is no Basque voice)"
            )
        model = self._load(voice_id)
        syn_config = SynthesisConfig(length_scale=self._config.piper_length_scale * pace)
        for chunk in model.synthesize(text, syn_config=syn_config):
            yield AudioChunk(
                pcm=chunk.audio_int16_bytes,
                sample_rate=chunk.sample_rate,
                is_final=False,
            )
        yield AudioChunk(pcm=b"", sample_rate=self._sample_rate(model), is_final=True)

    def _load(self, voice_id: str) -> "PiperVoice":
        if voice_id in self._voices:
            return self._voices[voice_id]

        from piper import PiperVoice
        from piper.download_voices import download_voice

        voices_dir = Path(self._config.voices_dir)
        voices_dir.mkdir(parents=True, exist_ok=True)
        model_path = voices_dir / f"{voice_id}.onnx"
        if not model_path.exists():
            download_voice(voice_id, voices_dir)
        self._voices[voice_id] = PiperVoice.load(model_path)
        return self._voices[voice_id]

    @staticmethod
    def _sample_rate(model: "PiperVoice") -> int:
        return model.config.sample_rate
