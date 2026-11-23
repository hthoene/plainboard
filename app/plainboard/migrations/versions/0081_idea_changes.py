"""Ideas, part 2 (#91): versions, undoable changes, the model's questions, what a person placed by hand

Revision ID: 0081
Revises: 0080
"""
import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("idea_topics") as batch:
        batch.add_column(sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("ideas") as batch:
        batch.add_column(sa.Column("topic_manual", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("merged_into_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("question", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("question_options", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("question_status", sa.String(10), nullable=False, server_default=""))
        batch.add_column(sa.Column("question_answer", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("question_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key("fk_ideas_merged_into_id_ideas", "ideas", ["merged_into_id"], ["id"], ondelete="SET NULL")
    op.create_table(
        "idea_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idea_id", sa.Integer(), sa.ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_idea_versions_project_id", "idea_versions", ["project_id"])
    op.create_index("ix_idea_versions_idea_id", "idea_versions", ["idea_id"])
    op.create_table(
        "idea_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("author", sa.String(100), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("undo", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_idea_changes_project_id", "idea_changes", ["project_id"])


def downgrade() -> None:
    op.drop_table("idea_changes")
    op.drop_table("idea_versions")
    with op.batch_alter_table("ideas") as batch:
        batch.drop_constraint("fk_ideas_merged_into_id_ideas", type_="foreignkey")
        for name in ("question_at", "question_answer", "question_status", "question_options", "question", "merged_into_id", "topic_manual"):
            batch.drop_column(name)
    with op.batch_alter_table("idea_topics") as batch:
        batch.drop_column("manual")
