"""The realtime audio loop: one websocket = one session (ADR-021).

Wire protocol
-------------
Client -> server:
  JSON  {"type": "start", "name": "...", "language": "es"|null, "goal": null}
  binary: 16 kHz mono Int16 PCM mic frames, streamed continuously
  JSON  {"type": "end_session"} | {"type": "abandon"}

Server -> client:
  JSON  {"type": "session_started", "language", "home", "level"}
  JSON  {"type": "state", "value": "listening"|"transcribing"|"thinking"|"speaking"}
  JSON  {"type": "you_said", "text", "language"}          (language = STT-detected)
  JSON  {"type": "delta", "text"}                          (tutor text, streamed; *word* = stressed recast)
  JSON  {"type": "audio_format", "sample_rate"}            (before audio at a new rate)
  binary: TTS Int16 PCM at that rate                       (an acknowledgement, then the reply)
  JSON  {"type": "stop_audio"}                             (barge-in: drop everything queued)
  JSON  {"type": "reply_done", "text", "language", "switched", "interrupted", "timings"}
  JSON  {"type": "session_ended", "summary", "next_focus", "recap"}
  JSON  {"type": "error", "message"}

Concurrency model: a receiver task drains the socket continuously. Mic frames feed
the VAD segmenter while state is "listening", and also while "speaking" when barge-in
is on (ADR-031); otherwise they are DROPPED, so stale audio can't pile up and replay
into the next turn. The main task consumes an event queue and does all the sending,
so there is exactly one writer.

Speech improvements layered on the basic loop:
  - patience and speaking pace by level, re-read after every reply (ADR-030);
  - early transcription: a pause of endpoint_probe_ms starts recognition on the audio
    so far; if the learner stays silent the result is reused, and if it arrives in
    time it decides whether to wait longer or end the turn early (ADR-030);
  - an instant acknowledgement ("mm", "ajá") while recognition runs (ADR-029);
  - barge-in: talking over the tutor stops its voice (ADR-031);
  - stressed recasts: *marked* words said after a pause and a little slower (ADR-032).

Latency budget (<2s utterance-end to first audio): per-stage timings are logged per
turn and sent to the client in reply_done; tune against real numbers, not vibes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from faria.db.connection import connect
from faria.db.repository import Repository
from faria.domain.levels import band
from faria.server.app import ServerDeps
from faria.session.lifecycle import SessionRunner
from faria.speech import pacing
from faria.speech.ack import AckBank
from faria.speech.emphasis import split_emphasis, strip_marks
from faria.speech.endpoint import verdict
from faria.speech.sentences import SentenceAssembler
from faria.speech.stt import TranscriptionResult, is_probable_speech
from faria.speech.vad import Probe, SpeechStart, UtteranceSegmenter, Utterance

log = logging.getLogger("faria.ws")

_END = "__end__"
_ABANDON = "__abandon__"
_DISCONNECT = "__disconnect__"
_BARGE = "__barge__"


@dataclass
class _Voice:
    """Per-session speaking state shared by the receiver and the main task."""

    pace: float = 1.0
    silence_ms: int = 700
    playback_until: float = 0.0          # when the client will finish playing what we sent
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    spec: tuple[int, int, asyncio.Task] | None = None  # early transcription: (seq, last_voiced, task)

    def played(self, seconds: float) -> None:
        now = time.monotonic()
        self.playback_until = max(self.playback_until, now + 0.05) + seconds

    def take_spec(self, utt: Utterance) -> asyncio.Task | None:
        """The early transcription, if it saw all of this utterance's speech."""
        spec, self.spec = self.spec, None
        if spec and spec[0] == utt.seq and spec[1] == utt.last_voiced:
            return spec[2]
        return None


def build_ws_router(deps: ServerDeps) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/session")
    async def session_socket(ws: WebSocket) -> None:
        await ws.accept()
        conn = connect(deps.settings.db_path)
        try:
            await _run_session(ws, deps, conn)
        except WebSocketDisconnect:
            pass
        finally:
            conn.close()

    return router


