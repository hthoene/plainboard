"""task.customer_title and customer_description, project.public_api_key_hash

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("customer_title", sa.String(150), nullable=False, server_default=""))
        batch.add_column(sa.Column("customer_description", sa.Text(), nullable=False, server_default=""))
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("public_api_key_hash", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_projects_public_api_key_hash", ["public_api_key_hash"])


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_public_api_key_hash", type_="unique")
        batch.drop_column("public_api_key_hash")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("customer_description")
        batch.drop_column("customer_title")
