"""areas and groups in a doc site's navigation (#159)

Revision ID: 0099
Revises: 0098

Areas (a switcher at the top of the navigation) and groups (headings) of a doc site's top-level pages, versioned like
pages; a page at the top names its area and group. Without any, a site looks as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def _common(key: str, table: str) -> list:
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("doc_sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column(key, sa.Integer(), sa.ForeignKey(f"{table}.id", ondelete="CASCADE"), nullable=True),
        sa.Column("since_seq", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("until_seq", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("texts", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "doc_areas",
        *_common("area_key", "doc_areas"),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("icon", sa.String(40), nullable=False),
    )
    op.create_table(
        "doc_groups",
        *_common("group_key", "doc_groups"),
        sa.Column("area_id", sa.Integer(), sa.ForeignKey("doc_areas.id", ondelete="SET NULL"), nullable=True),
    )
    for table, cols in (("doc_areas", ("project_id", "site_id", "area_key")), ("doc_groups", ("project_id", "site_id", "group_key", "area_id"))):
        for col in cols:
            op.create_index(f"ix_{table}_{col}", table, [col])
    with op.batch_alter_table("doc_pages") as batch:
        batch.add_column(sa.Column("area_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_doc_pages_area_id", "doc_areas", ["area_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_doc_pages_group_id", "doc_groups", ["group_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_doc_pages_area_id", ["area_id"])
        batch.create_index("ix_doc_pages_group_id", ["group_id"])


def downgrade() -> None:
    with op.batch_alter_table("doc_pages") as batch:
        batch.drop_index("ix_doc_pages_group_id")
        batch.drop_index("ix_doc_pages_area_id")
        batch.drop_constraint("fk_doc_pages_group_id", type_="foreignkey")
        batch.drop_constraint("fk_doc_pages_area_id", type_="foreignkey")
        batch.drop_column("group_id")
        batch.drop_column("area_id")
    op.drop_table("doc_groups")
    op.drop_table("doc_areas")
