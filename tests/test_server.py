"""Websocket-protocol tests with every heavy component faked — no models, no
network, no audio hardware. Drives the real ws.py loop end to end: PCM in,
VAD segmentation, (fake) STT, (fake) brain, (fake) TTS audio out."""

import json

import pytest
from fastapi.testclient import TestClient

from faria.config import Settings
from faria.db.connection import connect
from faria.db.migrate import apply_migrations
from faria.server.app import ServerDeps, create_app
from faria.speech.stt import Transcriber, TranscriberRegistry, TranscriptionResult
from faria.speech.tts import AudioChunk, Synthesizer
from faria.speech.vad import FRAME_MS

FRAME_BYTES = int(16000 * FRAME_MS / 1000) * 2

FAKE_TTS_PCM = b"\x07\x00" * 2000


class ByteMarkerDetector:
    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return frame[0] != 0


class FakeStt(Transcriber):
    def __init__(self, text: str, language: str = "es") -> None:
        self._result = TranscriptionResult(text=text, language=language, confidence=0.9, words=[])

    def transcribe(self, audio_pcm, sample_rate, language):
        return self._result


class FakeTts(Synthesizer):
    def synthesize(self, text, language, voice=None):
        yield AudioChunk(pcm=FAKE_TTS_PCM, sample_rate=22050, is_final=False)
        yield AudioChunk(pcm=b"", sample_rate=22050, is_final=True)

    def voices(self, language):
        return ["fake"]


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.model = "fake-model"


class _FakeStream:
    def __init__(self, text, final):
        self.text_stream = iter([text])
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeBrain:
    def __init__(self):
        self.calls = []
        self._script = []

    def queue(self, text):
        self._script.append(text)

    def stream_reply(self, system, messages, tools=None):
        self.calls.append({"tools": [t["name"] for t in (tools or [])]})
        text = self._script.pop(0)
        return _FakeStream(text, _FakeMessage([_Block("text", text=text)]))


@pytest.fixture()
def server(tmp_path):
    settings = Settings(data_dir=tmp_path)
    conn = connect(settings.db_path)
    apply_migrations(conn)
    conn.close()

    brain = FakeBrain()
    deps = ServerDeps(
        settings=settings,
        stt=TranscriberRegistry(FakeStt("Yo tiene mucho hambre")),
        tts=FakeTts(),
        detector_factory=ByteMarkerDetector,
        brain_factory=lambda: brain,
    )
    client = TestClient(create_app(settings, deps))
    return client, brain, settings


def _recv(ws):
    """Uniform receive: ('json', dict) or ('bytes', bytes)."""
    message = ws.receive()
    if message.get("text") is not None:
        return "json", json.loads(message["text"])
    return "bytes", message["bytes"]


def _drain_until(ws, msg_type, limit=50):
    """Collect messages until one of msg_type arrives; returns (all, that one)."""
    seen = []
    for _ in range(limit):
        kind, payload = _recv(ws)
        seen.append((kind, payload))
        if kind == "json" and payload.get("type") == msg_type:
            return seen, payload
    raise AssertionError(f"never saw {msg_type}; got {seen}")


def _speak_utterance(ws):
    silence_frames = 700 // FRAME_MS + 2
    ws.send_bytes((b"\x01\x00" * (FRAME_BYTES // 2)) * 15)   # ~450ms of "speech"
    ws.send_bytes(b"\x00" * FRAME_BYTES * silence_frames)     # trailing silence ends it


def test_full_audio_turn(server):
    client, brain, _ = server
    brain.queue("Ah, ¿tienes hambre? ¿Qué vas a comer?")

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json({"type": "start", "name": "bazh", "language": "es"})
        _, started = _drain_until(ws, "session_started")
        assert started["language"] == "es" and started["home"] is False

        _speak_utterance(ws)
        seen, done = _drain_until(ws, "reply_done")

        you_said = [p for k, p in seen if k == "json" and p.get("type") == "you_said"]
        assert you_said and you_said[0]["text"] == "Yo tiene mucho hambre"

        audio = b"".join(p for k, p in seen if k == "bytes")
        assert audio.count(FAKE_TTS_PCM) == 2  # one chunk per sentence

        formats = [p for k, p in seen if k == "json" and p.get("type") == "audio_format"]
        assert formats and formats[0]["sample_rate"] == 22050

        assert done["text"] == "Ah, ¿tienes hambre? ¿Qué vas a comer?"
        assert done["switched"] is False
        assert done["timings"]["stt_s"] is not None

        states = [p["value"] for k, p in seen if k == "json" and p.get("type") == "state"]
        # the initial post-start "listening" may open the window; the turn then
        # runs transcribing -> thinking -> speaking...
        assert states.index("transcribing") < states.index("speaking")
        # ...and the return to listening arrives right AFTER reply_done
        kind, after = _recv(ws)
        assert kind == "json" and after == {"type": "state", "value": "listening"}

        # target language: both tools ride along
        assert brain.calls[0]["tools"] == ["update_session_state", "annotate_errors"]

        ws.send_json({"type": "abandon"})
        _, ended = _drain_until(ws, "session_ended")
        assert ended["summary"] is None


def test_home_base_session_skips_annotation_tool(server):
    client, brain, _ = server
    brain.queue("Not a bad way to spend an afternoon.")

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json({"type": "start", "name": "bazh", "language": None})
        _, started = _drain_until(ws, "session_started")
        assert started["home"] is True and started["language"] == "en"

        _speak_utterance(ws)
        _drain_until(ws, "reply_done")
        assert brain.calls[0]["tools"] == ["update_session_state"]

        ws.send_json({"type": "abandon"})
        _drain_until(ws, "session_ended")


def test_noise_only_utterance_resumes_listening(server):
    client, brain, settings = server
    deps_stt_empty = FakeStt("")  # whisper produced nothing usable

    # swap the registry's default for this test
    client.app  # (app already built; easier: new app with empty-stt deps)
    brain2 = FakeBrain()
    deps = ServerDeps(
        settings=settings,
        stt=TranscriberRegistry(deps_stt_empty),
        tts=FakeTts(),
        detector_factory=ByteMarkerDetector,
        brain_factory=lambda: brain2,
    )
    client2 = TestClient(create_app(settings, deps))
    with client2.websocket_connect("/ws/session") as ws:
        ws.send_json({"type": "start", "name": "bazh", "language": "es"})
        _drain_until(ws, "session_started")
        _speak_utterance(ws)
        # expect: transcribing, then straight back to listening — no you_said,
        # no reply, no brain call
        seen: list = []
        saw_transcribing = False
        for _ in range(20):
            kind, payload = _recv(ws)
            seen.append((kind, payload))
            if kind == "json" and payload.get("type") == "state":
                if payload["value"] == "transcribing":
                    saw_transcribing = True
                elif payload["value"] == "listening" and saw_transcribing:
                    break
        else:
            raise AssertionError(f"never returned to listening: {seen}")
        assert not any(k == "json" and p.get("type") == "you_said" for k, p in seen)
        assert not brain2.calls

        ws.send_json({"type": "abandon"})
        _drain_until(ws, "session_ended")


def test_rest_profiles_and_health(server):
    client, _, _ = server
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/profiles").json() == []
    assert client.get("/api/patterns/es").json() == []
