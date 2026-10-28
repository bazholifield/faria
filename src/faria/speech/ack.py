"""Instant acknowledgement (ADR-029): a short "mm" / "ajá" the moment the learner stops.

Recognition plus Claude's first sentence take a couple of seconds; silence for that long
reads as "it didn't hear me". A brief neutral backchannel, in the tutor's own voice and
current pace, makes the same wait sound like listening. Rules that keep it human:

- not every turn (``ack_probability``), and never the same phrase twice in a row;
- only after real speech (``ack_min_speech_s``), never for a cough or a one-word blip;
- only neutral sounds ("mm", "ya veo"), never agreement ("vale", "right"), because the
  reply that follows may disagree;
- clips are synthesized once per session in the background (``warm``); if one isn't
  ready yet, that turn simply has no acknowledgement. Picking never blocks the turn.

The prompt tells the tutor a sound has already played, so it doesn't open its own reply
with a filler (prompts._AUDIO_INSTRUCTIONS).
"""

from __future__ import annotations

import random

import numpy as np

from faria.config import AudioConfig
from faria.speech.tts import AudioChunk, Synthesizer


class AckBank:
    def __init__(self, tts: Synthesizer, audio: AudioConfig, rng: random.Random | None = None) -> None:
        self._tts = tts
        self._audio = audio
        self._rng = rng or random.Random()
        self._clips: dict[tuple[str, str, float], list[AudioChunk]] = {}
        self._last: str | None = None

    def warm(self, language: str, pace: float) -> None:
        """Synthesize this language's phrases at this pace (blocking: run it in a thread)."""
        if not self._audio.ack_enabled:
            return
        for phrase in self._audio.ack_phrases.get(language, []):
            key = (language, phrase, pace)
            if key in self._clips:
                continue
            try:
                chunks = [c for c in self._tts.synthesize(phrase, language, pace=pace) if c.pcm]
            except Exception:  # noqa: BLE001 - a missing voice just means no acknowledgement
                return
            self._clips[key] = _trimmed(chunks)

    def pick(self, language: str, pace: float, speech_s: float) -> list[AudioChunk] | None:
        """The clip to play now, or None for this turn."""
        audio = self._audio
        phrases = audio.ack_phrases.get(language, [])
        if not audio.ack_enabled or not phrases or speech_s < audio.ack_min_speech_s:
            return None
        if self._rng.random() >= audio.ack_probability:
            return None
        candidates = [p for p in phrases if p != self._last] or phrases
        phrase = self._rng.choice(candidates)
        clip = self._clips.get((language, phrase, pace))
        if clip:
            self._last = phrase
        return clip


def _trimmed(chunks: list[AudioChunk], keep_ms: int = 40) -> list[AudioChunk]:
    """One clip without the voice engine's leading and trailing silence (Piper pads a
    short phrase to over a second); a backchannel has to be quick to sound like one."""
    if not chunks:
        return chunks
    rate = chunks[0].sample_rate
    pcm = np.frombuffer(b"".join(c.pcm for c in chunks), dtype=np.int16)
    loud = np.flatnonzero(np.abs(pcm.astype(np.int32)) > 500)
    if not len(loud):
        return chunks
    pad = rate * keep_ms // 1000
    start, end = max(0, loud[0] - pad), min(len(pcm), loud[-1] + pad)
    return [AudioChunk(pcm=pcm[start:end].tobytes(), sample_rate=rate)]
