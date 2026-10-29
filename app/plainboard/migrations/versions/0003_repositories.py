"""multiple repositories per project, per-agent repository access

Revision ID: 0003
Revises: 0002
"""
import re

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "repo"


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("default_branch", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name"),
    )
    op.create_index("ix_repositories_project_id", "repositories", ["project_id"])
    op.create_table(
        "agent_repositories",
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id", "repository_id"),
    )
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("all_repos", sa.Boolean(), nullable=False, server_default=sa.true()))

    # Move the former single repository URL into the new table.
    conn = op.get_bind()
    for project_id, url in conn.execute(sa.text("SELECT id, repo_url FROM projects WHERE repo_url IS NOT NULL AND repo_url != ''")):
        conn.execute(
            sa.text("INSERT INTO repositories (project_id, name, url, description) VALUES (:p, :n, :u, '')"),
            {"p": project_id, "n": _name_from_url(url), "u": url},
        )
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("repo_url")


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("repo_url", sa.String(length=500), nullable=True))
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("all_repos")
    op.drop_table("agent_repositories")
    op.drop_index("ix_repositories_project_id", "repositories")
    op.drop_table("repositories")
