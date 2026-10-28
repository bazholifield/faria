"""The October 2026 speech improvements (ADR-029..032), with every heavy component faked:
pacing and patience by level, the instant acknowledgement, early transcription and its
end-of-turn verdict, barge-in, stressed recasts, "slower, please", and the history cache."""

import random
import time

import pytest
from fastapi.testclient import TestClient

from faria.config import AudioConfig, Settings
from faria.db.connection import connect
from faria.db.migrate import apply_migrations
from faria.server.app import ServerDeps, create_app
from faria.speech import pacing
from faria.speech.ack import AckBank
from faria.speech.emphasis import split_emphasis, strip_marks
from faria.speech.endpoint import verdict
from faria.speech.stt import Transcriber, TranscriberRegistry, TranscriptionResult
from faria.speech.tts import AudioChunk, Synthesizer
from faria.speech.vad import FRAME_MS, Probe, SpeechStart, UtteranceSegmenter, Utterance
from faria.tutor.brain import _cache_latest

from test_lifecycle import FakeBrain as ScriptedBrain, control_call
from test_server import FRAME_BYTES, ByteMarkerDetector, FakeBrain, _drain_until, _recv

SPEECH = b"\x01\x00" * (FRAME_BYTES // 2)
SILENCE = b"\x00" * FRAME_BYTES


def frames(n, kind):
    return kind * n


# ---- pacing (ADR-030) ----------------------------------------------------------------
def test_patience_and_pace_follow_level():
    a = AudioConfig()
    assert pacing.silence_ms(a, 1.2, is_home=False) == a.vad_silence_ms_by_level["A1"]
    assert pacing.silence_ms(a, 4.1, is_home=False) == a.vad_silence_ms_by_level["B2"]
    assert pacing.silence_ms(a, 1.2, is_home=True) == a.vad_silence_ms
    assert pacing.silence_ms(a, None, is_home=False) == a.vad_silence_ms_by_level["A2"]  # no estimate yet
    assert pacing.pace(a, 1.2, False, None) > pacing.pace(a, 4.1, False, None)
    assert pacing.pace(a, 4.1, False, "slower") > pacing.pace(a, 4.1, False, None)
    assert pacing.pace(a, 2.0, True, None) == 1.0


# ---- acknowledgement (ADR-029) ----------------------------------------------------------
class RecordingTts(Synthesizer):
    def __init__(self, samples=2000):
        self.calls = []
        self._pcm = b"\x07\x00" * samples

    def synthesize(self, text, language, voice=None, pace=1.0):
        self.calls.append((text, language, pace))
        yield AudioChunk(pcm=self._pcm, sample_rate=22050)
        yield AudioChunk(pcm=b"", sample_rate=22050, is_final=True)

    def voices(self, language):
        return ["fake"]


def test_ack_rules():
    audio = AudioConfig(ack_probability=1.0)
    bank = AckBank(RecordingTts(), audio, rng=random.Random(1))
    assert bank.pick("es", 1.0, speech_s=2.0) is None  # not warmed yet: never blocks
    bank.warm("es", 1.0)
    assert bank.pick("es", 1.0, speech_s=0.3) is None  # a blip gets no acknowledgement
    seen = [bank._last for _ in range(10) if bank.pick("es", 1.0, speech_s=2.0)]
    assert all(a != b for a, b in zip(seen, seen[1:]))  # never the same phrase twice in a row
    assert AckBank(RecordingTts(), AudioConfig(ack_probability=0.0)).pick("es", 1.0, 2.0) is None


# ---- emphasis (ADR-032) -----------------------------------------------------------------
def test_split_emphasis():
    assert split_emphasis("Ah, ¿*fuiste* a la playa?") == [("Ah, ¿", False), ("fuiste", True), (" a la playa?", False)]
    assert split_emphasis("No marks here.") == [("No marks here.", False)]
    assert split_emphasis("A stray * star.") == [("A stray  star.", False)]
    assert strip_marks("¿*Fuiste* ayer?") == "¿Fuiste ayer?"


# ---- end-of-turn verdict (ADR-030) ------------------------------------------------------
@pytest.mark.parametrize("text,lang,expected", [
    ("Ayer fui al cine y", "es", "hold"),
    ("Ayer fui al cine.", "es", "end"),
    ("Weil äh", "de", "hold"),
    ("Kommst du mit?", "de", "end"),
    ("I think so.", "en", "end"),
    ("Je suis allé au…", "fr", "hold"),
    ("Hola", "es", None),
])
def test_verdict(text, lang, expected):
    assert verdict(text, lang) == expected


# ---- segmenter extensions ---------------------------------------------------------------
def _seg(**kw):
    return UtteranceSegmenter(AudioConfig(vad_silence_ms=600), ByteMarkerDetector(), **kw)


def test_segmenter_probe_start_and_shorten():
    seg = _seg(probe_ms=300, report_start=True)
    out = seg.feed(frames(15, SPEECH))
    assert any(isinstance(e, SpeechStart) for e in out)
    out = seg.feed(frames(10, SILENCE))  # 300ms of pause: one probe, no utterance yet
    probes = [e for e in out if isinstance(e, Probe)]
    assert len(probes) == 1 and not any(isinstance(e, Utterance) for e in out)
    assert seg.current_last_voiced == probes[0].last_voiced
    assert seg.shorten_to(300) is None  # "that sentence is finished", but never at the probe point itself
    out = seg.feed(frames(1, SILENCE))  # one more frame of silence ends it, well before 600ms
    utt = next(e for e in out if isinstance(e, Utterance))
    assert utt.seq == probes[0].seq and utt.last_voiced == probes[0].last_voiced


def test_segmenter_hold_waits_longer():
    seg = _seg()
    seg.feed(frames(15, SPEECH))
    seg.hold(600)
    assert not seg.feed(frames(25, SILENCE))  # 750ms: would have ended without the hold
    assert any(isinstance(e, Utterance) for e in seg.feed(frames(20, SILENCE)))


def test_segmenter_defaults_unchanged():
    seg = _seg()
    out = seg.feed(frames(15, SPEECH)) + seg.feed(frames(25, SILENCE))
    assert [type(e) for e in out] == [Utterance]


# ---- "slower, please" and the history cache -----------------------------------------------
def test_speech_pace_request_is_stored_per_language(repo):
    brain = ScriptedBrain()
    from faria.session.lifecycle import SessionRunner

    runner = SessionRunner(repo, brain, Settings())
    runner.start("bazh", language="de")
    brain.queue("Gern, langsamer.", control=control_call(action="set_speech_pace", speech_pace="slower"))
    list(runner.turn("Langsamer, bitte"))
    assert runner.context.profile.preferences["speech_pace"] == "slower"


def test_interruption_note_reaches_next_turn(repo):
    brain = ScriptedBrain()
    from faria.session.lifecycle import SessionRunner

    runner = SessionRunner(repo, brain, Settings())
    runner.start("bazh", language="es")
    runner.note_interrupted()
    brain.queue("Vale.")
    list(runner.turn("Espera"))
    assert "[note: the learner started talking over you]" in brain.calls[0]["messages"][-1]["content"]
    brain.queue("Bien.")
    list(runner.turn("Ya"))
    assert "talking over you" not in brain.calls[1]["messages"][-1]["content"]  # only once


def test_cache_latest_marks_only_the_newest_message():
    history = [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "¿qué tal?"},
               {"role": "user", "content": "bien"}]
    out = _cache_latest(history)
    assert out[-1]["content"] == [{"type": "text", "text": "bien", "cache_control": {"type": "ephemeral"}}]
    assert out[0] == history[0] and history[-1]["content"] == "bien"  # history itself untouched


