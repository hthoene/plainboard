"""task.user_action, project.label_colors, task_transitions (filled from existing tickets)

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("user_action", sa.String(500), nullable=False, server_default=""))
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("label_colors", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table(
        "task_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_number", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_task_transitions_project_id", "task_transitions", ["project_id"])
    # The past was not recorded: a done ticket counts as finished when it last changed.
    op.execute(
        "INSERT INTO task_transitions (project_id, task_number, from_status, status, at) "
        "SELECT project_id, number, NULL, CASE status WHEN 'done' THEN 'backlog' ELSE status END, created_at FROM tasks"
    )
    op.execute(
        "INSERT INTO task_transitions (project_id, task_number, from_status, status, at) "
        "SELECT project_id, number, 'backlog', 'done', updated_at FROM tasks WHERE status = 'done'"
    )


def downgrade() -> None:
    op.drop_index("ix_task_transitions_project_id", table_name="task_transitions")
    op.drop_table("task_transitions")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("label_colors")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("user_action")
