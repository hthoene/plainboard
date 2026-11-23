"""Held branches (#96): landings.hold, held_at, hold_reason, hold_told_at

Revision ID: 0082
Revises: 0081
"""
import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("landings") as batch:
        batch.add_column(sa.Column("hold", sa.String(20), nullable=False, server_default=""))
        batch.add_column(sa.Column("held_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("hold_reason", sa.String(300), nullable=False, server_default=""))
        batch.add_column(sa.Column("hold_told_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("landings") as batch:
        for name in ("hold_told_at", "hold_reason", "held_at", "hold"):
            batch.drop_column(name)
