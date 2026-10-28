"""OpenAI speech recognition behind the Transcriber interface (ADR-010).

Set audio.stt_default = "openai" and put OPENAI_API_KEY in .env. On this CPU local
whisper "base" takes ~1.5-2s per utterance; an API call for a few seconds of audio is
usually well under a second, and gpt-4o-mini-transcribe is far more accurate than "base"
on accented learner speech (about $0.003 per minute of audio, only your own turns). Raw
httpx, like openai_tts.py: one endpoint doesn't justify the SDK.

UNTESTED LIVE: written to the documented API but never run against it (no
OPENAI_API_KEY on this machine yet). If something's off, check the request format first.

What it can't give: per-word timestamps (whisper-1 only). Confidence comes from the token
log-probabilities when the API returns them; without them the result passes the
hallucination gate on text checks alone (ADR-025), so `words` stays empty and the
pronunciation-suspect logging (which needs word confidences) is quiet for this backend.
"""

from __future__ import annotations

import io
import math
import os
import wave

import httpx

from faria.config import AudioConfig
from faria.speech.stt import Transcriber, TranscriptionResult

_URL = "https://api.openai.com/v1/audio/transcriptions"


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class OpenAITranscriber(Transcriber):
    def __init__(self, config: AudioConfig) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("audio.stt_default is 'openai' but OPENAI_API_KEY is not set in .env")
        self._config = config
        self._client = httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=20.0)

    def transcribe(self, audio_pcm: bytes, sample_rate: int, language: str | None) -> TranscriptionResult:
        data = {"model": self._config.openai_stt_model, "response_format": "json",
                "include[]": "logprobs"}
        if language:
            data["language"] = language
        response = self._client.post(
            _URL, data=data, files={"file": ("turn.wav", _wav(audio_pcm, sample_rate), "audio/wav")}
        )
        response.raise_for_status()
        body = response.json()
        logprobs = [t["logprob"] for t in body.get("logprobs") or [] if "logprob" in t]
        confidence = math.exp(sum(logprobs) / len(logprobs)) if logprobs else 0.9
        return TranscriptionResult(
            text=body.get("text", "").strip(),
            language=language or body.get("language") or "",
            confidence=confidence,
            avg_logprob=sum(logprobs) / len(logprobs) if logprobs else 0.0,
        )
