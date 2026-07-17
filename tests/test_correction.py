"""Behavioral tests for the correction policy. Each test name states a rule
from the spec in domain/correction.py — if you change a rule, change its test."""

import pytest

from faria.config import CorrectionConfig
from faria.domain.correction import (
    MAX_RECAP_QUEUE,
    Aggressiveness,
    CorrectionAction,
    CorrectionPolicy,
    CorrectionStyle,
    TurnState,
)
from faria.domain.models import ErrorEvent, ErrorPattern
from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity


def event(
    category=ErrorCategory.MORPHOSYNTAX,
    subcategory=ErrorSubcategory.AGREEMENT,
    severity=Severity.DISRUPTIVE,
    confidence=0.9,
    key="morphosyntax:agreement:gender_noun_adj",
) -> ErrorEvent:
    return ErrorEvent(
        utterance_id=1,
        category=category,
        subcategory=subcategory,
        severity=severity,
        confidence=confidence,
        feature_key=key,
        detector="test",
    )


def pattern(count: int, key="morphosyntax:agreement:gender_noun_adj") -> ErrorPattern:
    return ErrorPattern(
        id=1, profile_id=1, category=ErrorCategory.MORPHOSYNTAX,
        subcategory=ErrorSubcategory.AGREEMENT, feature_key=key,
        description=None, status="active", occurrence_count=count,
    )


def policy(aggr=Aggressiveness.BALANCED, style=CorrectionStyle.RECAST) -> CorrectionPolicy:
    return CorrectionPolicy(CorrectionConfig(), aggr, style)


def fresh_turn(p: CorrectionPolicy) -> TurnState:
    state = TurnState()
    p.start_turn(state)
    return state


# -- R0 / R1 / off -----------------------------------------------------------

def test_orthography_is_silent_at_every_setting():
    for aggr in Aggressiveness:
        for style in CorrectionStyle:
            p = policy(aggr, style)
            err = event(ErrorCategory.ORTHOGRAPHY, ErrorSubcategory.SPELLING,
                        Severity.DISRUPTIVE)
            assert p.decide(err, None, fresh_turn(p), None) == CorrectionAction.LOGGED_SILENT


def test_comprehension_repair_survives_even_off():
    p = policy(Aggressiveness.OFF)
    err = event(ErrorCategory.COMPREHENSION, ErrorSubcategory.MISUNDERSTANDING,
                Severity.DISRUPTIVE)
    assert p.decide(err, None, fresh_turn(p), None) == CorrectionAction.CLARIFIED


def test_off_silences_everything_else():
    p = policy(Aggressiveness.OFF)
    state = fresh_turn(p)
    for sev in (Severity.MINOR, Severity.DISRUPTIVE, Severity.BREAKDOWN):
        assert p.decide(event(severity=sev), None, state, None) == CorrectionAction.LOGGED_SILENT
    assert state.recap_queue == []  # off means no recap either


# -- style mapping (R2) -------------------------------------------------------

def test_disruptive_gets_recast_by_default():
    p = policy()
    assert p.decide(event(), None, fresh_turn(p), None) == CorrectionAction.CORRECTED_RECAST


def test_breakdown_is_explicit_even_in_recast_style():
    p = policy()
    err = event(severity=Severity.BREAKDOWN)
    assert p.decide(err, None, fresh_turn(p), None) == CorrectionAction.CORRECTED_EXPLICIT


def test_explicit_style_names_the_error():
    p = policy(style=CorrectionStyle.EXPLICIT)
    assert p.decide(event(), None, fresh_turn(p), None) == CorrectionAction.CORRECTED_EXPLICIT


# -- budget -------------------------------------------------------------------

def test_second_inline_candidate_in_turn_is_deferred():
    p = policy()
    state = fresh_turn(p)
    assert p.decide(event(key="k1"), None, state, None) == CorrectionAction.CORRECTED_RECAST
    assert p.decide(event(key="k2"), None, state, None) == CorrectionAction.DEFERRED_RECAP


def test_window_budget_caps_across_turns():
    p = policy()  # defaults: 1/turn, 2 per 5 turns
    state = TurnState()
    actions = []
    for turn in range(3):
        p.start_turn(state)
        actions.append(p.decide(event(key=f"k{turn}"), None, state, None))
    assert actions == [
        CorrectionAction.CORRECTED_RECAST,
        CorrectionAction.CORRECTED_RECAST,
        CorrectionAction.DEFERRED_RECAP,  # window exhausted
    ]


