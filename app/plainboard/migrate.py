"""Alembic helpers (programmatic config, no alembic.ini needed).

    python -m plainboard.migrate upgrade            # apply migrations
    python -m plainboard.migrate revision "message" # autogenerate a new migration
    plainboard migrate-db --to postgresql://...      # copy the SQLite file into an empty PostgreSQL (db_move)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from . import db_move
from .config import Config, get_config

log = logging.getLogger("plainboard.migrate")


def alembic_config() -> AlembicConfig:
    return db_move.alembic_config(get_config().sync_database_url)


def upgrade() -> None:
    config = get_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    if config.postgres and _empty(config.sync_database_url):
        sqlite_file = config.data_dir / "plainboard.db"
        if sqlite_file.exists() and not db_move.marker(sqlite_file).exists():
            # An installation that ran on SQLite until now: its data comes along, before anything is served.
            try:
                db_move.move(sqlite_file, config.sync_database_url)
            except db_move.MoveError as exc:
                log.error("%s PlainBoard does not start.", exc)
                raise
            return
        if sqlite_file.exists():
            log.warning("%s was moved to PostgreSQL before; this PostgreSQL database is new and starts empty. To copy it again, delete %s.", sqlite_file, db_move.marker(sqlite_file))
        # The migrations up to here were written for SQLite, where every installation began. A new
        # PostgreSQL database starts at today's tables instead, and takes every migration from now on.
        from .models import Base

        engine = create_engine(config.sync_database_url)
        try:
            Base.metadata.create_all(engine)
            _first_rows(engine)
        finally:
            engine.dispose()
        command.stamp(alembic_config(), "head")
        return
    command.upgrade(alembic_config(), "head")


def _first_rows(engine) -> None:
    """What the migrations put into a new installation besides tables: the two subscriptions it starts with
    (0018), which use what the environment or the runner's own sign-in provides."""
    from .models import Account

    claude = "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "oauth_token" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "runner_login"
    with Session(engine) as session:
        session.add(Account(provider="claude_code", label="Claude", key="claude", auth_mode=claude, enabled=True))
        session.add(Account(provider="codex", label="Codex", key="default", auth_mode="chatgpt", enabled=True))
        session.commit()


def _empty(url: str) -> bool:
    engine = create_engine(url)
    try:
        return not inspect(engine).get_table_names()
    finally:
        engine.dispose()


def revision(message: str) -> None:
    command.revision(alembic_config(), message=message, autogenerate=True)


def migrate_db(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="plainboard migrate-db", description="Copy PlainBoard's SQLite file into an empty PostgreSQL database. Stop PlainBoard first: what it writes after the copy is not moved.")
    parser.add_argument("--to", default=os.environ.get("PLAINBOARD_DATABASE", ""), help="postgresql://user:password@host:5432/name (default: PLAINBOARD_DATABASE)")
    parser.add_argument("--from", dest="source", default="", help="the SQLite file (default: plainboard.db in the data folder)")
    args = parser.parse_args(argv)
    target = Config(database=args.to)
    if not target.postgres:
        parser.error("--to must be a postgresql:// URL")
    source = Path(args.source) if args.source else get_config().data_dir / "plainboard.db"
    try:
        counts = db_move.move(source, target.sync_database_url)
    except db_move.MoveError as exc:
        sys.exit(str(exc))
    print(f"Done: {sum(counts.values())} rows in {len(counts)} tables. Set PLAINBOARD_DATABASE to this database and start PlainBoard.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.argv[1:2] == ["migrate-db"]:
        migrate_db(sys.argv[2:])
    elif sys.argv[1:2] == ["revision"]:
        upgrade()
        revision(sys.argv[2] if len(sys.argv) > 2 else "change")
    else:
        upgrade()


if __name__ == "__main__":
    main()
