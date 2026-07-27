"""Configuration: code defaults <- TOML overrides <- environment variables.

Two kinds of settings, kept deliberately separate (DECISIONS.md ADR-008):

- Secrets live only in the environment (loaded from .env via python-dotenv).
  The Anthropic SDK resolves ANTHROPIC_API_KEY from the environment itself,
  so no key ever passes through these objects.
- Tunables are pydantic models below. Defaults in code mean the installed
  package works with zero config files; config/default.toml mirrors them for
  discoverability and is loaded automatically when present.

Resolution order for the TOML file: explicit argument > $FARIA_CONFIG >
./config/default.toml > none (pure code defaults).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

DEFAULT_CONFIG_PATH = Path("config/default.toml")
DEFAULT_DATA_DIR = Path("data")


class ModelsConfig(BaseModel):
    conversation: str = "claude-sonnet-5"
    summarizer: str = "claude-haiku-4-5"
    max_reply_tokens: int = 1024
    max_summary_tokens: int = 4096
    thinking: str = "disabled"  # "disabled" | "adaptive"


class CorrectionConfig(BaseModel):
    default_aggressiveness: str = "balanced"  # off | minimal | balanced | aggressive
    default_style: str = "recast"             # recast | recast_plus_note | explicit
    max_inline_per_turn: int = 1
    max_inline_per_window: int = 2
    window_turns: int = 5
    recurrence_escalation_threshold: int = 3
    # Pattern lifecycle (ADR-018): a pattern that keeps recurring past this
    # many occurrences is flagged fossilization_risk; one that's gone quiet
    # decays active/fossilization_risk -> improving -> resolved.
    fossilization_threshold: int = 6
    pattern_improving_after_days: int = 14
    pattern_resolved_after_days: int = 45
    # Companion mode (ADR-015/ADR-019): minimum learner turns between two
    # tutor-INITIATED language pivots. Does not apply to learner requests,
    # which always take effect immediately.
    min_turns_between_pivots: int = 4


class MemoryConfig(BaseModel):
    recent_summaries_in_prompt: int = 3


class AudioConfig(BaseModel):
    """Phase 2 audio pipeline knobs.

    The client (browser) sends 16 kHz mono Int16 PCM; everything server-side
    (VAD, whisper) is built around that rate. TTS output keeps each voice's
    native rate — the browser resamples on playback via WebAudio.
    """

    stt_default: str = "faster-whisper"
    tts_default: str = "piper"  # piper | openai
    sample_rate: int = 16000

    # faster-whisper. Default is "base": benchmarked on this WSL2 CPU,
    # "small" ran at ~3x realtime (7-9s for a 3s utterance — busts the <2s
    # budget) while "base" runs at ~0.6x realtime (~1.5s) with equal accuracy
    # on clean headset speech. Move up to "small"/"medium" if stt_device can
    # be "cuda" (WSL2 CUDA passthrough with a desktop GPU).
    stt_model: str = "base"       # tiny | base | small | medium | large-v3 ...
    stt_device: str = "auto"      # auto | cpu | cuda
    stt_compute: str = "int8"     # int8 is the sane CPU default
    # Words at/below this STT probability on an otherwise-confident utterance
    # get logged as phonology:pronunciation_suspected events (ROADMAP Phase 2).
    pronunciation_suspect_threshold: float = 0.5
    # Words below this STT probability are treated as possibly MIStranscribed
    # (ADR-023): flagged to the tutor in a transcription note, and any error
    # the sidecar reports on them is quarantined — logged for the record but
    # never corrected and never counted toward pattern recurrence.
    stt_suspect_threshold: float = 0.7

    # VAD utterance segmentation (webrtcvad)
    vad_aggressiveness: int = 2   # 0 (lenient) .. 3 (aggressive filtering)
    vad_silence_ms: int = 700     # this much trailing silence ends an utterance
    vad_min_speech_ms: int = 250  # shorter blips are discarded as noise
    vad_max_utterance_s: int = 45 # hard cap; emit even if the learner keeps going

    # Piper: voices auto-download to voices_dir on first use.
    voices_dir: Path = Path("data/voices")
    # Per-language Piper voice IDs. The en default is a first-guess "older
    # American man" candidate — audition alternatives with `faria say` and
    # override here (ADR-024). No Basque voice exists in the Piper catalog
    # (the TTS twin of the Whisper-Basque STT gap, ADR-010) — `eu` needs the
    # API backend or a community model when it matters.
    piper_voices: dict[str, str] = {
        "en": "en_US-john-medium",
        "es": "es_ES-sharvard-medium",
        "fr": "fr_FR-siwis-medium",
        "de": "de_DE-thorsten-medium",
        "ar": "ar_JO-kareem-medium",
    }
    # Speaking pace: >1.0 is slower/more deliberate (1.1 reads as unhurried —
    # part of the mentor character), <1.0 faster.
    piper_length_scale: float = 1.1

    # OpenAI TTS (tts_default = "openai"); needs OPENAI_API_KEY in .env.
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"


class ServerConfig(BaseModel):
    """Phase 2/3 server. 127.0.0.1 for the desk; 0.0.0.0 for LAN/Tailscale."""

    host: str = "127.0.0.1"
    port: int = 8765


class Settings(BaseModel):
    models: ModelsConfig = ModelsConfig()
    correction: CorrectionConfig = CorrectionConfig()
    memory: MemoryConfig = MemoryConfig()
    audio: AudioConfig = AudioConfig()
    server: ServerConfig = ServerConfig()
    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def db_path(self) -> Path:
        return self.data_dir / "faria.db"


def load_settings(config_path: Path | None = None) -> Settings:
    """Build Settings from defaults, an optional TOML file, and env overrides."""
    load_dotenv()  # makes .env's ANTHROPIC_API_KEY visible to the SDK

    path = config_path or _resolve_config_path()
    overrides: dict = {}
    if path is not None and path.is_file():
        with path.open("rb") as fh:
            overrides = tomllib.load(fh)

    settings = Settings.model_validate(overrides)

    if env_data_dir := os.environ.get("FARIA_DATA_DIR"):
        settings.data_dir = Path(env_data_dir)
    return settings


def _resolve_config_path() -> Path | None:
    if env_path := os.environ.get("FARIA_CONFIG"):
        return Path(env_path)
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None
