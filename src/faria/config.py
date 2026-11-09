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

    stt_default: str = "faster-whisper"  # faster-whisper | openai (needs OPENAI_API_KEY)
    openai_stt_model: str = "gpt-4o-mini-transcribe"
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

    # Hallucination filtering (ADR-025). Whisper invents words on silence/noise;
    # these gates drop a transcript that doesn't look like real speech.
    stt_vad_filter: bool = True         # run faster-whisper's built-in Silero VAD
    stt_no_speech_max: float = 0.6      # drop if mean no_speech_prob above this
    stt_logprob_min: float = -1.0       # drop if mean avg_logprob below this
    stt_min_confidence: float = 0.45    # drop if mean word confidence below this

    # VAD utterance segmentation (webrtcvad). This is the CHEAP first gate that
    # keeps whisper from running on pure silence; faster-whisper's Silero VAD
    # (stt_vad_filter) is the accurate second gate. Aggressiveness 3 leans
    # toward "only real speech triggers" — the user's complaint was noise
    # getting transcribed, so bias toward fewer false triggers (ADR-025).
    vad_aggressiveness: int = 3   # 0 (lenient) .. 3 (aggressive filtering)
    vad_silence_ms: int = 700     # this much trailing silence ends an utterance
    vad_min_speech_ms: int = 250  # shorter blips are discarded as noise
    vad_max_utterance_s: int = 45 # hard cap; emit even if the learner keeps going

    # Patience by level (ADR-030): learners pause mid-sentence to find a word, so
    # the trailing silence that ends a turn grows as the level drops. Home base
    # uses vad_silence_ms; a target language with no estimate yet counts as A2.
    vad_silence_ms_by_level: dict[str, int] = {
        "A1": 1100, "A2": 950, "B1": 800, "B2": 700, "C1": 650, "C2": 600,
    }
    # Early transcription (ADR-030): once a pause reaches endpoint_probe_ms the
    # audio so far is transcribed speculatively, so recognition overlaps the
    # wait instead of starting after it. If that transcript arrives while you're
    # still silent, it also decides the ending: a turn that trails off mid-thought
    # ("y…", "and…", "äh") waits endpoint_hold_ms longer; a clearly finished
    # sentence ends at endpoint_early_ratio of the usual wait. 0 turns it off.
    endpoint_probe_ms: int = 300
    endpoint_hold_ms: int = 700
    endpoint_early_ratio: float = 0.6

    # Instant acknowledgement (ADR-029): a short neutral backchannel in the
    # tutor's own voice the moment you stop, so the wait for recognition and
    # Claude sounds like listening. Not on every turn (that sounds robotic),
    # never twice in a row, never for a blip. Edit the phrases after listening.
    ack_enabled: bool = True
    ack_probability: float = 0.7
    ack_min_speech_s: float = 0.8
    ack_phrases: dict[str, list[str]] = {
        "en": ["Mm-hm.", "Mm.", "I see."],
        "es": ["Ajá.", "Mm.", "Ya veo."],
        "fr": ["Mm.", "Je vois.", "Mm-hm."],
        "de": ["Aha.", "Mm.", "Verstehe."],
    }

    # Barge-in (ADR-031): start talking while Faria speaks and its voice stops.
    # The speech has to last barge_in_min_ms so a cough or the tutor's own echo
    # doesn't cut it off. A headset is the reliable setup.
    barge_in: bool = True
    barge_in_min_ms: int = 400

    # Speaking pace (ADR-030): a length multiplier (>1 = slower) by level, times
    # the learner's own "slower / faster, please" (speech_pace preference).
    pace_by_level: dict[str, float] = {
        "A1": 1.25, "A2": 1.15, "B1": 1.05, "B2": 1.0, "C1": 1.0, "C2": 1.0,
    }
    # Stressed recasts (ADR-032): words the tutor marks *like this* when it
    # recasts an error are said after a short pause and a little slower, the
    # way a teacher leans on the corrected form. emphasis = false turns it off.
    emphasis: bool = True
    emphasis_pause_ms: int = 90
    emphasis_slowdown: float = 1.2

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
    openai_tts_instructions: str = (
        "Warm, unhurried and attentive, like an experienced mentor talking with a friend."
    )


class CourseConfig(BaseModel):
    """The course layer (ADR-034): syllabus, spaced review, topics, placement.

    The mentor gets suggestions, never an agenda: what's due for review, one or
    two syllabus items worth introducing, a couple of stories it could tell. The
    learner decides what to talk about and for how long."""

    enabled: bool = True
    curriculum_dir: Path = Path("curriculum")   # <lang>.yaml files (curriculum/README.md)
    max_due_words: int = 6                      # words offered for review per session
    new_items_offered: int = 2                  # syllabus items suggested per session
    topics_offered: int = 5
    review_intervals_days: list[int] = [1, 3, 7, 14, 30]  # Leitner boxes 1..5
    solid_after_sessions: int = 3               # correct use in this many sessions = solid
    # The variety the mentor speaks by default; other regions come up as asides.
    varieties: dict[str, str] = {
        "es": ("neutral Latin American Spanish (Mexico-centred, ustedes for 'you all'); Spain's "
               "vosotros and Argentina's vos are taught too, as variants to understand and enjoy"),
        "fr": "standard French of France",
        "de": "standard German of Germany",
    }


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
    course: CourseConfig = CourseConfig()
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