# ---- the voice loop end to end ------------------------------------------------------------
class CountingStt(Transcriber):
    def __init__(self, text):
        self.calls = 0
        self._text = text

    def transcribe(self, audio_pcm, sample_rate, language):
        self.calls += 1
        return TranscriptionResult(text=self._text, language="es", confidence=0.9, words=[])


class RecordingBrain(FakeBrain):
    def stream_reply(self, system, messages, tools=None):
        self.messages = list(messages)
        return super().stream_reply(system, messages, tools)


def _server(tmp_path, stt_text="Yo tiene mucho hambre", tts=None, **audio):
    settings = Settings(data_dir=tmp_path, audio=AudioConfig(**audio))
    conn = connect(settings.db_path)
    apply_migrations(conn)
    conn.close()
    brain, stt, tts = RecordingBrain(), CountingStt(stt_text), tts or RecordingTts()
    deps = ServerDeps(settings=settings, stt=TranscriberRegistry(stt), tts=tts,
                      detector_factory=ByteMarkerDetector, brain_factory=lambda: brain)
    return TestClient(create_app(settings, deps)), brain, stt, tts


def _start(ws, language="es"):
    ws.send_json({"type": "start", "name": "bazh", "language": language})
    _drain_until(ws, "session_started")
    time.sleep(0.2)  # let the acknowledgement clips warm in the background


