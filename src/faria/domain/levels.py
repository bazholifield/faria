"""CEFR level estimation (DECISIONS.md ADR-004).

First-pass implementation — the update rule is deliberately simple (a
confidence-weighted EWMA, "Kalman filter lite") and every constant below is a
tuning knob. Because raw evidence is kept in level_observations, you can
change this rule and recompute all estimates from scratch at any time.

Representation
--------------
Continuous scale: 1.0=A1, 2.0=A2, 3.0=B1, 4.0=B2, 5.0=C1, 6.0=C2. A score of
2.4 reads as "solid A2, approaching B1". Per-dimension scores + confidences
live in level_dimensions; the overall (level_overall on the profile) is a
weighted aggregate.

Dimensions and the signals that feed them
-----------------------------------------
- lexical_range: distinct lemmas per session, frequency-band distribution
  (Zipf via `wordfreq` is a cheap non-LLM signal), LLM judgment of vocabulary
  sophistication from the session-end sweep.
- grammatical_accuracy: error events per 100 learner words, weighted by
  severity, broken down by category (morphosyntax errors count more here than
  orthography). NOTE the coupling: this dimension consumes the error log, so
  detector recall changes (Phase 1 sweep) will shift it — keep `source` on
  observations so eras are comparable.
- syntactic_complexity: subordination rate, mean clause depth (spaCy parse is
  a Phase 1+ computed signal for es/fr/de; Basque/Arabic likely LLM-judged),
  variety of constructions attempted. Reward *attempts* at complex structures
  even when they fail — complexity and accuracy are separate axes on purpose.
- comprehension: outcomes of natural comprehension probes (the tutor's
  follow-up questions), misunderstanding events (taxonomy: comprehension),
  reaction to surprise language pivots (Phase 1.5 — the strongest signal).
- fluency (audio phases only): response latency, filled-pause rate,
  self-repair rate, speech rate. No text-phase signal — leave unpopulated.

Update rule
-----------
Per observation (value, weight), where weight encodes evidence strength
(words produced, source reliability):

    gain        = w / (w + GAIN_INERTIA * prior_confidence)
    score'      = score + gain * (value - score)
    confidence' = confidence + (1 - confidence) * w / (w + CONFIDENCE_RATE)

plus time decay applied at session start (``decay_confidence``). Properties
(asserted in tests/test_levels.py):

  - Early sessions move the estimate fast (low prior confidence -> gain ~1).
  - A single anomalous session barely moves a high-confidence estimate.
  - Confidence rises monotonically with evidence, asymptotically to 1.
  - A long gap lowers confidence, so the next session re-anchors quickly.
"""

from __future__ import annotations

#: Higher = high-confidence estimates resist single observations more.
GAIN_INERTIA = 6.0

#: Units of observation weight to reach ~0.5 confidence from zero.
CONFIDENCE_RATE = 8.0

#: Days of inactivity for confidence to halve.
CONFIDENCE_HALF_LIFE_DAYS = 45.0

CEFR_BANDS: list[tuple[float, str]] = [
    (1.0, "A1"),
    (2.0, "A2"),
    (3.0, "B1"),
    (4.0, "B2"),
    (5.0, "C1"),
    (6.0, "C2"),
]

#: Weights for aggregating dimensions into level_overall. Production
#: dimensions outweigh comprehension for the headline number: the tutor
#: speaks *to* comprehension but corrects production.
DIMENSION_WEIGHTS: dict[str, float] = {
    "grammatical_accuracy": 0.30,
    "lexical_range": 0.25,
    "syntactic_complexity": 0.25,
    "comprehension": 0.20,
    # "fluency" joins in Phase 2; re-normalize then.
}


def band(score: float) -> str:
    """Human-readable CEFR band for a continuous score, e.g. 2.4 -> 'A2'."""
    score = max(1.0, min(score, 6.0))
    base = min(int(score), 6)
    label = CEFR_BANDS[base - 1][1]
    fraction = score - base
    if fraction >= 0.66:
        return f"{label}+"
    return label


def update_dimension(
    prior_score: float | None,
    prior_confidence: float,
    obs_value: float,
    obs_weight: float,
) -> tuple[float, float]:
    """One EWMA step; returns (new_score, new_confidence).

    A None prior means "first observation": adopt obs_value outright; the
    confidence rule below then yields a modest starting confidence
    proportional to the observation's weight.
    """
    if obs_weight <= 0:
        raise ValueError("obs_weight must be positive")

    if prior_score is None:
        new_score = obs_value
    else:
        gain = obs_weight / (obs_weight + GAIN_INERTIA * prior_confidence)
        new_score = prior_score + gain * (obs_value - prior_score)

    new_confidence = prior_confidence + (1.0 - prior_confidence) * (
        obs_weight / (obs_weight + CONFIDENCE_RATE)
    )
    return max(1.0, min(new_score, 6.0)), min(new_confidence, 1.0)


def decay_confidence(confidence: float, days_idle: float) -> float:
    """Time decay applied when a session starts after a gap.

    Score is left alone — ability doesn't vanish, but our certainty about it
    fades, so the next sessions re-anchor the estimate quickly.
    """
    if days_idle <= 0:
        return confidence
    return confidence * 0.5 ** (days_idle / CONFIDENCE_HALF_LIFE_DAYS)


def aggregate_overall(dimensions: dict[str, tuple[float, float]]) -> tuple[float, float]:
    """Weighted aggregate of {dimension: (score, confidence)} -> (overall, confidence).

    Score uses DIMENSION_WEIGHTS restricted to the dimensions present,
    re-normalized. Overall confidence = (weighted mean of per-dimension
    confidences) x (coverage), so an estimate missing whole dimensions is
    marked less certain even if the dimensions we do have are solid.
    """
    present = {d: sc for d, sc in dimensions.items() if d in DIMENSION_WEIGHTS}
    if not present:
        raise ValueError("no known dimensions to aggregate")

    present_weight = sum(DIMENSION_WEIGHTS[d] for d in present)
    score = sum(DIMENSION_WEIGHTS[d] * s for d, (s, _) in present.items()) / present_weight
    mean_conf = sum(DIMENSION_WEIGHTS[d] * c for d, (_, c) in present.items()) / present_weight
    coverage = present_weight / sum(DIMENSION_WEIGHTS.values())
    return score, mean_conf * coverage
