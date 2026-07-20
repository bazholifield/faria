"""SessionRunner tests using a scripted fake brain — no network calls.

Each test queues exactly what the model "says" (text + tool calls) for one
turn, then asserts on the resulting runner state. This is the only place the
companion-mode wiring (language switching, pivot cooldown, tools-per-language)
is exercised without hitting the real API.
"""

from faria.config import Settings
from faria.session.lifecycle import SessionRunner
from faria.tutor.control import SESSION_CONTROL_TOOL
from faria.tutor.schema import ERROR_ANNOTATION_TOOL


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeMessage:
    def __init__(self, content, model="fake-model"):
        self.content = content
        self.model = model


class _FakeStream:
    def __init__(self, text, final):
        self.text_stream = iter([text] if text else [])
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


_CONTROL_DEFAULTS = {
    "language": None,
    "correction_frequency": None,
    "correction_style": None,
    "pivot_frequency": None,
    "reason": "learner_request",
}


def control_call(**overrides):
    return {**_CONTROL_DEFAULTS, **overrides}


class FakeBrain:
    """A queue of scripted replies, consumed one per turn()."""

    def __init__(self):
        self.calls: list[dict] = []
        self._script: list[tuple[str, list]] = []

    def queue(self, text: str, control: dict | None = None, errors: list[dict] | None = None):
        blocks = [_Block("text", text=text)]
        if control is not None:
            blocks.append(_Block("tool_use", name="update_session_state", input=control))
        if errors is not None:
            blocks.append(
                _Block("tool_use", name="annotate_errors", input={"errors": errors})
            )
        self._script.append((text, blocks))

    def stream_reply(self, system, messages, tools=None):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        text, blocks = self._script.pop(0)
        return _FakeStream(text, _FakeMessage(blocks))


def _make_runner(repo) -> tuple[SessionRunner, FakeBrain]:
    brain = FakeBrain()
    return SessionRunner(repo, brain, Settings()), brain


def _tool_names(call: dict) -> set[str]:
    return {t["name"] for t in (call["tools"] or [])}


def test_home_base_is_native_language_by_default(repo):
    runner, brain = _make_runner(repo)
    ctx = runner.start("bazh")
    assert ctx.profile.language == "en"  # learner default native_language
    assert runner.active_language == "en"

    brain.queue("Nice weather for it.")
    list(runner.turn("hi"))
    # nothing to correct in the learner's own native language
    assert _tool_names(brain.calls[0]) == {"update_session_state"}


def test_explicit_language_start_includes_annotation_tool(repo):
    runner, brain = _make_runner(repo)
    runner.start("bazh", language="es")
    assert runner.active_language == "es"

    brain.queue("Ah, ¿fuiste a la playa?")
    list(runner.turn("Yo voy a la playa ayer"))
    assert _tool_names(brain.calls[0]) == {"update_session_state", "annotate_errors"}


def test_learner_requested_switch_takes_effect(repo):
    runner, brain = _make_runner(repo)
    runner.start("bazh")  # home base: en
    brain.queue(
        "Klar, wechseln wir.",
        control=control_call(action="switch_language", language="de", reason="learner_request"),
    )
    list(runner.turn("let's do German"))
    assert runner.active_language == "de"


def test_tutor_pivot_respects_cooldown_learner_request_does_not(repo):
    runner, brain = _make_runner(repo)
    runner.start("bazh", language="es")

    brain.queue("Vale, sigamos.")  # turn 1: plain, turn_index -> 1
    list(runner.turn("hola"))
    assert runner.active_language == "es"

    # turn 2: tutor-initiated pivot, far past any cooldown (initialized very negative) -> allowed
    brain.queue(
        "Et maintenant, en français !",
        control=control_call(action="switch_language", language="fr", reason="tutor_pivot"),
    )
    list(runner.turn("d'accord"))
    assert runner.active_language == "fr"

    # turn 3: immediately another tutor pivot -> blocked by min_turns_between_pivots (default 4)
    brain.queue(
        "Und jetzt Deutsch!",
        control=control_call(action="switch_language", language="de", reason="tutor_pivot"),
    )
    list(runner.turn("d'accord encore"))
    assert runner.active_language == "fr"  # unchanged: cooldown blocked the second pivot

    # a LEARNER-requested switch on the very next turn is never throttled
    brain.queue(
        "Gut, Deutsch.",
        control=control_call(action="switch_language", language="de", reason="learner_request"),
    )
    list(runner.turn("auf Deutsch bitte"))
    assert runner.active_language == "de"


def test_set_correction_frequency_persists_to_profile(repo):
    runner, brain = _make_runner(repo)
    runner.start("bazh", language="es")
    brain.queue(
        "Como quieras.",
        control=control_call(action="set_correction_frequency", correction_frequency="aggressive"),
    )
    list(runner.turn("corrigeme mas"))
    assert runner.context.profile.correction_aggressiveness == "aggressive"


def test_set_pivot_frequency_stored_on_home_profile(repo):
    runner, brain = _make_runner(repo)
    runner.start("bazh")  # active language is already home base (en)
    brain.queue(
        "Understood — I'll keep you on your toes.",
        control=control_call(action="set_pivot_frequency", pivot_frequency="occasional"),
    )
    list(runner.turn("feel free to test me sometimes"))
    home_profile = repo.get_or_create_profile(runner.context.profile.learner_id, "en", "balanced")
    assert home_profile.preferences["pivot_frequency"] == "occasional"


def test_same_span_reported_twice_in_one_call_is_deduplicated(repo):
    """Observed live: the model can report the identical span under two
    different feature_keys within one annotate_errors call. Only the first
    should be persisted."""
    runner, brain = _make_runner(repo)
    runner.start("bazh", language="es")
    dup_error = {
        "span_text": "Yo tiene mucho hambre",
        "category": "morphosyntax",
        "subcategory": "agreement",
        "severity": 2,
        "confidence": 0.9,
        "correction": "Yo tengo mucho hambre",
        "explanation": "wrong conjugation",
        "interference_source": None,
        "handled": "corrected_recast",
    }
    brain.queue(
        "Vaya, ¿tienes hambre?",
        errors=[
            {**dup_error, "feature_key": "morphosyntax:agreement:yo_tener"},
            {**dup_error, "feature_key": "morphosyntax:agreement:subject_verb"},
        ],
    )
    list(runner.turn("Yo tiene mucho hambre"))

    patterns = repo.list_patterns(runner.context.profile.id)
    assert len(patterns) == 1
    assert patterns[0].feature_key == "morphosyntax:agreement:yo_tener"


def test_annotation_sidecar_feeds_correction_policy(repo):
    runner, brain = _make_runner(repo)
    runner.start("bazh", language="es")
    brain.queue(
        "Ah, ¿fuiste a la playa ayer?",
        errors=[
            {
                "span_text": "Yo voy a la playa ayer",
                "category": "morphosyntax",
                "subcategory": "tense_aspect",
                "severity": 2,
                "confidence": 0.95,
                "feature_key": "morphosyntax:tense_aspect:preterite_vs_present",
                "correction": "Yo fui a la playa ayer",
                "explanation": "Past event needs preterite, not present.",
                "interference_source": None,
                "handled": "corrected_recast",
            }
        ],
    )
    list(runner.turn("Yo voy a la playa ayer"))

    patterns = repo.list_patterns(runner.context.profile.id)
    assert [p.feature_key for p in patterns] == [
        "morphosyntax:tense_aspect:preterite_vs_present"
    ]
    assert patterns[0].occurrence_count == 1