async def _run_session(ws: WebSocket, deps: ServerDeps, conn) -> None:
    start = await ws.receive_json()
    if start.get("type") != "start":
        await ws.send_json({"type": "error", "message": "first message must be 'start'"})
        return

    runner = SessionRunner(Repository(conn), deps.brain_factory(), deps.settings)
    ctx = runner.start(
        learner_name=start.get("name") or "learner",
        language=start.get("language") or None,
        goal=start.get("goal") or None,
        mode="audio",
    )
    await ws.send_json(
        {
            "type": "session_started",
            "language": runner.active_language,
            "home": runner.active_language == ctx.learner.native_language,
            "level": band(ctx.profile.level_overall) if ctx.profile.level_overall else None,
        }
    )
    # Voices are preloaded at server startup (app.py), so this is a cheap
    # cache touch — kept as a safety net for a TTS backend that didn't warm.
    for lang in {runner.active_language, ctx.learner.native_language}:
        await asyncio.to_thread(deps.tts.preload, lang)

    audio = deps.settings.audio
    segmenter = UtteranceSegmenter(
        audio, deps.detector_factory(), probe_ms=audio.endpoint_probe_ms, report_start=audio.barge_in
    )
    acks = AckBank(deps.tts, audio)
    voice = _Voice()
    events: asyncio.Queue[Any] = asyncio.Queue()
    state = {"value": "listening"}

    def retune() -> None:
        """Patience and pace follow the active language's level and the learner's
        own "slower, please" (ADR-030); acknowledgement clips follow the pace."""
        c = runner.context
        is_home = runner.active_language == c.learner.native_language
        level = c.profile.level_overall
        voice.silence_ms = pacing.silence_ms(audio, level, is_home)
        voice.pace = pacing.pace(audio, level, is_home, c.profile.preferences.get("speech_pace"))
        segmenter.set_silence_ms(voice.silence_ms)
        asyncio.get_running_loop().run_in_executor(None, acks.warm, runner.active_language, voice.pace)

    retune()

    def on_early_transcript(probe: Probe, task: asyncio.Task) -> None:
        """Runs when an early transcription finishes. If the learner is still in
        that same pause, its text decides: mid-thought -> wait longer; a finished
        sentence -> end the turn now (or at the early threshold)."""
        if task.cancelled() or task.exception() is not None or state["value"] != "listening":
            return
        if segmenter.current_seq != probe.seq or segmenter.current_last_voiced != probe.last_voiced:
            return  # they kept talking (or the turn already ended): no opinion
        decision = verdict(task.result().text, runner.active_language)
        if decision == "hold":
            segmenter.hold(audio.endpoint_hold_ms)
        elif decision == "end":
            utt = segmenter.shorten_to(int(voice.silence_ms * audio.endpoint_early_ratio))
            if utt is not None:
                events.put_nowait(utt)

    async def receiver() -> None:
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    await events.put(_DISCONNECT)
                    return
                if (data := message.get("bytes")) is not None:
                    current = state["value"]
                    barging = audio.barge_in and current == "speaking"
                    if current == "listening" or barging:
                        for ev in segmenter.feed(data):
                            if isinstance(ev, SpeechStart):
                                continue
                            if barging and isinstance(ev, Probe):
                                continue  # no early transcription over the tutor's voice
                            if barging and isinstance(ev, Utterance) and not voice.interrupt.is_set():
                                continue  # heard while Faria spoke, never confirmed as you: likely echo
                            await events.put(ev)
                        if (audio.barge_in and not voice.interrupt.is_set()
                                and segmenter.active_speech_ms >= audio.barge_in_min_ms
                                and (barging or time.monotonic() < voice.playback_until)):
                            voice.interrupt.set()
                            if not barging:
                                await events.put(_BARGE)  # main task is idle: let it stop the audio
                    continue
                if (text := message.get("text")) is not None:
                    msg = json.loads(text)
                    if msg.get("type") == "end_session":
                        await events.put(_END)
                    elif msg.get("type") == "abandon":
                        await events.put(_ABANDON)
        except WebSocketDisconnect:
            await events.put(_DISCONNECT)

    recv_task = asyncio.create_task(receiver())
    await ws.send_json({"type": "state", "value": "listening"})
    # Warm Claude's prompt cache while the learner says hello (ADR-033). The first
    # turn waits for it rather than racing it on the shared DB connection.
    warming = asyncio.ensure_future(asyncio.to_thread(runner.warm_cache))

    try:
        while True:
            event = await events.get()
            if event is _DISCONNECT:
                runner.abandon()  # tab closed mid-conversation: no summary spend
                return
            if event is _ABANDON:
                runner.abandon()
                await ws.send_json({"type": "session_ended", "summary": None,
                                    "next_focus": [], "recap": []})
                return
            if event is _END:
                await _end_session(ws, runner, state)
                return
            if event is _BARGE:
                await ws.send_json({"type": "stop_audio"})
                voice.playback_until = 0.0
                runner.note_interrupted()
                continue
            if isinstance(event, Probe):
                if voice.spec is None or voice.spec[2].done():  # one at a time: CPU is shared
                    transcriber = deps.stt.for_language(runner.active_language)
                    task = asyncio.ensure_future(asyncio.to_thread(
                        transcriber.transcribe, event.pcm, audio.sample_rate, None))
                    voice.spec = (event.seq, event.last_voiced, task)
                    task.add_done_callback(lambda t, p=event: on_early_transcript(p, t))
                continue
            assert isinstance(event, Utterance)
            if not warming.done():
                await warming
            language_before = runner.active_language
            await _run_turn(ws, deps, runner, segmenter, state, event, voice, acks)
            retune()
            if runner.active_language != language_before:  # new prompt: warm it too
                warming = asyncio.ensure_future(asyncio.to_thread(runner.warm_cache))
    finally:
        recv_task.cancel()


