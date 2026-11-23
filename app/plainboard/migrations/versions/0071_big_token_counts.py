"""Token counts as 64-bit numbers: a conversation's total passes 2^31 (PostgreSQL's integer); SQLite's are 64-bit already

Revision ID: 0071
Revises: 0070
"""
import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

COLUMNS = {
    "conversations": ("total_input_tokens", "total_output_tokens"),
    "runs": ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"),
}


def _alter(to) -> None:  # noqa: ANN001
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite: nothing to do, and rebuilding the tables would take long
    for table, columns in COLUMNS.items():
        for column in columns:
            op.alter_column(table, column, type_=to)


def upgrade() -> None:
    _alter(sa.BigInteger())


def downgrade() -> None:
    _alter(sa.Integer())
