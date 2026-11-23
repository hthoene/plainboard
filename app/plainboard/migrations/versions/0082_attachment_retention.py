"""Attachments of done tickets can be deleted after a project's retention (#97): projects.attachment_retention_days
(0: never, the default for every project), projects.attachment_retention_min_mb, attachments.purged_at / purged_after_days

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
    op.add_column("projects", sa.Column("attachment_retention_days", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("projects", sa.Column("attachment_retention_min_mb", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("attachments", sa.Column("purged_at", sa.DateTime(), nullable=True))
    op.add_column("attachments", sa.Column("purged_after_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.drop_column("purged_after_days")
        batch.drop_column("purged_at")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("attachment_retention_min_mb")
        batch.drop_column("attachment_retention_days")
