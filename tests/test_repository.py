from faria.domain.models import ErrorEvent, SummaryRow
from faria.domain.taxonomy import ErrorCategory, ErrorSubcategory, Severity


def _bootstrap(repo):
    learner = repo.get_or_create_learner("test", "en")
    profile = repo.get_or_create_profile(learner.id, "es", "balanced")
    session = repo.create_session(profile.id, goal="talk about food")
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


def test_utterance_roundtrip(repo):
    _, _, session = _bootstrap(repo)
    repo.add_utterance(session.id, 0, "learner", "Hola, ¿cómo estás?")
    repo.add_utterance(session.id, 1, "tutor", "¡Muy bien! ¿Y tú?")
    utts = repo.session_utterances(session.id)
    assert [u.speaker for u in utts] == ["learner", "tutor"]
    assert utts[0].text.startswith("Hola")


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
    utt = repo.add_utterance(session.id, 0, "learner", "la problema es grande")
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
    )
    repo.record_error_event(session.id, event, pattern.id)
    row = conn.execute("SELECT * FROM error_events").fetchone()
    assert row["severity"] == 2
    assert row["pattern_id"] == pattern.id
    assert row["detector"] == "sidecar:test"


def test_summary_storage_and_recall_order(repo):
    _, profile, session = _bootstrap(repo)
    for i in range(4):
        s = repo.create_session(profile.id)
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
    recent = repo.recent_summaries(profile.id, limit=3)
    # last 3, oldest-first for prompt injection
    assert [s.summary_text for s in recent] == ["summary 1", "summary 2", "summary 3"]


def test_level_dimensions_upsert(repo):
    _, profile, _ = _bootstrap(repo)
    repo.upsert_dimension(profile.id, "lexical_range", 2.4, 0.3)
    repo.upsert_dimension(profile.id, "lexical_range", 2.6, 0.4)
    dims = repo.get_dimensions(profile.id)
    assert len(dims) == 1
    assert dims[0].score == 2.6
    repo.record_observation(profile.id, None, "lexical_range", 2.6, 1.0, "test", {"n": 40})
