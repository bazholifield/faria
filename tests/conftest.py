import sqlite3

import pytest

from faria.db.connection import connect
from faria.db.migrate import apply_migrations
from faria.db.repository import Repository


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def repo(conn) -> Repository:
    return Repository(conn)
