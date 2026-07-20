-- 0003: Preserve the model's own "handled" self-report on each error event,
-- distinct from `action` (CorrectionPolicy.decide()'s final ruling). The two
-- can disagree -- e.g. the model corrects something the turn budget didn't
-- allow -- and that disagreement is data, not something to hide (ADR-005).

ALTER TABLE error_events ADD COLUMN model_action TEXT
    CHECK (model_action IN ('corrected_explicit', 'corrected_recast', 'clarified', 'not_addressed'));
