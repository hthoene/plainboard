"""The versions of a doc site's theme (#128): doc_theme_versions.

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
    op.create_table(
        "doc_theme_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("doc_sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("theme", sa.JSON(), nullable=False),
        sa.Column("author", sa.String(100), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_doc_theme_versions_project_id", "doc_theme_versions", ["project_id"])
    op.create_index("ix_doc_theme_versions_site_id", "doc_theme_versions", ["site_id"])


def downgrade() -> None:
    op.drop_table("doc_theme_versions")
