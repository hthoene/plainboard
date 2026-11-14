"""agent_notes (a team member's own notes, always in its prompt)

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_by_agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agent_notes_project_id", "agent_notes", ["project_id"])
    op.create_index("ix_agent_notes_agent_id", "agent_notes", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_notes_agent_id", "agent_notes")
    op.drop_index("ix_agent_notes_project_id", "agent_notes")
    op.drop_table("agent_notes")
