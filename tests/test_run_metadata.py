"""Run-metadata substrate (migration 0005): what produced a session.

The point of these rows is answering "did that prompt change help?" — which
needs the config, the exact prompt text, and per-call cost/latency to survive
the session that produced them.
"""

import json

from faria.config import Settings
from faria.domain.models import TokenUsage
from faria.session.lifecycle import SessionRunner
from faria.tutor.pricing import estimate_cost
from faria.tutor.prompts import personas, prompt_template_version

from test_lifecycle import FakeBrain, _Block, _FakeMessage, _FakeStream


class _Usage:
    def __init__(self, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


class MeteredBrain(FakeBrain):
    """FakeBrain whose final message carries a real usage object + model id."""

    def __init__(self, model="claude-sonnet-5", usage=None):
        super().__init__()
        self._model = model
        self._usage = usage or _Usage(input_tokens=1000, output_tokens=200)

    def stream_reply(self, system, messages, tools=None):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        text, blocks = self._script.pop(0)
        message = _FakeMessage(blocks, model=self._model)
        message.usage = self._usage
        return _FakeStream(text, message)


# -- pricing ------------------------------------------------------------------


def test_cost_uses_input_and_output_rates():
    # Sonnet 5: $2/1M in, $10/1M out.
    cost = estimate_cost("claude-sonnet-5", TokenUsage(input_tokens=1_000_000, output_tokens=100_000))
    assert cost == 3.0  # 2.00 + 1.00


def test_cache_reads_are_cheaper_than_fresh_input():
    fresh = estimate_cost("claude-sonnet-5", TokenUsage(input_tokens=1_000_000))
    cached = estimate_cost("claude-sonnet-5", TokenUsage(cache_read_tokens=1_000_000))
    written = estimate_cost("claude-sonnet-5", TokenUsage(cache_write_tokens=1_000_000))
    assert cached == round(fresh * 0.1, 6)
    assert written == round(fresh * 1.25, 6)


def test_unknown_model_yields_no_cost_rather_than_a_guess():
    assert estimate_cost("some-model-we-never-priced", TokenUsage(input_tokens=1000)) is None
    assert estimate_cost(None, TokenUsage(input_tokens=1000)) is None


def test_dated_model_id_matches_its_base_price():
    """The API echoes back a resolved id that may carry a suffix."""
    assert estimate_cost("claude-sonnet-5-20260101", TokenUsage(output_tokens=1_000_000)) == 10.0


def test_token_usage_tolerates_a_brain_that_reports_nothing():
    assert TokenUsage.from_usage(None) == TokenUsage()
    assert TokenUsage.from_usage(object()).total == 0


# -- session stamping ---------------------------------------------------------


def test_session_records_config_prompt_version_and_code_version(repo):
    runner = SessionRunner(repo, FakeBrain(), Settings())
    runner.start("bazh", language="es")

    row = repo._conn.execute("SELECT * FROM sessions").fetchone()
    assert row["prompt_template_version"] == prompt_template_version()
    assert row["faria_version"]

    snapshot = json.loads(row["config_snapshot"])
    # Full resolved settings, not a diff against defaults — readable without
    # matching code.
    assert snapshot["models"]["conversation"] == "claude-sonnet-5"
    assert snapshot["correction"]["max_inline_per_turn"] == 1
    assert snapshot["audio"]["vad_silence_ms"] == 700


def test_config_snapshot_captures_overrides_not_just_defaults(repo):
    settings = Settings()
    settings.audio.vad_silence_ms = 1200
    runner = SessionRunner(repo, FakeBrain(), settings)
    runner.start("bazh", language="es")

    row = repo._conn.execute("SELECT config_snapshot FROM sessions").fetchone()
    assert json.loads(row["config_snapshot"])["audio"]["vad_silence_ms"] == 1200


# -- prompt trail -------------------------------------------------------------


def test_assembled_prompt_is_stored_verbatim(repo):
    runner = SessionRunner(repo, FakeBrain(), Settings())
    runner.start("bazh", language="es")

    row = repo._conn.execute(
        "SELECT sp.language, pv.text FROM session_prompts sp "
        "JOIN prompt_versions pv ON pv.hash = sp.prompt_hash WHERE sp.seq = 0"
    ).fetchone()
    assert row["language"] == "es"
    assert row["text"] == runner._system
    assert personas()["mentor"] in row["text"]


def test_language_switch_records_a_second_prompt(repo):
    runner = SessionRunner(repo, FakeBrain(), Settings())
    brain = runner._brain
    runner.start("bazh")  # home base: en
    brain.queue(
        "Klar.",
        control={
            "action": "switch_language", "language": "de", "reason": "learner_request",
            "correction_frequency": None, "correction_style": None, "pivot_frequency": None,
        },
    )
    list(runner.turn("let's do German"))

    rows = repo._conn.execute(
        "SELECT seq, language FROM session_prompts ORDER BY seq"
    ).fetchall()
    assert [(r["seq"], r["language"]) for r in rows] == [(0, "en"), (1, "de")]


def test_identical_prompt_text_is_stored_once(repo):
    """Content-addressed: two sessions with the same prompt share one row."""
    for _ in range(2):
        SessionRunner(repo, FakeBrain(), Settings()).start("bazh", language="es")

    assert repo._conn.execute("SELECT COUNT(*) c FROM session_prompts").fetchone()["c"] == 2
    assert repo._conn.execute("SELECT COUNT(*) c FROM prompt_versions").fetchone()["c"] == 1


# -- per-turn metrics ---------------------------------------------------------


def test_conversation_turn_records_tokens_and_cost(repo):
    brain = MeteredBrain(usage=_Usage(input_tokens=1000, output_tokens=200, cache_read=500))
    runner = SessionRunner(repo, brain, Settings())
    runner.start("bazh", language="es")
    brain.queue("Ah, ¿fuiste a la playa?")
    list(runner.turn("Yo voy a la playa ayer"))

    row = repo._conn.execute("SELECT * FROM turn_metrics").fetchone()
    assert row["stage"] == "conversation"
    assert row["language"] == "es"
    assert row["turn_index"] == 0
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (1000, 200, 500)
    assert row["cost_usd"] == estimate_cost(
        "claude-sonnet-5", TokenUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=500)
    )


