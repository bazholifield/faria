"""Property tests for the level estimator — these encode the behaviors the
docstring promises, not exact numbers, so constants stay tunable."""

import pytest

from faria.domain.levels import (
    CONFIDENCE_HALF_LIFE_DAYS,
    aggregate_overall,
    band,
    decay_confidence,
    update_dimension,
)


def test_band_labels():
    assert band(1.0) == "A1"
    assert band(2.4) == "A2"
    assert band(2.8) == "A2+"
    assert band(6.0) == "C2"
    assert band(9.0) == "C2"  # clamped


def test_first_observation_is_adopted_with_modest_confidence():
    score, conf = update_dimension(None, 0.0, 3.2, obs_weight=1.0)
    assert score == 3.2
    assert 0.0 < conf < 0.3


def test_low_confidence_estimates_move_fast():
    score, _ = update_dimension(2.0, 0.05, 4.0, obs_weight=1.0)
    assert score > 3.4  # most of the gap closed in one step


def test_high_confidence_estimates_resist_one_anomalous_session():
    score, _ = update_dimension(3.0, 0.9, 5.0, obs_weight=1.0)
    assert score < 3.5  # moved less than a quarter of the gap


def test_confidence_rises_monotonically_toward_one():
    score, conf = None, 0.0
    prev = conf
    for _ in range(30):
        score, conf = update_dimension(score, conf, 3.0, obs_weight=1.0)
        assert conf > prev
        prev = conf
    assert 0.5 < conf <= 1.0


def test_heavier_observations_move_the_estimate_more():
    light, _ = update_dimension(3.0, 0.5, 4.0, obs_weight=0.5)
    heavy, _ = update_dimension(3.0, 0.5, 4.0, obs_weight=3.0)
    assert heavy > light


def test_decay_halves_confidence_at_half_life():
    assert decay_confidence(0.8, CONFIDENCE_HALF_LIFE_DAYS) == pytest.approx(0.4)
    assert decay_confidence(0.8, 0) == 0.8


def test_gap_then_reanchor():
    """A long break lowers confidence, so the next observation moves the score again."""
    _, conf = update_dimension(3.0, 0.0, 3.0, 5.0)
    for _ in range(10):
        _, conf = update_dimension(3.0, conf, 3.0, 5.0)
    stale_conf = decay_confidence(conf, days_idle=120)
    moved_fresh, _ = update_dimension(3.0, conf, 4.0, 1.0)
    moved_stale, _ = update_dimension(3.0, stale_conf, 4.0, 1.0)
    assert moved_stale > moved_fresh


def test_aggregate_full_coverage():
    dims = {
        "grammatical_accuracy": (2.0, 1.0),
        "lexical_range": (3.0, 1.0),
        "syntactic_complexity": (3.0, 1.0),
        "comprehension": (4.0, 1.0),
    }
    score, conf = aggregate_overall(dims)
    assert 2.0 < score < 4.0
    assert conf == pytest.approx(1.0)


def test_aggregate_penalizes_missing_dimensions():
    full = aggregate_overall({
        "grammatical_accuracy": (3.0, 0.8),
        "lexical_range": (3.0, 0.8),
        "syntactic_complexity": (3.0, 0.8),
        "comprehension": (3.0, 0.8),
    })
    partial = aggregate_overall({"grammatical_accuracy": (3.0, 0.8)})
    assert partial[0] == pytest.approx(3.0)   # score honest to what we know
    assert partial[1] < full[1]               # but confidence marked down


def test_aggregate_rejects_empty_and_ignores_unknown():
    with pytest.raises(ValueError):
        aggregate_overall({})
    with pytest.raises(ValueError):
        aggregate_overall({"vibes": (3.0, 1.0)})
