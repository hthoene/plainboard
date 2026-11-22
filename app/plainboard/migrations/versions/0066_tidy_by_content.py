"""merged branches are removed by content, every hour, in every repository; old unmerged ones are named once

Revision ID: 0066
Revises: 0065

On for every repository now: only branches whose content is proven to be on the base branch go,
each one is named to the manager with the commit it pointed to, and can be restored.
"""
from alembic import op
import sqlalchemy as sa

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("tidy_listed", sa.JSON(), nullable=True))
        batch.alter_column("tidy_branches", existing_type=sa.Boolean(), existing_nullable=False, server_default=sa.true())
    op.execute(sa.text("UPDATE repositories SET tidy_branches = :on, tidied_at = NULL").bindparams(on=True))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.alter_column("tidy_branches", existing_type=sa.Boolean(), existing_nullable=False, server_default=sa.false())
        batch.drop_column("tidy_listed")
