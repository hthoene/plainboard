"""a switch per user for the @ list and the browser notifications of mentions

Revision ID: 0065
Revises: 0064
"""
from alembic import op
import sqlalchemy as sa

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("show_mentions", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("show_mentions")
