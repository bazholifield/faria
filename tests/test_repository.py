from faria.domain.models import ErrorEvent, SummaryRow
from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity


def _bootstrap(repo):
    learner = repo.get_or_create_learner("test", "en")
    profile = repo.get_or_create_profile(learner.id, "es", "balanced")
    session = repo.create_session(learner.id, goal="talk about food")
    return learner, profile, session


def test_learner_and_profile_are_idempotent(repo):
    l1 = repo.get_or_create_learner("test", "en")
    l2 = repo.get_or_create_learner("test", "en")
    assert l1.id == l2.id

    p1 = repo.get_or_create_profile(l1.id, "es", "balanced")
    p2 = repo.get_or_create_profile(l1.id, "es", "aggressive")
    assert p1.id == p2.id
    assert p2.correction_aggressiveness == "balanced"  # existing row wins

    p_fr = repo.get_or_create_profile(l1.id, "fr", "balanced")
    assert p_fr.id != p1.id
    assert p_fr.correction_style == "recast"  # style default flows through


def test_utterance_roundtrip(repo):
    _, _, session = _bootstrap(repo)
    repo.add_utterance(session.id, 0, "learner", "Hola, ¿cómo estás?", language="es")
    repo.add_utterance(session.id, 1, "tutor", "¡Muy bien! ¿Y tú?", language="es")
    utts = repo.session_utterances(session.id)
    assert [u.speaker for u in utts] == ["learner", "tutor"]
    assert utts[0].text.startswith("Hola")
    assert utts[0].language == "es"


def test_pattern_upsert_counts_recurrence(repo):
    _, profile, _ = _bootstrap(repo)
    key = "morphosyntax:agreement:gender_noun_adj"
    p1 = repo.upsert_pattern(
        profile.id, ErrorCategory.MORPHOSYNTAX, ErrorSubcategory.AGREEMENT, key, "gender concord"
    )
    p2 = repo.upsert_pattern(
        profile.id, ErrorCategory.MORPHOSYNTAX, ErrorSubcategory.AGREEMENT, key, "gender concord"
    )
    assert p1.id == p2.id
    assert p1.occurrence_count == 1
    assert p2.occurrence_count == 2

    # below threshold -> not a focus pattern yet
    assert repo.focus_patterns(profile.id, min_occurrences=3) == []
    repo.upsert_pattern(
        profile.id, ErrorCategory.MORPHOSYNTAX, ErrorSubcategory.AGREEMENT, key, None
    )
    focus = repo.focus_patterns(profile.id, min_occurrences=3)
    assert [f.feature_key for f in focus] == [key]


def test_error_event_roundtrip(repo, conn):
    _, profile, session = _bootstrap(repo)
    utt = repo.add_utterance(session.id, 0, "learner", "la problema es grande", language="es")
    pattern = repo.upsert_pattern(
        profile.id, ErrorCategory.MORPHOSYNTAX, ErrorSubcategory.DETERMINER,
        "morphosyntax:determiner:el_problema", "masculine -a noun",
    )
    event = ErrorEvent(
        utterance_id=utt.id,
        category=ErrorCategory.MORPHOSYNTAX,
        subcategory=ErrorSubcategory.DETERMINER,
        severity=Severity.MINOR,
        confidence=0.95,
        feature_key="morphosyntax:determiner:el_problema",
        span_text="la problema",
        span_start=0,
        span_end=11,
        correction="el problema",
        detector="sidecar:test",
        model_action="corrected_recast",
    )
    repo.record_error_event(session.id, event, pattern.id)
    row = conn.execute("SELECT * FROM error_events").fetchone()
    assert row["severity"] == 2
    assert row["pattern_id"] == pattern.id
    assert row["detector"] == "sidecar:test"
    assert row["model_action"] == "corrected_recast"


def test_summary_storage_and_recall_order(repo):
    learner, _, _ = _bootstrap(repo)
    for i in range(4):
        s = repo.create_session(learner.id)
        repo.add_utterance(s.id, 0, "learner", f"turn {i}", language="es")
        repo.store_summary(SummaryRow(
            session_id=s.id,
            summary_text=f"summary {i}",
            topics=[f"topic{i}"],
            vocab_exposed=[],
            error_digest=[],
            level_evidence=[],
            next_focus=[f"focus{i}"],
            model="test",
        ))
    recent = repo.recent_summaries("es", limit=3)
    # last 3, oldest-first for prompt injection
    assert [s.summary_text for s in recent] == ["summary 1", "summary 2", "summary 3"]


def test_recent_summaries_scoped_by_utterance_language_not_session(repo):
    """A session touching two languages (Phase 1.5) surfaces its summary for
    both languages' next_summaries, never for a language it never used."""
    learner, _, _ = _bootstrap(repo)
    session = repo.create_session(learner.id)
    repo.add_utterance(session.id, 0, "learner", "hello", language="en")
    repo.add_utterance(session.id, 1, "learner", "guten tag", language="de")
    repo.store_summary(SummaryRow(
        session_id=session.id, summary_text="mixed session", topics=[], vocab_exposed=[],
        error_digest=[], level_evidence=[], next_focus=[], model="test",
    ))
    assert [s.summary_text for s in repo.recent_summaries("en", 5)] == ["mixed session"]
    assert [s.summary_text for s in repo.recent_summaries("de", 5)] == ["mixed session"]
    assert repo.recent_summaries("fr", 5) == []


