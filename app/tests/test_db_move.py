"""Moving an installation from SQLite to PostgreSQL (db_move): every row of every table comes along, the copy
runs once, and a failure leaves PostgreSQL empty and the SQLite file as it was. Needs PostgreSQL."""

from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, LargeBinary, String, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from conftest import POSTGRES
from plainboard import config, db_move, migrate
from plainboard.models import Base

pytestmark = pytest.mark.skipif(not POSTGRES, reason="needs PostgreSQL (PLAINBOARD_DATABASE)")


@pytest.fixture
def target():
    """An empty PostgreSQL database of its own, next to the test's."""
    url = make_url(config.get_config().sync_database_url)
    name = f"{url.database}_move"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield url.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _value(table, column, n: int, ids: dict):
    if column.foreign_keys:
        fk = next(iter(column.foreign_keys))
        return ids[fk.column.table.name][n % len(ids[fk.column.table.name])]
    kind = column.type
    if isinstance(kind, JSON):
        return {"n": n, "list": [1, "zwei", None, True], "nested": {"ä": "ö☃"}}
    if isinstance(kind, Boolean):
        return n % 2 == 0
    if isinstance(kind, DateTime):
        return datetime(2026, 11, 17, 8, n % 60, 30, 123456)
    if isinstance(kind, LargeBinary):
        return bytes(range(256)) * 40 + bytes([n])
    if isinstance(kind, Float):
        return n + 0.25
    if isinstance(kind, Integer):
        return 3_000_000_000 + n if isinstance(kind, BigInteger) else n + 7
    if isinstance(kind, String):
        size = kind.length or 50
        return f"{column.name[:3]}{n}"[:size] if size < 20 else f"{table.name}.{column.name} {n} äöü"[:size]
    raise AssertionError(f"no value for {table.name}.{column.name} ({kind})")


def _populate(sqlite_file) -> None:
    """A SQLite installation at the newest migration with rows in every table: values of every kind, and
    SQL NULL, JSON null and empty values in the nullable columns."""
    sqlite_file.parent.mkdir(parents=True, exist_ok=True)
    from alembic import command

    command.upgrade(db_move.alembic_config(f"sqlite:///{sqlite_file}"), "head")
    engine = create_engine(f"sqlite:///{sqlite_file}")
    ids: dict[str, list] = {}
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            pk = list(table.primary_key.columns)
            start = (conn.scalar(select(pk[0]).order_by(pk[0].desc()).limit(1)) or 0) if len(pk) == 1 and isinstance(pk[0].type, Integer) else 0
            for n in range(1, 4):
                row = {}
                for column in table.columns:
                    if column.primary_key and isinstance(column.type, Integer) and not column.foreign_keys:
                        row[column.name] = start + n * 10  # gaps: the sequence continues after the largest
                    elif n == 3 and column.nullable and not column.primary_key:
                        row[column.name] = JSON.NULL if isinstance(column.type, JSON) and column.name.endswith("s") else None
                    else:
                        row[column.name] = _value(table, column, n, ids)
                conn.execute(table.insert(), row)
            ids[table.name] = [r[0] for r in conn.execute(select(pk[0]))] if pk else []
    engine.dispose()


def _rows(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {t.name: [tuple(r) for r in conn.execute(db_move.reading(t))] for t in Base.metadata.sorted_tables}
    finally:
        engine.dispose()


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_startup_moves_every_row_once(tmp_path, monkeypatch, target):
    sqlite_file = tmp_path / "data" / "plainboard.db"
    _populate(sqlite_file)
    before, digest = _rows(f"sqlite:///{sqlite_file}"), _digest(sqlite_file)
    monkeypatch.setenv("PLAINBOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PLAINBOARD_DATABASE", target)
    config.get_config.cache_clear()
    try:
        migrate.upgrade()
        url = config.get_config().sync_database_url
        assert _rows(url) == before
        assert all(before.values())
        assert _digest(sqlite_file) == digest
        assert db_move.marker(sqlite_file).exists() and not (tmp_path / "data" / "plainboard.db.moving").exists()

        engine = create_engine(url)
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(migrate.alembic_config()).get_current_head()
            # New rows continue after the copied ones.
            assert conn.scalar(text("SELECT nextval(pg_get_serial_sequence('projects', 'id'))")) == max(r[0] for r in before["projects"]) + 1
            conn.execute(text("SELECT setval(pg_get_serial_sequence('projects', 'id'), (SELECT max(id) FROM projects))"))
        engine.dispose()

        migrate.upgrade()  # the next start: no second copy
        assert _rows(url) == before

        # A new empty database later: the file was moved already, so it is not copied again.
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
        migrate.upgrade()
        assert not any(_rows(url)[name] for name in ("projects", "tasks", "users"))
    finally:
        config.get_config.cache_clear()


def test_failure_leaves_postgresql_empty_and_sqlite_untouched(tmp_path, target):
    sqlite_file = tmp_path / "plainboard.db"
    _populate(sqlite_file)
    engine = create_engine(f"sqlite:///{sqlite_file}")
    with engine.begin() as conn:  # longer than PostgreSQL's column allows (SQLite does not check)
        conn.execute(text("UPDATE tasks SET status = 'far too long for its column' WHERE id = (SELECT max(id) FROM tasks)"))
    engine.dispose()
    digest = _digest(sqlite_file)
    url = config.Config(database=target).sync_database_url
    with pytest.raises(db_move.MoveError, match="table tasks"):
        db_move.move(sqlite_file, url)
    engine = create_engine(url)
    assert inspect(engine).get_table_names() == []
    engine.dispose()
    assert _digest(sqlite_file) == digest
    assert not db_move.marker(sqlite_file).exists()
