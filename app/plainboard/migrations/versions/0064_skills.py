"""Claude skills of a project

Revision ID: 0064
Revises: 0063
"""
from alembic import op
import sqlalchemy as sa

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(1024), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("all_agents", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("agent_ids", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(10), nullable=False, server_default="upload"),
        sa.Column("source_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("source_ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("source_path", sa.String(300), nullable=False, server_default=""),
        sa.Column("archive", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("project_id", "name"),
    )
    op.create_index("ix_skills_project_id", "skills", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_skills_project_id", "skills")
    op.drop_table("skills")
