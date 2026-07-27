"""Sentence assembly for streaming TTS (the <2s latency budget's main lever).

Claude streams text deltas; waiting for the full reply before synthesizing
would add the whole generation time to time-to-first-audio. Instead deltas
accumulate here and each completed sentence is released to TTS immediately —
the tutor starts speaking its first sentence while still writing its third.

Pure text logic, no I/O — deliberately dumb about abbreviations ("Dr.",
"z.B.") because a false sentence break costs only a slightly early TTS
handoff, not a wrong answer. Spanish ¿¡ openers attach to the *following*
sentence because splits happen after terminal punctuation only.
"""

from __future__ import annotations

_TERMINALS = ".!?…"
# Closers that may trail a terminal and still belong to the sentence: quotes,
# parens, guillemets.
_TRAILERS = "\"')»”’"

_MAX_BUFFER = 280  # a delta stream with no punctuation still flushes eventually


class SentenceAssembler:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        """Add a streamed delta; return any sentences it completed."""
        self._buffer += delta
        out: list[str] = []
        while (cut := self._split_point()) is not None:
            sentence = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:]
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str | None:
        """Stream ended: whatever remains is the last sentence."""
        rest, self._buffer = self._buffer.strip(), ""
        return rest or None

    def _split_point(self) -> int | None:
        for i, ch in enumerate(self._buffer):
            if ch not in _TERMINALS:
                continue
            j = i + 1
            # keep runs like "?!"/"..." and trailing quotes with the sentence
            while j < len(self._buffer) and self._buffer[j] in _TERMINALS + _TRAILERS:
                j += 1
            # need whitespace after the run — protects decimals ("2.50") and
            # mid-token dots from early cuts
            if j >= len(self._buffer) or not self._buffer[j].isspace():
                continue
            k = j
            while k < len(self._buffer) and self._buffer[k].isspace():
                k += 1
            if k >= len(self._buffer):
                # what follows hasn't streamed in yet — wait rather than guess
                # (flush() handles the true end of the reply)
                return None
            # a lowercase continuation means a pause, not a boundary
            # ("Bueno... vamos"). Caseless scripts (Arabic etc.) are never
            # islower(), so they split normally.
            if self._buffer[k].islower():
                continue
            return j
        if len(self._buffer) >= _MAX_BUFFER:
            # no sentence break in sight: cut at the last space BEFORE the cap
            # (a single oversized delta must still come out in capped pieces)
            space = self._buffer.rfind(" ", 0, _MAX_BUFFER)
            if space > 0:
                return space
        return None
