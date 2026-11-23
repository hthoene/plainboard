"""Moving an installation from its SQLite file to PostgreSQL, with all its data.

At the start (`migrate.upgrade`): PostgreSQL is named, its database has no tables yet, and the data volume
holds a SQLite file that was not moved before. By hand: `plainboard migrate-db --to postgresql://...`.

The SQLite file is only read: a snapshot of it (SQLite's backup, WAL included) is brought to the newest
migration and copied, table by table in the order of their references, in one PostgreSQL transaction.
Rows are counted on both sides before the commit. Whatever fails, PostgreSQL is left without a table and
the SQLite file as it was. A file next to it (`plainboard.db.moved-to-postgresql`) says it was moved, so
it is never copied twice; the SQLite file stays as the backup.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, Integer, String, Table, Text, bindparam, cast, create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import Connection

from .models import Base

log = logging.getLogger("plainboard.db_move")

BATCH = 1000
MARKER_SUFFIX = ".moved-to-postgresql"


class MoveError(RuntimeError):
    pass


def marker(sqlite_file: Path) -> Path:
    return sqlite_file.with_name(sqlite_file.name + MARKER_SUFFIX)


def alembic_config(url: str) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def move(sqlite_file: Path, url: str) -> dict[str, int]:
    """Copy every row of `sqlite_file` into the PostgreSQL database `url` (a sync SQLAlchemy URL), which
    must have no tables. Returns the rows per table; raises MoveError with PostgreSQL left empty."""
    started = time.monotonic()
    snapshot = sqlite_file.with_name(sqlite_file.name + ".moving")  # in the data volume: room for a copy
    log.info("Moving the data from SQLite (%s) to PostgreSQL ...", sqlite_file)
    try:
        snapshot.unlink(missing_ok=True)
        _snapshot(sqlite_file, snapshot)
        command.upgrade(alembic_config(f"sqlite:///{snapshot}"), "head")
        source, target = create_engine(f"sqlite:///{snapshot}"), create_engine(url)
        try:
            with source.connect() as src, target.begin() as dst:
                counts = _copy(src, dst)
        finally:
            source.dispose()
            target.dispose()
    except Exception as exc:
        raise MoveError(
            f"Moving the data from SQLite to PostgreSQL failed: {exc}. Nothing of it was kept in PostgreSQL, and the "
            f"SQLite file {sqlite_file} is as it was. Fix the cause and start again, or leave PLAINBOARD_DATABASE "
            "empty to go on with SQLite."
        ) from exc
    finally:
        snapshot.unlink(missing_ok=True)
    rows, seconds = sum(counts.values()), time.monotonic() - started
    marker(sqlite_file).write_text(
        f"Moved to PostgreSQL at {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC: {rows} rows in {len(counts)} tables.\n"
        "This SQLite file is the backup from then; PlainBoard does not copy it again while this file exists.\n",
        encoding="utf-8",
    )
    log.info(
        "Moved %d rows in %d tables from SQLite to PostgreSQL in %.1f s, every table counted on both sides. The SQLite file stays as a backup: %s",
        rows, len(counts), seconds, sqlite_file,
    )
    return counts


def _snapshot(sqlite_file: Path, to: Path) -> None:
    """A consistent copy, read-only on the original (what is still in its WAL file comes along)."""
    if not sqlite_file.is_file():
        raise MoveError(f"no SQLite file at {sqlite_file}")
    src = sqlite3.connect(f"file:{sqlite_file}?mode=ro", uri=True)
    dst = sqlite3.connect(to)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def reading(table: Table):
    """The rows of a table, JSON as its text: SQL NULL and JSON null stay apart, and nothing is re-encoded."""
    columns = [cast(c, Text).label(c.name) if isinstance(c.type, JSON) else c for c in table.columns]
    return select(*columns).order_by(*table.primary_key.columns)


def _writing(table: Table):
    return insert(table).values(
        {c.name: cast(bindparam(f"p_{c.name}", type_=Text), c.type) if isinstance(c.type, JSON) else bindparam(f"p_{c.name}", type_=c.type) for c in table.columns}
    )


def _value(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, String):
        # SQLite keeps what it was given (a number in a text column); PostgreSQL text takes no NUL character.
        return (value if isinstance(value, str) else str(value)).replace("\x00", "")
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)  # timestamps are naive UTC
    return value


def _copy(src: Connection, dst: Connection) -> dict[str, int]:
    if inspect(dst).get_table_names():
        raise MoveError("the PostgreSQL database is not empty")
    tables = Base.metadata.sorted_tables  # parents before the rows that point at them
    known = {t.name for t in tables} | {"alembic_version"}
    for name in inspect(src).get_table_names():
        if name not in known and not name.startswith("sqlite_") and src.scalar(text(f'SELECT count(*) FROM "{name}"')):
            log.warning("SQLite table %s is not part of PlainBoard's schema and is not moved", name)
    Base.metadata.create_all(dst)
    counts: dict[str, int] = {}
    for table in tables:
        write, columns, copied = _writing(table), list(table.columns), 0
        try:
            for chunk in src.execution_options(yield_per=BATCH).execute(reading(table)).partitions():
                dst.execute(write, [{f"p_{c.name}": _value(c, row[i]) for i, c in enumerate(columns)} for row in chunk])
                copied += len(chunk)
        except Exception as exc:
            reason = str(getattr(exc, "orig", None) or exc).strip()  # the database's words, not the rows sent
            raise MoveError(f"table {table.name}: {reason}") from exc
        have, want = dst.scalar(select(func.count()).select_from(table)), src.scalar(select(func.count()).select_from(table))
        if not have == want == copied:
            raise MoveError(f"table {table.name}: {want} rows in SQLite, {have} in PostgreSQL")
        counts[table.name] = have
        if copied >= 10 * BATCH:
            log.info("  %s: %d rows", table.name, copied)
        pk = list(table.primary_key.columns)
        if len(pk) == 1 and isinstance(pk[0].type, Integer):  # the next new row continues after the copied ones
            dst.execute(
                text(f"SELECT setval(pg_get_serial_sequence(:table, :column), COALESCE((SELECT MAX(\"{pk[0].name}\") FROM \"{table.name}\"), 0) + 1, false)"),
                {"table": table.name, "column": pk[0].name},
            )
    MigrationContext.configure(dst).stamp(ScriptDirectory(str(Path(__file__).parent / "migrations")), "head")
    return counts
