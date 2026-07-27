"""FastAPI app factory (Phase 2).

Wiring only: heavy components (whisper, piper, one DB connection per
websocket) are built here and handed to the WS/REST modules. The STT model
loads eagerly at startup so the first utterance doesn't eat the load time;
Piper voices load lazily per language.

Dependency injection is deliberate — tests pass fakes for brain/stt/tts/vad
via create_app() and drive the whole websocket protocol without models,
network, or audio hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from faria.config import Settings
from faria.speech.lang_router import SentenceLanguageRouter
from faria.speech.stt import Transcriber, TranscriberRegistry
from faria.speech.tts import Synthesizer
from faria.speech.vad import SpeechDetector, default_detector

WEB_DIR = Path(__file__).parent / "web"


@dataclass
class ServerDeps:
    """Everything the WS/REST handlers need, in one injectable bundle."""

    settings: Settings
    stt: TranscriberRegistry
    tts: Synthesizer
    detector_factory: Callable[[], SpeechDetector]  # one detector per connection (stateful)
    brain_factory: Callable[[], object] = field(default=lambda: None)  # returns a TutorBrain-alike
    lang_router: SentenceLanguageRouter | None = None  # per-sentence TTS routing (ADR-024)


def _default_deps(settings: Settings) -> ServerDeps:
    from faria.speech.whisper_stt import FasterWhisperTranscriber
    from faria.tutor.brain import TutorBrain

    stt = TranscriberRegistry(
        FasterWhisperTranscriber(
            settings.audio, download_root=str(settings.data_dir / "whisper")
        )
    )
    # Per-language STT overrides (the Basque hook, ADR-010) get registered
    # here when an alternative backend exists; none configured yet.

    if settings.audio.tts_default == "openai":
        from faria.speech.openai_tts import OpenAISynthesizer

        tts: Synthesizer = OpenAISynthesizer(settings.audio)
    else:
        from faria.speech.piper_tts import PiperSynthesizer

        tts = PiperSynthesizer(settings.audio)

    return ServerDeps(
        settings=settings,
        stt=stt,
        tts=tts,
        detector_factory=lambda: default_detector(settings.audio.vad_aggressiveness),
        brain_factory=lambda: TutorBrain(settings.models),
        # constrained to the languages we can actually speak
        lang_router=SentenceLanguageRouter(list(settings.audio.piper_voices)),
    )


def create_app(settings: Settings, deps: ServerDeps | None = None) -> FastAPI:
    from faria.server.rest import build_rest_router
    from faria.server.ws import build_ws_router

    deps = deps or _default_deps(settings)
    app = FastAPI(title="faria", docs_url=None, redoc_url=None)
    app.include_router(build_rest_router(deps))
    app.include_router(build_ws_router(deps))

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app
