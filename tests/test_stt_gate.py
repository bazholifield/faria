"""Unit tests for the hallucination gate (ADR-025). The whisper adapter is
exercised separately in test_speech_roundtrip; here we test the pure predicate."""

from faria.config import AudioConfig
from faria.speech.stt import TranscriptionResult, WordSpan, is_probable_speech

CFG = AudioConfig()


def result(text, *, no_speech=0.0, logprob=0.0, conf=0.9, words=True):
    w = [WordSpan(t, 0, 0, conf) for t in text.split()] if words else []
    return TranscriptionResult(
        text=text, language="es", confidence=conf, words=w,
        no_speech_prob=no_speech, avg_logprob=logprob,
    )


def test_real_speech_passes():
    assert is_probable_speech(result("hola como estas"), CFG)


def test_empty_and_too_short_dropped():
    assert not is_probable_speech(result(""), CFG)
    assert not is_probable_speech(result("a"), CFG)


def test_high_no_speech_dropped():
    assert not is_probable_speech(result("you", no_speech=0.95), CFG)


def test_low_logprob_dropped():
    assert not is_probable_speech(result("thanks for watching", logprob=-2.0), CFG)


def test_low_word_confidence_dropped():
    assert not is_probable_speech(result("mmm hmm", conf=0.2), CFG)


def test_pure_punctuation_and_music_dropped():
    assert not is_probable_speech(result("...", words=False), CFG)
    assert not is_probable_speech(result("♪♪♪", words=False), CFG)


def test_bracketed_stage_directions_dropped():
    assert not is_probable_speech(result("[Music]"), CFG)
    assert not is_probable_speech(result("(wind blowing)"), CFG)


def test_real_short_word_survives_when_confident():
    # "gracias" is a legit short answer — not blocklisted, passes on good signals
    assert is_probable_speech(result("gracias", no_speech=0.05, logprob=-0.2, conf=0.9), CFG)
