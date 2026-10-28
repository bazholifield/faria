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

Smarter endpointing (ADR-030), all off by default so the plain collector above is
unchanged unless the voice loop asks for it:
  - set_silence_ms(): patience by level (longer waits for beginners).
  - probe_ms: when a pause inside an utterance reaches this length, emit a Probe
    with the audio so far, so recognition can start during the wait.
  - hold(ms) / end_now(): the loop's verdict on that early transcript, "they're
    mid-thought, wait longer" or "that sentence is finished, end the turn".
  - report_start: emit SpeechStart when an utterance begins (barge-in, ADR-031).

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
    seq: int = 0         # which utterance of this segmenter (matches Probe.seq)
    last_voiced: int = 0 # index of the last voiced frame: a Probe with the same value saw all the speech
    speech_s: float = 0.0  # voiced audio only


@dataclass(frozen=True)
class Probe:
    """A pause of probe_ms inside an utterance: the audio so far, for early transcription."""
    pcm: bytes
    seq: int
    last_voiced: int


@dataclass(frozen=True)
class SpeechStart:
    seq: int


def default_detector(aggressiveness: int) -> SpeechDetector:
    import webrtcvad

    return webrtcvad.Vad(aggressiveness)


class UtteranceSegmenter:
    """Feed arbitrary-sized PCM with feed(); completed utterances come back."""

    def __init__(
        self,
        config: AudioConfig,
        detector: SpeechDetector | None = None,
        probe_ms: int = 0,
        report_start: bool = False,
    ) -> None:
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
        self._probe_frames = probe_ms // FRAME_MS if probe_ms else 0
        self._report_start = report_start
        self._extra_silence = 0       # hold(): extra frames for the current utterance only
        self._seq = 0
        self._last_voiced = -1
        self._voiced_count = 0

    def feed(self, pcm: bytes) -> list[Utterance | Probe | SpeechStart]:
        """Consume a chunk of the mic stream; return utterances completed by it
        (plus Probe / SpeechStart events when those are switched on)."""
        self._buffer += pcm
        completed: list[Utterance | Probe | SpeechStart] = []
        while len(self._buffer) >= self._frame_bytes:
            frame = self._buffer[: self._frame_bytes]
            self._buffer = self._buffer[self._frame_bytes:]
            completed.extend(self._push_frame(frame))
        return completed

    # -- endpointing controls (ADR-030) ----------------------------------------

    def set_silence_ms(self, ms: int) -> None:
        self._silence_frames = max(1, ms // FRAME_MS)

    def hold(self, ms: int) -> None:
        """Wait this much longer before ending the current utterance."""
        if self._active:
            self._extra_silence = max(self._extra_silence, ms // FRAME_MS)

    def end_now(self) -> Utterance | None:
        """End the current utterance now, if it's in a pause (never mid-word)."""
        if self._active and self._trailing_silence > 0:
            return self._finish()
        return None

    def shorten_to(self, ms: int) -> Utterance | None:
        """End the current utterance once its pause reaches ms (never below the
        probe point); ends it right away if the pause is already that long."""
        if not self._active:
            return None
        target = max(self._probe_frames + 1, ms // FRAME_MS)
        self._extra_silence = min(self._extra_silence, target - self._silence_frames)
        if self._trailing_silence >= target:
            return self._finish()
        return None

    @property
    def current_seq(self) -> int | None:
        return self._seq if self._active else None

    @property
    def current_last_voiced(self) -> int | None:
        return self._last_voiced if self._active else None

    @property
    def active_speech_ms(self) -> int:
        """Voiced audio so far in the utterance in progress (barge-in threshold)."""
        return self._voiced_count * FRAME_MS if self._active else 0

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
        self._extra_silence = 0

    # -- internals -----------------------------------------------------------

    def _push_frame(self, frame: bytes) -> list[Utterance | Probe | SpeechStart]:
        voiced = self._detector.is_speech(frame, self._sample_rate)

        if not self._active:
            self._ring.append((frame, voiced))
            voiced_in_ring = sum(1 for _, v in self._ring if v)
            if voiced_in_ring >= max(2, int(0.6 * len(self._ring))):
                self._active = list(self._ring)
                self._ring.clear()
                self._trailing_silence = 0
                self._extra_silence = 0
                self._seq += 1
                self._voiced_count = sum(1 for _, v in self._active if v)
                self._last_voiced = max(i for i, (_, v) in enumerate(self._active) if v)
                if self._report_start:
                    return [SpeechStart(self._seq)]
            return []

        self._active.append((frame, voiced))
        if voiced:
            self._trailing_silence = 0
            self._voiced_count += 1
            self._last_voiced = len(self._active) - 1
        else:
            self._trailing_silence += 1
        if (self._trailing_silence >= self._silence_frames + self._extra_silence
                or len(self._active) >= self._max_frames):
            utt = self._finish()
            return [utt] if utt is not None else []
        if (self._probe_frames and self._trailing_silence == self._probe_frames
                and self._voiced_count >= self._min_speech_frames):
            return [Probe(pcm=b"".join(f for f, _ in self._active), seq=self._seq,
                          last_voiced=self._last_voiced)]
        return []

    def _finish(self) -> Utterance | None:
        frames, self._active = self._active, []
        self._trailing_silence = 0
        self._extra_silence = 0
        last_voiced, self._last_voiced = self._last_voiced, -1
        self._voiced_count = 0
        voiced_count = sum(1 for _, v in frames if v)
        if voiced_count < self._min_speech_frames:
            return None  # a blip, not speech
        # No voiced-ratio gate here: the mandatory trailing silence would
        # deflate the ratio and drop short real turns. Noise filtering is
        # Silero's job downstream (stt_vad_filter), which is far more accurate
        # than an energy ratio (ADR-025).
        pcm = b"".join(f for f, _ in frames)
        return Utterance(
            pcm=pcm,
            duration_s=len(frames) * FRAME_MS / 1000,
            voiced_ratio=voiced_count / len(frames),
            seq=self._seq,
            last_voiced=last_voiced,
            speech_s=voiced_count * FRAME_MS / 1000,
        )