def test_metrics_are_recorded_even_when_the_brain_reports_no_usage(repo):
    """A row with zeros beats a missing row: the turn still happened."""
    runner = SessionRunner(repo, FakeBrain(), Settings())
    brain = runner._brain
    runner.start("bazh", language="es")
    brain.queue("Ya veo.")
    list(runner.turn("hola"))

    row = repo._conn.execute("SELECT * FROM turn_metrics").fetchone()
    assert row["stage"] == "conversation"
    assert row["input_tokens"] == 0
    assert row["cost_usd"] is None  # 'fake-model' has no price; don't invent one


def test_audio_timings_attach_to_the_turn_just_run(repo):
    brain = MeteredBrain()
    runner = SessionRunner(repo, brain, Settings())
    runner.start("bazh", language="es", mode="audio")
    brain.queue("Ya veo.")
    list(runner.turn("hola"))
    runner.attach_turn_timings(stt_s=1.8, first_delta_s=2.4, first_audio_s=3.1)

    row = repo._conn.execute("SELECT * FROM turn_metrics").fetchone()
    assert (row["stt_s"], row["first_delta_s"], row["first_audio_s"]) == (1.8, 2.4, 3.1)
    assert row["input_tokens"] == 1000  # the earlier token write survived the update


def test_attaching_timings_before_any_turn_is_a_noop(repo):
    runner = SessionRunner(repo, FakeBrain(), Settings())
    runner.start("bazh", language="es")
    runner.attach_turn_timings(stt_s=1.0)  # must not raise
    assert repo._conn.execute("SELECT COUNT(*) c FROM turn_metrics").fetchone()["c"] == 0
