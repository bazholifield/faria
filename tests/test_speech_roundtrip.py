"""Offline integration: Piper speech in -> whisper transcript out.

Exercises both real adapters with real models, no audio hardware and no
network (models must already be downloaded — skipped otherwise, so a fresh
clone's test run doesn't hang on a 500 MB download)."""

from pathlib import Path

import numpy as np
import pytest

from faria.config import AudioConfig

VOICE = Path("data/voices/es_ES-sharvard-medium.onnx")
WHISPER_DIR = Path("data/whisper")

pytestmark = pytest.mark.skipif(
    not (VOICE.exists() and WHISPER_DIR.exists()),
    reason="speech models not downloaded (run `faria serve` once, or the download snippet in README)",
)


def resample_to_16k(pcm: bytes, from_rate: int) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    idx = np.arange(0, len(x), from_rate / 16000)
    return np.interp(idx, np.arange(len(x)), x).astype(np.int16).tobytes()


def test_piper_to_whisper_roundtrip():
    from faria.speech.piper_tts import PiperSynthesizer
    from faria.speech.whisper_stt import FasterWhisperTranscriber

    cfg = AudioConfig()
    tts = PiperSynthesizer(cfg)
    chunks = list(tts.synthesize("Ayer fuimos a la playa con mi hermana.", "es"))
    assert chunks[-1].is_final
    pcm = b"".join(c.pcm for c in chunks)
    assert len(pcm) > 16000  # produced actual audio

    stt = FasterWhisperTranscriber(cfg, download_root=str(WHISPER_DIR))
    result = stt.transcribe(resample_to_16k(pcm, chunks[0].sample_rate), 16000, None)

    assert result.language == "es"
    assert "playa" in result.text.lower()
    assert "hermana" in result.text.lower()
    assert result.confidence > 0.6
    assert result.words, "word-level confidences must be populated (pronunciation suspicion needs them)"