def test_window_slides_open_again():
    p = policy()
    state = TurnState()
    p.start_turn(state)
    p.decide(event(key="k1"), None, state, None)
    p.start_turn(state)
    p.decide(event(key="k2"), None, state, None)
    for _ in range(5):  # idle turns push the corrections out of the window
        p.start_turn(state)
    assert p.decide(event(key="k3"), None, state, None) == CorrectionAction.CORRECTED_RECAST


# -- aggressiveness -----------------------------------------------------------

def test_minimal_lets_disruptive_pass_but_not_breakdown():
    p = policy(Aggressiveness.MINIMAL)
    state = fresh_turn(p)
    assert p.decide(event(severity=Severity.DISRUPTIVE), None, state, None) == \
        CorrectionAction.DEFERRED_RECAP
    assert p.decide(event(severity=Severity.BREAKDOWN, key="k2"), None, state, None) == \
        CorrectionAction.CORRECTED_EXPLICIT


def test_aggressive_corrects_minor_and_doubles_caps():
    p = policy(Aggressiveness.AGGRESSIVE)
    state = fresh_turn(p)
    a1 = p.decide(event(severity=Severity.MINOR, key="k1"), None, state, None)
    a2 = p.decide(event(severity=Severity.MINOR, key="k2"), None, state, None)
    a3 = p.decide(event(severity=Severity.MINOR, key="k3"), None, state, None)
    assert (a1, a2) == (CorrectionAction.CORRECTED_RECAST, CorrectionAction.CORRECTED_RECAST)
    assert a3 == CorrectionAction.DEFERRED_RECAP  # doubled per-turn cap is 2


# -- recurrence, slips, recap, guards ----------------------------------------

def test_recurring_pattern_escalates_a_minor_error():
    p = policy()
    err = event(severity=Severity.MINOR)
    assert p.decide(err, pattern(3), fresh_turn(p), None) == \
        CorrectionAction.CORRECTED_RECAST      # at/above threshold -> inline
    assert p.decide(err, pattern(2), fresh_turn(p), None) == \
        CorrectionAction.DEFERRED_RECAP        # below threshold -> normal R4


def test_slips_are_always_silent():
    p = policy(Aggressiveness.AGGRESSIVE)
    err = event(severity=Severity.SLIP)
    assert p.decide(err, None, fresh_turn(p), None) == CorrectionAction.LOGGED_SILENT


def test_low_confidence_never_goes_inline():
    p = policy()
    err = event(confidence=0.4)
    assert p.decide(err, None, fresh_turn(p), None) == CorrectionAction.DEFERRED_RECAP


def test_harping_guard_silences_a_corrected_key():
    p = policy()
    state = TurnState()
    p.start_turn(state)
    assert p.decide(event(), None, state, None) == CorrectionAction.CORRECTED_RECAST
    p.start_turn(state)
    assert p.decide(event(), None, state, None) == CorrectionAction.LOGGED_SILENT


def test_recap_queue_overflow_goes_silent():
    p = policy()
    state = fresh_turn(p)
    state.inline_used_this_turn = 99  # exhaust budget so everything defers
    for i in range(MAX_RECAP_QUEUE):
        assert p.decide(event(severity=Severity.MINOR, key=f"k{i}"), None, state, None) == \
            CorrectionAction.DEFERRED_RECAP
    assert p.decide(event(severity=Severity.MINOR, key="overflow"), None, state, None) == \
        CorrectionAction.LOGGED_SILENT


# -- prompt directives --------------------------------------------------------

def test_directives_off_forbids_correction():
    text = policy(Aggressiveness.OFF).prompt_directives([], None)
    assert "Do not correct" in text


def test_directives_carry_budget_style_and_orthography_ban():
    text = policy().prompt_directives([], None)
    assert "at most 1 correction(s) per learner turn" in text
    assert "RECAST" in text and "fuiste a la playa" in text
    assert "Never correct spelling" in text


def test_directives_list_focus_patterns_and_level_tweak():
    text = policy().prompt_directives([pattern(4)], learner_level=2.2)
    assert "seen 4x" in text
    assert "below B1" in text


def test_directives_double_budget_when_aggressive():
    text = policy(Aggressiveness.AGGRESSIVE).prompt_directives([], None)
    assert "at most 2 correction(s) per learner turn" in text
