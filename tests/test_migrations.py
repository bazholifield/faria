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
