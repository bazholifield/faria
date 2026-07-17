"""Forward-only migration runner (DECISIONS.md ADR-006).

Migrations are numbered .sql files shipped inside the package
(faria/db/migrations/NNNN_name.sql), applied in filename order. Applied
versions are recorded in schema_migrations. No down-migrations: the DB is a
single local file — back it up before risky changes and restore if needed.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply all pending migrations. Returns the versions applied this call."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}

    migrations_dir = files("faria.db") / "migrations"
    pending = sorted(
        (res.name, res.read_text(encoding="utf-8"))
        for res in migrations_dir.iterdir()
        if res.name.endswith(".sql") and res.name not in done
    )

    applied: list[str] = []
    for name, sql in pending:
        try:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (name,))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise RuntimeError(f"Migration failed: {name}") from exc
        applied.append(name)
    return applied
