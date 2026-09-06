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
  JSON  {"type": "delta", "text"}                          (tutor text, streamed)
  JSON  {"type": "audio_format", "sample_rate"}            (before each reply's audio)
  binary: TTS Int16 PCM at that rate
  JSON  {"type": "reply_done", "text", "language", "switched", "timings"}
  JSON  {"type": "session_ended", "summary", "next_focus", "recap"}
  JSON  {"type": "error", "message"}

Concurrency model: a receiver task drains the socket continuously — mic
frames feed the VAD segmenter only while state is "listening" and are DROPPED
otherwise (half-duplex v1: the tutor doesn't listen while thinking/speaking,
so stale audio can't pile up in the socket and replay into the next turn;
barge-in is future work). The main task consumes an event queue and does all
the sending, so there is exactly one writer.

Latency budget (<2s utterance-end to first audio): per-stage timings are
logged per turn and sent to the client in reply_done — tune stt_model /
vad_silence_ms against real numbers, not vibes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from faria.db.connection import connect
from faria.db.repository import Repository
from faria.domain.levels import band
from faria.server.app import ServerDeps
from faria.session.lifecycle import SessionRunner
from faria.speech.sentences import SentenceAssembler
from faria.speech.stt import TranscriptionResult, is_probable_speech
from faria.speech.vad import UtteranceSegmenter, Utterance

log = logging.getLogger("faria.ws")

_END = "__end__"
_ABANDON = "__abandon__"
_DISCONNECT = "__disconnect__"


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

    segmenter = UtteranceSegmenter(deps.settings.audio, deps.detector_factory())
    events: asyncio.Queue[Any] = asyncio.Queue()
    state = {"value": "listening"}

    async def receiver() -> None:
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    await events.put(_DISCONNECT)
                    return
                if (data := message.get("bytes")) is not None:
                    if state["value"] == "listening":
                        for utt in segmenter.feed(data):
                            await events.put(utt)
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
            assert isinstance(event, Utterance)
            await _run_turn(ws, deps, runner, segmenter, state, event)
    finally:
        recv_task.cancel()


async def _run_turn(
    ws: WebSocket,
    deps: ServerDeps,
    runner: SessionRunner,
    segmenter: UtteranceSegmenter,
    state: dict,
    utt: Utterance,
) -> None:
    t_utt_end = time.monotonic()
    await _set_state(ws, state, "transcribing")

    transcriber = deps.stt.for_language(runner.active_language)
    stt: TranscriptionResult = await asyncio.to_thread(
        transcriber.transcribe, utt.pcm, deps.settings.audio.sample_rate, None
    )
    t_transcript = time.monotonic()

    text = stt.text.strip()
    if not is_probable_speech(stt, deps.settings.audio):
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
    timings = {"stt_s": round(t_transcript - t_utt_end, 2), "first_delta_s": None,
               "first_audio_s": None}
    audio_format_sent = {"rate": None}

    async def speak(sentence: str) -> None:
        # Split each sentence into per-language runs and synthesize each in its
        # own voice (ADR-026): in English mode a Spanish phrase gets the Spanish
        # voice mid-sentence, not English phonemes mangling it. Runs default to
        # the active language on any doubt, so worst case is the status quo. An
        # API voice that code-switches natively skips the split.
        if deps.lang_router is not None and not deps.tts.handles_code_switching:
            runs = deps.lang_router.split_runs(sentence, default=speak_language)
        else:
            runs = [(sentence, speak_language)]
        for run_text, run_language in runs:
            if not run_text.strip():
                continue
            chunks = await asyncio.to_thread(
                lambda t=run_text, l=run_language: list(deps.tts.synthesize(t, l))
            )
            for chunk in chunks:
                if not chunk.pcm:
                    continue
                if audio_format_sent["rate"] != chunk.sample_rate:
                    await ws.send_json({"type": "audio_format", "sample_rate": chunk.sample_rate})
                    audio_format_sent["rate"] = chunk.sample_rate
                if timings["first_audio_s"] is None:
                    timings["first_audio_s"] = round(time.monotonic() - t_utt_end, 2)
                    await _set_state(ws, state, "speaking")
                await ws.send_bytes(chunk.pcm)

    async for delta in _iter_turn(runner, text, stt):
        if timings["first_delta_s"] is None:
            timings["first_delta_s"] = round(time.monotonic() - t_utt_end, 2)
        reply_parts.append(delta)
        await ws.send_json({"type": "delta", "text": delta})
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

    switched = runner.active_language != speak_language
    await ws.send_json(
        {
            "type": "reply_done",
            "text": "".join(reply_parts),
            "language": runner.active_language,
            "switched": switched,
            "timings": timings,
        }
    )
    log.info(
        "turn timings: stt=%ss first_delta=%ss first_audio=%ss (lang=%s%s)",
        timings["stt_s"], timings["first_delta_s"], timings["first_audio_s"],
        speak_language, f" -> {runner.active_language}" if switched else "",
    )
    segmenter.reset()
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
