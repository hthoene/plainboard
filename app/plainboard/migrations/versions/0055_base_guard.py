"""the base-branch guard: a re-run of failed checks, and when the manager was last reminded of a red base

Revision ID: 0055
Revises: 0054
"""
from alembic import op
import sqlalchemy as sa

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trains") as batch:
        batch.add_column(sa.Column("rerun_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("red_told_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("red_told_at")
    with op.batch_alter_table("trains") as batch:
        batch.drop_column("rerun_at")
