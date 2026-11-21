"""Claude Code's limit messages, stored as the agent's own answers, become error lines

Revision ID: 0056
Revises: 0055
"""
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The agent took them for its own words, and its summaries for a lasting fact (no subscription left).
    op.execute(
        "UPDATE messages SET role = 'system', kind = 'error', meta = '{\"limit\": true}' "
        "WHERE role = 'assistant' AND kind = 'text' AND length(content) < 200 "
        "AND (content LIKE 'You''ve hit your %limit%' OR content LIKE 'Claude AI usage limit reached%')"
    )


def downgrade() -> None:
    pass