async def _run_turn(
    ws: WebSocket,
    deps: ServerDeps,
    runner: SessionRunner,
    segmenter: UtteranceSegmenter,
    state: dict,
    utt: Utterance,
    voice: _Voice,
    acks: AckBank,
) -> None:
    t_utt_end = time.monotonic()
    audio = deps.settings.audio
    voice.interrupt.clear()
    await _set_state(ws, state, "transcribing")
    audio_format_sent = {"rate": None}

    async def send_audio(chunks, pause_ms: int = 0) -> None:
        for i, chunk in enumerate(chunks):
            if not chunk.pcm:
                continue
            pcm = chunk.pcm
            if i == 0 and pause_ms:
                pcm = b"\x00\x00" * int(chunk.sample_rate * pause_ms / 1000) + pcm
            if audio_format_sent["rate"] != chunk.sample_rate:
                await ws.send_json({"type": "audio_format", "sample_rate": chunk.sample_rate})
                audio_format_sent["rate"] = chunk.sample_rate
            await ws.send_bytes(pcm)
            voice.played(len(pcm) / 2 / chunk.sample_rate)

    # Instant acknowledgement (ADR-029): plays while recognition and Claude work.
    timings = {"stt_s": None, "first_delta_s": None, "first_audio_s": None,
               "ack_s": None, "early_stt": False}
    if (clip := acks.pick(runner.active_language, voice.pace, utt.speech_s)) is not None:
        await send_audio(clip)
        timings["ack_s"] = round(time.monotonic() - t_utt_end, 2)

    early = voice.take_spec(utt)
    if early is not None:
        stt: TranscriptionResult = await early
        timings["early_stt"] = True
    else:
        transcriber = deps.stt.for_language(runner.active_language)
        stt = await asyncio.to_thread(
            transcriber.transcribe, utt.pcm, audio.sample_rate, None
        )
    t_transcript = time.monotonic()
    timings["stt_s"] = round(t_transcript - t_utt_end, 2)

    text = stt.text.strip()
    if not is_probable_speech(stt, audio):
        # VAD fired on noise, or whisper hallucinated on silence (ADR-025) —
        # drop it silently and keep listening. This is the "only transcribe
        # sounds actually directed at it" behavior.
        log.info("dropped non-speech: %r (no_speech=%.2f logprob=%.2f conf=%.2f)",
                 text[:40], stt.no_speech_prob, stt.avg_logprob, stt.confidence)
        segmenter.reset()
        await _set_state(ws, state, "listening")
        return

    await ws.send_json({"type": "you_said", "text": text, "language": stt.language})
    await _set_state(ws, state, "thinking")

    speak_language = runner.active_language  # a switch this turn applies to the NEXT reply
    assembler = SentenceAssembler()
    reply_parts: list[str] = []

    async def speak(sentence: str) -> None:
        # Stressed recasts (ADR-032): *marked* words get a short pause and a slower
        # pace. Then split each piece into per-language runs and synthesize each in
        # its own voice (ADR-026): in English mode a Spanish phrase gets the Spanish
        # voice mid-sentence, not English phonemes mangling it. Runs default to the
        # active language on any doubt. An API voice that code-switches natively
        # skips the split.
        if voice.interrupt.is_set():
            return  # barge-in: the learner is talking; say nothing more (ADR-031)
        pieces = split_emphasis(sentence) if audio.emphasis else [(strip_marks(sentence), False)]
        for piece, stressed in pieces:
            if deps.lang_router is not None and not deps.tts.handles_code_switching:
                runs = deps.lang_router.split_runs(piece, default=speak_language)
            else:
                runs = [(piece, speak_language)]
            pace = voice.pace * (audio.emphasis_slowdown if stressed else 1.0)
            for n, (run_text, run_language) in enumerate(runs):
                if not run_text.strip() or voice.interrupt.is_set():
                    continue
                chunks = await asyncio.to_thread(
                    lambda t=run_text, l=run_language, p=pace: list(deps.tts.synthesize(t, l, pace=p))
                )
                if timings["first_audio_s"] is None and any(c.pcm for c in chunks):
                    timings["first_audio_s"] = round(time.monotonic() - t_utt_end, 2)
                    await _set_state(ws, state, "speaking")
                await send_audio(chunks, audio.emphasis_pause_ms if stressed and n == 0 else 0)

    stopped = {"sent": False}

    async def stop_if_interrupted() -> None:
        # The learner started talking over the reply: silence the audio already
        # queued in the browser now, not when Claude finishes writing (ADR-031).
        if voice.interrupt.is_set() and not stopped["sent"]:
            await ws.send_json({"type": "stop_audio"})
            voice.playback_until = 0.0
            stopped["sent"] = True

    async for delta in _iter_turn(runner, text, stt):
        if timings["first_delta_s"] is None:
            timings["first_delta_s"] = round(time.monotonic() - t_utt_end, 2)
        reply_parts.append(delta)
        await ws.send_json({"type": "delta", "text": delta})
        await stop_if_interrupted()
        for sentence in assembler.feed(delta):
            await speak(sentence)
    if (rest := assembler.flush()) is not None:
        await speak(rest)

    # Persist alongside the token counts the runner already recorded for this
    # turn, so latency can be charted against the config that produced it
    # (migration 0005) instead of only appearing in the log.
    await asyncio.to_thread(
        runner.attach_turn_timings,
        stt_s=timings["stt_s"],
        first_delta_s=timings["first_delta_s"],
        first_audio_s=timings["first_audio_s"],
    )

    interrupted = voice.interrupt.is_set()
    if interrupted:
        await stop_if_interrupted()
        runner.note_interrupted()

    switched = runner.active_language != speak_language
    await ws.send_json(
        {
            "type": "reply_done",
            "text": "".join(reply_parts),
            "language": runner.active_language,
            "switched": switched,
            "interrupted": interrupted,
            "timings": timings,
        }
    )
    log.info(
        "turn timings: stt=%ss%s ack=%ss first_delta=%ss first_audio=%ss (lang=%s%s)%s",
        timings["stt_s"], " (early)" if timings["early_stt"] else "", timings["ack_s"],
        timings["first_delta_s"], timings["first_audio_s"],
        speak_language, f" -> {runner.active_language}" if switched else "",
        " interrupted" if interrupted else "",
    )
    if not interrupted:
        segmenter.reset()  # an interruption keeps the learner's words in progress
    await _set_state(ws, state, "listening")


async def _iter_turn(runner: SessionRunner, text: str, stt: TranscriptionResult):
    """Bridge the sync turn() generator into async without blocking the loop."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def run() -> None:
        try:
            for delta in runner.turn(text, stt=stt):
                loop.call_soon_threadsafe(queue.put_nowait, delta)
        except BaseException as exc:  # surfaced in the async consumer
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    threading.Thread(target=run, daemon=True).start()
    while True:
        item = await queue.get()
        if item is sentinel:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


async def _end_session(ws: WebSocket, runner: SessionRunner, state: dict) -> None:
    await _set_state(ws, state, "thinking")
    recap = [
        {"said": e.span_text, "correction": e.correction, "note": e.explanation}
        for e in runner.recap_queue
    ]
    summary = await asyncio.to_thread(runner.end)
    await ws.send_json(
        {
            "type": "session_ended",
            "summary": summary.summary_text if summary else None,
            "next_focus": summary.next_focus if summary else [],
            "recap": recap,
        }
    )


async def _set_state(ws: WebSocket, state: dict, value: str) -> None:
    state["value"] = value
    await ws.send_json({"type": "state", "value": value})