def test_ack_plays_before_the_transcript(tmp_path):
    client, brain, _, _ = _server(tmp_path, ack_probability=1.0)
    brain.queue("¿Qué vas a comer?")
    with client.websocket_connect("/ws/session") as ws:
        _start(ws)
        ws.send_bytes(frames(30, SPEECH) + frames(40, SILENCE))
        seen, done = _drain_until(ws, "reply_done")
        kinds = [k if k == "bytes" else p.get("type") for k, p in seen]
        assert kinds.index("bytes") < kinds.index("you_said")
        assert done["timings"]["ack_s"] is not None
        ws.send_json({"type": "abandon"})


def test_early_transcription_is_reused_and_can_end_the_turn(tmp_path):
    # A finished sentence ("…cine.") ends the turn at 0.6 × the A2 wait (570ms), so 690ms
    # of silence is enough although the usual wait is 950ms, and recognition ran once.
    client, brain, stt, _ = _server(tmp_path, stt_text="Ayer fui al cine.", ack_enabled=False)
    brain.queue("¡Qué bien! ¿Qué película viste?")
    with client.websocket_connect("/ws/session") as ws:
        _start(ws)
        ws.send_bytes(frames(20, SPEECH) + frames(11, SILENCE))  # probe at 300ms
        time.sleep(0.3)                                          # the early transcript lands
        ws.send_bytes(frames(12, SILENCE))                       # 690ms in all: < 950
        _, done = _drain_until(ws, "reply_done")
        assert done["timings"]["early_stt"] is True and stt.calls == 1
        ws.send_json({"type": "abandon"})


def test_stressed_recast_is_slower_after_a_pause(tmp_path):
    tts = RecordingTts()
    client, brain, _, _ = _server(tmp_path, tts=tts, ack_enabled=False)
    brain.queue("Ah, ¿*fuiste* a la playa?")
    with client.websocket_connect("/ws/session") as ws:
        _start(ws)
        ws.send_bytes(frames(15, SPEECH) + frames(40, SILENCE))
        seen, done = _drain_until(ws, "reply_done")
        spoken = [(t, p) for t, _, p in tts.calls]
        assert [t for t, _ in spoken] == ["Ah, ¿", "fuiste", " a la playa?"]
        base = spoken[0][1]
        assert spoken[1][1] == pytest.approx(base * 1.2)
        audio = [p for k, p in seen if k == "bytes"]
        assert len(audio[1]) == len(audio[0]) + int(22050 * 0.09) * 2  # 90ms of silence first
        assert done["text"] == "Ah, ¿*fuiste* a la playa?"  # the browser shows it in bold
        ws.send_json({"type": "abandon"})


def test_barge_in_stops_the_voice_and_tells_the_tutor(tmp_path):
    tts = RecordingTts(samples=22050 * 5)  # five seconds of reply still playing
    client, brain, _, _ = _server(tmp_path, tts=tts, ack_enabled=False)
    brain.queue("Te voy a contar una historia larga.")
    brain.queue("Ah, perdona. Dime.")
    with client.websocket_connect("/ws/session") as ws:
        _start(ws)
        ws.send_bytes(frames(15, SPEECH) + frames(40, SILENCE))
        _drain_until(ws, "reply_done")
        _drain_until(ws, "state")                                # back to listening
        ws.send_bytes(frames(16, SPEECH))                        # 480ms over the tutor's voice
        _drain_until(ws, "stop_audio")
        ws.send_bytes(frames(40, SILENCE))
        _drain_until(ws, "reply_done")
        assert "[note: the learner started talking over you]" in brain.messages[-1]["content"]
        ws.send_json({"type": "abandon"})
