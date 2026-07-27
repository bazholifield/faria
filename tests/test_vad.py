"""Segmenter state-machine tests with a scripted detector — webrtcvad itself
is not under test (it's a battle-tested C library); the segmentation logic is."""

from faria.config import AudioConfig
from faria.speech.vad import FRAME_MS, UtteranceSegmenter

FRAME_BYTES = int(16000 * FRAME_MS / 1000) * 2  # 960


class ByteMarkerDetector:
    """Frames starting with a nonzero byte are 'speech'."""

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return frame[0] != 0


def voiced(n: int) -> bytes:
    return (b"\x01\x00" * (FRAME_BYTES // 2)) * n


def silent(n: int) -> bytes:
    return b"\x00" * FRAME_BYTES * n


def make(config: AudioConfig | None = None) -> UtteranceSegmenter:
    return UtteranceSegmenter(config or AudioConfig(), ByteMarkerDetector())


def test_utterance_emitted_after_trailing_silence():
    seg = make()
    silence_frames = AudioConfig().vad_silence_ms // FRAME_MS
    assert seg.feed(voiced(15)) == []           # speaking: nothing emitted yet
    utts = seg.feed(silent(silence_frames + 1))  # stop talking
    assert len(utts) == 1
    assert utts[0].duration_s > 0.4
    assert 0 < utts[0].voiced_ratio <= 1


def test_short_blip_is_discarded():
    seg = make()
    silence_frames = AudioConfig().vad_silence_ms // FRAME_MS
    # ring needs ~7 voiced frames to trigger; 7 voiced < min_speech (250ms ~ 8)
    seg.feed(voiced(7))
    assert seg.feed(silent(silence_frames + 1)) == []


def test_pure_silence_never_emits():
    seg = make()
    assert seg.feed(silent(200)) == []


def test_partial_frames_carry_over():
    seg = make()
    blob = voiced(12) + silent(30)
    # feed in awkward 100-byte slices; behavior must match one big feed
    out = []
    for i in range(0, len(blob), 100):
        out.extend(seg.feed(blob[i : i + 100]))
    assert len(out) == 1


def test_max_utterance_cap_forces_emit():
    cfg = AudioConfig(vad_max_utterance_s=1)
    seg = UtteranceSegmenter(cfg, ByteMarkerDetector())
    frames_for_1s = 1000 // FRAME_MS + 5
    utts = seg.feed(voiced(frames_for_1s))
    assert len(utts) == 1  # emitted even though the speaker never paused


def test_reset_drops_in_flight_audio():
    seg = make()
    seg.feed(voiced(15))
    seg.reset()
    silence_frames = AudioConfig().vad_silence_ms // FRAME_MS
    assert seg.feed(silent(silence_frames + 1)) == []


def test_flush_returns_in_flight_utterance():
    seg = make()
    seg.feed(voiced(15))
    utt = seg.flush()
    assert utt is not None and utt.duration_s > 0.4
