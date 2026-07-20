from faria.db.connection import connect
from faria.db.migrate import apply_migrations

EXPECTED_TABLES = {
    "learners",
    "language_profiles",
    "level_dimensions",
    "level_observations",
    "sessions",
    "utterances",
    "error_patterns",
    "error_events",
    "session_summaries",
    "schema_migrations",
}


def test_migrations_apply_and_create_all_tables(tmp_path):
    conn = connect(tmp_path / "m.db")
    applied = apply_migrations(conn)
    assert applied, "expected at least one migration to apply"

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert EXPECTED_TABLES <= tables


def test_migrations_are_idempotent(tmp_path):
    conn = connect(tmp_path / "m.db")
    apply_migrations(conn)
    assert apply_migrations(conn) == []


def test_0002_upgrade_preserves_existing_profiles(tmp_path):
    """The path a real DB takes: 0001 applied with data, then later migrations land.

    Only 0001 is pre-registered, so this also exercises 0002 and 0003 landing
    together in one apply_migrations() call — a real upgrade scenario, not an
    artifact of the test setup.
    """
    from importlib.resources import files

    conn = connect(tmp_path / "m.db")
    sql_0001 = (files("faria.db") / "migrations" / "0001_initial.sql").read_text()
    conn.executescript(sql_0001)
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO schema_migrations (version) VALUES ('0001_initial.sql')")
    conn.execute("INSERT INTO learners (name) VALUES ('bazh')")
    conn.execute(
        "INSERT INTO language_profiles (learner_id, language, correction_aggressiveness) "
        "VALUES (1, 'es', 'balanced')"
    )
    conn.execute("INSERT INTO sessions (profile_id) VALUES (1)")
    conn.commit()

    applied = apply_migrations(conn)
    assert applied == ["0002_correction_style.sql", "0003_model_action.sql"]

    row = conn.execute("SELECT * FROM language_profiles WHERE language = 'es'").fetchone()
    assert row["correction_aggressiveness"] == "balanced"  # data survived the rebuild
    assert row["correction_style"] == "recast"             # new column got its default
    # child FK still resolves and enforcement is back on
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_0003_upgrade_preserves_existing_error_events(tmp_path):
    """The path a real DB takes: 0001+0002 applied with data, then 0003 lands."""
    from importlib.resources import files

    conn = connect(tmp_path / "m.db")
    for name in ("0001_initial.sql", "0002_correction_style.sql"):
        sql = (files("faria.db") / "migrations" / name).read_text()
        conn.executescript(sql)
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.executemany(
        "INSERT INTO schema_migrations (version) VALUES (?)",
        [("0001_initial.sql",), ("0002_correction_style.sql",)],
    )
    conn.execute("INSERT INTO learners (name) VALUES ('bazh')")
    conn.execute("INSERT INTO language_profiles (learner_id, language) VALUES (1, 'es')")
    conn.execute("INSERT INTO sessions (profile_id) VALUES (1)")
    conn.execute(
        "INSERT INTO utterances (session_id, turn_index, speaker, text) "
        "VALUES (1, 0, 'learner', 'la problema es grande')"
    )
    conn.execute(
        "INSERT INTO error_events "
        "(session_id, utterance_id, category, subcategory, severity, action, detector) "
        "VALUES (1, 1, 'morphosyntax', 'determiner', 2, 'logged_silent', 'sidecar:test')"
    )
    conn.commit()

    applied = apply_migrations(conn)
    assert applied == ["0003_model_action.sql"]

    row = conn.execute("SELECT * FROM error_events").fetchone()
    assert row["detector"] == "sidecar:test"  # existing data survived
    assert row["model_action"] is None        # new column defaulted to NULL


def test_0003_check_constraint_rejects_unknown_model_action(tmp_path):
    import sqlite3

    import pytest

    conn = connect(tmp_path / "m.db")
    apply_migrations(conn)
    conn.execute("INSERT INTO learners (name) VALUES ('t')")
    conn.execute("INSERT INTO language_profiles (learner_id, language) VALUES (1, 'es')")
    conn.execute("INSERT INTO sessions (profile_id) VALUES (1)")
    conn.execute(
        "INSERT INTO utterances (session_id, turn_index, speaker, text) VALUES (1, 0, 'learner', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO error_events "
            "(session_id, utterance_id, category, subcategory, severity, action, detector, model_action) "
            "VALUES (1, 1, 'morphosyntax', 'determiner', 2, 'logged_silent', 'sidecar:test', 'shrugged')"
        )


def test_0002_check_constraints(tmp_path):
    import sqlite3

    import pytest

    conn = connect(tmp_path / "m.db")
    apply_migrations(conn)
    conn.execute("INSERT INTO learners (name) VALUES ('t')")

    # 'off' is now a legal frequency
    conn.execute(
        "INSERT INTO language_profiles (learner_id, language, correction_aggressiveness) "
        "VALUES (1, 'de', 'off')"
    )
    # unknown style is rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO language_profiles (learner_id, language, correction_style) "
            "VALUES (1, 'fr', 'sarcastic')"
        )
