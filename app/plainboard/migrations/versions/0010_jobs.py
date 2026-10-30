"""jobs

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("runner_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_path", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_jobs_project_id", "jobs", ["project_id"])
    op.create_index("ix_jobs_agent_id", "jobs", ["agent_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade() -> None:
    op.drop_table("jobs")
