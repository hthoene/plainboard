"""frozen system prompt per session

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("system_prompt", sa.Text(), nullable=True))
        batch.add_column(sa.Column("prompt_sections", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("prompt_sections")
        batch.drop_column("system_prompt")
