"""Server-side utterance segmentation over a continuous mic stream (ADR-021).

The browser client streams raw PCM without deciding anything; this state
machine decides where utterances begin and end — keeping the client dumb (the
thin-client principle) and the tuning knobs server-side in config.

Classic webrtcvad collector pattern:
  IDLE:   keep a small ring buffer of recent frames. When enough of the ring
          is voiced, an utterance has started — emit the ring (so the first
          syllable isn't clipped) and go ACTIVE.
  ACTIVE: accumulate every frame. When the trailing `silence_ms` of frames is
          all unvoiced, the utterance is over: emit it if it contains at
          least `min_speech_ms` of voiced audio (else it was a cough/noise),
          reset, back to IDLE. A `max_utterance_s` cap force-ends monologues.

"Tolerance for long learner silences" (ROADMAP Phase 2) is inherent: IDLE has
no timeout. Faria waits like a person who knows you're sanding a board.

The webrtcvad dependency is injected (any object with is_speech(bytes, rate))
so tests can drive the state machine with a scripted fake.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol

from faria.config import AudioConfig

FRAME_MS = 30  # webrtcvad accepts 10/20/30ms frames at 16 kHz; 30 is the usual pick


class SpeechDetector(Protocol):
    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...


@dataclass(frozen=True)
class Utterance:
    pcm: bytes           # 16 kHz mono int16
    duration_s: float
    voiced_ratio: float  # share of frames VAD called speech — a crude quality signal


def default_detector(aggressiveness: int) -> SpeechDetector:
    import webrtcvad

    return webrtcvad.Vad(aggressiveness)


class UtteranceSegmenter:
    """Feed arbitrary-sized PCM with feed(); completed utterances come back."""

    def __init__(self, config: AudioConfig, detector: SpeechDetector | None = None) -> None:
        self._sample_rate = config.sample_rate
        self._frame_bytes = int(self._sample_rate * FRAME_MS / 1000) * 2  # int16 mono
        self._detector = detector or default_detector(config.vad_aggressiveness)

        self._silence_frames = max(1, config.vad_silence_ms // FRAME_MS)
        self._min_speech_frames = max(1, config.vad_min_speech_ms // FRAME_MS)
        self._max_frames = config.vad_max_utterance_s * 1000 // FRAME_MS
        # Ring size: enough context that the utterance keeps its first syllable.
        self._ring: deque[tuple[bytes, bool]] = deque(maxlen=max(6, self._silence_frames // 2))

        self._buffer = b""            # partial frame carry-over between feed() calls
        self._active: list[tuple[bytes, bool]] = []
        self._trailing_silence = 0

    def feed(self, pcm: bytes) -> list[Utterance]:
        """Consume a chunk of the mic stream; return utterances completed by it."""
        self._buffer += pcm
        completed: list[Utterance] = []
        while len(self._buffer) >= self._frame_bytes:
            frame = self._buffer[: self._frame_bytes]
            self._buffer = self._buffer[self._frame_bytes:]
            if (utt := self._push_frame(frame)) is not None:
                completed.append(utt)
        return completed

    def flush(self) -> Utterance | None:
        """End-of-stream: emit whatever is in flight (client hung up mid-word)."""
        if self._active:
            return self._finish()
        return None

    def reset(self) -> None:
        """Drop all state — used when the server starts ignoring the mic
        (half-duplex: the tutor is speaking) so stale audio never leaks into
        the next turn."""
        self._ring.clear()
        self._active = []
        self._buffer = b""
        self._trailing_silence = 0

    # -- internals -----------------------------------------------------------

    def _push_frame(self, frame: bytes) -> Utterance | None:
        voiced = self._detector.is_speech(frame, self._sample_rate)

        if not self._active:
            self._ring.append((frame, voiced))
            voiced_in_ring = sum(1 for _, v in self._ring if v)
            if voiced_in_ring >= max(2, int(0.6 * len(self._ring))):
                self._active = list(self._ring)
                self._ring.clear()
                self._trailing_silence = 0
            return None

        self._active.append((frame, voiced))
        self._trailing_silence = 0 if voiced else self._trailing_silence + 1
        if self._trailing_silence >= self._silence_frames or len(self._active) >= self._max_frames:
            return self._finish()
        return None

    def _finish(self) -> Utterance | None:
        frames, self._active = self._active, []
        self._trailing_silence = 0
        voiced_count = sum(1 for _, v in frames if v)
        if voiced_count < self._min_speech_frames:
            return None  # a blip, not speech
        pcm = b"".join(f for f, _ in frames)
        return Utterance(
            pcm=pcm,
            duration_s=len(frames) * FRAME_MS / 1000,
            voiced_ratio=voiced_count / len(frames),
        )
