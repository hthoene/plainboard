"""external runners, and the runner of each project

Revision ID: 0057
Revises: 0056

Every project stays on the built-in runner (runner_id NULL).
"""
from alembic import op
import sqlalchemy as sa

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("version", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("away_told_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("runner_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_projects_runner_id", "runners", ["runner_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("fk_projects_runner_id", type_="foreignkey")
        batch.drop_column("runner_id")
    op.drop_table("runners")
