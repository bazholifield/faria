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
    """Phase 2 placeholders; parsed now so the file format is stable."""

    stt_default: str = "faster-whisper"
    tts_default: str = "piper"
    sample_rate: int = 16000


class Settings(BaseModel):
    models: ModelsConfig = ModelsConfig()
    correction: CorrectionConfig = CorrectionConfig()
    memory: MemoryConfig = MemoryConfig()
    audio: AudioConfig = AudioConfig()
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
