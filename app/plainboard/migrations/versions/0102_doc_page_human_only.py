"""doc pages written by people only (#166)

Revision ID: 0102
Revises: 0101

A doc page can be marked as written by people only: the doc agent reads and links it but never changes it, and the
docs watcher orders no work on it.
"""
from alembic import op
import sqlalchemy as sa

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("doc_pages") as batch:
        batch.add_column(sa.Column("human_only", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("doc_pages") as batch:
        batch.drop_column("human_only")
