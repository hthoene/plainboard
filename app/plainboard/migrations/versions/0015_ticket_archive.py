"""task.archived_at, project.auto_archive_days

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_tasks_archived_at", ["archived_at"])
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("auto_archive_days", sa.Integer(), nullable=False, server_default="14"))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("auto_archive_days")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_archived_at")
        batch.drop_column("archived_at")
