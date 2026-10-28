"""Streaming wrapper around the Anthropic Messages API.

Thin by design: model choice, token limits, and thinking mode come from config;
prompt content comes from tutor/prompts.py; what to do with the reply is the
session layer's business.

Credentials: the SDK resolves ANTHROPIC_API_KEY from the environment
(config.load_settings() loads .env first). No key handling here.

Thinking mode: claude-sonnet-5 runs adaptive thinking BY DEFAULT when the
parameter is omitted, which costs latency on every conversational turn — so we
send an explicit ``{"type": "disabled"}`` unless config says adaptive. Caveat
for Phase 1: with thinking disabled the model is less eager to call tools; if
the annotation sidecar under-triggers, flip ``models.thinking`` to "adaptive"
and re-measure before prompt surgery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anthropic

from faria.config import ModelsConfig

if TYPE_CHECKING:
    from anthropic.lib.streaming import MessageStreamManager


class TutorBrain:
    def __init__(self, models: ModelsConfig) -> None:
        self._models = models
        self._client = anthropic.Anthropic()

    def stream_reply(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> "MessageStreamManager":
        """Start a streamed conversation turn; caller consumes the stream.

        Usage::

            with brain.stream_reply(system, history) as stream:
                for delta in stream.text_stream:
                    ...
                final = stream.get_final_message()

        ``system`` is sent as a content block with cache_control so the
        assembled prompt gets prompt-cached once it exceeds the model's
        minimum cacheable prefix (small Phase 0 prompts won't cache yet —
        that's expected and harmless).

        ``tools`` is the Phase 1 hook for the annotation sidecar
        (tutor/schema.py): pass [ERROR_ANNOTATION_TOOL] and read tool_use
        blocks off ``stream.get_final_message().content``.
        """
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.stream(
            model=self._models.conversation,
            max_tokens=self._models.max_reply_tokens,
            thinking=self._thinking_param(),
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=_cache_latest(messages),
            **kwargs,
        )

    def warm(self, system: str, tools: list[dict] | None = None):
        """Write the prompt cache before the first turn (ADR-033).

        Measured: the first conversation call of a session took ~8s to its first
        word with a cold cache and ~1.4s once cached. One max_tokens=1 call with
        the same tools + system prefix, sent while the learner is still saying
        hello, moves that cost off the first turn. The cache write is paid either
        way, so the extra cost is one cache read and one output token."""
        kwargs: dict = {"tools": tools} if tools else {}
        return self._client.messages.create(
            model=self._models.conversation,
            max_tokens=1,
            thinking=self._thinking_param(),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "(the learner is about to speak)"}],
            **kwargs,
        )

    def _thinking_param(self) -> dict:
        if self._models.thinking == "adaptive":
            return {"type": "adaptive"}
        return {"type": "disabled"}


def _cache_latest(messages: list[dict]) -> list[dict]:
    """A second cache breakpoint on the newest message, so each turn re-reads the
    conversation so far from cache instead of reprocessing it: over an hour-long
    session that's most of the input, and the main thing slowing the first word.
    The history list itself is never modified."""
    if not messages:
        return messages
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    content = [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]
    return [*messages[:-1], {**last, "content": content}]
