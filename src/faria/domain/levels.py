"""CEFR level estimation (DECISIONS.md ADR-004). [STUB — yours to implement]

Representation
--------------
Continuous scale: 1.0=A1, 2.0=A2, 3.0=B1, 4.0=B2, 5.0=C1, 6.0=C2. A score of
2.4 reads as "solid A2, approaching B1". Per-dimension scores + confidences
live in level_dimensions; the overall (level_overall on the profile) is a
weighted aggregate. Raw evidence is appended to level_observations so the
estimate can be recomputed from scratch when the update rule changes.

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
  whether the learner tracks when the tutor speaks at/above level.
- fluency (audio phases only): response latency, filled-pause rate,
  self-repair rate, speech rate. No text-phase signal — leave unpopulated.

Update rule (proposed starting point)
-------------------------------------
Confidence-weighted exponential moving average per dimension — a Kalman filter
lite. Each observation carries (value, weight); weight encodes evidence
strength (e.g. words produced in the session, source reliability):

    gain  = obs_weight / (obs_weight + k * prior_confidence)   # k tunes inertia
    score' = score + gain * (obs_value - score)
    confidence' = decayed toward 0 with time since last session, pushed toward
                  1 with accumulated evidence; e.g.
                  conf' = conf + gain * (1 - conf), with a per-week decay.

Properties this should have (test for them):
  - Early sessions move the estimate fast (low prior confidence).
  - A single anomalous session barely moves a high-confidence estimate.
  - A month's gap lowers confidence, so the next session re-anchors quickly.
  - Overall = weighted mean of dimensions; production dimensions (accuracy,
    complexity, lexical) weigh more than comprehension for the headline
    number, since the tutor speaks *to* comprehension but corrects production.
"""

from __future__ import annotations

CEFR_BANDS: list[tuple[float, str]] = [
    (1.0, "A1"),
    (2.0, "A2"),
    (3.0, "B1"),
    (4.0, "B2"),
    (5.0, "C1"),
    (6.0, "C2"),
]

#: Weights for aggregating dimensions into level_overall. Tune freely; they
#: must sum to 1.0 across the dimensions present.
DIMENSION_WEIGHTS: dict[str, float] = {
    "grammatical_accuracy": 0.30,
    "lexical_range": 0.25,
    "syntactic_complexity": 0.25,
    "comprehension": 0.20,
    # "fluency" joins in Phase 2; re-normalize then.
}


def band(score: float) -> str:
    """Human-readable CEFR band for a continuous score, e.g. 2.4 -> 'A2+'."""
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
    """One EWMA/Kalman-lite step; returns (new_score, new_confidence).

    See module docstring for the proposed rule and the properties to test.
    A None prior means "first observation": adopt obs_value with modest
    confidence proportional to obs_weight.
    """
    raise NotImplementedError  # Phase 1: yours


def aggregate_overall(dimensions: dict[str, tuple[float, float]]) -> tuple[float, float]:
    """Weighted aggregate of {dimension: (score, confidence)} -> (overall, confidence).

    Use DIMENSION_WEIGHTS restricted to the dimensions present, re-normalized.
    Overall confidence should reflect both per-dimension confidences and
    coverage (an estimate missing comprehension evidence is less certain).
    """
    raise NotImplementedError  # Phase 1: yours
