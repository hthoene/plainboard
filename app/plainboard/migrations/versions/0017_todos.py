"""todos (task lists of team members)

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "todos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_todos_project_id", "todos", ["project_id"])
    op.create_index("ix_todos_agent_id", "todos", ["agent_id"])
    op.create_index("ix_todos_done_at", "todos", ["done_at"])
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("todos_shown", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("todos_shown")
    op.drop_index("ix_todos_done_at", "todos")
    op.drop_index("ix_todos_agent_id", "todos")
    op.drop_index("ix_todos_project_id", "todos")
    op.drop_table("todos")
