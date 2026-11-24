"""Versions of doc sites (#126): doc_releases, and the range of versions each doc page row holds.

Revision ID: 0092
Revises: 0091
"""
import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doc_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("doc_sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("working", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("slug", sa.String(40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("released_on", sa.DateTime(), nullable=True),
        sa.Column("deprecated", sa.Boolean(), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("site_id", "slug"),
    )
    op.create_index("ix_doc_releases_project_id", "doc_releases", ["project_id"])
    op.create_index("ix_doc_releases_site_id", "doc_releases", ["site_id"])
    with op.batch_alter_table("doc_pages") as batch:
        batch.add_column(sa.Column("page_key", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("since_seq", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("until_seq", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_doc_pages_page_key", "doc_pages", ["page_key"], ["id"], ondelete="CASCADE")
        batch.create_index("ix_doc_pages_page_key", ["page_key"])
    op.execute("UPDATE doc_pages SET page_key = id")
    op.execute(
        "INSERT INTO doc_releases (project_id, site_id, seq, working, name, slug, notes, deprecated, hidden, is_default, created_at, updated_at) "
        "SELECT project_id, id, 1, true, 'next', 'next', '', false, false, false, created_at, created_at FROM doc_sites"
    )


def downgrade() -> None:
    op.execute("DELETE FROM doc_pages WHERE until_seq IS NOT NULL")
    with op.batch_alter_table("doc_pages") as batch:
        batch.drop_index("ix_doc_pages_page_key")
        batch.drop_constraint("fk_doc_pages_page_key", type_="foreignkey")
        batch.drop_column("until_seq")
        batch.drop_column("since_seq")
        batch.drop_column("page_key")
    op.drop_table("doc_releases")
