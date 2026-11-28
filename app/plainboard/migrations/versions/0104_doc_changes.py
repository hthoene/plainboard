"""Docs find what changed (#190): repo_tags, doc_sites.check_budget and changes_at

Revision ID: 0104
Revises: 0103
"""
import sqlalchemy as sa
from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repo_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", sa.Integer(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(250), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("tagged_at", sa.DateTime(), nullable=True),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("repository_id", "name", name="uq_repo_tags_name"),
    )
    op.create_index("ix_repo_tags_project_id", "repo_tags", ["project_id"])
    op.create_index("ix_repo_tags_repository_id", "repo_tags", ["repository_id"])
    op.add_column("doc_sites", sa.Column("check_budget", sa.Integer(), nullable=False, server_default="4000"))
    op.add_column("doc_sites", sa.Column("changes_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("doc_sites") as batch:
        batch.drop_column("changes_at")
        batch.drop_column("check_budget")
    op.drop_index("ix_repo_tags_repository_id", "repo_tags")
    op.drop_index("ix_repo_tags_project_id", "repo_tags")
    op.drop_table("repo_tags")