def test_level_dimensions_upsert(repo):
    _, profile, _ = _bootstrap(repo)
    repo.upsert_dimension(profile.id, "lexical_range", 2.4, 0.3)
    repo.upsert_dimension(profile.id, "lexical_range", 2.6, 0.4)
    dims = repo.get_dimensions(profile.id)
    assert len(dims) == 1
    assert dims[0].score == 2.6
    assert dims[0].updated_at  # timestamp populated
    repo.record_observation(profile.id, None, "lexical_range", 2.6, 1.0, "test", {"n": 40})


# -- pattern lifecycle (ADR-018) ----------------------------------------------

def test_upsert_pattern_crosses_fossilization_threshold(repo):
    _, profile, _ = _bootstrap(repo)
    key = "morphosyntax:case:dative"
    for _ in range(3):
        p = repo.upsert_pattern(
            profile.id, ErrorCategory.MORPHOSYNTAX, ErrorSubcategory.CASE, key, None,
            fossilization_threshold=3,
        )
    assert p.occurrence_count == 3
    assert p.status == "fossilization_risk"


def test_upsert_pattern_recurrence_resets_status_to_active(repo, conn):
    _, profile, _ = _bootstrap(repo)
    key = "lexis:false_friend:actualmente"
    p = repo.upsert_pattern(profile.id, ErrorCategory.LEXIS, ErrorSubcategory.FALSE_FRIEND, key, None)
    conn.execute("UPDATE error_patterns SET status = 'improving' WHERE id = ?", (p.id,))
    conn.commit()
    p2 = repo.upsert_pattern(profile.id, ErrorCategory.LEXIS, ErrorSubcategory.FALSE_FRIEND, key, None)
    assert p2.status == "active"


def test_decay_pattern_statuses(repo, conn):
    _, profile, _ = _bootstrap(repo)
    key = "morphosyntax:mood:subjunctive"
    p = repo.upsert_pattern(profile.id, ErrorCategory.MORPHOSYNTAX, ErrorSubcategory.MOOD, key, None)
    conn.execute(
        "UPDATE error_patterns SET last_seen_at = datetime('now', '-20 days') WHERE id = ?", (p.id,)
    )
    conn.commit()
    repo.decay_pattern_statuses(profile.id, improving_after_days=14, resolved_after_days=45)
    assert conn.execute("SELECT status FROM error_patterns WHERE id = ?", (p.id,)).fetchone()[0] == "improving"

    conn.execute(
        "UPDATE error_patterns SET last_seen_at = datetime('now', '-50 days') WHERE id = ?", (p.id,)
    )
    conn.commit()
    repo.decay_pattern_statuses(profile.id, improving_after_days=14, resolved_after_days=45)
    assert conn.execute("SELECT status FROM error_patterns WHERE id = ?", (p.id,)).fetchone()[0] == "resolved"


def test_list_patterns_and_examples(repo):
    _, profile, session = _bootstrap(repo)
    key = "morphosyntax:agreement:gender_noun_adj"
    pattern = repo.upsert_pattern(
        profile.id, ErrorCategory.MORPHOSYNTAX, ErrorSubcategory.AGREEMENT, key, None
    )
    utt = repo.add_utterance(session.id, 0, "learner", "la problema", language="es")
    event = ErrorEvent(
        utterance_id=utt.id, category=ErrorCategory.MORPHOSYNTAX, subcategory=ErrorSubcategory.AGREEMENT,
        severity=Severity.MINOR, confidence=0.9, feature_key=key, span_text="la problema",
        correction="el problema", detector="sidecar:test",
    )
    repo.record_error_event(session.id, event, pattern.id)

    listed = repo.list_patterns(profile.id, limit=5)
    assert [p.feature_key for p in listed] == [key]
    examples = repo.pattern_examples(pattern.id)
    assert examples == [("la problema", "el problema")]
    assert repo.pattern_feature_keys_for_utterance(utt.id) == {key}
    assert repo.pattern_feature_keys_for_utterance(999) == set()


# -- companion mode (ADR-014/019) --------------------------------------------

def test_known_target_languages_excludes_native(repo):
    learner = repo.get_or_create_learner("test", "en")
    repo.get_or_create_profile(learner.id, "es", "balanced")
    repo.get_or_create_profile(learner.id, "de", "balanced")
    repo.get_or_create_profile(learner.id, "en", "balanced")  # the home profile itself
    known = repo.known_target_languages(learner.id, exclude_language="en")
    assert set(known) == {"es", "de"}


def test_update_correction_settings_partial(repo):
    _, profile, _ = _bootstrap(repo)
    repo.update_correction_settings(profile.id, aggressiveness="aggressive", style=None)
    refreshed = repo.get_or_create_profile(profile.learner_id, profile.language, "balanced")
    assert refreshed.correction_aggressiveness == "aggressive"
    assert refreshed.correction_style == "recast"  # untouched

    repo.update_correction_settings(profile.id, aggressiveness=None, style="explicit")
    refreshed = repo.get_or_create_profile(profile.learner_id, profile.language, "balanced")
    assert refreshed.correction_style == "explicit"


def test_update_preferences_roundtrip(repo):
    _, profile, _ = _bootstrap(repo)
    repo.update_preferences(profile.id, {"pivot_frequency": "occasional"})
    refreshed = repo.get_or_create_profile(profile.learner_id, profile.language, "balanced")
    assert refreshed.preferences == {"pivot_frequency": "occasional"}
