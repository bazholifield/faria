"""How fast Faria talks and how long it waits for you, by level (ADR-030).

Learners pause mid-sentence to find a word, and beginners need slower speech, so both
scale with the CEFR band of the active language's estimate. The home language uses the
plain config values; a target language with no estimate yet counts as A2 (the placement
conversation starts simple). The learner's own "slower / faster, please", stored as the
profile's ``speech_pace`` preference by the control tool, multiplies on top.
"""

from __future__ import annotations

from faria.config import AudioConfig
from faria.domain.levels import band

# speech_pace preference -> length multiplier (>1 = slower)
PACE_PREFERENCE = {"slower": 1.2, "normal": 1.0, "faster": 0.87}
UNKNOWN_LEVEL_BAND = "A2"


def _band(level: float | None) -> str:
    return band(level).rstrip("+") if level is not None else UNKNOWN_LEVEL_BAND


def silence_ms(audio: AudioConfig, level: float | None, is_home: bool) -> int:
    """Trailing silence that ends the learner's turn."""
    if is_home:
        return audio.vad_silence_ms
    return audio.vad_silence_ms_by_level.get(_band(level), audio.vad_silence_ms)


def pace(audio: AudioConfig, level: float | None, is_home: bool, preference: str | None) -> float:
    """Length multiplier for the tutor's voice (>1 = slower)."""
    base = 1.0 if is_home else audio.pace_by_level.get(_band(level), 1.0)
    return round(base * PACE_PREFERENCE.get(preference or "normal", 1.0), 3)
