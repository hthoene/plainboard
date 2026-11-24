"""Promotion of the base branch to a release branch (#129): repositories.release_branch, promotions.

Revision ID: 0091
Revises: 0090
"""
import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("release_branch", sa.String(200), nullable=False, server_default=""))
    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", sa.Integer(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("base", sa.String(200), nullable=False),
        sa.Column("release", sa.String(200), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("release_sha", sa.String(64), nullable=False),
        sa.Column("tag", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("how", sa.String(20), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("commits", sa.Integer(), nullable=False),
        sa.Column("landing_ids", sa.JSON(), nullable=False),
        sa.Column("by", sa.String(100), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_promotions_project_id", "promotions", ["project_id"])
    op.create_index("ix_promotions_repository_id", "promotions", ["repository_id"])
    op.create_index("ix_promotions_status", "promotions", ["status"])


def downgrade() -> None:
    op.drop_table("promotions")
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("release_branch")
