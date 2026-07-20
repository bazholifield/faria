from faria.domain.models import LanguageProfile, Learner, SessionContext
from faria.tutor.prompts import assemble_system_prompt


def _ctx(**overrides) -> SessionContext:
    learner = Learner(id=1, name="test", native_language="en", other_languages=[])
    profile = LanguageProfile(
        id=1, learner_id=1, language="es", level_overall=None, level_confidence=0.0,
        correction_aggressiveness="balanced", correction_style="recast",
        explanation_language="adaptive", preferences={},
    )
    defaults = dict(learner=learner, profile=profile)
    defaults.update(overrides)
    return SessionContext(**defaults)


def test_known_feature_keys_surfaced_when_present():
    ctx = _ctx(known_feature_keys=["morphosyntax:agreement:yo_tener"])
    prompt = assemble_system_prompt(ctx, policy_directives="Correct judiciously.")
    assert "morphosyntax:agreement:yo_tener" in prompt
    assert "reuse" in prompt.lower()


def test_known_feature_keys_section_omitted_when_empty():
    ctx = _ctx(known_feature_keys=[])
    prompt = assemble_system_prompt(ctx, policy_directives="Correct judiciously.")
    assert "already-logged error fingerprints" not in prompt


def test_home_base_omits_correction_and_known_keys_sections():
    home_profile = LanguageProfile(
        id=2, learner_id=1, language="en", level_overall=None, level_confidence=0.0,
        correction_aggressiveness="balanced", correction_style="recast",
        explanation_language="adaptive", preferences={},
    )
    ctx = _ctx(profile=home_profile, known_feature_keys=["some:key:here"])
    prompt = assemble_system_prompt(ctx, policy_directives="Correct judiciously.")
    assert "Correct judiciously." not in prompt
    assert "some:key:here" not in prompt
    assert "home base" in prompt.lower()


def test_pivot_section_only_when_frequency_on_and_languages_present():
    ctx = _ctx()
    without = assemble_system_prompt(ctx, pivot_frequency="off", other_known_languages=["fr"])
    assert "comprehension check" not in without  # pivot-section-only phrasing
    with_pivot = assemble_system_prompt(ctx, pivot_frequency="occasional", other_known_languages=["fr"])
    assert "French" in with_pivot
    assert "comprehension check" in with_pivot
