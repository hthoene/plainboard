"""effort per assignment / run

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assignments") as batch:
        batch.add_column(sa.Column("effort", sa.String(length=20), nullable=True))
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("effort", sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("effort")
    with op.batch_alter_table("assignments") as batch:
        batch.drop_column("effort")
