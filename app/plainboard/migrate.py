"""Alembic helpers (programmatic config, no alembic.ini needed).

    python -m plainboard.migrate upgrade            # apply migrations
    python -m plainboard.migrate revision "message" # autogenerate a new migration
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from .config import get_config


def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{get_config().data_dir / 'plainboard.db'}")
    return cfg


def upgrade() -> None:
    get_config().data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(), "head")


def revision(message: str) -> None:
    command.revision(alembic_config(), message=message, autogenerate=True)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "revision":
        upgrade()
        revision(sys.argv[2] if len(sys.argv) > 2 else "change")
    else:
        upgrade()
